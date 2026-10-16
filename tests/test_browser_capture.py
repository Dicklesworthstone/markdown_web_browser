#!/usr/bin/env python3
"""Tests for browser capture, build pinning, transport reporting, and BrowserPool."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import sys
import threading

import pytest

from app.capture import (
    BrowserPinMismatchError,
    BrowserPool,
    CaptureConfig,
    UnsupportedTransportError,
    _normalize_channel,
    _validate_playwright_version,
    build_user_agent_for_version,
    capture_tiles,
    compute_effective_style_hash,
    format_cft_version,
    resolve_browser_transport,
    verify_browser_build,
)
from app.settings import get_settings


class _SimpleHTMLHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        body = (
            b"<!doctype html><html><head><title>Pin Test</title></head>"
            b"<body><main><h1>Browser Build Verification</h1><p>Deterministic content.</p></main></body></html>"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return


@pytest.fixture()
def local_html_url():
    server = HTTPServer(("127.0.0.1", 0), _SimpleHTMLHandler)
    port = server.server_port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}/"
    finally:
        server.shutdown()
        server.server_close()


async def test_playwright_installed() -> None:
    """Verify Playwright and the configured Chrome channel launch cleanly."""
    from playwright.async_api import async_playwright

    settings = get_settings()
    channel = _normalize_channel(settings.browser.playwright_channel)
    async with async_playwright() as p:
        launch_kwargs: dict[str, object] = {
            "headless": True,
            "args": ["--headless=new", "--no-sandbox", "--disable-gpu"],
        }
        if channel:
            launch_kwargs["channel"] = channel
        browser = await p.chromium.launch(**launch_kwargs)
        version = browser.version
        await browser.close()
    assert version and version[0].isdigit()


async def test_basic_capture(local_html_url: str) -> None:
    """Verify real browser capture records observed build, transport, UA, and checklist."""
    config = CaptureConfig(
        url=local_html_url,
        viewport_width=1280,
        viewport_height=800,
        device_scale_factor=1,
    )
    result = await capture_tiles(config)

    assert len(result.tiles) >= 1
    assert result.manifest.cft_version.startswith("chrome-")
    observed_ver = result.manifest.cft_version.removeprefix("chrome-")
    assert f"Chrome/{observed_ver}" in result.manifest.user_agent
    assert result.manifest.browser_transport == "cdp"
    assert result.manifest.playwright_version != "unknown"
    assert result.manifest.screenshot_style_hash == compute_effective_style_hash(
        settings_browser=get_settings().browser,
        config=config,
        blocklist_version=result.manifest.blocklist_version,
    )

    checklist = result.manifest.metadata_checklist()
    assert checklist["cft_label"] == result.manifest.cft_label
    assert checklist["cft_version"] == result.manifest.cft_version
    assert checklist["browser_transport"] == "cdp"
    assert checklist["playwright_version"] == result.manifest.playwright_version
    assert checklist["screenshot_style_hash"] == result.manifest.screenshot_style_hash
    assert checklist["ocr_model"] == result.manifest.ocr_model
    assert isinstance(checklist["viewport_sweep_retries_triggered"], bool)


def test_resolve_browser_transport_validates_and_rejects_unexercised_bidi() -> None:
    """Transport resolution must report exercised CDP and reject strict BiDi spoofing."""
    assert resolve_browser_transport("cdp") == "cdp"
    assert resolve_browser_transport("CDP") == "cdp"
    assert resolve_browser_transport("bidi,cdp") == "cdp"

    with pytest.raises(UnsupportedTransportError, match="not supported"):
        resolve_browser_transport("bidi")

    with pytest.raises(UnsupportedTransportError, match="Unsupported browser_transport"):
        resolve_browser_transport("websocket-magic")


def test_verify_browser_build_enforces_pin_mismatch() -> None:
    """When enforce_pin is enabled, version mismatches raise BrowserPinMismatchError."""
    # Unpinned mode records the actual observed browser version even if configured differs
    observed = verify_browser_build(
        observed_version="155.0.8059.79",
        configured_version="chrome-130.0.6723.69",
        enforce_pin=False,
    )
    assert observed == "chrome-155.0.8059.79"

    # Pinned mode succeeds when versions match
    matched = verify_browser_build(
        observed_version="155.0.8059.79",
        configured_version="chrome-155.0.8059.79",
        enforce_pin=True,
    )
    assert matched == "chrome-155.0.8059.79"

    # Pinned mode fails fast before navigation/OCR when versions diverge
    with pytest.raises(BrowserPinMismatchError, match="Browser build mismatch"):
        verify_browser_build(
            observed_version="155.0.8059.79",
            configured_version="chrome-130.0.6723.69",
            enforce_pin=True,
        )


async def test_capture_fails_fast_on_pinned_version_or_missing_executable(
    local_html_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """capture_tiles must fail before navigation when CFT_ENFORCE_PIN or executable_path fails."""
    base_settings = get_settings()
    pinned_browser = replace(
        base_settings.browser,
        cft_version="chrome-99.0.1234.56",
        enforce_version_pin=True,
    )
    monkeypatch.setattr(
        "app.capture.get_settings",
        lambda: replace(base_settings, browser=pinned_browser),
    )

    with pytest.raises(BrowserPinMismatchError, match="Browser build mismatch"):
        await capture_tiles(
            CaptureConfig(
                url=local_html_url,
                viewport_width=800,
                viewport_height=600,
                device_scale_factor=1,
            )
        )

    missing_exec_browser = replace(
        base_settings.browser,
        executable_path=Path("/nonexistent/cft/chrome-binary"),
        enforce_version_pin=False,
    )
    monkeypatch.setattr(
        "app.capture.get_settings",
        lambda: replace(base_settings, browser=missing_exec_browser),
    )
    with pytest.raises(BrowserPinMismatchError, match="does not exist"):
        await capture_tiles(
            CaptureConfig(
                url=local_html_url,
                viewport_width=800,
                viewport_height=600,
                device_scale_factor=1,
            )
        )


def test_playwright_version_gate_and_user_agent_builder() -> None:
    """Playwright version gate requires >= 1.50 and UA reflects actual browser version."""
    assert _validate_playwright_version("1.57.0") == "1.57.0"
    with pytest.raises(RuntimeError, match=">= 1.50"):
        _validate_playwright_version("1.49.0")

    ua = build_user_agent_for_version("chrome-155.0.8059.79")
    assert "Chrome/155.0.8059.79" in ua
    assert "130.0.0.0" not in ua
    assert format_cft_version("155.0.8059.79") == "chrome-155.0.8059.79"


async def test_browser_pool_concurrency_and_crash_recycling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BrowserPool caps concurrency, reuses healthy browsers, and recycles crashed ones."""

    class FakePW:
        async def start(self) -> FakePW:
            return self

        async def stop(self) -> None:
            pass

    class FakeBrowser:
        def __init__(self, idx: int) -> None:
            self.idx = idx
            self.version = f"155.0.0.{idx}"
            self.connected = True
            self.closed = False

        def is_connected(self) -> bool:
            return self.connected and not self.closed

        async def close(self) -> None:
            self.closed = True
            self.connected = False

    created_browsers: list[FakeBrowser] = []

    async def fake_launch_browser(*args: object, **kwargs: object) -> FakeBrowser:
        browser = FakeBrowser(len(created_browsers) + 1)
        created_browsers.append(browser)
        return browser

    monkeypatch.setattr("app.capture.async_playwright", lambda: FakePW())
    monkeypatch.setattr("app.capture._launch_browser", fake_launch_browser)

    pool = BrowserPool(max_concurrent=2)
    active = 0
    max_active = 0
    gate = asyncio.Event()

    async def worker(simulate_crash: bool = False) -> int:
        nonlocal active, max_active
        async with pool.acquire_browser(
            channel="cft",
            viewport_width=800,
            viewport_height=600,
        ) as browser:
            active += 1
            max_active = max(max_active, active)
            if active == 2:
                gate.set()
            await gate.wait()
            if simulate_crash:
                browser.connected = False
            active -= 1
            return browser.idx

    results = await asyncio.gather(
        worker(simulate_crash=True),
        worker(simulate_crash=False),
        worker(simulate_crash=False),
    )
    assert max_active <= 2
    assert len(results) == 3
    assert pool.recycle_count >= 1
    assert created_browsers[0].closed is True

    await pool.close_all()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))

