"""Host hardware capability detection for OCR runtime policy."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from importlib import import_module
import os
import platform
import subprocess
from typing import Callable, Sequence

_MB = 1024 * 1024
_NVIDIA_QUERY = (
    "nvidia-smi",
    "--query-gpu=index,name,memory.total,driver_version,cuda_version",
    "--format=csv,noheader,nounits",
)

CommandRunner = Callable[[Sequence[str], float], subprocess.CompletedProcess[str]]


@dataclass(slots=True, frozen=True)
class GPUDeviceCapability:
    """Capability metadata for one detected GPU device."""

    index: int
    vendor: str
    name: str
    memory_total_mb: int | None = None
    driver_version: str | None = None
    runtime_version: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "index": self.index,
            "vendor": self.vendor,
            "name": self.name,
            "memory_total_mb": self.memory_total_mb,
            "driver_version": self.driver_version,
            "runtime_version": self.runtime_version,
        }


@dataclass(slots=True, frozen=True)
class HardwareCapabilitySnapshot:
    """Stable hardware snapshot consumed by policy + manifest writers."""

    os_platform: str
    architecture: str
    cpu_physical_cores: int
    cpu_logical_cores: int
    memory_total_mb: int | None
    memory_available_mb: int | None
    gpu_devices: tuple[GPUDeviceCapability, ...] = ()
    detection_sources: tuple[str, ...] = ()
    detection_warnings: tuple[str, ...] = ()

    @property
    def gpu_count(self) -> int:
        return len(self.gpu_devices)

    @property
    def has_gpu(self) -> bool:
        return self.gpu_count > 0

    @property
    def preferred_hardware_path(self) -> str:
        return "gpu" if self.has_gpu else "cpu"

    def to_dict(self) -> dict[str, object]:
        return {
            "os_platform": self.os_platform,
            "architecture": self.architecture,
            "cpu_physical_cores": self.cpu_physical_cores,
            "cpu_logical_cores": self.cpu_logical_cores,
            "memory_total_mb": self.memory_total_mb,
            "memory_available_mb": self.memory_available_mb,
            "gpu_count": self.gpu_count,
            "has_gpu": self.has_gpu,
            "preferred_hardware_path": self.preferred_hardware_path,
            "gpu_devices": [device.to_dict() for device in self.gpu_devices],
            "detection_sources": list(self.detection_sources),
            "detection_warnings": list(self.detection_warnings),
        }


def _default_command_runner(
    command: Sequence[str], timeout_seconds: float
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout_seconds,
    )


def _detect_nvidia_gpus(runner: CommandRunner) -> tuple[list[GPUDeviceCapability], list[str]]:
    warnings: list[str] = []
    try:
        result = runner(_NVIDIA_QUERY, 2.0)
    except FileNotFoundError:
        return [], ["nvidia-smi-missing"]
    except subprocess.TimeoutExpired:
        return [], ["nvidia-smi-timeout"]
    except OSError:
        return [], ["nvidia-smi-error"]

    if result.returncode != 0:
        return [], ["nvidia-smi-nonzero-exit"]

    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        return [], []

    devices: list[GPUDeviceCapability] = []
    for line in lines:
        parts = [part.strip() for part in line.split(",", maxsplit=4)]
        if len(parts) < 5:
            warnings.append("nvidia-smi-parse-error")
            continue
        index_raw, name, memory_raw, driver_version, cuda_version = parts
        try:
            index = int(index_raw)
        except ValueError:
            warnings.append("nvidia-smi-parse-error")
            continue
        try:
            memory_total_mb = int(float(memory_raw))
        except ValueError:
            memory_total_mb = None
            warnings.append("nvidia-smi-memory-parse-error")
        devices.append(
            GPUDeviceCapability(
                index=index,
                vendor="nvidia",
                name=name,
                memory_total_mb=memory_total_mb,
                driver_version=driver_version or None,
                runtime_version=cuda_version or None,
            )
        )
    return devices, warnings


def _detect_torch_gpus() -> tuple[list[GPUDeviceCapability], list[str]]:
    try:
        torch = import_module("torch")
    except Exception:
        return [], []

    warnings: list[str] = []
    devices: list[GPUDeviceCapability] = []
    try:
        if torch.cuda.is_available():
            runtime = getattr(torch.version, "cuda", None)
            for index in range(int(torch.cuda.device_count())):
                props = torch.cuda.get_device_properties(index)
                devices.append(
                    GPUDeviceCapability(
                        index=index,
                        vendor="nvidia",
                        name=torch.cuda.get_device_name(index),
                        memory_total_mb=int(props.total_memory // _MB),
                        runtime_version=str(runtime) if runtime else None,
                    )
                )
            return devices, warnings
    except Exception:
        warnings.append("torch-cuda-probe-error")

    try:
        mps_backend = getattr(torch.backends, "mps", None)
        if mps_backend and mps_backend.is_available():
            devices.append(
                GPUDeviceCapability(
                    index=0,
                    vendor="apple",
                    name="Apple MPS",
                    runtime_version="mps",
                )
            )
            return devices, warnings
    except Exception:
        warnings.append("torch-mps-probe-error")
    return devices, warnings


def _safe_physical_core_count(logical_cores: int) -> int:
    try:
        import psutil
    except Exception:
        return logical_cores

    try:
        physical = psutil.cpu_count(logical=False)
    except Exception:
        return logical_cores
    if isinstance(physical, int) and physical > 0:
        return physical
    return logical_cores


def _safe_memory_snapshot() -> tuple[int | None, int | None]:
    psutil_module = None
    try:
        import psutil as _psutil_module
    except Exception:
        pass
    else:
        psutil_module = _psutil_module

    if psutil_module is not None:
        try:
            vm = psutil_module.virtual_memory()
            return int(vm.total // _MB), int(vm.available // _MB)
        except Exception:
            pass

    total_mb = _sysconf_memory("SC_PHYS_PAGES")
    available_mb = _sysconf_memory("SC_AVPHYS_PAGES")
    if total_mb is not None:
        return total_mb, available_mb

    mem_available = _linux_meminfo_mb("MemAvailable")
    mem_total = _linux_meminfo_mb("MemTotal")
    return mem_total, mem_available


def _sysconf_memory(pages_key: str) -> int | None:
    if not hasattr(os, "sysconf"):
        return None
    try:
        pages = os.sysconf(pages_key)
        page_size = os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, AttributeError):
        return None
    if not isinstance(pages, int) or not isinstance(page_size, int):
        return None
    if pages <= 0 or page_size <= 0:
        return None
    return int((pages * page_size) // _MB)


def _linux_meminfo_mb(field: str) -> int | None:
    meminfo_path = "/proc/meminfo"
    if not os.path.exists(meminfo_path):
        return None
    try:
        with open(meminfo_path, encoding="utf-8") as handle:
            for line in handle:
                if not line.startswith(f"{field}:"):
                    continue
                _, raw_value = line.split(":", maxsplit=1)
                token = raw_value.strip().split()[0]
                return int(token) // 1024
    except (OSError, ValueError):
        return None
    return None


def detect_host_capabilities(
    *,
    command_runner: CommandRunner | None = None,
) -> HardwareCapabilitySnapshot:
    """Detect host CPU/GPU capabilities with defensive fallbacks."""

    runner = command_runner or _default_command_runner
    logical_cores = os.cpu_count() or 1
    physical_cores = _safe_physical_core_count(logical_cores)
    memory_total_mb, memory_available_mb = _safe_memory_snapshot()

    gpu_devices: list[GPUDeviceCapability] = []
    detection_sources: list[str] = []
    detection_warnings: list[str] = []

    nvidia_devices, nvidia_warnings = _detect_nvidia_gpus(runner)
    if nvidia_devices:
        gpu_devices.extend(nvidia_devices)
        detection_sources.append("nvidia-smi")
    detection_warnings.extend(nvidia_warnings)

    if not gpu_devices:
        torch_devices, torch_warnings = _detect_torch_gpus()
        if torch_devices:
            gpu_devices.extend(torch_devices)
            detection_sources.append("torch")
        detection_warnings.extend(torch_warnings)

    return HardwareCapabilitySnapshot(
        os_platform=platform.system().lower(),
        architecture=platform.machine().lower(),
        cpu_physical_cores=max(1, physical_cores),
        cpu_logical_cores=max(1, logical_cores),
        memory_total_mb=memory_total_mb,
        memory_available_mb=memory_available_mb,
        gpu_devices=tuple(gpu_devices),
        detection_sources=tuple(detection_sources),
        detection_warnings=tuple(detection_warnings),
    )


@lru_cache(maxsize=1)
def get_host_capabilities() -> HardwareCapabilitySnapshot:
    """Return cached host capabilities for policy/manifest consumers."""

    return detect_host_capabilities()


def reset_host_capabilities_cache() -> None:
    """Clear cached host capability snapshot (test helper)."""

    get_host_capabilities.cache_clear()
