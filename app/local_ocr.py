"""Local OCR runtime lifecycle management for self-hosted GLM/olm inference."""

from __future__ import annotations

import argparse
import asyncio
import atexit
import base64
import logging
from dataclasses import dataclass, replace
import subprocess
import sys
import time
from typing import Sequence
from urllib.parse import urlparse

import httpx

from app.hardware import HardwareCapabilitySnapshot, get_host_capabilities
from app.settings import Settings, get_settings

LOGGER = logging.getLogger(__name__)

_DEFAULT_LOCAL_ENDPOINT = "http://127.0.0.1:8001/v1"
_MODEL_ALIASES = {
    "glm-ocr": "zai-org/GLM-4.1V-9B-Thinking",
}


@dataclass(slots=True, frozen=True)
class LocalOCRServiceStatus:
    """Lifecycle metadata surfaced to diagnostics/provenance manifests."""

    enabled: bool
    endpoint: str
    healthy: bool
    action: str
    reason: str | None = None
    managed: bool = False
    pid: int | None = None
    launch_attempts: int = 0
    restart_count: int = 0
    startup_ms: int | None = None
    status_code: int | None = None
    probe_url: str | None = None
    command: tuple[str, ...] = ()
    hardware_path: str | None = None
    model: str | None = None
    served_model_name: str | None = None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "enabled": self.enabled,
            "endpoint": self.endpoint,
            "healthy": self.healthy,
            "action": self.action,
            "managed": self.managed,
            "launch_attempts": self.launch_attempts,
            "restart_count": self.restart_count,
        }
        if self.reason is not None:
            payload["reason"] = self.reason
        if self.pid is not None:
            payload["pid"] = self.pid
        if self.startup_ms is not None:
            payload["startup_ms"] = self.startup_ms
        if self.status_code is not None:
            payload["status_code"] = self.status_code
        if self.probe_url is not None:
            payload["probe_url"] = self.probe_url
        if self.command:
            payload["command"] = list(self.command)
        if self.hardware_path is not None:
            payload["hardware_path"] = self.hardware_path
        if self.model is not None:
            payload["model"] = self.model
        if self.served_model_name is not None:
            payload["served_model_name"] = self.served_model_name
        return payload


@dataclass(slots=True, frozen=True)
class _StartPlan:
    endpoint: str
    host: str
    port: int
    command: tuple[str, ...]
    hardware_path: str
    model: str
    served_model_name: str | None


class LocalOCRServiceManager:
    """Manage a local vLLM/SGLang-compatible OCR server lifecycle."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._process: subprocess.Popen[str] | None = None
        self._endpoint: str | None = None
        self._launch_attempts = 0
        self._restart_count = 0
        self._last_command: tuple[str, ...] = ()
        self._last_model: str | None = None
        self._last_served_model_name: str | None = None
        self._last_hardware_path: str | None = None

    async def ensure_service(
        self,
        *,
        settings: Settings | None = None,
        capabilities: HardwareCapabilitySnapshot | None = None,
        preferred_hardware_path: str | None = None,
    ) -> LocalOCRServiceStatus:
        """Ensure local OCR service is ready when local mode is configured."""

        cfg = settings or get_settings()
        local_url = (cfg.ocr.local_url or "").strip()
        if not local_url:
            return LocalOCRServiceStatus(
                enabled=False,
                endpoint=_DEFAULT_LOCAL_ENDPOINT,
                healthy=False,
                action="disabled",
                reason="local-url-not-configured",
            )

        try:
            normalized_endpoint = _normalize_endpoint(local_url)
        except ValueError as exc:
            return LocalOCRServiceStatus(
                enabled=True,
                endpoint=local_url,
                healthy=False,
                action="unavailable",
                reason=f"invalid-local-url:{exc}",
            )

        status_code: int | None
        probe_url: str | None
        healthy, status_code, probe_url = await _probe_health(
            normalized_endpoint,
            timeout=max(1, cfg.ocr.local_healthcheck_timeout_s),
        )
        if healthy:
            process = self._process
            pid = process.pid if process and process.poll() is None else None
            managed = pid is not None and self._endpoint == normalized_endpoint
            return LocalOCRServiceStatus(
                enabled=True,
                endpoint=normalized_endpoint,
                healthy=True,
                action="reused",
                reason="service-healthy",
                managed=managed,
                pid=pid,
                launch_attempts=self._launch_attempts,
                restart_count=self._restart_count,
                status_code=status_code,
                probe_url=probe_url,
                command=self._last_command,
                hardware_path=self._last_hardware_path,
                model=self._last_model,
                served_model_name=self._last_served_model_name,
            )

        if not cfg.ocr.local_autostart:
            return LocalOCRServiceStatus(
                enabled=True,
                endpoint=normalized_endpoint,
                healthy=False,
                action="unavailable",
                reason="autostart-disabled",
                managed=False,
                launch_attempts=self._launch_attempts,
                restart_count=self._restart_count,
                status_code=status_code,
                probe_url=probe_url,
                command=self._last_command,
                hardware_path=self._last_hardware_path,
                model=self._last_model,
                served_model_name=self._last_served_model_name,
            )

        async with self._lock:
            healthy, status_code, probe_url = await _probe_health(
                normalized_endpoint,
                timeout=max(1, cfg.ocr.local_healthcheck_timeout_s),
            )
            if healthy:
                process = self._process
                pid = process.pid if process and process.poll() is None else None
                managed = pid is not None and self._endpoint == normalized_endpoint
                return LocalOCRServiceStatus(
                    enabled=True,
                    endpoint=normalized_endpoint,
                    healthy=True,
                    action="reused",
                    reason="service-healthy",
                    managed=managed,
                    pid=pid,
                    launch_attempts=self._launch_attempts,
                    restart_count=self._restart_count,
                    status_code=status_code,
                    probe_url=probe_url,
                    command=self._last_command,
                    hardware_path=self._last_hardware_path,
                    model=self._last_model,
                    served_model_name=self._last_served_model_name,
                )

            if self._process is not None:
                await asyncio.to_thread(_terminate_process, self._process)
                self._process = None
                self._endpoint = None

            plan = _build_start_plan(
                cfg,
                endpoint=normalized_endpoint,
                capabilities=capabilities or get_host_capabilities(),
                preferred_hardware_path=preferred_hardware_path,
            )
            self._last_command = plan.command
            self._last_hardware_path = plan.hardware_path
            self._last_model = plan.model
            self._last_served_model_name = plan.served_model_name

            max_attempts = max(1, cfg.ocr.local_max_restarts + 1)
            last_startup_ms: int | None = None
            last_reason = "startup-timeout"
            for attempt_idx in range(max_attempts):
                self._launch_attempts += 1
                startup = time.perf_counter()
                try:
                    process = subprocess.Popen(
                        list(plan.command),
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.STDOUT,
                        text=True,
                    )
                except (FileNotFoundError, OSError) as exc:
                    last_reason = f"spawn-failed:{exc.__class__.__name__}"
                    LOGGER.exception("Local OCR service spawn failed")
                    return LocalOCRServiceStatus(
                        enabled=True,
                        endpoint=plan.endpoint,
                        healthy=False,
                        action="start-failed",
                        reason=last_reason,
                        launch_attempts=self._launch_attempts,
                        restart_count=self._restart_count,
                        command=plan.command,
                        hardware_path=plan.hardware_path,
                        model=plan.model,
                        served_model_name=plan.served_model_name,
                    )

                self._process = process
                self._endpoint = plan.endpoint

                ready = await self._wait_until_ready(
                    endpoint=plan.endpoint,
                    process=process,
                    startup_timeout_s=max(1, cfg.ocr.local_startup_timeout_s),
                    health_timeout_s=max(1, cfg.ocr.local_healthcheck_timeout_s),
                )
                last_startup_ms = int((time.perf_counter() - startup) * 1000)
                if ready:
                    action = "started" if attempt_idx == 0 else "restarted"
                    return LocalOCRServiceStatus(
                        enabled=True,
                        endpoint=plan.endpoint,
                        healthy=True,
                        action=action,
                        reason="service-ready",
                        managed=True,
                        pid=process.pid,
                        launch_attempts=self._launch_attempts,
                        restart_count=self._restart_count,
                        startup_ms=last_startup_ms,
                        command=plan.command,
                        hardware_path=plan.hardware_path,
                        model=plan.model,
                        served_model_name=plan.served_model_name,
                    )

                await asyncio.to_thread(_terminate_process, process)
                self._process = None
                self._endpoint = None
                last_reason = "startup-timeout"
                if attempt_idx < max_attempts - 1:
                    self._restart_count += 1
                    LOGGER.warning(
                        "Local OCR service startup timed out; retrying (attempt=%s/%s, endpoint=%s)",
                        attempt_idx + 2,
                        max_attempts,
                        plan.endpoint,
                    )

            return LocalOCRServiceStatus(
                enabled=True,
                endpoint=plan.endpoint,
                healthy=False,
                action="start-failed",
                reason=last_reason,
                managed=False,
                launch_attempts=self._launch_attempts,
                restart_count=self._restart_count,
                startup_ms=last_startup_ms,
                command=plan.command,
                hardware_path=plan.hardware_path,
                model=plan.model,
                served_model_name=plan.served_model_name,
            )

    async def _wait_until_ready(
        self,
        *,
        endpoint: str,
        process: subprocess.Popen[str],
        startup_timeout_s: int,
        health_timeout_s: int,
    ) -> bool:
        deadline = time.monotonic() + max(1, startup_timeout_s)
        while time.monotonic() < deadline:
            if process.poll() is not None:
                return False
            healthy, _, _ = await _probe_health(endpoint, timeout=max(1, health_timeout_s))
            if healthy:
                return True
            await asyncio.sleep(1)
        return False

    def shutdown(self) -> None:
        process = self._process
        self._process = None
        self._endpoint = None
        if process is not None:
            _terminate_process(process)

    def current_process(self) -> subprocess.Popen[str] | None:
        process = self._process
        if process is None:
            return None
        if process.poll() is not None:
            return None
        return process


class LocalOCRClient:
    """Small direct client for local OCR inference via OpenAI-compatible endpoints."""

    def __init__(
        self,
        endpoint: str = "http://localhost:8001/v1/chat/completions",
        model: str = "olmOCR-2-7B-1025-FP8",
        timeout: int = 30,
    ) -> None:
        self.endpoint = endpoint
        self.model = model
        self.timeout = timeout

    async def process_tile(self, tile_bytes: bytes, prompt: str | None = None) -> str:
        image_b64 = base64.b64encode(tile_bytes).decode("utf-8")
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": prompt
                            or "Convert this image to markdown preserving structure and text.",
                        },
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b64}"}},
                    ],
                }
            ],
            "max_tokens": 4096,
            "temperature": 0.0,
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(self.endpoint, json=payload)
            response.raise_for_status()
            data = response.json()
        choices = data.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0]
            if isinstance(first, dict):
                message = first.get("message")
                if isinstance(message, dict):
                    content = message.get("content")
                    if isinstance(content, str) and content.strip():
                        return content.strip()
                text_value = first.get("text")
                if isinstance(text_value, str) and text_value.strip():
                    return text_value.strip()
        raise ValueError("Unexpected local OCR response shape")

    async def process_batch(self, tiles: Sequence[bytes], batch_size: int = 3) -> list[str]:
        results: list[str] = []
        for idx in range(0, len(tiles), max(1, batch_size)):
            batch = tiles[idx : idx + max(1, batch_size)]
            jobs = [self.process_tile(tile) for tile in batch]
            batch_results = await asyncio.gather(*jobs, return_exceptions=True)
            for value in batch_results:
                if isinstance(value, BaseException):
                    LOGGER.warning("Local OCR tile failed: %s", value)
                    results.append("")
                else:
                    results.append(value)
        return results


_SERVICE_MANAGER = LocalOCRServiceManager()


async def ensure_local_ocr_service(
    *,
    settings: Settings | None = None,
    capabilities: HardwareCapabilitySnapshot | None = None,
    preferred_hardware_path: str | None = None,
) -> LocalOCRServiceStatus:
    """Probe/reuse/autostart local OCR service and return lifecycle metadata."""

    return await _SERVICE_MANAGER.ensure_service(
        settings=settings,
        capabilities=capabilities,
        preferred_hardware_path=preferred_hardware_path,
    )


def get_local_ocr_service_manager() -> LocalOCRServiceManager:
    return _SERVICE_MANAGER


def _normalize_endpoint(local_url: str) -> str:
    parsed = urlparse(local_url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("local-url-scheme")
    if not parsed.netloc:
        raise ValueError("local-url-netloc")

    path = parsed.path.rstrip("/")
    if not path:
        path = "/v1"
    if path.endswith("/chat/completions"):
        path = path.removesuffix("/chat/completions") or "/v1"
    if path.endswith("/models"):
        path = path.removesuffix("/models") or "/v1"
    return f"{parsed.scheme}://{parsed.netloc}{path}"


def _probe_candidates(endpoint: str) -> tuple[str, ...]:
    endpoint = endpoint.rstrip("/")
    parsed = urlparse(endpoint)
    base = f"{parsed.scheme}://{parsed.netloc}"
    candidates: list[str] = []
    if endpoint.endswith("/v1"):
        candidates.append(f"{endpoint}/models")
    else:
        candidates.append(f"{endpoint}/models")
    candidates.append(f"{base}/health")
    deduped: list[str] = []
    for candidate in candidates:
        if candidate not in deduped:
            deduped.append(candidate)
    return tuple(deduped)


async def _probe_health(endpoint: str, *, timeout: int) -> tuple[bool, int | None, str | None]:
    timeout_seconds = max(1, timeout)
    last_status: int | None = None
    last_url: str | None = None
    async with httpx.AsyncClient(timeout=timeout_seconds, http2=True) as client:
        for probe_url in _probe_candidates(endpoint):
            try:
                response = await client.get(probe_url)
            except Exception:
                continue
            last_status = response.status_code
            last_url = probe_url
            if response.status_code < 500:
                return True, response.status_code, probe_url
    return False, last_status, last_url


def _resolve_launch_model(model: str) -> tuple[str, str | None]:
    normalized = model.strip()
    if not normalized:
        raise ValueError("ocr-model-empty")
    mapped = _MODEL_ALIASES.get(normalized.lower())
    if mapped:
        return mapped, normalized
    return normalized, None


def _build_start_plan(
    settings: Settings,
    *,
    endpoint: str,
    capabilities: HardwareCapabilitySnapshot,
    preferred_hardware_path: str | None,
) -> _StartPlan:
    parsed = urlparse(endpoint)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 8001

    hardware_path = preferred_hardware_path or capabilities.preferred_hardware_path
    if hardware_path not in {"gpu", "cpu"}:
        hardware_path = capabilities.preferred_hardware_path

    model, served_model_name = _resolve_launch_model(settings.ocr.model)
    command = [
        sys.executable,
        "-m",
        "vllm.entrypoints.openai.api_server",
        "--model",
        model,
        "--host",
        host,
        "--port",
        str(port),
        "--trust-remote-code",
        "--max-model-len",
        "8192",
    ]
    if served_model_name and served_model_name != model:
        command.extend(["--served-model-name", served_model_name])

    if hardware_path == "gpu":
        tensor_parallel_size = max(1, capabilities.gpu_count)
        command.extend(["--tensor-parallel-size", str(tensor_parallel_size)])
        command.extend(["--gpu-memory-utilization", "0.90"])
    else:
        command.extend(["--device", "cpu"])

    return _StartPlan(
        endpoint=endpoint,
        host=host,
        port=port,
        command=tuple(command),
        hardware_path=hardware_path,
        model=model,
        served_model_name=served_model_name,
    )


def _terminate_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
    except OSError:
        return


async def start_local_ocr_server(
    model: str = "olmOCR-2-7B-1025-FP8",
    host: str = "0.0.0.0",
    port: int = 8001,
    wait_for_ready: bool = True,
    ready_timeout: int = 300,
) -> subprocess.Popen[str]:
    """Compatibility helper used by ad-hoc startup scripts."""

    cfg = get_settings()
    if not wait_for_ready:
        plan = _build_start_plan(
            replace(
                cfg,
                ocr=replace(
                    cfg.ocr,
                    model=model,
                    local_url=f"http://{host}:{port}/v1",
                ),
            ),
            endpoint=f"http://{host}:{port}/v1",
            capabilities=get_host_capabilities(),
            preferred_hardware_path=None,
        )
        return subprocess.Popen(
            list(plan.command),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
            text=True,
        )

    local_cfg = replace(
        cfg,
        ocr=replace(
            cfg.ocr,
            model=model,
            local_url=f"http://{host}:{port}/v1",
            local_autostart=True,
            local_startup_timeout_s=max(1, ready_timeout),
        ),
    )
    status = await ensure_local_ocr_service(settings=local_cfg)
    if not status.healthy:
        raise TimeoutError(status.reason or "local-service-unavailable")
    process = get_local_ocr_service_manager().current_process()
    if process is None:
        raise RuntimeError("Local OCR service process handle missing")
    return process


def cli_start_server() -> None:
    """CLI entry point for starting local OCR server."""

    parser = argparse.ArgumentParser(description="Start local OCR server")
    parser.add_argument("--model", default="olmOCR-2-7B-1025-FP8", help="Model to load")
    parser.add_argument("--host", default="0.0.0.0", help="Server host")
    parser.add_argument("--port", type=int, default=8001, help="Server port")
    parser.add_argument("--no-wait", action="store_true", help="Don't wait for ready")
    args = parser.parse_args()

    async def _main() -> None:
        process = await start_local_ocr_server(
            model=args.model,
            host=args.host,
            port=args.port,
            wait_for_ready=not args.no_wait,
        )
        print(f"\nLocal OCR service running on http://{args.host}:{args.port}/v1")
        print(f"Model: {args.model}")
        print(f"PID: {process.pid}\n")
        print("Press Ctrl+C to stop.\n")
        try:
            process.wait()
        except KeyboardInterrupt:
            print("\nShutting down local OCR service...")
            _terminate_process(process)
            print("Server stopped.\n")

    asyncio.run(_main())


@atexit.register
def _cleanup_managed_local_process() -> None:
    _SERVICE_MANAGER.shutdown()


if __name__ == "__main__":
    cli_start_server()
