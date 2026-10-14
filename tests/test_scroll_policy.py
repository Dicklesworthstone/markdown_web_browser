"""Actual installed-Chrome/pyvips sweep tests; no CfT pin, DPR-2, or OCR qualification claim."""

from __future__ import annotations

from hashlib import sha256
from importlib import metadata
from pathlib import Path
from typing import AsyncIterator

import pyvips
import pytest
from playwright.async_api import BrowserContext, async_playwright

from app.blocklist import BlocklistConfig
from app.capture import CaptureConfig, _perform_viewport_sweeps
from app.settings import WarningSettings

COLORS = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0)]


@pytest.fixture
async def browser_context(record_property) -> AsyncIterator[BrowserContext]:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(channel="chrome", headless=True)
        record_property("browser_build", browser.version)
        record_property("browser_channel", "installed chrome; pin not qualified")
        record_property("browser_transport", "native Playwright launch")
        record_property("playwright_version", metadata.version("playwright"))
        context = await browser.new_context(
            viewport={"width": 320, "height": 240},
            device_scale_factor=1,
            color_scheme="light",
            reduced_motion="reduce",
            user_agent="OpenAI File Downloader, XaiImageApiFetch/1.0",
        )
        try:
            yield context
        finally:
            await context.close()
            await browser.close()


@pytest.mark.parametrize(
    "document_height,max_steps,expected_offsets",
    [(120, 10, [0]), (960, 10, [0, 180, 360, 540, 720]), (960, 2, [0, 180])],
)
async def test_real_viewport_sweeps_reach_content_and_obey_budget(
    browser_context: BrowserContext,
    tmp_path: Path,
    document_height: int,
    max_steps: int,
    expected_offsets: list[int],
) -> None:
    sections = []
    for index, start in enumerate(range(0, document_height, 240)):
        red, green, blue = COLORS[index]
        height = min(240, document_height - start)
        sections.append(
            f'<section style="height:{height}px;background:rgb({red},{green},{blue})">'
            f"band-{index}</section>"
        )
    fixture = tmp_path / "bands.html"
    fixture.write_text(
        '<html><body style="margin:0">' + "".join(sections) + "</body></html>", encoding="utf-8"
    )
    result = await _perform_viewport_sweeps(
        browser_context,
        CaptureConfig(
            url=fixture.as_uri(), viewport_width=320, viewport_height=240, device_scale_factor=1
        ),
        viewport_overlap_px=60,
        tile_overlap_px=60,
        target_long_side_px=1288,
        settle_ms=10,
        max_steps=max_steps,
        mask_selectors=[],
        shrink_retry_limit=1,
        blocklist_config=BlocklistConfig("fixture-v1", (), {}),
        warning_settings=WarningSettings(2, 2, 1, 0.5, 0.95, 4),
    )
    tiles, stats, user_agent, hits, warnings, dom, failures, markers = result
    assert [tile.viewport_y_offset for tile in tiles] == expected_offsets
    assert [tile.index for tile in tiles] == list(range(len(expected_offsets)))
    assert stats.sweep_count == len(expected_offsets)
    assert stats.total_scroll_height == max(240, document_height)
    assert stats.shrink_events == 0
    assert stats.retry_attempts == 0
    assert stats.overlap_pairs == len(expected_offsets) - 1
    assert hits == {}
    assert failures == []
    assert dom is not None and b"band-0" in dom
    assert user_agent == "OpenAI File Downloader, XaiImageApiFetch/1.0"
    assert {(marker["tile_index"], marker["position"]) for marker in markers} == {
        *((index, "bottom") for index in range(len(tiles))),
        *((index, "top") for index in range(1, len(tiles))),
    }
    assert len(markers) == 2 * len(tiles) - 1
    assert all(warning.code != "scroll-shrink" for warning in warnings)
    for tile, offset in zip(tiles, expected_offsets, strict=True):
        image = pyvips.Image.new_from_buffer(tile.png_bytes, "")
        assert (image.width, image.height) == (320, 240)
        assert (tile.width, tile.height, tile.scale) == (320, 240, 1.0)
        assert tile.sha256 == sha256(tile.png_bytes).hexdigest()
        assert image.getpoint(220, 90)[:3] == list(COLORS[(offset + 90) // 240])


@pytest.mark.parametrize("masked", [False, True])
async def test_screenshot_mask_changes_pixels_without_hiding_surrounding_content(
    browser_context: BrowserContext, tmp_path: Path, masked: bool
) -> None:
    fixture = tmp_path / "masked.html"
    fixture.write_text(
        '<html><body style="margin:0;height:240px;background:rgb(255,0,0)">'
        '<div id="volatile" style="position:fixed;left:50px;top:50px;'
        'width:100px;height:100px;background:rgb(0,0,255)"></div></body></html>',
        encoding="utf-8",
    )
    tiles, stats, *_ = await _perform_viewport_sweeps(
        browser_context,
        CaptureConfig(
            url=fixture.as_uri(), viewport_width=320, viewport_height=240, device_scale_factor=1
        ),
        viewport_overlap_px=60,
        tile_overlap_px=60,
        target_long_side_px=1288,
        settle_ms=10,
        max_steps=2,
        mask_selectors=["#volatile"] if masked else [],
        shrink_retry_limit=1,
        blocklist_config=BlocklistConfig("fixture-v1", (), {}),
        warning_settings=WarningSettings(2, 2, 1, 0.5, 0.95, 4),
    )
    assert stats.sweep_count == 1
    assert len(tiles) == 1
    image = pyvips.Image.new_from_buffer(tiles[0].png_bytes, "")
    assert image.getpoint(100, 100)[:3] == ([255, 0, 255] if masked else [0, 0, 255])
    assert image.getpoint(220, 100)[:3] == [255, 0, 0]
