from __future__ import annotations

from dataclasses import replace

from app.hardware import GPUDeviceCapability, HardwareCapabilitySnapshot
from app.ocr_client import resolve_ocr_backend
from app.settings import get_settings


def _snapshot(*, has_gpu: bool) -> HardwareCapabilitySnapshot:
    devices: tuple[GPUDeviceCapability, ...] = ()
    if has_gpu:
        devices = (
            GPUDeviceCapability(
                index=0,
                vendor="nvidia",
                name="RTX 4090",
                memory_total_mb=24576,
                runtime_version="12.4",
            ),
        )
    return HardwareCapabilitySnapshot(
        os_platform="linux",
        architecture="x86_64",
        cpu_physical_cores=16,
        cpu_logical_cores=32,
        memory_total_mb=128000,
        memory_available_mb=96000,
        gpu_devices=devices,
        detection_sources=("nvidia-smi",),
        detection_warnings=(),
    )


def test_resolve_backend_prefers_local_gpu_when_available() -> None:
    settings = get_settings()
    local_settings = replace(
        settings,
        ocr=replace(
            settings.ocr,
            local_url="http://localhost:8001/v1",
            server_url="https://remote.example.com/v1",
            model="glm-ocr",
        ),
    )
    backend = resolve_ocr_backend(local_settings, capabilities=_snapshot(has_gpu=True))
    assert backend.backend_id == "glm-ocr-local-openai"
    assert backend.hardware_path == "gpu"
    assert backend.fallback_chain == ("glm-ocr-local-openai", "glm-ocr-remote-openai")


def test_resolve_backend_uses_local_cpu_when_no_gpu_present() -> None:
    settings = get_settings()
    local_settings = replace(
        settings,
        ocr=replace(
            settings.ocr,
            local_url="http://localhost:8001/v1",
            server_url="https://remote.example.com/v1",
            model="glm-ocr",
        ),
    )
    backend = resolve_ocr_backend(local_settings, capabilities=_snapshot(has_gpu=False))
    assert backend.backend_id == "glm-ocr-local-openai"
    assert backend.hardware_path == "cpu"
    assert backend.reason_codes


def test_resolve_backend_remote_only_configuration() -> None:
    settings = get_settings()
    remote_only = replace(
        settings,
        ocr=replace(
            settings.ocr,
            local_url="",
            server_url="https://remote.example.com/v1",
            model="glm-ocr",
        ),
    )
    backend = resolve_ocr_backend(remote_only, capabilities=_snapshot(has_gpu=False))
    assert backend.backend_id == "glm-ocr-remote-openai"
    assert backend.hardware_path == "remote"
    assert backend.fallback_chain == ("glm-ocr-remote-openai",)
