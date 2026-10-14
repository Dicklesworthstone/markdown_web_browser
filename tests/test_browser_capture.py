#!/usr/bin/env python3
"""Local native browser/capture smoke; excludes hosted OCR and CfT qualification."""

import hashlib
from dataclasses import replace
from importlib import metadata
from pathlib import Path

import pyvips
import pytest
from playwright.async_api import async_playwright

import app.capture as capture_module
from app.capture import CaptureConfig, capture_tiles
from app.tiler import TileSlice


COLORS = ((230, 20, 30), (20, 210, 40), (20, 80, 220))


def _assert_tile_coverage(tiles: list[TileSlice], scroll_height: int) -> None:
    assert tiles, "Capture tiles missing"
    assert tiles[0].source_y_offset == 0, "Capture must include the page top"
    previous_end = 0.0
    for index, tile in enumerate(tiles):
        assert tile.index == index
        assert tile.source_y_offset <= previous_end, "Capture left an uncaptured gap"
        assert tile.sha256 == hashlib.sha256(tile.png_bytes).hexdigest(), "Tile checksum mismatch"
        image = pyvips.Image.new_from_buffer(tile.png_bytes, "")
        assert (image.width, image.height) == (320, 240)
        assert (tile.width, tile.height) == (image.width, image.height)
        assert tile.scale == 1.0
        expected = COLORS[(int(tile.source_y_offset) + 20) // 240]
        assert tuple(image.getpoint(10, 20)[:3]) == expected, "Known tile pixels absent"
        previous_end = tile.source_y_offset + tile.height
    assert previous_end == scroll_height, "Capture must include the page bottom"


@pytest.mark.asyncio
async def test_basic_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capture_fixture_url, operator_network_settings
) -> None:
    """The native capture pipeline returns complete known content and metadata."""
    fixture = tmp_path / "capture.html"
    fixture.write_text(
        """<!doctype html><html><head><title>Capture smoke fixture</title><style>
        body { margin: 0; } section { height: 240px; }
        #top { background: rgb(230, 20, 30); }
        #middle { background: rgb(20, 210, 40); }
        #bottom { background: rgb(20, 80, 220); }
        </style></head><body><section id="top">Top capture section</section>
        <section id="middle">Middle capture section</section>
        <section id="bottom">Bottom capture section</section></body></html>""",
        encoding="utf-8",
    )
    settings = operator_network_settings
    capture_settings = replace(
        settings,
        browser=replace(
            settings.browser,
            playwright_channel="chromium",
            viewport_overlap_px=60,
            tile_overlap_px=40,
            scroll_settle_ms=0,
            max_viewport_sweeps=12,
        ),
    )
    monkeypatch.setattr(capture_module, "get_settings", lambda: capture_settings)
    stages: list[str] = []

    def record_stage(stage: str, done: int | None, total: int | None) -> None:
        stages.append(stage)

    config = CaptureConfig(
        url=capture_fixture_url(fixture.read_bytes()),
        viewport_width=320,
        viewport_height=240,
        device_scale_factor=1,
        long_side_px=320,
        stage_callback=record_stage,
    )
    result = await capture_tiles(config)

    _assert_tile_coverage(result.tiles, scroll_height=720)
    assert len(result.tiles) == 4
    assert result.manifest.tiles_total == len(result.tiles)
    assert result.manifest.viewport_width == 320
    assert result.manifest.viewport_height == 240
    assert result.manifest.device_scale_factor == 1
    assert result.manifest.url == config.url
    assert result.manifest.network_policy["mode"] == "public-plus-server-grants"
    connections = result.manifest.network_policy["connections_opened"]
    assert isinstance(connections, int) and connections >= 1
    assert result.manifest.user_agent
    assert result.manifest.capture_ms > 0
    assert result.manifest.playwright_version == metadata.version("playwright")
    assert result.manifest.playwright_channel == "chromium"
    assert result.manifest.cft_label and result.manifest.cft_version
    assert (
        result.manifest.configured_screenshot_style_hash
        == capture_settings.browser.screenshot_style_hash
    )
    assert len(result.manifest.screenshot_style_hash) == 64
    assert result.manifest.browser_transport == capture_settings.browser.browser_transport
    assert result.manifest.sweep_stats.sweep_count == 4
    assert result.manifest.sweep_stats.total_scroll_height == 720
    assert result.manifest.validation_failures == []
    assert stages[0] == "BROWSER_STARTING"
    assert stages.count("CAPTURING") == 4
    assert stages.count("TILING") == 4
    assert stages.count("SCROLLING") == 3
    assert result.dom_snapshot is not None
    for content in (b"Top capture section", b"Middle capture section", b"Bottom capture section"):
        assert content in result.dom_snapshot

    with pytest.raises(AssertionError, match="page bottom"):
        _assert_tile_coverage(result.tiles[:-1], scroll_height=720)
    corrupt_tile = replace(result.tiles[0], png_bytes=b"invalid PNG bytes")
    with pytest.raises(AssertionError, match="Tile checksum mismatch"):
        _assert_tile_coverage([corrupt_tile, *result.tiles[1:]], scroll_height=720)


@pytest.mark.asyncio
async def test_playwright_installed() -> None:
    """Launch the actual native channel and observe its reported browser build."""
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(channel="chromium", headless=True)
        try:
            version_parts = browser.version.split(".")
            assert len(version_parts) == 4 and all(part.isdigit() for part in version_parts)
            assert int(version_parts[0]) > 0
            playwright_version = metadata.version("playwright")
            assert tuple(int(part) for part in playwright_version.split(".")[:2]) >= (1, 50)
            print(f"Native Chromium={browser.version}; Playwright={playwright_version}")
            context = await browser.new_context(viewport={"width": 320, "height": 240})
            page = await context.new_page()
            await page.set_content("<h1>Native browser is running</h1>")
            assert await page.locator("h1").inner_text() == "Native browser is running"
        finally:
            await browser.close()
