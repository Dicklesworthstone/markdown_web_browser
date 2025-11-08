#!/usr/bin/env python3
"""Prometheus metrics health check for Markdown Web Browser."""

from __future__ import annotations

from pathlib import Path

import httpx
import typer
from decouple import Config as DecoupleConfig, RepositoryEnv

cli = typer.Typer(help="Ping the Prometheus metrics endpoints and fail when unreachable.")


def _load_config() -> DecoupleConfig:
    env_path = Path(".env")
    if env_path.exists():
        target = env_path
    else:
        fallback = env_path.with_suffix(env_path.suffix + ".example") if env_path.suffix else Path(".env.example")
        target = fallback if fallback.exists() else env_path
    return DecoupleConfig(RepositoryEnv(str(target)))


def _default_api_base(cfg: DecoupleConfig) -> str:
    return cfg("API_BASE_URL", default="http://localhost:8000").rstrip("/")


def _default_exporter_port(cfg: DecoupleConfig) -> int:
    return cfg("PROMETHEUS_PORT", cast=int, default=9000)


def _probe(metrics_url: str, timeout: float) -> None:
    with httpx.Client(timeout=timeout) as client:
        response = client.get(metrics_url)
        response.raise_for_status()


@cli.command("run")
def run_check(
    api_base: str | None = typer.Option(
        None,
        help="Override API_BASE_URL (defaults to value from .env).",
    ),
    exporter_host: str = typer.Option(
        "localhost",
        help="Host to use when probing the standalone exporter on PROMETHEUS_PORT.",
    ),
    exporter_port: int | None = typer.Option(
        None,
        help="Override PROMETHEUS_PORT (defaults to value from .env).",
    ),
    include_exporter: bool = typer.Option(
        True,
        "--include-exporter/--no-include-exporter",
        help="Whether to probe the standalone exporter in addition to /metrics on the API base.",
    ),
    timeout: float = typer.Option(5.0, help="HTTP timeout per request (seconds)."),
) -> None:
    """Check Prometheus metrics endpoints and exit non-zero when unreachable."""

    cfg = _load_config()
    base = (api_base or _default_api_base(cfg)).rstrip("/")
    port = exporter_port if exporter_port is not None else _default_exporter_port(cfg)

    targets: list[str] = [f"{base}/metrics"]
    if include_exporter and port > 0:
        targets.append(f"http://{exporter_host}:{port}/metrics")

    errors: list[str] = []
    for url in targets:
        try:
            _probe(url, timeout)
            typer.echo(f"[OK] {url}")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"[FAIL] {url}: {exc}")

    if errors:
        for line in errors:
            typer.echo(line)
        raise typer.Exit(code=1)


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
