#!/usr/bin/env python3
"""Quick health check for the Prometheus metrics endpoint."""

from __future__ import annotations

import sys

import httpx

from app.settings import get_settings
from scripts import mdwb_cli


def main() -> None:
    settings = mdwb_cli._resolve_settings(None)  # reuse CLI env loader
    app_settings = get_settings()
    base_url = settings.base_url.rstrip("/")
    urls = [f"{base_url}/metrics"]
    exporter_port = app_settings.telemetry.prometheus_port
    if exporter_port:
        urls.append(f"http://localhost:{exporter_port}/metrics")
    with httpx.Client(timeout=5) as client:
        for url in urls:
            try:
                response = client.get(url)
                response.raise_for_status()
            except Exception as exc:  # noqa: BLE001
                print(f"❌ metrics scrape failed: {url} ({exc})")
                sys.exit(1)
            else:
                print(f"✅ metrics scrape ok: {url}")


if __name__ == "__main__":
    main()
