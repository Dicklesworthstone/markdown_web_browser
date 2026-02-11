from __future__ import annotations

from dataclasses import replace
import subprocess

import pytest

from app.hardware import GPUDeviceCapability, HardwareCapabilitySnapshot
from app.local_ocr import (
    LocalOCRServiceManager,
    _build_start_plan,
    _normalize_endpoint,
)
from app.settings import get_settings


class _FakeProcess:
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self._alive = True

    def poll(self) -> int | None:
        return None if self._alive else 0

    def terminate(self) -> None:
        self._alive = False

    def wait(self, timeout: int | float | None = None) -> int:  # noqa: ARG002
        self._alive = False
        return 0

    def kill(self) -> None:
        self._alive = False


def _cpu_snapshot() -> HardwareCapabilitySnapshot:
    return HardwareCapabilitySnapshot(
        os_platform="linux",
        architecture="x86_64",
        cpu_physical_cores=8,
        cpu_logical_cores=16,
        memory_total_mb=64000,
        memory_available_mb=32000,
        gpu_devices=(),
        detection_sources=("psutil",),
        detection_warnings=(),
    )


def _gpu_snapshot() -> HardwareCapabilitySnapshot:
    return HardwareCapabilitySnapshot(
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
        ),
        detection_sources=("nvidia-smi",),
        detection_warnings=(),
    )


def test_normalize_endpoint_trims_openai_suffix() -> None:
    assert _normalize_endpoint("http://localhost:8001/v1/chat/completions") == "http://localhost:8001/v1"
    assert _normalize_endpoint("http://localhost:8001/v1/models") == "http://localhost:8001/v1"


def test_build_start_plan_gpu_uses_alias_and_parallelism() -> None:
    settings = get_settings()
    local_settings = replace(
        settings,
        ocr=replace(
            settings.ocr,
            model="glm-ocr",
            local_url="http://localhost:8001/v1",
        ),
    )
    plan = _build_start_plan(
        local_settings,
        endpoint="http://localhost:8001/v1",
        capabilities=_gpu_snapshot(),
        preferred_hardware_path="gpu",
    )
    assert plan.hardware_path == "gpu"
    assert plan.model == "zai-org/GLM-4.1V-9B-Thinking"
    assert plan.served_model_name == "glm-ocr"
    assert "--tensor-parallel-size" in plan.command
    assert "2" in plan.command


@pytest.mark.asyncio
async def test_ensure_service_reuses_existing_healthy_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = LocalOCRServiceManager()
    settings = get_settings()
    local_settings = replace(
        settings,
        ocr=replace(settings.ocr, local_url="http://localhost:8001/v1"),
    )

    async def _healthy_probe(
        endpoint: str, *, timeout: int  # noqa: ARG001
    ) -> tuple[bool, int | None, str | None]:
        return True, 200, f"{endpoint}/models"

    monkeypatch.setattr("app.local_ocr._probe_health", _healthy_probe)

    def _should_not_spawn(*_: object, **__: object) -> subprocess.Popen[str]:
        raise AssertionError("spawn should not be called when service is already healthy")

    monkeypatch.setattr("app.local_ocr.subprocess.Popen", _should_not_spawn)

    status = await manager.ensure_service(settings=local_settings, capabilities=_cpu_snapshot())
    assert status.healthy is True
    assert status.action == "reused"
    assert status.launch_attempts == 0


@pytest.mark.asyncio
async def test_ensure_service_autostarts_when_unhealthy(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = LocalOCRServiceManager()
    settings = get_settings()
    local_settings = replace(
        settings,
        ocr=replace(
            settings.ocr,
            local_url="http://localhost:8001/v1",
            local_autostart=True,
            local_max_restarts=1,
            local_startup_timeout_s=5,
        ),
    )
    probe_calls = 0

    async def _probe(
        endpoint: str, *, timeout: int  # noqa: ARG001
    ) -> tuple[bool, int | None, str | None]:
        nonlocal probe_calls
        probe_calls += 1
        return False, None, f"{endpoint}/models"

    async def _ready(**_: object) -> bool:
        return True

    popen_calls = 0

    def _spawn(*_: object, **__: object) -> _FakeProcess:
        nonlocal popen_calls
        popen_calls += 1
        return _FakeProcess(pid=4321)

    monkeypatch.setattr("app.local_ocr._probe_health", _probe)
    monkeypatch.setattr(manager, "_wait_until_ready", _ready)
    monkeypatch.setattr("app.local_ocr.subprocess.Popen", _spawn)

    status = await manager.ensure_service(settings=local_settings, capabilities=_gpu_snapshot())
    assert popen_calls == 1
    assert probe_calls >= 2
    assert status.healthy is True
    assert status.action == "started"
    assert status.pid == 4321
    assert status.managed is True
    assert status.launch_attempts == 1


@pytest.mark.asyncio
async def test_ensure_service_honors_autostart_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = LocalOCRServiceManager()
    settings = get_settings()
    local_settings = replace(
        settings,
        ocr=replace(
            settings.ocr,
            local_url="http://localhost:8001/v1",
            local_autostart=False,
        ),
    )

    async def _probe(
        endpoint: str, *, timeout: int  # noqa: ARG001
    ) -> tuple[bool, int | None, str | None]:
        return False, None, f"{endpoint}/models"

    monkeypatch.setattr("app.local_ocr._probe_health", _probe)

    status = await manager.ensure_service(settings=local_settings, capabilities=_cpu_snapshot())
    assert status.healthy is False
    assert status.action == "unavailable"
    assert status.reason == "autostart-disabled"


@pytest.mark.asyncio
async def test_ensure_service_retries_and_reports_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = LocalOCRServiceManager()
    settings = get_settings()
    local_settings = replace(
        settings,
        ocr=replace(
            settings.ocr,
            local_url="http://localhost:8001/v1",
            local_autostart=True,
            local_max_restarts=1,
            local_startup_timeout_s=5,
        ),
    )
    pid_counter = 1000

    async def _probe(
        endpoint: str, *, timeout: int  # noqa: ARG001
    ) -> tuple[bool, int | None, str | None]:
        return False, None, f"{endpoint}/models"

    async def _never_ready(**_: object) -> bool:
        return False

    def _spawn(*_: object, **__: object) -> _FakeProcess:
        nonlocal pid_counter
        pid_counter += 1
        return _FakeProcess(pid=pid_counter)

    monkeypatch.setattr("app.local_ocr._probe_health", _probe)
    monkeypatch.setattr(manager, "_wait_until_ready", _never_ready)
    monkeypatch.setattr("app.local_ocr.subprocess.Popen", _spawn)

    status = await manager.ensure_service(settings=local_settings, capabilities=_cpu_snapshot())
    assert status.healthy is False
    assert status.action == "start-failed"
    assert status.reason == "startup-timeout"
    assert status.launch_attempts == 2
    assert status.restart_count == 1
