from __future__ import annotations

from typing import Any, Iterator, Sequence, cast

import pytest

import app.capture as capture_module
from app.blocklist import BlocklistConfig
from app.capture import CaptureConfig, _perform_viewport_sweeps
from app.capture_warnings import CaptureWarningEntry
from app.settings import WarningSettings


class _FakeLocator:
    def __init__(self, selector: str) -> None:
        self.selector = selector


class _FakePage:
    def __init__(self, scroll_heights: Sequence[int], dom_html: str = "<html></html>") -> None:
        self._heights: Iterator[int] = iter(scroll_heights)
        self.dom_html = dom_html
        self.user_agent = "FakeAgent/1.0"
        self.scroll_calls: list[int] = []
        self.screenshot_calls = 0

    async def add_init_script(self, script: str) -> None:  # noqa: ARG002
        return None

    async def add_style_tag(self, content: str) -> None:  # noqa: ARG002
        return None

    async def goto(self, url: str, wait_until: str = "load", **kwargs: Any) -> None:  # noqa: ARG002
        return None

    def locator(self, selector: str) -> _FakeLocator:
        return _FakeLocator(selector)

    async def screenshot(self, **kwargs: Any) -> bytes:  # noqa: ARG002
        self.screenshot_calls += 1
        import pyvips

        return pyvips.Image.black(100, 800).pngsave_buffer()

    async def evaluate(self, script: str, *args: Any) -> Any:
        if script == "document.scrollingElement.scrollHeight":
            try:
                return next(self._heights)
            except StopIteration:
                return 0
        if script == "window.scrollTo(0, 0)":
            self.scroll_calls.append(0)
            return None
        if script == "window.scrollTo(0, arguments[0])":
            value = args[0] if args else 0
            self.scroll_calls.append(int(value))
            return None
        # Handle f-string formatted scrollTo calls like "window.scrollTo(0, 200)"
        if script.startswith("window.scrollTo(0, ") and script.endswith(")"):
            offset_str = script[len("window.scrollTo(0, ") : -1]
            try:
                self.scroll_calls.append(int(offset_str))
            except ValueError:
                pass
            return None
        if script == "navigator.userAgent":
            return self.user_agent
        return None

    async def wait_for_timeout(self, timeout_ms: int) -> None:  # noqa: ARG002
        return None

    async def content(self) -> str:
        return self.dom_html

    async def close(self) -> None:
        return None


class _FakeContext:
    def __init__(self, page: _FakePage) -> None:
        self._page = page

    async def new_page(self) -> _FakePage:
        return self._page


async def _stub_apply_blocklist(page, *, url: str, config: BlocklistConfig) -> dict[str, int]:  # noqa: ARG001
    await page.add_style_tag(content="/* masked */")
    return {"#banner": 2}


async def _stub_collect_warnings(page, settings: WarningSettings) -> list[CaptureWarningEntry]:  # noqa: ARG002
    return [
        CaptureWarningEntry(code="canvas-heavy", message="canvas", count=5, threshold=3),
    ]


@pytest.mark.asyncio()
async def test_perform_viewport_sweeps_handles_shrink_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = _FakePage(scroll_heights=[3000, 2500, 2500, 2500, 2500, 2500])
    context = cast(Any, _FakeContext(page))
    blocklist_config = BlocklistConfig(version="test", global_selectors=(), domain_selectors={})
    warning_settings = WarningSettings(
        canvas_warning_threshold=3,
        video_warning_threshold=2,
        shrink_warning_threshold=1,
        overlap_warning_ratio=0.6,
        seam_warning_ratio=0.8,
        seam_warning_min_pairs=1,
    )
    monkeypatch.setattr(capture_module, "apply_blocklist", _stub_apply_blocklist)
    monkeypatch.setattr(capture_module, "collect_capture_warnings", _stub_collect_warnings)

    (
        tiles,
        stats,
        user_agent,
        blocklist_hits,
        warnings,
        dom_bytes,
        failures,
        sweep_events,
    ) = await _perform_viewport_sweeps(
        context=cast(capture_module.BrowserContext, context),
        config=CaptureConfig(
            url="https://example.com",
            viewport_width=100,
            viewport_height=800,
            device_scale_factor=1,
        ),
        viewport_overlap_px=200,
        tile_overlap_px=50,
        target_long_side_px=800,
        settle_ms=1,
        max_steps=5,
        mask_selectors=(),
        shrink_retry_limit=1,
        blocklist_config=blocklist_config,
        warning_settings=warning_settings,
    )

    assert stats.shrink_events == 1
    assert stats.retry_attempts == 1
    assert stats.attempt_count == 2
    assert stats.abandoned_tile_count == 1
    assert len(stats.attempt_history) == 2
    assert stats.attempt_history[0]["reason"] == "scroll_height_shrank"
    assert stats.attempt_history[1]["reason"] == "completed"
    assert stats.sweep_count == 2
    assert stats.overlap_pairs == stats.sweep_count - 1
    assert stats.overlap_match_ratio == pytest.approx(1.0)
    assert user_agent == "FakeAgent/1.0"
    assert blocklist_hits == {"#banner": 2}
    codes = {warning.code for warning in warnings}
    assert "canvas-heavy" in codes
    assert "scroll-shrink" in codes
    assert dom_bytes == b"<html></html>"
    assert failures == []
    assert sweep_events, "sweep events should capture per-step telemetry"
    assert len(tiles) == stats.sweep_count
    # Active tiles must not duplicate y_offset=0 from the abandoned attempt
    assert [t.viewport_y_offset for t in tiles] == [0, 500]
    # Ensure scrollTo was invoked with the expected offsets (0 for reset plus >0 for sweeps).
    assert page.scroll_calls[0] == 0
    assert any(offset > 0 for offset in page.scroll_calls[1:])


@pytest.mark.asyncio()
async def test_adversarial_lazy_content_seam_alignment_and_dom_overlay(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real browser capture settles [aria-busy], triggers lazy growth, aligns cross-sweep seams, and harvests dom_overlay."""
    from dataclasses import replace
    from datetime import datetime, timezone
    from http.server import BaseHTTPRequestHandler, HTTPServer
    import threading
    from app.capture import capture_tiles
    from app.settings import get_settings
    from app.store import StorageConfig, Store

    html_doc = b"""<!doctype html>
    <html>
    <head>
      <meta charset="utf-8">
      <title>Adversarial Lazy & Seam Test</title>
      <style>
        body { margin: 0; font-family: sans-serif; background: #ffffff; color: #111111; }
        .block { height: 500px; padding: 20px; box-sizing: border-box; border-bottom: 2px solid #333; }
      </style>
    </head>
    <body>
      <main id="app" aria-busy="true">
        <section class="block" id="sec-1"><h1>Section One Alpha</h1><p>Initial visible paragraph.</p></section>
        <section class="block" id="sec-2"><h2>Section Two Beta</h2><p>Overlap seam region content.</p></section>
        <div id="lazy-slot" data-lazy="true" style="min-height: 50px;"></div>
      </main>
      <script>
        setTimeout(() => {
          const lazy = document.getElementById('lazy-slot');
          if (lazy) {
            lazy.innerHTML = '<section class="block" id="sec-3"><h2>Section Three Lazy</h2><p><a href="https://example.com/lazy">Lazy Link</a></p></section>';
          }
          document.getElementById('app').setAttribute('aria-busy', 'false');
        }, 40);
      </script>
    </body>
    </html>"""

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html_doc)))
            self.end_headers()
            self.wfile.write(html_doc)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base_settings = get_settings()
        browser_cfg = replace(
            base_settings.browser,
            viewport_overlap_px=120,
            tile_overlap_px=120,
            scroll_settle_ms=60,
        )
        monkeypatch.setattr(
            "app.capture.get_settings",
            lambda: replace(base_settings, browser=browser_cfg),
        )

        url = f"http://127.0.0.1:{server.server_port}/"
        config = CaptureConfig(
            url=url,
            viewport_width=800,
            viewport_height=600,
            device_scale_factor=1,
        )
        result = await capture_tiles(config)

        # 3 sections * 500px = 1500px > 600px viewport -> multiple sweeps
        assert len(result.tiles) >= 2
        assert result.manifest.sweep_stats.busy_timeout_count == 0
        assert result.dom_snapshot is not None
        dom_text = result.dom_snapshot.decode("utf-8")
        assert "Section Three Lazy" in dom_text
        # Verify no visible watermark DOM element was injected
        assert "mdwb-watermark-top" not in dom_text
        assert "__mdwb_capture_watermark" not in dom_text

        # Cross-sweep seam hashes AND pixel overlap SHA256 must match across sweeps!
        assert result.tiles[0].seam_bottom_hash is not None
        assert result.tiles[0].seam_bottom_hash == result.tiles[1].seam_top_hash
        assert result.tiles[0].bottom_overlap_sha256 is not None
        assert result.tiles[0].bottom_overlap_sha256 == result.tiles[1].top_overlap_sha256
        assert result.manifest.sweep_stats.overlap_match_ratio == pytest.approx(1.0)

        # Verify viewport-synchronized dom_overlay boxes were harvested and persist via Store
        assert len(result.dom_overlay) >= 3
        overlay_texts = {str(item.get("text")) for item in result.dom_overlay}
        assert "Section One Alpha" in overlay_texts
        assert "Section Three Lazy" in overlay_texts

        store = Store(StorageConfig(cache_root=tmp_path / "cache", db_path=tmp_path / "runs.db"))
        store.allocate_run(
            job_id="job-overlay-1",
            url=url,
            started_at=datetime.now(timezone.utc),
        )
        overlay_path = store.write_dom_overlay(job_id="job-overlay-1", overlay=result.dom_overlay)
        assert overlay_path is not None and overlay_path.exists()
        loaded_overlay = store.read_dom_overlay("job-overlay-1")
        assert len(loaded_overlay) == len(result.dom_overlay)
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.asyncio()
async def test_adversarial_busy_timeout_and_max_steps_truncation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real browser capture bounds stuck [aria-busy='true'] and flags max_steps truncation."""
    from dataclasses import replace
    from http.server import BaseHTTPRequestHandler, HTTPServer
    import threading
    from app.capture import capture_tiles
    from app.settings import get_settings

    html_doc = b"""<!doctype html>
    <html>
    <body style="margin:0">
      <div aria-busy="true" style="height: 4000px; background: #eee;">
        <h1>Stuck Busy Tall Document</h1>
      </div>
    </body>
    </html>"""

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html_doc)))
            self.end_headers()
            self.wfile.write(html_doc)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base_settings = get_settings()
        browser_cfg = replace(
            base_settings.browser,
            scroll_settle_ms=20,
            max_viewport_sweeps=2,
        )
        monkeypatch.setattr(
            "app.capture.get_settings",
            lambda: replace(base_settings, browser=browser_cfg),
        )

        result = await capture_tiles(
            CaptureConfig(
                url=f"http://127.0.0.1:{server.server_port}/",
                viewport_width=800,
                viewport_height=600,
                device_scale_factor=1,
            )
        )
        assert result.manifest.sweep_stats.sweep_count == 2
        assert result.manifest.sweep_stats.truncated_by_max_steps is True
        assert result.manifest.sweep_stats.busy_timeout_count >= 1
        warning_codes = {w.code for w in result.manifest.warnings}
        assert "aria-busy-timeout" in warning_codes
        assert "max-sweeps-truncated" in warning_codes
    finally:
        server.shutdown()
        server.server_close()

