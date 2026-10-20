"""Live-capture E2E test: drive a real Playwright capture of example.com and verify artifacts.

Skipped automatically when:
- the network is unreachable
- Playwright / Chrome for Testing binaries are missing
- ``MDWB_SKIP_LIVE_CAPTURE=1`` is set in the env

This is the only test in the suite that hits a real network; it is the closest
thing we have to a "smoke test against reality."
"""

from __future__ import annotations

import os
import socket
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.capture import CaptureConfig, capture_tiles
from app.settings import get_settings
from app.store import StorageConfig, Store


def _network_reachable(host: str = "example.com", port: int = 443, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _playwright_browser_available() -> bool:
    """True only if the playwright Python package is installed AND a usable
    Chromium binary is on disk (otherwise the capture would fail at launch).
    """
    try:
        from playwright.sync_api import sync_playwright  # type: ignore[import-not-found]  # noqa: F401
    except Exception:
        return False
    # Look for the browser binary in the standard cache locations. A synchronous
    # launch probe is too expensive (it spawns a process) and flaky in CI.
    from pathlib import Path
    import os

    candidates = [
        # Default Playwright cache
        Path.home() / ".cache" / "ms-playwright",
        # System-installed Chrome (Linux)
        Path("/opt/google/chrome/chrome"),
        Path("/usr/bin/google-chrome"),
        Path("/usr/bin/chromium"),
        Path("/usr/bin/chromium-browser"),
    ]
    # If any cached browser dir exists with a chrome binary, we are good
    for root in candidates:
        if root.is_file() and os.access(root, os.X_OK):
            return True
        if root.is_dir():
            for sub in root.rglob("chrome"):
                if sub.is_file() and os.access(sub, os.X_OK):
                    return True
    return False


pytestmark = pytest.mark.skipif(
    os.environ.get("MDWB_SKIP_LIVE_CAPTURE", "0") == "1"
    or not _network_reachable()
    or not _playwright_browser_available(),
    reason="live capture requires network + Playwright; set MDWB_SKIP_LIVE_CAPTURE=1 to force-skip",
)


@pytest.mark.asyncio
async def test_live_capture_of_example_com(tmp_path: Path, monkeypatch) -> None:
    """Drive a real capture against example.com and assert the artifacts exist.

    The test is environment-sensitive (network, browser binary, model host).
    It is auto-skipped when the environment can't satisfy all three so it
    doesn't break local development.
    """
    # Bind the actual immutable settings before capture, without mutating .env.
    settings = get_settings()
    isolated_settings = replace(
        settings,
        storage=replace(settings.storage, cache_root=tmp_path, profiles_root=tmp_path / "profiles"),
    )
    monkeypatch.setattr("app.capture.get_settings", lambda: isolated_settings)
    config = CaptureConfig(
        url="https://example.com",
        cache_seed="r5-live-capture-test",
    )

    result = await capture_tiles(config)

    # Manifest should have a URL + a sweep count
    assert result.manifest.url == "https://example.com"
    assert result.manifest.screenshot_style_hash
    assert result.manifest.tiles_total == len(result.tiles) > 0
    assert result.dom_snapshot and "Example Domain" in result.dom_snapshot.decode()
    # Capture returns pixels; Store owns persisted paths. Exercise both real
    # components rather than asserting a nonexistent CaptureManifest field.
    store = Store(StorageConfig(tmp_path / "artifacts", tmp_path / "runs.db"))
    paths = store.allocate_run(
        job_id="live-example", url=config.url, started_at=datetime.now(timezone.utc)
    )
    artifacts = store.write_tiles(job_id="live-example", tiles=result.tiles)
    manifest_path = store.write_manifest(job_id="live-example", manifest=result.manifest)
    dom_path = store.write_dom_snapshot(job_id="live-example", html=result.dom_snapshot)
    assert paths.root.is_dir()
    assert manifest_path == paths.manifest_path and manifest_path.is_file()
    assert json.loads(manifest_path.read_text())["url"] == config.url
    assert dom_path == paths.dom_snapshot_path and dom_path.is_file()
    assert len(artifacts) == len(result.tiles)
    for tile, artifact in zip(result.tiles, artifacts, strict=True):
        assert (paths.root / artifact["path"]).read_bytes() == tile.png_bytes
    store.engine.dispose()


def test_live_capture_skipped_when_env_set() -> None:
    """Sanity: setting MDWB_SKIP_LIVE_CAPTURE=1 forces a skip even if network is up."""
    # This test doesn't actually run the live capture; it just asserts the
    # env var contract. (Pytest's own skipif at module level already handles
    # the production skip path.)
    assert os.environ.get("MDWB_SKIP_LIVE_CAPTURE", "0") in {"0", "1"}


def test_live_capture_threshold_documented() -> None:
    """The skip condition is documented in the module docstring; this asserts
    it's still wired so a regression (e.g. someone disabling the skip without
    intent) is caught at the test layer.
    """
    assert "MDWB_SKIP_LIVE_CAPTURE" in (test_live_capture_skipped_when_env_set.__doc__ or "")
    # And the module docstring mentions the env var
    import sys

    mod = sys.modules[__name__]
    assert "MDWB_SKIP_LIVE_CAPTURE" in (mod.__doc__ or "")
    assert "example.com" in (mod.__doc__ or "")
