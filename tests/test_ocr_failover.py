from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Iterator

import httpx
import pytest

from app.hardware import HardwareCapabilitySnapshot, reset_host_capabilities_cache
from app.ocr_client import (
    OCRRequest,
    reset_circuit_breakers,
    reset_policy_runtime_state,
    reset_quota_tracker,
    submit_tiles,
)
from app.settings import get_settings


@pytest.fixture(autouse=True)
def _reset_runtime_state() -> Iterator[None]:
    reset_quota_tracker()
    reset_circuit_breakers()
    reset_policy_runtime_state()
    reset_host_capabilities_cache()
    yield
    reset_quota_tracker()
    reset_circuit_breakers()
    reset_policy_runtime_state()
    reset_host_capabilities_cache()


def _cpu_snapshot() -> HardwareCapabilitySnapshot:
    return HardwareCapabilitySnapshot(
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


def _mock_local_service_unhealthy(monkeypatch: pytest.MonkeyPatch, *, status_code: int = 503) -> None:
    @dataclass(slots=True)
    class _Status:
        managed: bool
        healthy: bool
        endpoint: str
        action: str
        launch_attempts: int
        restart_count: int
        reason: str | None
        status_code: int | None

        def to_dict(self) -> dict[str, object]:
            return {
                "managed": self.managed,
                "healthy": self.healthy,
                "endpoint": self.endpoint,
                "action": self.action,
                "launch_attempts": self.launch_attempts,
                "restart_count": self.restart_count,
                "reason": self.reason,
                "status_code": self.status_code,
            }

    async def _fake_ensure_local_ocr_service(**_: object) -> _Status:
        return _Status(
            managed=False,
            healthy=False,
            endpoint="http://localhost:8001/v1",
            action="unhealthy",
            launch_attempts=1,
            restart_count=0,
            reason="startup-timeout",
            status_code=status_code,
        )

    monkeypatch.setattr("app.ocr_client.ensure_local_ocr_service", _fake_ensure_local_ocr_service)


@pytest.mark.asyncio
async def test_failover_events_use_stable_schema(monkeypatch: pytest.MonkeyPatch) -> None:
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
    monkeypatch.setattr("app.ocr_client.get_host_capabilities", _cpu_snapshot)
    _mock_local_service_unhealthy(monkeypatch, status_code=503)

    def _handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "remote-ok"}}]})

    request = OCRRequest(tile_id="tile-failover-schema", tile_bytes=b"tile")
    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
        result = await submit_tiles(requests=[request], settings=failover_settings, client=client)

    assert result.backend.backend_id == "glm-ocr-remote-openai"
    assert result.failover_events
    event = result.failover_events[0].to_dict()
    assert set(event).issuperset(
        {"event", "backend_id", "backend_mode", "hardware_path", "reason_code", "circuit_open"}
    )
    assert event["reason_code"] == "runtime.failover.local-unhealthy"


@pytest.mark.asyncio
async def test_local_failures_open_circuit_and_next_run_skips_local(
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
    monkeypatch.setattr("app.ocr_client.get_host_capabilities", _cpu_snapshot)
    _mock_local_service_unhealthy(monkeypatch, status_code=503)

    def _handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "remote-ok"}}]})

    request = OCRRequest(tile_id="tile-circuit", tile_bytes=b"tile")
    transport = httpx.MockTransport(_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        first = await submit_tiles(requests=[request], settings=failover_settings, client=client)
        second = await submit_tiles(requests=[request], settings=failover_settings, client=client)
        third = await submit_tiles(requests=[request], settings=failover_settings, client=client)

    assert first.failover_events[0].event == "backend_failed"
    assert second.failover_events[0].event == "backend_failed"
    assert second.failover_events[0].circuit_open is True
    assert third.failover_events[0].event == "backend_skipped"
    assert third.failover_events[0].reason_code == "runtime.failover.circuit-open"
