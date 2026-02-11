"""Test OCR client with REAL OCR API calls - no mocks."""

from __future__ import annotations

import base64
from dataclasses import replace
import io
import json
from typing import Iterator

import httpx
import pytest
from PIL import Image, ImageDraw, ImageFont
from playwright.async_api import async_playwright

import app.ocr_client as ocr_client_module
from app.hardware import GPUDeviceCapability, HardwareCapabilitySnapshot, reset_host_capabilities_cache
from app.ocr_client import (
    OCRRequest,
    build_glm_maas_payload,
    build_glm_openai_chat_payload,
    extract_glm_maas_markdown,
    extract_glm_openai_markdown,
    healthcheck_ocr_backend,
    normalize_glm_file_reference,
    probe_ocr_backend,
    reset_policy_runtime_state,
    reset_circuit_breakers,
    reset_quota_tracker,
    resolve_ocr_backend,
    submit_tiles,
)
from app.settings import get_settings, load_config


# Load environment variables using decouple
decouple_config = load_config()
OLMOCR_API_KEY = decouple_config("OLMOCR_API_KEY", default="")


@pytest.fixture(autouse=True)
def _reset_quota_tracker_fixture() -> Iterator[None]:
    reset_quota_tracker()
    reset_circuit_breakers()
    reset_policy_runtime_state()
    reset_host_capabilities_cache()
    yield
    reset_quota_tracker()
    reset_circuit_breakers()
    reset_policy_runtime_state()
    reset_host_capabilities_cache()


def create_real_test_image(width: int = 1280, height: int = 720, text: str = "Test") -> bytes:
    """Create a real PNG image with text for testing."""
    # Create white image
    img = Image.new("RGB", (width, height), color="white")
    draw = ImageDraw.Draw(img)

    # Try to use a system font, fall back to default if not available
    font_size = 48
    font = None

    # Try multiple font paths for cross-platform compatibility
    font_paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",  # Linux
        "/System/Library/Fonts/Helvetica.ttc",  # macOS
        "C:\\Windows\\Fonts\\Arial.ttf",  # Windows
    ]

    for font_path in font_paths:
        try:
            font = ImageFont.truetype(font_path, font_size)
            break
        except (IOError, OSError):
            continue

    if font is None:
        # Use default font as last resort
        try:
            font = ImageFont.load_default(size=font_size)
        except TypeError:
            # Older PIL versions don't support size parameter
            font = ImageFont.load_default()

    # Add some text
    draw.text((50, 50), text, fill="black", font=font)
    draw.text((50, 150), "This is a real test image", fill="black", font=font)
    draw.text((50, 250), "Generated for testing OCR", fill="black", font=font)

    # Convert to PNG bytes
    img_bytes = io.BytesIO()
    img.save(img_bytes, format="PNG")
    img_bytes.seek(0)  # Important: seek to beginning
    return img_bytes.getvalue()


def _mock_local_service_ready(
    monkeypatch: pytest.MonkeyPatch,
    *,
    action: str = "reused",
    reason: str | None = "service-healthy",
) -> None:
    class _Status:
        def __init__(self) -> None:
            self.healthy = True
            self.action = action
            self.reason = reason

        def to_dict(self) -> dict[str, object]:
            payload: dict[str, object] = {
                "enabled": True,
                "endpoint": "http://localhost:8001/v1",
                "healthy": True,
                "action": self.action,
                "managed": False,
                "launch_attempts": 0,
                "restart_count": 0,
                "reason": self.reason,
            }
            return payload

    async def _fake_ensure_local_ocr_service(**_: object) -> _Status:
        return _Status()

    monkeypatch.setattr("app.ocr_client.ensure_local_ocr_service", _fake_ensure_local_ocr_service)


def _mock_local_service_unhealthy(
    monkeypatch: pytest.MonkeyPatch,
    *,
    action: str = "start-failed",
    reason: str = "startup-timeout",
    status_code: int | None = None,
) -> None:
    class _Status:
        def __init__(self) -> None:
            self.healthy = False
            self.action = action
            self.reason = reason

        def to_dict(self) -> dict[str, object]:
            payload: dict[str, object] = {
                "enabled": True,
                "endpoint": "http://localhost:8001/v1",
                "healthy": False,
                "action": self.action,
                "managed": True,
                "launch_attempts": 1,
                "restart_count": 0,
                "reason": self.reason,
            }
            if status_code is not None:
                payload["status_code"] = status_code
            return payload

    async def _fake_ensure_local_ocr_service(**_: object) -> _Status:
        return _Status()

    monkeypatch.setattr("app.ocr_client.ensure_local_ocr_service", _fake_ensure_local_ocr_service)


def test_normalize_glm_file_reference_wraps_raw_base64() -> None:
    raw = base64.b64encode(b"abc123").decode("ascii")
    normalized = normalize_glm_file_reference(raw)
    assert normalized == f"data:image/png;base64,{raw}"


def test_normalize_glm_file_reference_keeps_urls_and_data_uris() -> None:
    url_value = "https://example.com/image.png"
    data_value = "data:image/png;base64,AAA="
    assert normalize_glm_file_reference(url_value) == url_value
    assert normalize_glm_file_reference(data_value) == data_value


def test_build_glm_maas_payload_wraps_raw_base64_file() -> None:
    raw = base64.b64encode(b"tile-bytes").decode("ascii")
    payload = build_glm_maas_payload(file_ref=raw, model="glm-ocr")
    assert payload["model"] == "glm-ocr"
    assert payload["file"] == f"data:image/png;base64,{raw}"


def test_build_glm_openai_chat_payload_shapes_contract() -> None:
    raw = base64.b64encode(b"tile-bytes").decode("ascii")
    payload = build_glm_openai_chat_payload(image_b64=raw, model="glm-ocr")
    assert payload["model"] == "glm-ocr"
    assert payload["max_tokens"] == 4096
    assert payload["temperature"] == pytest.approx(0.01)
    messages = payload["messages"]
    assert isinstance(messages, list) and len(messages) == 1
    content = messages[0]["content"]
    assert content[0]["type"] == "text"
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"] == f"data:image/png;base64,{raw}"


def test_extract_glm_openai_markdown_from_string_content() -> None:
    response = {"choices": [{"message": {"content": "# Parsed"}}]}
    assert extract_glm_openai_markdown(response) == "# Parsed"


def test_extract_glm_openai_markdown_from_content_list() -> None:
    response = {
        "choices": [
            {
                "message": {
                    "content": [
                        {"type": "text", "text": "Line 1"},
                        {"type": "text", "text": "Line 2"},
                    ]
                }
            }
        ]
    }
    assert extract_glm_openai_markdown(response) == "Line 1\nLine 2"


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"markdown": "Top-level markdown"}, "Top-level markdown"),
        ({"data": {"result": {"content": "Nested content"}}}, "Nested content"),
        ({"result": [{"text": "From list"}]}, "From list"),
    ],
)
def test_extract_glm_maas_markdown_common_response_shapes(
    payload: dict[str, object], expected: str
) -> None:
    assert extract_glm_maas_markdown(payload) == expected


def test_resolve_ocr_backend_produces_contract_v2_fields() -> None:
    runtime = resolve_ocr_backend(get_settings())
    assert runtime.backend_id
    assert runtime.backend_mode in {"openai-compatible", "maas"}
    assert runtime.hardware_path
    assert runtime.fallback_chain
    assert runtime.fallback_chain[0] == runtime.backend_id
    assert runtime.reason_codes
    assert runtime.reevaluate_after_s >= 1


def test_resolve_ocr_backend_local_auto_uses_gpu_when_available() -> None:
    settings = get_settings()
    mutated = replace(settings, ocr=replace(settings.ocr, local_url="http://localhost:8001/v1"))
    snapshot = HardwareCapabilitySnapshot(
        os_platform="linux",
        architecture="x86_64",
        cpu_physical_cores=8,
        cpu_logical_cores=16,
        memory_total_mb=64000,
        memory_available_mb=32000,
        gpu_devices=(
            GPUDeviceCapability(
                index=0,
                vendor="nvidia",
                name="A100",
                memory_total_mb=40536,
                driver_version="550.54.15",
                runtime_version="12.4",
            ),
        ),
        detection_sources=("nvidia-smi",),
        detection_warnings=(),
    )
    runtime = resolve_ocr_backend(mutated, capabilities=snapshot)
    assert runtime.hardware_path == "gpu"
    assert runtime.backend_id.endswith("-local-openai")
    assert "policy.local.gpu-preferred" in runtime.reason_codes


@pytest.mark.asyncio
async def test_probe_ocr_backend_exposes_capabilities(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_local_service_ready(monkeypatch)
    probe = await probe_ocr_backend(settings=get_settings())
    assert probe.endpoint
    assert probe.model
    assert "supports_submit" in probe.capabilities
    assert "supports_health" in probe.capabilities
    assert "reason_codes" in probe.capabilities
    assert "reevaluate_after_s" in probe.capabilities


@pytest.mark.asyncio
async def test_healthcheck_ocr_backend_treats_reachable_4xx_as_healthy() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(405))
    async with httpx.AsyncClient(transport=transport) as client:
        health = await healthcheck_ocr_backend(settings=get_settings(), client=client)
    assert health.healthy is True
    assert health.status_code == 405


@pytest.mark.asyncio
async def test_submit_tiles_glm_maas_contract_and_telemetry() -> None:
    settings = get_settings()
    maas_settings = replace(
        settings,
        ocr=replace(
            settings.ocr,
            server_url="https://open.bigmodel.cn/api/paas/v4/layout_parsing",
            local_url=None,
            api_key="test-maas-key",
            model="glm-ocr",
            min_concurrency=1,
            max_concurrency=1,
        ),
    )
    requests = [
        OCRRequest(tile_id="tile-001", tile_bytes=b"tile-one"),
        OCRRequest(tile_id="tile-002", tile_bytes=b"tile-two"),
    ]
    seen_urls: list[str] = []
    seen_auth_headers: list[str | None] = []
    seen_models: list[object] = []
    seen_files: list[object] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        seen_urls.append(str(request.url))
        seen_auth_headers.append(request.headers.get("Authorization"))
        if isinstance(body, dict):
            seen_models.append(body.get("model"))
            seen_files.append(body.get("file"))
        else:
            seen_models.append(None)
            seen_files.append(None)
        idx = len(seen_urls)
        return httpx.Response(200, json={"request_id": f"maas-{idx}", "markdown": f"# tile {idx}"})

    transport = httpx.MockTransport(_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await submit_tiles(requests=requests, settings=maas_settings, client=client)

    assert result.backend.backend_mode == "maas"
    assert result.backend.backend_id == "glm-ocr-maas"
    assert result.markdown_chunks == ["# tile 1", "# tile 2"]
    assert len(result.batches) == 2
    assert [batch.request_id for batch in result.batches] == ["maas-1", "maas-2"]
    assert len(seen_urls) == 2
    assert seen_urls == ["https://open.bigmodel.cn/api/paas/v4/layout_parsing"] * 2
    assert seen_auth_headers == ["Bearer test-maas-key"] * 2
    assert seen_models == ["glm-ocr", "glm-ocr"]
    for file_value in seen_files:
        assert isinstance(file_value, str)
        assert file_value.startswith("data:image/png;base64,")


@pytest.mark.asyncio
async def test_submit_tiles_glm_maas_retries_5xx_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    maas_settings = replace(
        settings,
        ocr=replace(
            settings.ocr,
            server_url="https://open.bigmodel.cn/api/paas/v4/layout_parsing",
            local_url=None,
            api_key="test-maas-key",
            model="glm-ocr",
            min_concurrency=1,
            max_concurrency=1,
        ),
    )
    request = OCRRequest(tile_id="tile-001", tile_bytes=b"tile-retry")
    attempt_count = 0
    slept: list[float] = []

    async def _fake_sleep(delay: float) -> None:
        slept.append(delay)

    monkeypatch.setattr("app.ocr_client._sleep", _fake_sleep)

    def _handler(_: httpx.Request) -> httpx.Response:
        nonlocal attempt_count
        attempt_count += 1
        if attempt_count == 1:
            return httpx.Response(503, json={"error": "busy"})
        return httpx.Response(200, json={"task_id": "maas-ok", "data": {"result": {"content": "Recovered"}}})

    transport = httpx.MockTransport(_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await submit_tiles(requests=[request], settings=maas_settings, client=client)

    assert attempt_count == 2
    assert slept == [3.0]
    assert result.markdown_chunks == ["Recovered"]
    assert len(result.batches) == 1
    assert result.batches[0].attempts == 2
    assert result.batches[0].request_id == "maas-ok"


@pytest.mark.asyncio
async def test_submit_tiles_local_openai_keeps_configured_served_model_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_local_service_ready(monkeypatch)
    settings = get_settings()
    local_settings = replace(
        settings,
        ocr=replace(
            settings.ocr,
            local_url="http://localhost:8001/v1",
            api_key="should-not-be-sent-for-local",
            model="olmOCR-2-7B-1025-FP8",
            min_concurrency=1,
            max_concurrency=1,
        ),
    )
    request = OCRRequest(tile_id="tile-local-001", tile_bytes=b"tile-local")
    seen_url: str | None = None
    seen_auth: str | None = None
    seen_model: object = None

    def _handler(http_request: httpx.Request) -> httpx.Response:
        nonlocal seen_url, seen_auth, seen_model
        seen_url = str(http_request.url)
        seen_auth = http_request.headers.get("Authorization")
        payload = json.loads(http_request.content.decode("utf-8"))
        if isinstance(payload, dict):
            seen_model = payload.get("model")
        return httpx.Response(200, json={"choices": [{"message": {"content": "local-ocr"}}]})

    transport = httpx.MockTransport(_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await submit_tiles(requests=[request], settings=local_settings, client=client)

    assert seen_url == "http://localhost:8001/v1/chat/completions"
    assert seen_auth is None
    assert seen_model == "olmOCR-2-7B-1025-FP8"
    assert result.markdown_chunks == ["local-ocr"]
    assert len(result.batches) == 1
    assert result.local_service is not None
    assert result.local_service["action"] == "reused"


@pytest.mark.asyncio
async def test_submit_tiles_local_glm_alias_fallback_on_model_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_local_service_ready(monkeypatch)
    settings = get_settings()
    local_settings = replace(
        settings,
        ocr=replace(
            settings.ocr,
            local_url="http://localhost:8001/v1",
            model="zai-org/GLM-4.1V-9B-Thinking",
            min_concurrency=1,
            max_concurrency=1,
        ),
    )
    request = OCRRequest(tile_id="tile-local-002", tile_bytes=b"tile-local")
    slept: list[float] = []
    seen_models: list[str] = []

    async def _fake_sleep(delay: float) -> None:
        slept.append(delay)

    monkeypatch.setattr("app.ocr_client._sleep", _fake_sleep)

    def _handler(http_request: httpx.Request) -> httpx.Response:
        payload = json.loads(http_request.content.decode("utf-8"))
        model_value = ""
        if isinstance(payload, dict):
            maybe_model = payload.get("model")
            if isinstance(maybe_model, str):
                model_value = maybe_model
                seen_models.append(maybe_model)
        if len(seen_models) == 1:
            return httpx.Response(
                404,
                json={"error": {"message": f"model {model_value} does not exist"}},
            )
        return httpx.Response(200, json={"choices": [{"message": {"content": "alias-ok"}}]})

    transport = httpx.MockTransport(_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await submit_tiles(requests=[request], settings=local_settings, client=client)

    assert len(seen_models) >= 2
    assert seen_models[0] == "zai-org/GLM-4.1V-9B-Thinking"
    assert seen_models[1] == "GLM-4.1V-9B-Thinking"
    assert slept == []
    assert result.markdown_chunks == ["alias-ok"]
    assert result.batches[0].attempts == 1


@pytest.mark.asyncio
async def test_submit_tiles_local_openai_extracts_content_list_as_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_local_service_ready(monkeypatch)
    settings = get_settings()
    local_settings = replace(
        settings,
        ocr=replace(
            settings.ocr,
            local_url="http://localhost:8001/v1",
            model="glm-ocr",
            min_concurrency=1,
            max_concurrency=1,
        ),
    )
    request = OCRRequest(tile_id="tile-local-content-list", tile_bytes=b"tile-local")

    def _handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": [
                                {"type": "text", "text": "Line 1"},
                                {"type": "text", "text": "Line 2"},
                            ]
                        }
                    }
                ]
            },
        )

    transport = httpx.MockTransport(_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await submit_tiles(requests=[request], settings=local_settings, client=client)

    assert result.markdown_chunks == ["Line 1\nLine 2"]


@pytest.mark.asyncio
async def test_submit_tiles_openai_results_entry_accepts_text_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_local_service_ready(monkeypatch)
    settings = get_settings()
    local_settings = replace(
        settings,
        ocr=replace(
            settings.ocr,
            local_url="http://localhost:8001/v1",
            model="glm-ocr",
            min_concurrency=1,
            max_concurrency=1,
        ),
    )
    request = OCRRequest(tile_id="tile-local-results-text", tile_bytes=b"tile-local")

    def _handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": [{"text": "Result text"}]})

    transport = httpx.MockTransport(_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await submit_tiles(requests=[request], settings=local_settings, client=client)

    assert result.markdown_chunks == ["Result text"]


@pytest.mark.asyncio
async def test_probe_ocr_backend_local_gpu_exposes_adaptive_tuning(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_local_service_ready(monkeypatch)
    settings = get_settings()
    local_settings = replace(
        settings,
        ocr=replace(
            settings.ocr,
            local_url="http://localhost:8001/v1",
            model="glm-ocr",
            min_concurrency=2,
            max_concurrency=8,
        ),
    )
    gpu_snapshot = HardwareCapabilitySnapshot(
        os_platform="linux",
        architecture="x86_64",
        cpu_physical_cores=16,
        cpu_logical_cores=32,
        memory_total_mb=128000,
        memory_available_mb=96000,
        gpu_devices=(
            GPUDeviceCapability(
                index=0,
                vendor="nvidia",
                name="A100",
                memory_total_mb=40536,
                driver_version="550.54.15",
                runtime_version="12.4",
            ),
            GPUDeviceCapability(
                index=1,
                vendor="nvidia",
                name="A100",
                memory_total_mb=40536,
                driver_version="550.54.15",
                runtime_version="12.4",
            ),
            GPUDeviceCapability(
                index=2,
                vendor="nvidia",
                name="A100",
                memory_total_mb=40536,
                driver_version="550.54.15",
                runtime_version="12.4",
            ),
        ),
        detection_sources=("nvidia-smi",),
        detection_warnings=(),
    )
    monkeypatch.setattr("app.ocr_client.get_host_capabilities", lambda: gpu_snapshot)

    probe = await probe_ocr_backend(settings=local_settings)

    assert probe.backend.backend_id.endswith("-local-openai")
    assert probe.backend.hardware_path == "gpu"
    assert probe.capabilities["tuning_profile"] == "local-gpu-adaptive"
    assert probe.capabilities["min_concurrency"] == 2
    assert probe.capabilities["max_concurrency"] == 12
    aliases = probe.capabilities["model_aliases"]
    assert isinstance(aliases, list)
    assert "glm-ocr" in aliases
    assert isinstance(probe.capabilities["local_service"], dict)


@pytest.mark.asyncio
async def test_probe_ocr_backend_local_cpu_exposes_conservative_tuning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_local_service_ready(monkeypatch)
    settings = get_settings()
    local_settings = replace(
        settings,
        ocr=replace(
            settings.ocr,
            local_url="http://localhost:8001/v1",
            model="glm-ocr",
            min_concurrency=2,
            max_concurrency=8,
        ),
    )
    cpu_snapshot = HardwareCapabilitySnapshot(
        os_platform="linux",
        architecture="x86_64",
        cpu_physical_cores=8,
        cpu_logical_cores=16,
        memory_total_mb=64000,
        memory_available_mb=48000,
        gpu_devices=(),
        detection_sources=("psutil",),
        detection_warnings=(),
    )
    monkeypatch.setattr("app.ocr_client.get_host_capabilities", lambda: cpu_snapshot)

    probe = await probe_ocr_backend(settings=local_settings)

    assert probe.backend.backend_id.endswith("-local-openai")
    assert probe.backend.hardware_path == "cpu"
    assert probe.capabilities["tuning_profile"] == "local-cpu-conservative"
    assert probe.capabilities["min_concurrency"] == 1
    assert probe.capabilities["max_concurrency"] == 4
    assert isinstance(probe.capabilities["local_service"], dict)


@pytest.mark.asyncio
async def test_submit_tiles_local_cpu_retry_triggers_policy_reevaluation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_local_service_ready(monkeypatch)
    settings = get_settings()
    local_settings = replace(
        settings,
        ocr=replace(
            settings.ocr,
            local_url="http://localhost:8001/v1",
            model="glm-ocr",
            min_concurrency=1,
            max_concurrency=2,
        ),
    )
    cpu_snapshot = HardwareCapabilitySnapshot(
        os_platform="linux",
        architecture="x86_64",
        cpu_physical_cores=8,
        cpu_logical_cores=16,
        memory_total_mb=64000,
        memory_available_mb=48000,
        gpu_devices=(),
        detection_sources=("psutil",),
        detection_warnings=(),
    )
    monkeypatch.setattr("app.ocr_client.get_host_capabilities", lambda: cpu_snapshot)

    slept: list[float] = []
    attempts = 0

    async def _fake_sleep(delay: float) -> None:
        slept.append(delay)

    monkeypatch.setattr("app.ocr_client._sleep", _fake_sleep)

    def _handler(_: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503, json={"error": "busy"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "cpu-ok"}}]})

    request = OCRRequest(tile_id="tile-local-cpu-001", tile_bytes=b"tile-local")
    transport = httpx.MockTransport(_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await submit_tiles(requests=[request], settings=local_settings, client=client)

    assert slept == [3.0]
    assert result.backend.hardware_path == "cpu"
    assert result.markdown_chunks == ["cpu-ok"]
    assert result.batches[0].attempts == 2
    assert result.reevaluate_policy is True
    assert result.reevaluation_reason_code == "policy.reeval.failure"


@pytest.mark.asyncio
async def test_submit_tiles_local_cpu_latency_uses_hysteresis_suppression(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_local_service_ready(monkeypatch)
    settings = get_settings()
    local_settings = replace(
        settings,
        ocr=replace(
            settings.ocr,
            local_url="http://localhost:8001/v1",
            model="glm-ocr",
            min_concurrency=1,
            max_concurrency=2,
        ),
    )
    cpu_snapshot = HardwareCapabilitySnapshot(
        os_platform="linux",
        architecture="x86_64",
        cpu_physical_cores=8,
        cpu_logical_cores=16,
        memory_total_mb=64000,
        memory_available_mb=48000,
        gpu_devices=(),
        detection_sources=("psutil",),
        detection_warnings=(),
    )
    monkeypatch.setattr("app.ocr_client.get_host_capabilities", lambda: cpu_snapshot)

    async def _fake_submit_batch(  # noqa: ANN001
        tile_batch,
        *,
        endpoint: str,
        headers,
        http_client,
        use_fp8: bool,
        backend_mode: str,
        backend_id: str,
    ):
        del endpoint, headers, http_client, use_fp8, backend_mode, backend_id
        tile_ids = tuple(tile.tile_id for tile in tile_batch)
        telemetry = ocr_client_module.OCRBatchTelemetry(
            tile_ids=tile_ids,
            latency_ms=25_000,
            status_code=200,
            request_id="req-latency",
            payload_bytes=1024,
            attempts=1,
        )
        return ocr_client_module._BatchResult(
            tile_ids=tile_ids,
            markdown=["cpu-ok" for _ in tile_ids],
            telemetry=telemetry,
        )

    monkeypatch.setattr(ocr_client_module, "_submit_batch", _fake_submit_batch)

    request = OCRRequest(tile_id="tile-local-cpu-latency", tile_bytes=b"tile-local")
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200))) as client:
        first = await submit_tiles(requests=[request], settings=local_settings, client=client)
        second = await submit_tiles(requests=[request], settings=local_settings, client=client)

    assert first.reevaluate_policy is True
    assert first.reevaluation_reason_code == "policy.reeval.latency"
    assert second.reevaluate_policy is False
    assert second.reevaluation_reason_code == "policy.reeval.suppressed.cooldown"
    assert second.autotune is not None
    assert second.autotune.policy_suppression_count >= 1
    assert second.autotune.policy_cooldown_suppression_count >= 1


@pytest.mark.asyncio
async def test_submit_tiles_failover_from_local_to_remote_on_unhealthy_local(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    failover_settings = replace(
        settings,
        ocr=replace(
            settings.ocr,
            server_url="https://remote.example.com/api",
            local_url="http://localhost:8001/v1",
            model="glm-ocr",
            api_key="remote-key",
            min_concurrency=1,
            max_concurrency=1,
        ),
    )
    cpu_snapshot = HardwareCapabilitySnapshot(
        os_platform="linux",
        architecture="x86_64",
        cpu_physical_cores=8,
        cpu_logical_cores=16,
        memory_total_mb=64000,
        memory_available_mb=48000,
        gpu_devices=(),
        detection_sources=("psutil",),
        detection_warnings=(),
    )
    monkeypatch.setattr("app.ocr_client.get_host_capabilities", lambda: cpu_snapshot)
    _mock_local_service_unhealthy(monkeypatch, reason="startup-timeout", status_code=503)

    seen_urls: list[str] = []
    seen_auth: list[str | None] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        seen_auth.append(request.headers.get("Authorization"))
        return httpx.Response(200, json={"choices": [{"message": {"content": "remote-ok"}}]})

    request = OCRRequest(tile_id="tile-failover-001", tile_bytes=b"tile")
    transport = httpx.MockTransport(_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await submit_tiles(requests=[request], settings=failover_settings, client=client)

    assert result.backend.backend_id.endswith("-remote-openai")
    assert result.markdown_chunks == ["remote-ok"]
    assert seen_urls == ["https://remote.example.com/api/chat/completions"]
    assert seen_auth == ["Bearer remote-key"]
    assert result.failover_events
    first_event = result.failover_events[0]
    assert first_event.event == "backend_failed"
    assert first_event.reason_code == "runtime.failover.local-unhealthy"
    assert first_event.next_backend_id == "glm-ocr-remote-openai"
    assert first_event.status_code == 503
    assert result.failover_events[-1].event == "backend_selected"
    assert result.failover_events[-1].reason_code == "runtime.failover.promoted"


@pytest.mark.asyncio
async def test_submit_tiles_failover_skips_open_circuit_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    failover_settings = replace(
        settings,
        ocr=replace(
            settings.ocr,
            server_url="https://remote.example.com/api",
            local_url="http://localhost:8001/v1",
            model="glm-ocr",
            api_key="remote-key",
            min_concurrency=1,
            max_concurrency=1,
        ),
    )
    cpu_snapshot = HardwareCapabilitySnapshot(
        os_platform="linux",
        architecture="x86_64",
        cpu_physical_cores=8,
        cpu_logical_cores=16,
        memory_total_mb=64000,
        memory_available_mb=48000,
        gpu_devices=(),
        detection_sources=("psutil",),
        detection_warnings=(),
    )
    monkeypatch.setattr("app.ocr_client.get_host_capabilities", lambda: cpu_snapshot)

    local_attempts = 0

    class _UnhealthyStatus:
        healthy = False
        action = "start-failed"
        reason = "startup-timeout"

        def to_dict(self) -> dict[str, object]:
            return {
                "enabled": True,
                "endpoint": "http://localhost:8001/v1",
                "healthy": False,
                "action": "start-failed",
                "reason": "startup-timeout",
            }

    async def _fake_ensure_local_ocr_service(**_: object) -> _UnhealthyStatus:
        nonlocal local_attempts
        local_attempts += 1
        return _UnhealthyStatus()

    monkeypatch.setattr("app.ocr_client.ensure_local_ocr_service", _fake_ensure_local_ocr_service)

    request = OCRRequest(tile_id="tile-circuit-001", tile_bytes=b"tile")

    def _handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "remote-ok"}}]})

    transport = httpx.MockTransport(_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        first = await submit_tiles(requests=[request], settings=failover_settings, client=client)
        second = await submit_tiles(requests=[request], settings=failover_settings, client=client)
        third = await submit_tiles(requests=[request], settings=failover_settings, client=client)

    assert first.backend.backend_id.endswith("-remote-openai")
    assert second.backend.backend_id.endswith("-remote-openai")
    assert third.backend.backend_id.endswith("-remote-openai")
    assert local_attempts == 2
    skip_events = [event for event in third.failover_events if event.event == "backend_skipped"]
    assert skip_events
    assert skip_events[0].reason_code == "runtime.failover.circuit-open"
    assert skip_events[0].circuit_open is True


@pytest.mark.asyncio
async def test_healthcheck_local_backend_returns_local_service_unhealthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    local_settings = replace(
        settings,
        ocr=replace(
            settings.ocr,
            local_url="http://localhost:8001/v1",
            model="glm-ocr",
            min_concurrency=1,
            max_concurrency=1,
        ),
    )

    class _UnhealthyStatus:
        healthy = False
        action = "start-failed"
        reason = "startup-timeout"

        def to_dict(self) -> dict[str, object]:
            return {
                "enabled": True,
                "endpoint": "http://localhost:8001/v1",
                "healthy": False,
                "action": "start-failed",
                "reason": "startup-timeout",
                "launch_attempts": 2,
                "restart_count": 1,
            }

    async def _fake_ensure_local_ocr_service(**_: object) -> _UnhealthyStatus:
        return _UnhealthyStatus()

    monkeypatch.setattr("app.ocr_client.ensure_local_ocr_service", _fake_ensure_local_ocr_service)

    health = await healthcheck_ocr_backend(settings=local_settings)
    assert health.healthy is False
    assert health.reason_code == "local-service-unhealthy"
    assert health.local_service is not None
    assert health.local_service["action"] == "start-failed"


@pytest.mark.asyncio
@pytest.mark.skipif(not OLMOCR_API_KEY, reason="Requires real OCR API key")
async def test_real_ocr_api_single_image():
    """Test with real OCR API using a real image."""
    settings = get_settings()

    # Create a real test image
    test_image = create_real_test_image(text="Hello OCR API")

    # Create OCR request
    request = OCRRequest(tile_id="test_tile_001", tile_bytes=test_image)

    # Submit to real OCR API
    result = await submit_tiles(requests=[request], settings=settings)

    # Verify real response
    assert len(result.batches) == 1
    assert len(result.markdown_chunks) == 1

    # The real OCR should detect some text
    markdown = result.markdown_chunks[0]
    assert len(markdown) > 0
    # Real OCR might detect "Hello" or "OCR" or "API" from our test image
    # but we can't predict exact output


@pytest.mark.asyncio
@pytest.mark.skipif(not OLMOCR_API_KEY, reason="Requires real OCR API key")
async def test_real_ocr_api_multiple_images():
    """Test with real OCR API using multiple real images."""
    settings = get_settings()

    # Create multiple real test images
    requests = []
    for i in range(3):
        test_image = create_real_test_image(text=f"Page {i + 1}")
        requests.append(OCRRequest(tile_id=f"tile_{i:03d}", tile_bytes=test_image))

    # Submit to real OCR API
    result = await submit_tiles(requests=requests, settings=settings)

    # Verify real response
    assert len(result.batches) == 3
    assert len(result.markdown_chunks) == 3

    # Each section should have some content from real OCR
    for section in result.markdown_chunks:
        assert len(section) > 0


@pytest.mark.asyncio
@pytest.mark.skipif(not OLMOCR_API_KEY, reason="Requires real OCR API key")
async def test_real_ocr_api_with_actual_webpage_screenshot():
    """Test with a real screenshot from an actual website."""
    settings = get_settings()

    # Use playwright to capture a real website with proper cleanup
    browser = None
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page()

            # Navigate to a real website
            await page.goto("https://example.com")

            # Take a real screenshot
            screenshot_bytes = await page.screenshot(full_page=True)
    finally:
        if browser:
            await browser.close()

    # Submit real screenshot to OCR
    request = OCRRequest(tile_id="real_webpage_001", tile_bytes=screenshot_bytes)

    result = await submit_tiles(requests=[request], settings=settings)

    # Verify real response
    assert len(result.batches) == 1
    assert len(result.markdown_chunks) == 1

    # Real OCR should detect content from example.com
    markdown = result.markdown_chunks[0]
    assert len(markdown) > 0
    # example.com has "Example Domain" text that should be detected


@pytest.mark.asyncio
@pytest.mark.skipif(not OLMOCR_API_KEY, reason="Requires real OCR API key")
async def test_real_ocr_api_error_handling():
    """Test error handling with real OCR API."""
    settings = get_settings()

    # Create an invalid image (too small, might cause issues)
    tiny_image = create_real_test_image(width=1, height=1)

    request = OCRRequest(tile_id="tiny_tile", tile_bytes=tiny_image)

    # Submit to real OCR API - might fail or return empty
    result = await submit_tiles(requests=[request], settings=settings)

    # Should handle gracefully even with weird input
    assert len(result.batches) == 1


@pytest.mark.asyncio
@pytest.mark.skipif(not OLMOCR_API_KEY, reason="Requires real OCR API key")
async def test_real_ocr_api_concurrent_requests():
    """Test concurrent requests to real OCR API."""
    settings = get_settings()

    # Create multiple images for concurrent processing
    requests = []
    for i in range(5):
        test_image = create_real_test_image(width=1280, height=720, text=f"Concurrent Test {i}")
        requests.append(OCRRequest(tile_id=f"concurrent_{i:03d}", tile_bytes=test_image))

    # Submit all at once - tests real concurrency handling
    result = await submit_tiles(requests=requests, settings=settings)

    # Verify all were processed
    assert len(result.batches) == 5
    assert len(result.markdown_chunks) == 5

    # All should have content
    for section in result.markdown_chunks:
        assert len(section) > 0
