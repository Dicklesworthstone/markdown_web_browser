"""olmOCR client adapters for remote/local inference."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import base64
import logging
import time
from dataclasses import dataclass, field
from typing import Sequence, cast

import httpx

from app.settings import Settings, get_settings

LOGGER = logging.getLogger(__name__)
DEFAULT_ENDPOINT_SUFFIX = "/chat/completions"  # OpenAI-compatible endpoint
REQUEST_TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=10.0)
_BACKOFF_SCHEDULE = (3.0, 9.0)
_MAX_ATTEMPTS = len(_BACKOFF_SCHEDULE) + 1
_QUOTA_WARNING_RATIO = 0.7


@dataclass(slots=True)
class OCRRequest:
    """Describe each tile submission to the OCR backend."""

    tile_id: str
    tile_bytes: bytes
    model: str | None = None


@dataclass(slots=True)
class OCRBatchTelemetry:
    """Structured metrics for each HTTP request sent to the OCR backend."""

    tile_ids: tuple[str, ...]
    latency_ms: int
    status_code: int
    request_id: str | None
    payload_bytes: int
    attempts: int


@dataclass(slots=True)
class OCRQuotaStatus:
    """Tracks daily quota usage for hosted OCR endpoints."""

    limit: int | None
    used: int | None
    threshold_ratio: float
    warning_triggered: bool


@dataclass(slots=True)
class SubmitTilesResult:
    """Return value for :func:`submit_tiles` with telemetry + quota state."""

    markdown_chunks: list[str]
    batches: list[OCRBatchTelemetry]
    quota: OCRQuotaStatus
    autotune: "OcrAutotuneSnapshot | None" = None


@dataclass(slots=True)
class _EncodedTile:
    """Internal helper storing base64 payload + size metadata."""

    tile_id: str
    image_b64: str
    size_bytes: int
    model: str | None


@dataclass(slots=True)
class OcrAutotuneEvent:
    previous_limit: int
    new_limit: int
    reason: str
    status_code: int
    latency_ms: int
    attempts: int


@dataclass(slots=True)
class OcrAutotuneSnapshot:
    initial_limit: int
    final_limit: int
    peak_limit: int
    events: list[OcrAutotuneEvent] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "initial_limit": self.initial_limit,
            "final_limit": self.final_limit,
            "peak_limit": self.peak_limit,
            "events": [event.__dict__ for event in self.events],
        }


class _QuotaTracker:
    """Process-level tracker for hosted OCR quota consumption."""

    def __init__(self) -> None:
        self._current_day: str | None = None
        self._count: int = 0
        self._warned: bool = False

    def record(self, tiles: int, *, limit: int | None, ratio: float) -> OCRQuotaStatus:
        today = time.strftime("%Y-%m-%d")
        if self._current_day != today:
            self._current_day = today
            self._count = 0
            self._warned = False
        self._count += tiles
        warning = False
        if limit and not self._warned and self._count >= int(limit * ratio):
            warning = True
            self._warned = True
        return OCRQuotaStatus(
            limit=limit,
            used=self._count if limit else None,
            threshold_ratio=ratio,
            warning_triggered=warning,
        )

    def reset(self) -> None:
        """Reset tracker (useful for tests)."""

        self._current_day = None
        self._count = 0
        self._warned = False


_quota_tracker = _QuotaTracker()


class _AutotuneController:
    """Latency/error-based concurrency controller."""

    def __init__(self, *, min_limit: int, max_limit: int) -> None:
        self._min = max(1, min_limit)
        self._max = max(self._min, max_limit)
        self._initial = self._min
        self._current = self._min
        self._peak = self._min
        self._events: list[OcrAutotuneEvent] = []
        self._success_streak = 0

    @property
    def current(self) -> int:
        return self._current

    def observe(self, telemetry: OCRBatchTelemetry) -> OcrAutotuneEvent | None:
        status = telemetry.status_code
        latency = telemetry.latency_ms
        attempts = telemetry.attempts
        new_limit = self._current
        reason: str | None = None

        if status in {408, 429} or status >= 500:
            new_limit = max(self._min, self._current - 1)
            reason = f"http-{status}"
            self._success_streak = 0
        elif attempts > 1:
            new_limit = max(self._min, self._current - 1)
            reason = "retry"
            self._success_streak = 0
        elif latency >= 7000:
            new_limit = max(self._min, self._current - 1)
            reason = "slow"
            self._success_streak = 0
        else:
            self._success_streak += 1
            if self._success_streak >= 2 and latency <= 3500 and self._current < self._max:
                new_limit = min(self._max, self._current + 1)
                reason = "healthy"

        if new_limit == self._current or reason is None:
            return None

        event = OcrAutotuneEvent(
            previous_limit=self._current,
            new_limit=new_limit,
            reason=reason,
            status_code=status,
            latency_ms=latency,
            attempts=attempts,
        )
        self._current = new_limit
        self._peak = max(self._peak, self._current)
        self._events.append(event)
        if len(self._events) > 50:
            del self._events[: len(self._events) - 50]
        return event

    def snapshot(self) -> OcrAutotuneSnapshot:
        return OcrAutotuneSnapshot(
            initial_limit=self._initial,
            final_limit=self._current,
            peak_limit=self._peak,
            events=list(self._events),
        )


class _AdaptiveLimiter:
    """Async semaphore wrapper with adjustable concurrency."""

    def __init__(self, controller: _AutotuneController) -> None:
        self._controller = controller
        self._semaphore = asyncio.Semaphore(controller.current)
        self._pending_reduction = 0
        self._limit_lock = asyncio.Lock()

    @asynccontextmanager
    async def slot(self):
        await self._semaphore.acquire()
        try:
            yield
        finally:
            if self._pending_reduction > 0:
                self._pending_reduction -= 1
            else:
                self._semaphore.release()

    async def record(self, telemetry: OCRBatchTelemetry) -> OcrAutotuneEvent | None:
        event = self._controller.observe(telemetry)
        if not event:
            return None
        async with self._limit_lock:
            diff = event.new_limit - event.previous_limit
            if diff > 0:
                for _ in range(diff):
                    self._semaphore.release()
            elif diff < 0:
                self._pending_reduction += -diff
        return event

    def snapshot(self) -> OcrAutotuneSnapshot:
        return self._controller.snapshot()


async def submit_tiles(
    *,
    requests: Sequence[OCRRequest],
    settings: Settings | None = None,
    client: httpx.AsyncClient | None = None,
) -> SubmitTilesResult:
    """Submit tiles to the configured olmOCR endpoint and return Markdown + telemetry."""

    if not requests:
        empty_quota = OCRQuotaStatus(limit=None, used=None, threshold_ratio=_QUOTA_WARNING_RATIO, warning_triggered=False)
        return SubmitTilesResult(markdown_chunks=[], batches=[], quota=empty_quota)

    cfg = cast(Settings, settings or get_settings())
    server_url = _select_server_url(cfg)
    endpoint = _normalize_endpoint(server_url)

    headers = {"Content-Type": "application/json"}
    if cfg.ocr.api_key and not cfg.ocr.local_url:
        headers["Authorization"] = f"Bearer {cfg.ocr.api_key}"

    owns_client = client is None
    http_client = client or httpx.AsyncClient(timeout=REQUEST_TIMEOUT, http2=True)

    min_limit = max(1, cfg.ocr.min_concurrency)
    max_limit = max(min_limit, cfg.ocr.max_concurrency)
    limiter = _AdaptiveLimiter(_AutotuneController(min_limit=min_limit, max_limit=max_limit))
    encoded_tiles = [_encode_request(req, cfg) for req in requests]
    batches = _group_tiles(
        encoded_tiles,
        max_tiles=max(1, cfg.ocr.max_batch_tiles),
        max_bytes=max(1, cfg.ocr.max_batch_bytes),
    )

    telemetry: list[OCRBatchTelemetry] = []
    markdown_by_id: dict[str, str] = {req.tile_id: "" for req in requests}

    async def _submit(group: list[_EncodedTile]) -> None:
        async with limiter.slot():
            batch_result = await _submit_batch(
                group,
                endpoint=endpoint,
                headers=headers,
                http_client=http_client,
                use_fp8=cfg.ocr.use_fp8,
            )
        telemetry.append(batch_result.telemetry)
        for tile_id, chunk in zip(batch_result.tile_ids, batch_result.markdown, strict=True):
            markdown_by_id[tile_id] = chunk
        await limiter.record(batch_result.telemetry)

    try:
        await asyncio.gather(*(_submit(group) for group in batches))
    finally:
        if owns_client:
            await http_client.aclose()

    quota_status = _quota_tracker.record(len(requests), limit=cfg.ocr.daily_quota_tiles, ratio=_QUOTA_WARNING_RATIO)
    markdown_chunks = [markdown_by_id[tile.tile_id] for tile in encoded_tiles]
    return SubmitTilesResult(
        markdown_chunks=markdown_chunks,
        batches=telemetry,
        quota=quota_status,
        autotune=limiter.snapshot(),
    )


def reset_quota_tracker() -> None:
    """Reset quota accounting (exposed for testability)."""

    _quota_tracker.reset()


@dataclass(slots=True)
class _BatchResult:
    tile_ids: tuple[str, ...]
    markdown: list[str]
    telemetry: OCRBatchTelemetry


async def _submit_batch(
    tiles: list[_EncodedTile],
    *,
    endpoint: str,
    headers: dict[str, str],
    http_client: httpx.AsyncClient,
    use_fp8: bool,
) -> _BatchResult:
    payload_bytes = sum(tile.size_bytes for tile in tiles) + 2048
    attempts = 0
    last_error: Exception | None = None
    status_code = 0
    request_id: str | None = None
    tile_ids = tuple(tile.tile_id for tile in tiles)
    while attempts < _MAX_ATTEMPTS:
        attempts += 1
        payload = _build_payload(tiles, use_fp8=use_fp8)
        start = time.perf_counter()
        try:
            response = await http_client.post(endpoint, headers=headers, json=payload)
            status_code = response.status_code
            response.raise_for_status()
            data = response.json()
            markdown = _extract_markdown_batch(data, tile_ids)
            latency_ms = int((time.perf_counter() - start) * 1000)
            request_id = _extract_request_id(response, data)
            telemetry = OCRBatchTelemetry(
                tile_ids=tile_ids,
                latency_ms=latency_ms,
                status_code=status_code,
                request_id=request_id,
                payload_bytes=payload_bytes,
                attempts=attempts,
            )
            return _BatchResult(tile_ids=tile_ids, markdown=markdown, telemetry=telemetry)
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            last_error = exc
            LOGGER.warning(
                "olmOCR request failed (status=%s, attempt=%s/%s)",
                status_code,
                attempts,
                _MAX_ATTEMPTS,
            )
        except Exception as exc:
            last_error = exc
            LOGGER.warning("olmOCR request error on attempt %s/%s: %s", attempts, _MAX_ATTEMPTS, exc)
        if attempts >= _MAX_ATTEMPTS:
            break
        await _sleep(_BACKOFF_SCHEDULE[attempts - 1])
    raise RuntimeError(f"olmOCR request failed after {_MAX_ATTEMPTS} attempts") from last_error


def _build_payload(tiles: Sequence[_EncodedTile], *, use_fp8: bool) -> dict:
    # OpenAI-compatible vision format (send first tile, batching handled at higher level)
    tile = tiles[0]

    # Add allenai/ prefix if not present (for DeepInfra)
    model = tile.model
    if not model.startswith("allenai/") and "olmOCR" in model:
        model = f"allenai/{model.split('-FP8')[0]}"  # Remove -FP8 suffix and add prefix

    return {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [{
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/png;base64,{tile.image_b64}"
                }
            }]
        }],
        "max_tokens": 4096
    }


def _encode_request(request: OCRRequest, settings: Settings) -> _EncodedTile:
    image_b64 = base64.b64encode(request.tile_bytes).decode("ascii")
    model = request.model or settings.ocr.model
    return _EncodedTile(
        tile_id=request.tile_id,
        image_b64=image_b64,
        size_bytes=len(image_b64),
        model=model,
    )


def _group_tiles(
    tiles: Sequence[_EncodedTile],
    *,
    max_tiles: int,
    max_bytes: int,
) -> list[list[_EncodedTile]]:
    groups: list[list[_EncodedTile]] = []
    current: list[_EncodedTile] = []
    current_bytes = 0
    current_model: str | None = None

    for tile in tiles:
        tile_bytes = tile.size_bytes
        flush = False
        if current and len(current) >= max_tiles:
            flush = True
        if current and current_bytes + tile_bytes > max_bytes:
            flush = True
        if current and current_model and tile.model != current_model:
            flush = True
        if flush:
            groups.append(current[:])
            current = []
            current_bytes = 0
            current_model = None
        current.append(tile)
        current_bytes += tile_bytes
        current_model = current_model or tile.model
        if current_bytes >= max_bytes:
            groups.append(current[:])
            current = []
            current_bytes = 0
            current_model = None
    if current:
        groups.append(current)
    return groups


def _extract_markdown_batch(response_json: dict, tile_ids: Sequence[str]) -> list[str]:
    """Normalize various olmOCR response formats with multi-input support."""

    if not isinstance(response_json, dict):
        raise ValueError("OCR response must be a JSON object")

    # OpenAI-compatible format: {"choices": [{"message": {"content": "..."}}]}
    choices = response_json.get("choices")
    if isinstance(choices, list) and len(choices) > 0:
        choice = choices[0]
        if isinstance(choice, dict):
            message = choice.get("message")
            if isinstance(message, dict):
                content = message.get("content")
                if content is not None:
                    return [str(content)]

    def _extract_from_entry(entry: dict) -> str | None:
        if not isinstance(entry, dict):
            return None
        if "markdown" in entry:
            return str(entry["markdown"])
        if "content" in entry:
            return str(entry["content"])
        return None

    buckets: list[str] = []
    results = response_json.get("results")
    data_entries = response_json.get("data")
    source = None
    if isinstance(results, list) and len(results) >= len(tile_ids):
        source = results
    elif isinstance(data_entries, list) and len(data_entries) >= len(tile_ids):
        source = data_entries

    if source is not None:
        for idx, tile_id in enumerate(tile_ids):
            entry = source[idx]
            chunk = _extract_from_entry(entry)
            if chunk is None:
                raise ValueError(f"OCR response missing markdown content for tile {tile_id}")
            buckets.append(chunk)
        return buckets

    # Single-field fallback for older endpoints
    single = _extract_from_entry(response_json)
    if single is not None and len(tile_ids) == 1:
        return [single]

    raise ValueError("OCR response missing markdown content for batch")


def _extract_request_id(response: httpx.Response, payload: dict) -> str | None:
    header_id = response.headers.get("x-request-id") or response.headers.get("X-Request-ID")
    if header_id:
        return header_id
    req_id = payload.get("request_id")
    if isinstance(req_id, str):
        return req_id
    return None


def _select_server_url(settings: Settings) -> str:
    if settings.ocr.local_url:
        return settings.ocr.local_url
    return settings.ocr.server_url


def _normalize_endpoint(base: str) -> str:
    base = base.rstrip("/")
    if base.endswith(DEFAULT_ENDPOINT_SUFFIX.strip("/")):
        return base
    return f"{base}{DEFAULT_ENDPOINT_SUFFIX}"


async def _sleep(delay: float) -> None:
    await asyncio.sleep(delay)
