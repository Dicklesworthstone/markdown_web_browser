"""Launcher for the Markdown Web Browser API using uvicorn or Granian."""

from __future__ import annotations

import os
from typing import Optional

import typer
import uvicorn

app = typer.Typer(help="Run the Markdown Web Browser FastAPI app with uvicorn or Granian.", add_completion=False)


def _env_str(key: str, default: str) -> str:
    return os.getenv(key, default)


def _env_int(key: str, default: int) -> int:
    raw = os.getenv(key, str(default))
    try:
        return int(raw)
    except ValueError as exc:  # pragma: no cover - defensive
        raise typer.BadParameter(f"{key} must be an integer") from exc


def _env_bool(key: str, default: bool) -> bool:
    raw = os.getenv(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _server_default() -> str:
    return os.getenv("MDWB_SERVER_IMPL", os.getenv("SERVER_IMPL", "uvicorn")).lower()


def _granian_log_level(value: str):  # type: ignore[no-untyped-def]
    from granian.log import LogLevels

    normalized = (value or "info").strip().lower()
    mapping = {
        "critical": LogLevels.critical,
        "error": LogLevels.error,
        "warning": LogLevels.warning,
        "warn": LogLevels.warning,
        "info": LogLevels.info,
        "debug": LogLevels.debug,
        "trace": LogLevels.debug,
    }
    return mapping.get(normalized, LogLevels.info)


@app.callback(invoke_without_command=True)
def serve(  # type: ignore[no-untyped-def]
    server: Optional[str] = typer.Option(None, "--server", "-s", help="Server implementation (uvicorn or granian)."),
    host: Optional[str] = typer.Option(None, "--host", help="Bind host."),
    port: Optional[int] = typer.Option(None, "--port", help="Bind port."),
    app_path: Optional[str] = typer.Option(None, "--app", help="ASGI import path (default app.main:app)."),
    reload: Optional[bool] = typer.Option(None, "--reload/--no-reload", help="Enable auto-reload."),
    workers: Optional[int] = typer.Option(None, "--workers", help="Worker processes."),
    log_level: Optional[str] = typer.Option(None, "--log-level", help="Log level."),
    granian_runtime_threads: Optional[int] = typer.Option(
        None,
        "--granian-runtime-threads",
        help="Runtime threads for Granian.",
    ),
) -> None:
    """Launch the FastAPI app using the requested runtime."""

    server_impl = (server or _server_default()).lower()
    if server_impl not in {"uvicorn", "granian"}:
        raise typer.BadParameter("server must be 'uvicorn' or 'granian'", param_hint="--server")

    host = host or _env_str("HOST", "127.0.0.1")
    port = port or _env_int("PORT", 8000)
    app_path = app_path or _env_str("APP_MODULE", "app.main:app")

    if reload is None:
        reload = _env_bool("MDWB_SERVER_RELOAD", server_impl == "uvicorn")

    workers = workers or _env_int("MDWB_SERVER_WORKERS", 1)
    log_level = (log_level or _env_str("MDWB_SERVER_LOG_LEVEL", "info")).lower()
    granian_runtime_threads = granian_runtime_threads or _env_int("MDWB_GRANIAN_RUNTIME_THREADS", 1)

    if server_impl == "uvicorn":
        uvicorn.run(
            app_path,
            host=host,
            port=port,
            reload=reload,
            workers=max(1, workers),
            log_level=log_level,
        )
        return

    from granian import Granian
    from granian.constants import Interfaces, Loops

    granian_server = Granian(
        app_path,
        address=host,
        port=port,
        interface=Interfaces.ASGI,
        workers=max(1, workers),
        reload=reload,
        runtime_threads=max(1, granian_runtime_threads),
        log_level=_granian_log_level(log_level),
        loop=Loops.auto,
    )
    granian_server.serve()


def main() -> None:
    app()


if __name__ == "__main__":
    main()
