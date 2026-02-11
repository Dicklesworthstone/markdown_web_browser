"""olmOCR client adapters for remote/local inference."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import base64
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence

import httpx

from app.hardware import HardwareCapabilitySnapshot, get_host_capabilities
from app.local_ocr import ensure_local_ocr_service
from app.ocr_policy import (
    BACKEND_MODE_MAAS,
    BACKEND_MODE_OPENAI_COMPATIBLE,
    OCRBackendCandidate,
    OCRHysteresisSettings,
    OCRPolicyDecision,
    OCRPolicyInputs,
    OCRPolicyRuntimeState,
    OCRReevaluationContext,
    OCRRuntimeSignal,
    REASON_REEVAL_SUPPRESSED_COOLDOWN,
    REASON_REEVAL_SUPPRESSED_FLAPPING,
    select_ocr_backend,
    should_reevaluate_policy,
)
from app.settings import Settings, get_settings, resolve_ocr_backend_defaults

LOGGER = logging.getLogger(__name__)
DEFAULT_ENDPOINT_SUFFIX = "/chat/completions"  # OpenAI-compatible endpoint
REQUEST_TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=10.0)
_BACKOFF_SCHEDULE = (3.0, 9.0)
_MAX_ATTEMPTS = len(_BACKOFF_SCHEDULE) + 1
_QUOTA_WARNING_RATIO = 0.7
_CPU_LATENCY_SPIKE_MS = 20_000
_CIRCUIT_BREAKER_FAILURE_THRESHOLD = 2
_CIRCUIT_BREAKER_OPEN_SECONDS = 120
_POLICY_HYSTERESIS_DEFAULTS = OCRHysteresisSettings()
REASON_FAILOVER_SUBMISSION_FAILED = "runtime.failover.submission-failed"
REASON_FAILOVER_LOCAL_UNHEALTHY = "runtime.failover.local-unhealthy"
REASON_FAILOVER_CIRCUIT_OPEN = "runtime.failover.circuit-open"
REASON_FAILOVER_PROMOTED = "runtime.failover.promoted"
GLM_OPENAI_DEFAULT_MODEL = "glm-ocr"
GLM_MAAS_DEFAULT_MODEL = "glm-ocr"
GLM_MAAS_DEFAULT_API_URL = "https://open.bigmodel.cn/api/paas/v4/layout_parsing"
GLM_DEFAULT_PROMPT = (
    "Recognize the text in the image and output in Markdown format. Preserve the original "
    "layout including headings, paragraphs, tables, and formulas."
)
@dataclass(slots=True, frozen=True)
class OCRBackendRuntime:
    """Normalized OCR backend identity captured in manifests/telemetry."""

    backend_id: str
    backend_mode: str
    hardware_path: str
    fallback_chain: tuple[str, ...]
    provider: str
    reason_codes: tuple[str, ...] = ()
    reevaluate_after_s: int = 120


@dataclass(slots=True, frozen=True)
class OCRBackendProbe:
    """Static backend capabilities resolved from runtime configuration."""

    backend: OCRBackendRuntime
    endpoint: str
    model: str
    capabilities: dict[str, object]
    local_service: dict[str, object] | None = None


@dataclass(slots=True, frozen=True)
class OCRBackendHealth:
    """Best-effort backend health check metadata."""

    backend: OCRBackendRuntime
    healthy: bool
    status_code: int | None = None
    latency_ms: int | None = None
    reason_code: str | None = None
    detail: str | None = None
    local_service: dict[str, object] | None = None


class OCRBackend(Protocol):
    """Backend contract used by policy/autopilot and adapter implementations."""

    async def submit(
        self,
        *,
        requests: Sequence["OCRRequest"],
        settings: Settings | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> "SubmitTilesResult": ...

    async def probe(
        self,
        *,
        settings: Settings | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> OCRBackendProbe: ...

    async def health(
        self,
        *,
        settings: Settings | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> OCRBackendHealth: ...


def normalize_glm_file_reference(file_ref: str, *, mime_type: str = "image/png") -> str:
    """Normalize GLM MaaS file input to URL or data URI.

    GLM MaaS accepts either an HTTP(S) URL or a ``data:<mime>;base64,...`` URI for the
    ``file`` field. This helper wraps raw base64 input into a standards-compliant data URI.
    """

    raw = file_ref.strip()
    if not raw:
        raise ValueError("GLM file reference cannot be empty")
    if raw.startswith(("http://", "https://", "data:")):
        return raw
    return f"data:{mime_type};base64,{raw}"


def build_glm_maas_payload(
    *,
    file_ref: str,
    model: str = GLM_MAAS_DEFAULT_MODEL,
) -> dict[str, Any]:
    """Build request payload for GLM MaaS layout parsing API."""

    return {
        "model": model,
        "file": normalize_glm_file_reference(file_ref),
    }


def build_glm_openai_chat_payload(
    *,
    image_b64: str,
    model: str = GLM_OPENAI_DEFAULT_MODEL,
    prompt: str = GLM_DEFAULT_PROMPT,
    max_tokens: int = 4096,
    temperature: float = 0.01,
) -> dict[str, Any]:
    """Build OpenAI-compatible vision payload for GLM self-hosted endpoints."""

    image_ref = normalize_glm_file_reference(image_b64)
    return {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": image_ref}},
                ],
            }
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }


def extract_glm_openai_markdown(response_json: dict[str, Any]) -> str:
    """Extract Markdown from OpenAI-compatible GLM OCR responses."""

    choices = response_json.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            message = first.get("message")
            if isinstance(message, dict):
                content = _extract_openai_message_content(message.get("content"))
                if content:
                    return content
            fallback_text = first.get("text")
            if isinstance(fallback_text, str) and fallback_text.strip():
                return fallback_text.strip()
    for key in ("markdown", "content", "text"):
        value = response_json.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    raise ValueError("GLM OpenAI response missing markdown/text content")


def extract_glm_maas_markdown(response_json: dict[str, Any]) -> str:
    """Extract Markdown from GLM MaaS responses.

    The MaaS API may return markdown under top-level keys or nested result/data blocks.
    """

    content = _extract_glm_nested_text(response_json)
    if content:
        return content
    raise ValueError("GLM MaaS response missing markdown/text content")


def _extract_openai_message_content(content: Any) -> str | None:
    if isinstance(content, str) and content.strip():
        return content.strip()
    if not isinstance(content, list):
        return None
    parts: list[str] = []
    for item in content:
        if isinstance(item, dict):
            text_value = item.get("text")
            if isinstance(text_value, str) and text_value.strip():
                parts.append(text_value.strip())
    if not parts:
        return None
    return "\n".join(parts)


def _extract_glm_nested_text(payload: Any) -> str | None:
    if isinstance(payload, str):
        text = payload.strip()
        return text or None
    if isinstance(payload, list):
        for entry in payload:
            nested = _extract_glm_nested_text(entry)
            if nested:
                return nested
        return None
    if not isinstance(payload, dict):
        return None

    for key in ("markdown", "content", "text", "result_markdown"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    for key in ("data", "result", "output"):
        nested = _extract_glm_nested_text(payload.get(key))
        if nested:
            return nested
    return None


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
class OCRFailoverEvent:
    """Structured failover transition/skip/failure metadata."""

    event: str
    backend_id: str
    backend_mode: str
    hardware_path: str
    reason_code: str
    from_backend_id: str | None = None
    next_backend_id: str | None = None
    status_code: int | None = None
    detail: str | None = None
    circuit_open: bool = False

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "event": self.event,
            "backend_id": self.backend_id,
            "backend_mode": self.backend_mode,
            "hardware_path": self.hardware_path,
            "reason_code": self.reason_code,
            "circuit_open": self.circuit_open,
        }
        if self.from_backend_id is not None:
            payload["from_backend_id"] = self.from_backend_id
        if self.next_backend_id is not None:
            payload["next_backend_id"] = self.next_backend_id
        if self.status_code is not None:
            payload["status_code"] = self.status_code
        if self.detail is not None:
            payload["detail"] = self.detail
        return payload


def _default_backend_runtime() -> OCRBackendRuntime:
    return OCRBackendRuntime(
        backend_id="olmocr-remote-openai",
        backend_mode=BACKEND_MODE_OPENAI_COMPATIBLE,
        hardware_path="remote",
        fallback_chain=("olmocr-remote-openai",),
        provider="olmocr",
        reason_codes=("policy.remote.fallback",),
        reevaluate_after_s=120,
    )


@dataclass(slots=True)
class SubmitTilesResult:
    """Return value for :func:`submit_tiles` with telemetry + quota state."""

    markdown_chunks: list[str]
    batches: list[OCRBatchTelemetry]
    quota: OCRQuotaStatus
    backend: OCRBackendRuntime = field(default_factory=_default_backend_runtime)
    host_capabilities: dict[str, object] | None = None
    local_service: dict[str, object] | None = None
    failover_events: list[OCRFailoverEvent] = field(default_factory=list)
    autotune: "OcrAutotuneSnapshot | None" = None
    reevaluate_policy: bool = False
    reevaluation_reason_code: str | None = None


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
    reevaluate_policy: bool = False
    reevaluation_reason_code: str | None = None
    policy_suppression_count: int = 0
    policy_cooldown_suppression_count: int = 0
    policy_flap_suppression_count: int = 0
    policy_switch_count_window: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "initial_limit": self.initial_limit,
            "final_limit": self.final_limit,
            "peak_limit": self.peak_limit,
            "reevaluate_policy": self.reevaluate_policy,
            "reevaluation_reason_code": self.reevaluation_reason_code,
            "policy_suppression_count": self.policy_suppression_count,
            "policy_cooldown_suppression_count": self.policy_cooldown_suppression_count,
            "policy_flap_suppression_count": self.policy_flap_suppression_count,
            "policy_switch_count_window": self.policy_switch_count_window,
            "events": [
                {
                    "previous_limit": e.previous_limit,
                    "new_limit": e.new_limit,
                    "reason": e.reason,
                    "status_code": e.status_code,
                    "latency_ms": e.latency_ms,
                    "attempts": e.attempts,
                }
                for e in self.events
            ],
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


class _CircuitBreaker:
    """Simple per-backend circuit breaker with failure threshold + cooldown."""

    def __init__(self, *, threshold: int, open_seconds: int) -> None:
        self._threshold = max(1, threshold)
        self._open_seconds = max(1, open_seconds)
        self._consecutive_failures: dict[str, int] = {}
        self._open_until: dict[str, float] = {}

    def is_open(self, backend_id: str) -> bool:
        until = self._open_until.get(backend_id, 0.0)
        now = time.monotonic()
        if until <= now:
            if backend_id in self._open_until:
                self._open_until.pop(backend_id, None)
            return False
        return True

    def open_until(self, backend_id: str) -> float | None:
        until = self._open_until.get(backend_id)
        if until is None:
            return None
        remaining = until - time.monotonic()
        if remaining <= 0:
            self._open_until.pop(backend_id, None)
            return None
        return remaining

    def record_failure(self, backend_id: str) -> tuple[bool, float | None]:
        failures = self._consecutive_failures.get(backend_id, 0) + 1
        self._consecutive_failures[backend_id] = failures
        if failures < self._threshold:
            return False, None
        until = time.monotonic() + float(self._open_seconds)
        self._open_until[backend_id] = until
        return True, self._open_seconds

    def record_success(self, backend_id: str) -> None:
        self._consecutive_failures.pop(backend_id, None)
        self._open_until.pop(backend_id, None)

    def reset(self) -> None:
        self._consecutive_failures.clear()
        self._open_until.clear()


_circuit_breaker = _CircuitBreaker(
    threshold=_CIRCUIT_BREAKER_FAILURE_THRESHOLD,
    open_seconds=_CIRCUIT_BREAKER_OPEN_SECONDS,
)
_policy_runtime_state_by_backend: dict[str, OCRPolicyRuntimeState] = {}


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
            # Protect _pending_reduction access to prevent race conditions
            async with self._limit_lock:
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


def resolve_ocr_backend(
    settings: Settings | None = None,
    *,
    capabilities: HardwareCapabilitySnapshot | None = None,
) -> OCRBackendRuntime:
    """Return normalized backend identity from runtime settings."""

    cfg = settings or get_settings()
    capability_snapshot = capabilities or get_host_capabilities()
    backend_id, backend_mode, hardware_path, fallback_chain, provider = (
        resolve_ocr_backend_defaults(cfg.ocr)
    )
    ordered_ids = [backend_id, *fallback_chain]
    deduped_ids: list[str] = []
    for candidate_id in ordered_ids:
        if candidate_id and candidate_id not in deduped_ids:
            deduped_ids.append(candidate_id)
    policy_candidates: list[OCRBackendCandidate] = []
    for candidate_id in deduped_ids:
        candidate_mode = backend_mode if candidate_id == backend_id else _mode_from_backend_id(
            candidate_id
        )
        candidate_hardware = _hardware_path_for_candidate(
            candidate_id,
            default_hardware_path=hardware_path,
            preferred_local_hardware=capability_snapshot.preferred_hardware_path,
        )
        policy_candidates.append(
            OCRBackendCandidate(
                backend_id=candidate_id,
                backend_mode=candidate_mode,
                hardware_path=candidate_hardware,
                healthy=None,
            )
        )
    decision = select_ocr_backend(OCRPolicyInputs(candidates=tuple(policy_candidates)))
    effective_chain = (decision.backend_id, *decision.fallback_chain)
    return OCRBackendRuntime(
        backend_id=decision.backend_id,
        backend_mode=decision.backend_mode,
        hardware_path=decision.hardware_path,
        fallback_chain=effective_chain,
        provider=provider,
        reason_codes=decision.reason_codes,
        reevaluate_after_s=decision.reevaluate_after_s,
    )


async def probe_ocr_backend(
    *,
    settings: Settings | None = None,
    client: httpx.AsyncClient | None = None,
) -> OCRBackendProbe:
    """Resolve backend endpoint + static capabilities without sending OCR payloads."""

    _ = client  # Reserved for future adapter-specific probe requests.
    cfg = settings or get_settings()
    capabilities_snapshot = get_host_capabilities()
    backend = resolve_ocr_backend(cfg, capabilities=capabilities_snapshot)
    local_service: dict[str, object] | None = None
    if backend.backend_id.endswith("-local-openai"):
        status = await ensure_local_ocr_service(
            settings=cfg,
            capabilities=capabilities_snapshot,
            preferred_hardware_path=backend.hardware_path,
        )
        local_service = status.to_dict()
    endpoint = _normalize_endpoint(
        _select_server_url(cfg, backend=backend),
        backend_mode=backend.backend_mode,
    )
    capabilities: dict[str, object] = {
        "supports_submit": backend.backend_mode in {BACKEND_MODE_OPENAI_COMPATIBLE, BACKEND_MODE_MAAS},
        "supports_probe": True,
        "supports_health": True,
        "max_batch_tiles": cfg.ocr.max_batch_tiles,
        "max_batch_bytes": cfg.ocr.max_batch_bytes,
        "reason_codes": list(backend.reason_codes),
        "reevaluate_after_s": backend.reevaluate_after_s,
        "host_capabilities": capabilities_snapshot.to_dict(),
    }
    min_concurrency, max_concurrency, tuning_profile = _resolve_runtime_concurrency_limits(
        settings=cfg,
        backend=backend,
        capabilities=capabilities_snapshot,
    )
    capabilities["min_concurrency"] = min_concurrency
    capabilities["max_concurrency"] = max_concurrency
    capabilities["tuning_profile"] = tuning_profile
    if backend.backend_id.endswith("-local-openai"):
        capabilities["model_aliases"] = list(
            _model_alias_candidates(cfg.ocr.model, backend_id=backend.backend_id)
        )
        if local_service is not None:
            capabilities["local_service"] = local_service
    return OCRBackendProbe(
        backend=backend,
        endpoint=endpoint,
        model=cfg.ocr.model,
        capabilities=capabilities,
        local_service=local_service,
    )


async def healthcheck_ocr_backend(
    *,
    settings: Settings | None = None,
    client: httpx.AsyncClient | None = None,
) -> OCRBackendHealth:
    """Run a best-effort liveness check for the configured OCR backend endpoint."""

    probe = await probe_ocr_backend(settings=settings, client=client)
    cfg = settings or get_settings()
    if (
        probe.backend.backend_id.endswith("-local-openai")
        and probe.local_service is not None
        and not bool(probe.local_service.get("healthy"))
    ):
        local_status_value = probe.local_service.get("status_code")
        local_status_code = local_status_value if isinstance(local_status_value, int) else None
        reason = str(probe.local_service.get("reason") or "local-service-unhealthy")
        return OCRBackendHealth(
            backend=probe.backend,
            healthy=False,
            status_code=local_status_code,
            reason_code="local-service-unhealthy",
            detail=reason,
            local_service=probe.local_service,
        )
    request_timeout = _resolve_request_timeout(backend=probe.backend)
    owns_client = client is None
    http_client = client or httpx.AsyncClient(timeout=request_timeout, http2=True)
    headers: dict[str, str] = {}
    if cfg.ocr.api_key and not cfg.ocr.local_url:
        headers["Authorization"] = f"Bearer {cfg.ocr.api_key}"

    status_code: int | None = None
    latency_ms: int | None = None
    reason_code: str | None = None
    detail: str | None = None
    healthy = False
    probe_url = _health_probe_url(probe.endpoint, backend_mode=probe.backend.backend_mode)
    try:
        started = time.perf_counter()
        response = await http_client.get(probe_url, headers=headers)
        latency_ms = int((time.perf_counter() - started) * 1000)
        status_code = response.status_code
        # Treat auth/not-found/method-not-allowed as reachable in infra.
        healthy = status_code < 500
        if not healthy:
            reason_code = f"http-{status_code}"
            detail = f"Backend health probe to {probe_url} returned {status_code}."
    except Exception as exc:  # pragma: no cover - network dependent
        reason_code = "network-error"
        detail = str(exc)
    finally:
        if owns_client:
            await http_client.aclose()
    return OCRBackendHealth(
        backend=probe.backend,
        healthy=healthy,
        status_code=status_code,
        latency_ms=latency_ms,
        reason_code=reason_code,
        detail=detail,
        local_service=probe.local_service,
    )


async def submit_tiles(
    *,
    requests: Sequence[OCRRequest],
    settings: Settings | None = None,
    client: httpx.AsyncClient | None = None,
) -> SubmitTilesResult:
    """Submit tiles to the configured olmOCR endpoint and return Markdown + telemetry."""

    cfg = settings or get_settings()
    capabilities_snapshot = get_host_capabilities()
    backend = resolve_ocr_backend(cfg, capabilities=capabilities_snapshot)
    if not requests:
        empty_quota = OCRQuotaStatus(
            limit=None, used=None, threshold_ratio=_QUOTA_WARNING_RATIO, warning_triggered=False
        )
        return SubmitTilesResult(
            markdown_chunks=[],
            batches=[],
            quota=empty_quota,
            backend=backend,
            host_capabilities=capabilities_snapshot.to_dict(),
            local_service=None,
        )

    failover_events: list[OCRFailoverEvent] = []
    chain = list(backend.fallback_chain)
    if not chain:
        chain = [backend.backend_id]
    last_error: Exception | None = None
    previous_backend_id: str | None = None

    for idx, candidate_id in enumerate(chain):
        candidate = _runtime_for_backend_candidate(
            selected=backend,
            candidate_id=candidate_id,
            capabilities=capabilities_snapshot,
        )
        next_backend_id = chain[idx + 1] if idx + 1 < len(chain) else None

        if _circuit_breaker.is_open(candidate.backend_id):
            open_remaining = _circuit_breaker.open_until(candidate.backend_id)
            detail = (
                f"circuit-open-{open_remaining:.0f}s"
                if isinstance(open_remaining, (int, float))
                else "circuit-open"
            )
            failover_events.append(
                OCRFailoverEvent(
                    event="backend_skipped",
                    backend_id=candidate.backend_id,
                    backend_mode=candidate.backend_mode,
                    hardware_path=candidate.hardware_path,
                    reason_code=REASON_FAILOVER_CIRCUIT_OPEN,
                    from_backend_id=previous_backend_id,
                    next_backend_id=next_backend_id,
                    detail=detail,
                    circuit_open=True,
                )
            )
            previous_backend_id = candidate.backend_id
            continue

        local_service: dict[str, object] | None = None
        if candidate.backend_id.endswith("-local-openai"):
            status = await ensure_local_ocr_service(
                settings=cfg,
                capabilities=capabilities_snapshot,
                preferred_hardware_path=candidate.hardware_path,
            )
            local_service = status.to_dict()
            if not status.healthy:
                circuit_opened, cooldown_s = _circuit_breaker.record_failure(candidate.backend_id)
                detail_parts = [str(status.reason or status.action)]
                if circuit_opened and cooldown_s is not None:
                    detail_parts.append(f"circuit-open-{int(cooldown_s)}s")
                status_code = local_service.get("status_code")
                failover_events.append(
                    OCRFailoverEvent(
                        event="backend_failed",
                        backend_id=candidate.backend_id,
                        backend_mode=candidate.backend_mode,
                        hardware_path=candidate.hardware_path,
                        reason_code=REASON_FAILOVER_LOCAL_UNHEALTHY,
                        from_backend_id=previous_backend_id,
                        next_backend_id=next_backend_id,
                        status_code=status_code if isinstance(status_code, int) else None,
                        detail=", ".join(detail_parts),
                        circuit_open=circuit_opened,
                    )
                )
                previous_backend_id = candidate.backend_id
                last_error = RuntimeError(
                    f"Local OCR service unavailable ({status.reason or status.action})"
                )
                continue

        try:
            result = await _submit_tiles_single_backend(
                requests=requests,
                settings=cfg,
                capabilities=capabilities_snapshot,
                backend=candidate,
                local_service=local_service,
                client=client,
            )
        except Exception as exc:
            circuit_opened, cooldown_s = _circuit_breaker.record_failure(candidate.backend_id)
            status_code = _status_code_from_exception(exc)
            detail_parts = [f"{exc.__class__.__name__}: {exc}"]
            if circuit_opened and cooldown_s is not None:
                detail_parts.append(f"circuit-open-{int(cooldown_s)}s")
            failover_events.append(
                OCRFailoverEvent(
                    event="backend_failed",
                    backend_id=candidate.backend_id,
                    backend_mode=candidate.backend_mode,
                    hardware_path=candidate.hardware_path,
                    reason_code=REASON_FAILOVER_SUBMISSION_FAILED,
                    from_backend_id=previous_backend_id,
                    next_backend_id=next_backend_id,
                    status_code=status_code,
                    detail=", ".join(detail_parts),
                    circuit_open=circuit_opened,
                )
            )
            previous_backend_id = candidate.backend_id
            last_error = exc
            continue

        _circuit_breaker.record_success(candidate.backend_id)
        if idx > 0:
            failover_events.append(
                OCRFailoverEvent(
                    event="backend_selected",
                    backend_id=candidate.backend_id,
                    backend_mode=candidate.backend_mode,
                    hardware_path=candidate.hardware_path,
                    reason_code=REASON_FAILOVER_PROMOTED,
                    from_backend_id=previous_backend_id,
                )
            )
        result.failover_events = failover_events
        return result

    if last_error is None:
        last_error = RuntimeError("No OCR backend candidates available")
    raise RuntimeError(
        "OCR failover exhausted all backend candidates: "
        + ", ".join(chain)
    ) from last_error


async def _submit_tiles_single_backend(
    *,
    requests: Sequence[OCRRequest],
    settings: Settings,
    capabilities: HardwareCapabilitySnapshot,
    backend: OCRBackendRuntime,
    local_service: dict[str, object] | None,
    client: httpx.AsyncClient | None,
) -> SubmitTilesResult:
    if backend.backend_mode not in {BACKEND_MODE_OPENAI_COMPATIBLE, BACKEND_MODE_MAAS}:
        raise NotImplementedError(
            "Configured OCR backend mode is not wired for tile submission yet: "
            f"{backend.backend_mode}"
        )

    server_url = _select_server_url(settings, backend=backend)
    endpoint = _normalize_endpoint(server_url, backend_mode=backend.backend_mode)

    headers = {"Content-Type": "application/json"}
    if settings.ocr.api_key and not backend.backend_id.endswith("-local-openai"):
        headers["Authorization"] = f"Bearer {settings.ocr.api_key}"

    request_timeout = _resolve_request_timeout(backend=backend)
    owns_client = client is None
    http_client = client or httpx.AsyncClient(timeout=request_timeout, http2=True)

    min_limit, max_limit, _ = _resolve_runtime_concurrency_limits(
        settings=settings,
        backend=backend,
        capabilities=capabilities,
    )
    limiter = _AdaptiveLimiter(_AutotuneController(min_limit=min_limit, max_limit=max_limit))
    encoded_tiles = [_encode_request(req, settings) for req in requests]

    max_batch_tiles = (
        1
        if backend.backend_mode in {BACKEND_MODE_OPENAI_COMPATIBLE, BACKEND_MODE_MAAS}
        else max(1, settings.ocr.max_batch_tiles)
    )
    batches = _group_tiles(
        encoded_tiles,
        max_tiles=max_batch_tiles,
        max_bytes=max(1, settings.ocr.max_batch_bytes),
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
                use_fp8=settings.ocr.use_fp8,
                backend_mode=backend.backend_mode,
                backend_id=backend.backend_id,
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

    quota_status = _quota_tracker.record(
        len(requests), limit=settings.ocr.daily_quota_tiles, ratio=_QUOTA_WARNING_RATIO
    )
    reevaluate_policy, reevaluation_reason_code, policy_state = _evaluate_runtime_policy_reevaluation(
        backend=backend,
        batches=telemetry,
    )
    autotune_snapshot = limiter.snapshot()
    autotune_snapshot.reevaluate_policy = reevaluate_policy
    autotune_snapshot.reevaluation_reason_code = reevaluation_reason_code
    if policy_state is not None:
        autotune_snapshot.policy_suppression_count = policy_state.suppression_count
        autotune_snapshot.policy_cooldown_suppression_count = (
            policy_state.cooldown_suppression_count
        )
        autotune_snapshot.policy_flap_suppression_count = policy_state.flap_suppression_count
        autotune_snapshot.policy_switch_count_window = len(policy_state.switch_timestamps)
    markdown_chunks = [markdown_by_id[tile.tile_id] for tile in encoded_tiles]
    return SubmitTilesResult(
        markdown_chunks=markdown_chunks,
        batches=telemetry,
        quota=quota_status,
        backend=backend,
        host_capabilities=capabilities.to_dict(),
        local_service=local_service,
        autotune=autotune_snapshot,
        reevaluate_policy=reevaluate_policy,
        reevaluation_reason_code=reevaluation_reason_code,
    )


def reset_quota_tracker() -> None:
    """Reset quota accounting (exposed for testability)."""

    _quota_tracker.reset()


def reset_circuit_breakers() -> None:
    """Reset failover circuit breaker state (exposed for testability)."""

    _circuit_breaker.reset()


def reset_policy_runtime_state() -> None:
    """Reset hysteresis/flapping runtime state (exposed for testability)."""

    _policy_runtime_state_by_backend.clear()


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
    backend_mode: str,
    backend_id: str,
) -> _BatchResult:
    payload_bytes = sum(tile.size_bytes for tile in tiles) + 2048
    attempts = 0
    last_error: Exception | None = None
    status_code = 0
    request_id: str | None = None
    tile_ids = tuple(tile.tile_id for tile in tiles)
    model_aliases = _model_alias_candidates(tiles[0].model or "", backend_id=backend_id)
    model_alias_index = 0
    while attempts < _MAX_ATTEMPTS:
        attempts += 1
        model_override = model_aliases[model_alias_index] if model_aliases else None
        payload = _build_payload(
            tiles,
            use_fp8=use_fp8,
            backend_mode=backend_mode,
            backend_id=backend_id,
            model_override=model_override,
        )
        start = time.perf_counter()
        try:
            response = await http_client.post(endpoint, headers=headers, json=payload)
            status_code = response.status_code
            response.raise_for_status()
            data = response.json()
            markdown = _extract_markdown_batch(data, tile_ids, backend_mode=backend_mode)
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
            if _should_try_next_model_alias(
                exc.response,
                backend_mode=backend_mode,
                backend_id=backend_id,
                current_alias_index=model_alias_index,
                model_aliases=model_aliases,
            ):
                previous_model = model_aliases[model_alias_index]
                model_alias_index += 1
                next_model = model_aliases[model_alias_index]
                attempts = max(0, attempts - 1)
                LOGGER.warning(
                    "OCR model alias fallback (mode=%s, backend=%s): %s -> %s",
                    backend_mode,
                    backend_id,
                    previous_model,
                    next_model,
                )
                continue
            last_error = exc
            LOGGER.warning(
                "OCR request failed (mode=%s, status=%s, attempt=%s/%s)",
                backend_mode,
                status_code,
                attempts,
                _MAX_ATTEMPTS,
            )
        except Exception as exc:
            last_error = exc
            LOGGER.warning(
                "OCR request error (mode=%s) on attempt %s/%s: %s",
                backend_mode,
                attempts,
                _MAX_ATTEMPTS,
                exc,
            )
        if attempts >= _MAX_ATTEMPTS:
            break
        await _sleep(_BACKOFF_SCHEDULE[attempts - 1])
    raise RuntimeError(
        f"OCR request failed after {_MAX_ATTEMPTS} attempts (mode={backend_mode})"
    ) from last_error


def _build_payload(
    tiles: Sequence[_EncodedTile],
    *,
    use_fp8: bool,
    backend_mode: str,
    backend_id: str,
    model_override: str | None = None,
) -> dict:
    if not tiles:
        raise ValueError("Must provide at least one tile")

    if backend_mode == BACKEND_MODE_MAAS:
        if len(tiles) != 1:
            raise ValueError("GLM MaaS requests must contain exactly one tile")
        tile = tiles[0]
        model = model_override or tile.model or GLM_MAAS_DEFAULT_MODEL
        return build_glm_maas_payload(file_ref=tile.image_b64, model=model)

    # OpenAI-compatible vision format with multiple images in content array
    # Add allenai/ prefix if not present (for DeepInfra)
    model = model_override or tiles[0].model
    if not model:
        raise ValueError("Model must be specified for OCR requests")
    if (
        backend_id.endswith("-remote-openai")
        and not model.startswith("allenai/")
        and "olmocr" in model.lower()
    ):
        model = f"allenai/{model.split('-FP8')[0]}"  # Remove -FP8 suffix and add prefix

    # olmOCR's official prompt from build_no_anchoring_v4_yaml_prompt()
    # https://github.com/allenai/olmocr/blob/main/olmocr/prompts/prompts.py
    # Note: Model doesn't format output with markdown syntax (headings, bold, etc) despite instructions
    content = [
        {
            "type": "text",
            "text": (
                "Attached is one page of a document that you must process. "
                "Just return the plain text representation of this document as if you were reading it naturally. "
                "Convert equations to LaTeX and tables to HTML.\n"
                "If there are any figures or charts, label them with the following markdown syntax "
                "![Alt text describing the contents of the figure](page_startx_starty_width_height.png)\n"
                "Return your output as markdown, with a front matter section on top specifying values for the "
                "primary_language, is_rotation_valid, rotation_correction, is_table, and is_diagram parameters."
            ),
        }
    ]

    # Add all tile images
    for tile in tiles:
        content.append(
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{tile.image_b64}"}}
        )

    return {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "max_tokens": 8000,  # Match olmOCR toolkit configuration
        "temperature": 0.1,  # Low temperature for deterministic OCR output (per olmOCR docs)
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


def _extract_markdown_batch(
    response_json: dict,
    tile_ids: Sequence[str],
    *,
    backend_mode: str,
) -> list[str]:
    """Normalize various olmOCR response formats with multi-input support."""

    if not isinstance(response_json, dict):
        raise ValueError("OCR response must be a JSON object")

    if backend_mode == BACKEND_MODE_MAAS:
        if len(tile_ids) != 1:
            raise ValueError("GLM MaaS response requires exactly one tile id")
        return [extract_glm_maas_markdown(response_json)]

    # OpenAI-compatible format: {"choices": [{"message": {"content": "..."}}]}
    try:
        openai_chunk = extract_glm_openai_markdown(response_json)
    except ValueError:
        openai_chunk = None
    if openai_chunk is not None:
        if len(tile_ids) != 1:
            raise ValueError("OpenAI-compatible responses require exactly one tile id")
        return [openai_chunk]

    def _extract_from_entry(entry: dict) -> str | None:
        if not isinstance(entry, dict):
            return None
        if "markdown" in entry:
            return str(entry["markdown"])
        if "content" in entry:
            return str(entry["content"])
        if "text" in entry:
            return str(entry["text"])
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
    payload_id = payload.get("id") or payload.get("task_id")
    if isinstance(payload_id, str):
        return payload_id
    return None


def _status_code_from_exception(exc: Exception) -> int | None:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code
    cause = getattr(exc, "__cause__", None)
    if isinstance(cause, Exception):
        return _status_code_from_exception(cause)
    return None


def _runtime_for_backend_candidate(
    *,
    selected: OCRBackendRuntime,
    candidate_id: str,
    capabilities: HardwareCapabilitySnapshot,
) -> OCRBackendRuntime:
    if candidate_id == selected.backend_id:
        return selected

    candidate_mode = _mode_from_backend_id(candidate_id)
    candidate_hardware = _hardware_path_for_candidate(
        candidate_id,
        default_hardware_path=selected.hardware_path,
        preferred_local_hardware=capabilities.preferred_hardware_path,
    )
    reason_codes = list(selected.reason_codes)
    if REASON_FAILOVER_PROMOTED not in reason_codes:
        reason_codes.append(REASON_FAILOVER_PROMOTED)
    return OCRBackendRuntime(
        backend_id=candidate_id,
        backend_mode=candidate_mode,
        hardware_path=candidate_hardware,
        fallback_chain=selected.fallback_chain,
        provider=selected.provider,
        reason_codes=tuple(reason_codes),
        reevaluate_after_s=selected.reevaluate_after_s,
    )


def _select_server_url(
    settings: Settings,
    *,
    backend: OCRBackendRuntime | None = None,
) -> str:
    if backend and backend.backend_id.endswith("-local-openai"):
        if settings.ocr.local_url:
            return settings.ocr.local_url
        return settings.ocr.server_url
    if settings.ocr.local_url and backend is None:
        return settings.ocr.local_url
    return settings.ocr.server_url


def _normalize_endpoint(base: str, *, backend_mode: str = BACKEND_MODE_OPENAI_COMPATIBLE) -> str:
    base = base.rstrip("/")
    if backend_mode == BACKEND_MODE_MAAS:
        return base
    if base.endswith(DEFAULT_ENDPOINT_SUFFIX.strip("/")):
        return base
    return f"{base}{DEFAULT_ENDPOINT_SUFFIX}"


def _health_probe_url(endpoint: str, *, backend_mode: str) -> str:
    """Return a low-cost URL used for connectivity checks."""

    endpoint = endpoint.rstrip("/")
    if backend_mode == BACKEND_MODE_MAAS:
        return endpoint
    if endpoint.endswith("/chat/completions"):
        return f"{endpoint.removesuffix('/chat/completions')}/models"
    if endpoint.endswith("/v1"):
        return f"{endpoint}/models"
    if endpoint.endswith("/models"):
        return endpoint
    return f"{endpoint}/models"


async def _sleep(delay: float) -> None:
    await asyncio.sleep(delay)


def _resolve_runtime_concurrency_limits(
    *,
    settings: Settings,
    backend: OCRBackendRuntime,
    capabilities: HardwareCapabilitySnapshot,
) -> tuple[int, int, str]:
    min_limit = max(1, settings.ocr.min_concurrency)
    max_limit = max(min_limit, settings.ocr.max_concurrency)
    profile = "configured"

    if backend.backend_id.endswith("-local-openai") and backend.hardware_path == "gpu":
        # On multi-GPU hosts, scale the ceiling automatically so local vLLM/SGLang
        # deployments can keep devices saturated without user tuning.
        suggested_max = min(16, max(max_limit, max(1, capabilities.gpu_count) * 4))
        max_limit = max(min_limit, suggested_max)
        min_limit = max(2, min_limit)
        profile = "local-gpu-adaptive"
    elif backend.backend_id.endswith("-local-openai") and backend.hardware_path == "cpu":
        # CPU path intentionally stays conservative to avoid starving capture/stitch threads.
        cpu_cores = max(1, capabilities.cpu_logical_cores)
        suggested_max = max(1, min(4, max(1, cpu_cores // 4)))
        max_limit = max(1, min(max_limit, suggested_max))
        min_limit = 1
        profile = "local-cpu-conservative"

    return min_limit, max_limit, profile


def _resolve_request_timeout(*, backend: OCRBackendRuntime) -> httpx.Timeout:
    if backend.backend_id.endswith("-local-openai") and backend.hardware_path == "cpu":
        return httpx.Timeout(connect=10.0, read=180.0, write=60.0, pool=10.0)
    return REQUEST_TIMEOUT


def _runtime_policy_decision_from_backend(backend: OCRBackendRuntime) -> OCRPolicyDecision:
    fallback_chain = tuple(
        candidate for candidate in backend.fallback_chain if candidate != backend.backend_id
    )
    return OCRPolicyDecision(
        backend_id=backend.backend_id,
        backend_mode=backend.backend_mode,
        hardware_path=backend.hardware_path,
        fallback_chain=fallback_chain,
        reason_codes=backend.reason_codes,
        reevaluate_after_s=backend.reevaluate_after_s,
    )


def _evaluate_runtime_policy_reevaluation(
    *,
    backend: OCRBackendRuntime,
    batches: Sequence[OCRBatchTelemetry],
) -> tuple[bool, str | None, OCRPolicyRuntimeState | None]:
    if not batches:
        return False, None, None
    if not (backend.backend_id.endswith("-local-openai") and backend.hardware_path == "cpu"):
        return False, None, None

    signal = OCRRuntimeSignal.NO_CHANGE
    if any(batch.attempts >= 2 for batch in batches):
        signal = OCRRuntimeSignal.REQUEST_FAILED
    elif max(batch.latency_ms for batch in batches) >= _CPU_LATENCY_SPIKE_MS:
        signal = OCRRuntimeSignal.LATENCY_SPIKE

    if signal == OCRRuntimeSignal.NO_CHANGE:
        return False, None, None

    previous_state = _policy_runtime_state_by_backend.get(
        backend.backend_id, OCRPolicyRuntimeState()
    )

    decision = should_reevaluate_policy(
        signal=signal,
        decision=_runtime_policy_decision_from_backend(backend),
        context=OCRReevaluationContext(
            now_ts=time.monotonic(),
            state=previous_state,
            hysteresis=_POLICY_HYSTERESIS_DEFAULTS,
        ),
    )
    _policy_runtime_state_by_backend[backend.backend_id] = decision.state
    if decision.should_reevaluate:
        LOGGER.warning(
            "OCR runtime requested policy reevaluation (backend=%s, signal=%s, reason=%s)",
            backend.backend_id,
            signal.value,
            decision.reason_code,
        )
        return True, decision.reason_code, decision.state
    if decision.reason_code in {
        REASON_REEVAL_SUPPRESSED_COOLDOWN,
        REASON_REEVAL_SUPPRESSED_FLAPPING,
    }:
        LOGGER.info(
            "OCR runtime policy reevaluation suppressed (backend=%s, signal=%s, reason=%s, cooldown_remaining_s=%s, flap_window_count=%s)",
            backend.backend_id,
            signal.value,
            decision.reason_code,
            decision.cooldown_remaining_s,
            decision.flap_window_count,
        )
        return False, decision.reason_code, decision.state
    return False, None, decision.state


def _model_alias_candidates(model: str, *, backend_id: str) -> tuple[str, ...]:
    normalized = model.strip()
    if not normalized:
        return tuple()

    candidates: list[str] = []

    def _add(candidate: str) -> None:
        value = candidate.strip()
        if value and value not in candidates:
            candidates.append(value)

    _add(normalized)

    if not backend_id.endswith("-local-openai"):
        return tuple(candidates)

    if "/" in normalized:
        _add(normalized.rsplit("/", 1)[-1])

    lower_model = normalized.lower()
    if "glm" in lower_model:
        _add(GLM_OPENAI_DEFAULT_MODEL)
        _add("GLM-4.1V-9B-Thinking")
        _add("THUDM/GLM-4.1V-9B-Thinking")
        _add("zai-org/GLM-4.1V-9B-Thinking")
    elif "olmocr" in lower_model:
        bare_model = normalized.removeprefix("allenai/")
        _add(bare_model)
        _add(f"allenai/{bare_model}")

    return tuple(candidates)


def _should_try_next_model_alias(
    response: httpx.Response,
    *,
    backend_mode: str,
    backend_id: str,
    current_alias_index: int,
    model_aliases: Sequence[str],
) -> bool:
    if backend_mode != BACKEND_MODE_OPENAI_COMPATIBLE:
        return False
    if not backend_id.endswith("-local-openai"):
        return False
    if current_alias_index + 1 >= len(model_aliases):
        return False
    if response.status_code not in {400, 404}:
        return False
    try:
        detail = response.text.lower()
    except Exception:  # pragma: no cover - defensive
        return False
    return "model" in detail and any(
        token in detail for token in ("not found", "does not exist", "unknown", "invalid")
    )


def _mode_from_backend_id(backend_id: str) -> str:
    if backend_id.endswith("-maas"):
        return BACKEND_MODE_MAAS
    return BACKEND_MODE_OPENAI_COMPATIBLE


def _hardware_path_for_candidate(
    backend_id: str,
    *,
    default_hardware_path: str,
    preferred_local_hardware: str,
) -> str:
    if backend_id.endswith("-local-openai"):
        return preferred_local_hardware
    if backend_id.endswith("-maas"):
        return "remote"
    if default_hardware_path == "local-auto":
        return "remote"
    return default_hardware_path if backend_id.endswith("-openai") else "remote"
