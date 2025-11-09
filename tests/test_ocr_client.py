from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from typing import Any, Iterator

import httpx
import pytest

from app.ocr_client import OCRBatchTelemetry, OCRRequest, reset_quota_tracker, submit_tiles, _BatchResult
from app.settings import (
    BrowserSettings,
    LoggingSettings,
    OCRSettings,
    Settings,
    StorageSettings,
    TelemetrySettings,
    WarningSettings,
)


def _dummy_settings(
    server_url: str = "https://example.com/api",
    api_key: str | None = "sk-test",
    *,
    max_concurrency: int = 4,
    max_batch_tiles: int = 2,
    max_batch_bytes: int = 25_000_000,
    daily_quota_tiles: int | None = None,
) -> Settings:
    browser = BrowserSettings(
        cft_version="chrome-130.0.6723.69",
        cft_label="Stable-1",
        playwright_channel="cft",
        browser_transport="cdp",
        viewport_width=1280,
        viewport_height=2000,
        device_scale_factor=2,
        color_scheme="light",
        long_side_px=1288,
        viewport_overlap_px=120,
        tile_overlap_px=120,
        scroll_settle_ms=350,
        max_viewport_sweeps=200,
        shrink_retry_limit=2,
        screenshot_mask_selectors=tuple(),
        screenshot_style_hash="test-style",
        blocklist_path=Path("config/blocklist.json"),
    )
    ocr = OCRSettings(
        server_url=server_url,
        api_key=api_key,
        model="olmOCR-2-7B-1025-FP8",
        local_url=None,
        use_fp8=True,
        min_concurrency=1,
        max_concurrency=max_concurrency,
        max_batch_tiles=max_batch_tiles,
        max_batch_bytes=max_batch_bytes,
        daily_quota_tiles=daily_quota_tiles,
    )
    telemetry = TelemetrySettings(prometheus_port=9000, htmx_sse_heartbeat_ms=4000)
    storage = StorageSettings(
        cache_root=Path(".cache"),
        db_path=Path("runs.db"),
        profiles_root=Path(".cache") / "profiles",
    )
    warning_settings = WarningSettings(
        canvas_warning_threshold=3,
        video_warning_threshold=2,
        shrink_warning_threshold=1,
        overlap_warning_ratio=0.65,
        seam_warning_ratio=0.9,
        seam_warning_min_pairs=5,
    )
    logging_settings = LoggingSettings(warning_log_path=Path("ops/warnings.jsonl"))
    return Settings(
        env_path=".env",
        browser=browser,
        ocr=ocr,
        telemetry=telemetry,
        storage=storage,
        warnings=warning_settings,
        logging=logging_settings,
        webhook_secret="test-secret",
        server_runtime="pytest",
    )


@pytest.fixture(autouse=True)
def _reset_quota_tracker_fixture() -> Iterator[None]:
    reset_quota_tracker()
    yield
    reset_quota_tracker()


@pytest.mark.asyncio
async def test_submit_tiles_posts_base64_payload() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        body = json.loads(request.content.decode("utf-8"))
        captured["body"] = body
        return httpx.Response(
            200,
            json={"results": [{"markdown": "tile md"}]},
            headers={"x-request-id": "req-123"},
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await submit_tiles(
            requests=[OCRRequest(tile_id="tile-1", tile_bytes=b"hello world")],
            settings=_dummy_settings(),
            client=client,
        )

    assert result.markdown_chunks == ["tile md"]
    assert result.autotune is not None
    assert captured["url"].endswith("/v1/ocr")
    payload = captured["body"]
    assert payload["model"] == "olmOCR-2-7B-1025-FP8"
    image = payload["input"][0]["image"]
    assert base64.b64decode(image.encode("ascii")) == b"hello world"
    assert result.batches[0].request_id == "req-123"


@pytest.mark.asyncio
async def test_submit_tiles_raises_when_markdown_missing() -> None:
    def handler(_: httpx.Request) -> httpx.Response:  # pragma: no cover - simple path
        return httpx.Response(200, json={"unexpected": []})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(RuntimeError) as excinfo:
            await submit_tiles(
                requests=[OCRRequest(tile_id="tile-1", tile_bytes=b"data")],
                settings=_dummy_settings(),
                client=client,
            )
        assert "olmOCR request failed" in str(excinfo.value)
        assert isinstance(excinfo.value.__cause__, ValueError)


@pytest.mark.asyncio
async def test_submit_tiles_respects_concurrency_limit() -> None:
    class RecordingClient:
        def __init__(self) -> None:
            self.inflight = 0
            self.max_inflight = 0

        async def post(self, url, headers=None, json=None):  # noqa: ANN001
            self.inflight += 1
            self.max_inflight = max(self.max_inflight, self.inflight)
            await asyncio.sleep(0)
            self.inflight -= 1
            payload = json or {}
            identifier = payload.get("input", [{}])[0].get("id", "")
            request = httpx.Request("POST", url)
            return httpx.Response(200, json={"results": [{"markdown": identifier}]}, request=request)

        async def aclose(self) -> None:  # pragma: no cover - interface shim
            return None

    fake_client = RecordingClient()
    settings = _dummy_settings(max_concurrency=2, max_batch_tiles=1)

    requests = [OCRRequest(tile_id=f"tile-{idx}", tile_bytes=b"bytes") for idx in range(4)]

    result = await submit_tiles(
        requests=requests,
        settings=settings,
        client=fake_client,  # type: ignore[arg-type]
    )

    assert result.markdown_chunks == [f"tile-{idx}" for idx in range(4)]
    assert fake_client.max_inflight <= 2
    assert result.autotune is not None


@pytest.mark.asyncio
async def test_submit_tiles_autotune_adjusts_limits(monkeypatch) -> None:
    sequence = [
        {"status": 200, "latency": 1500, "attempts": 1},
        {"status": 200, "latency": 1400, "attempts": 1},
        {"status": 200, "latency": 1200, "attempts": 1},
        {"status": 500, "latency": 3200, "attempts": 1},
    ]

    async def fake_submit_batch(tiles, **kwargs):  # noqa: ANN001
        data = sequence.pop(0)
        telemetry = OCRBatchTelemetry(
            tile_ids=tuple(tile.tile_id for tile in tiles),
            latency_ms=data["latency"],
            status_code=data["status"],
            request_id="req",
            payload_bytes=1024,
            attempts=data["attempts"],
        )
        markdown = [tile.tile_id for tile in tiles]
        return _BatchResult(tile_ids=telemetry.tile_ids, markdown=markdown, telemetry=telemetry)

    monkeypatch.setattr("app.ocr_client._submit_batch", fake_submit_batch)

    requests = [OCRRequest(tile_id=f"tile-{idx}", tile_bytes=b"data") for idx in range(4)]
    settings = _dummy_settings(max_concurrency=3, max_batch_tiles=1)

    async with httpx.AsyncClient() as fake_client:
        result = await submit_tiles(requests=requests, settings=settings, client=fake_client)

    autotune = result.autotune
    assert autotune is not None
    assert autotune.peak_limit >= 3
    assert autotune.final_limit <= autotune.peak_limit
    assert any(event.reason == "healthy" for event in autotune.events)
    assert any(event.reason.startswith("http-") for event in autotune.events)


@pytest.mark.asyncio
async def test_submit_tiles_batches_multiple_tiles() -> None:
    payload_sizes: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        payload_sizes.append(len(body.get("input", [])))
        tiles = [{"markdown": f"chunk-{entry['id']}"} for entry in body.get("input", [])]
        return httpx.Response(200, json={"results": tiles})

    settings = _dummy_settings(max_batch_tiles=3)
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        requests = [
            OCRRequest(tile_id=f"tile-{idx}", tile_bytes=b"bytes")
            for idx in range(5)
        ]
        result = await submit_tiles(requests=requests, settings=settings, client=client)

    assert payload_sizes == [3, 2]
    assert result.markdown_chunks == [f"chunk-tile-{idx}" for idx in range(5)]


@pytest.mark.asyncio
async def test_submit_tiles_retry_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = 0

    async def fake_sleep(_: float) -> None:
        return None

    monkeypatch.setattr("app.ocr_client._sleep", fake_sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(500, json={"error": "boom"})
        return httpx.Response(200, json={"results": [{"markdown": "ok"}]})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await submit_tiles(
            requests=[OCRRequest(tile_id="tile-1", tile_bytes=b"bytes")],
            settings=_dummy_settings(),
            client=client,
        )

    assert attempts == 2
    assert result.markdown_chunks == ["ok"]
    assert result.batches[0].attempts == 2
    assert result.batches[0].status_code == 200


@pytest.mark.asyncio
async def test_submit_tiles_quota_warning() -> None:
    settings = _dummy_settings(daily_quota_tiles=4)

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        tiles = [{"markdown": f"chunk-{entry['id']}"} for entry in body.get("input", [])]
        return httpx.Response(200, json={"results": tiles})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await submit_tiles(
            requests=[OCRRequest(tile_id=f"tile-{idx}", tile_bytes=b"bytes") for idx in range(3)],
            settings=settings,
            client=client,
        )

    assert result.quota.warning_triggered is True
    assert result.quota.used == 3
    assert result.quota.limit == 4
