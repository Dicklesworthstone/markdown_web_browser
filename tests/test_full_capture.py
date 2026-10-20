#!/usr/bin/env python3
"""Exercise real browser capture and PNG tiling against local, known content."""

import hashlib
from dataclasses import replace
from importlib import metadata
from pathlib import Path

import pyvips
import pytest

import app.capture as capture_module
from app.capture import CaptureConfig, capture_tiles
from app.settings import get_settings


@pytest.mark.asyncio
@pytest.mark.parametrize("target_channel", ["chromium", "cft"])
async def test_full_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target_channel: str,
    capture_fixture_url,
    operator_network_settings,
) -> None:
    """A local page must produce real, decodable tiles and capture metadata."""
    page = tmp_path / "capture.html"
    page.write_text(
        """<!doctype html><html><head><style>
        body { margin: 0; font: 20px sans-serif; }
        section { height: 600px; }
        .top { background: rgb(20, 80, 220); }
        .bottom { background: rgb(230, 20, 30); animation: pulse 0.1s linear infinite; }
        @keyframes pulse {
            from { background: rgb(230, 20, 30); }
            to { background: rgb(20, 230, 30); }
        }
        </style></head><body>
        <section class="top"><h1>Local capture heading</h1>
        <a href="https://example.com/guide">Guide</a></section>
        <section class="bottom"><h2>Local capture final section</h2></section>
        </body></html>""",
        encoding="utf-8",
    )
    base_settings = operator_network_settings
    capture_settings = replace(
        base_settings,
        browser=replace(
            base_settings.browser,
            playwright_channel=target_channel,
            scroll_settle_ms=0,
            max_viewport_sweeps=8,
            viewport_overlap_px=80,
            tile_overlap_px=64,
        ),
    )
    monkeypatch.setattr(capture_module, "get_settings", lambda: capture_settings)
    config = CaptureConfig(
        url=capture_fixture_url(page.read_bytes()),
        viewport_width=512,
        viewport_height=600,
        device_scale_factor=1,
        color_scheme="light",
        long_side_px=512,
    )
    result = await capture_tiles(config)

    assert result.tiles
    assert result.manifest.tiles_total == len(result.tiles)
    connections = result.manifest.network_policy["connections_opened"]
    assert isinstance(connections, int) and connections >= 1
    assert result.manifest.url == config.url
    assert result.manifest.user_agent
    assert result.manifest.playwright_version == metadata.version("playwright")
    assert result.manifest.cft_label
    assert result.manifest.cft_version
    assert (
        result.manifest.configured_screenshot_style_hash
        == capture_settings.browser.screenshot_style_hash
    )
    assert len(result.manifest.screenshot_style_hash) == 64
    assert result.manifest.browser_transport == "cdp"
    assert result.manifest.requested_browser_transport == capture_settings.browser.browser_transport
    assert result.manifest.capture_ms > 0
    assert result.manifest.playwright_channel == target_channel
    assert result.manifest.cft_pin_verified is (target_channel == "cft")
    observed_build = result.manifest.cft_version.removeprefix("chrome-")
    assert f"Chrome/{observed_build}" in result.manifest.user_agent
    if target_channel == "cft":
        assert result.manifest.cft_version == capture_settings.browser.cft_version
    assert result.manifest.viewport_width == config.viewport_width
    assert result.manifest.viewport_height == config.viewport_height
    assert result.manifest.device_scale_factor == config.device_scale_factor
    assert result.manifest.sweep_stats.sweep_count >= 2
    assert result.manifest.sweep_stats.total_scroll_height >= 1200
    assert result.manifest.validation_failures == []
    assert result.dom_snapshot is not None
    assert b"Local capture heading" in result.dom_snapshot
    assert b"Local capture final section" in result.dom_snapshot
    assert result.tiles[0].source_y_offset == 0
    first_image = pyvips.Image.new_from_buffer(result.tiles[0].png_bytes, "").copy_memory()
    assert first_image.min() == 0, "The known black heading/link glyphs must actually paint"
    for index, tile in enumerate(result.tiles):
        assert tile.index == index
        assert tile.sha256 == hashlib.sha256(tile.png_bytes).hexdigest()
        image = pyvips.Image.new_from_buffer(tile.png_bytes, "")
        assert image.width == tile.width
        assert image.height == tile.height
        assert max(image.width, image.height) <= config.long_side_px

    last_image = pyvips.Image.new_from_buffer(result.tiles[-1].png_bytes, "")
    red, green, blue = last_image.getpoint(last_image.width // 2, last_image.height - 5)[:3]
    assert red > 200 and green < 70 and blue < 70, "The last tile must include the page bottom"

    repeated = await capture_tiles(config)
    assert repeated.manifest.sweep_stats == result.manifest.sweep_stats
    assert [tile.png_bytes for tile in repeated.tiles] == [
        tile.png_bytes for tile in result.tiles
    ], "Repeated captures must freeze the infinite color animation to identical pixels"


async def test_pinned_browser_exercises_real_cdp_and_refuses_mismatch():
    from playwright.async_api import async_playwright

    settings = get_settings()
    build = settings.browser.cft_version.removeprefix("chrome-")
    executable = (
        settings.browser.cft_executable
        or Path.home() / ".cache/mdwb/cft" / build / "chrome-linux64/chrome"
    )
    async with async_playwright() as playwright:
        browser, channel, transport = await capture_module._launch_browser(
            playwright,
            "cft",
            expected_build=settings.browser.cft_version,
            transport="bidi,cdp",
            executable=executable,
        )
        try:
            context = await browser.new_context()
            page = await context.new_page()
            session = await context.new_cdp_session(page)
            observed = await session.send("Browser.getVersion")
            assert channel == "cft" and transport == "cdp"
            assert browser.version == build
            assert observed["product"].endswith(build)
            await context.close()
        finally:
            await browser.close()
        with pytest.raises(ValueError, match="CfT pin mismatch"):
            await capture_module._launch_browser(
                playwright, "cft", expected_build="0.0.0.0", executable=executable
            )
        with pytest.raises(ValueError, match="BiDi is unsupported"):
            await capture_module._launch_browser(
                playwright, "cft", expected_build=settings.browser.cft_version, transport="bidi"
            )
        with pytest.raises(ValueError, match="Unsupported browser channel"):
            await capture_module._launch_browser(
                playwright, "invented,cft", expected_build=settings.browser.cft_version
            )


def test_effective_style_hash_binds_actual_css_and_masks(monkeypatch):
    settings = get_settings()
    config = CaptureConfig(url="https://example.com")
    first = capture_module.effective_screenshot_hash(config, settings.browser)
    masked = replace(settings.browser, screenshot_mask_selectors=("#volatile",))
    assert capture_module.effective_screenshot_hash(config, masked) != first
    monkeypatch.setattr(
        capture_module, "_WATERMARK_STYLE", capture_module._WATERMARK_STYLE + "body { color: red; }"
    )
    assert capture_module.effective_screenshot_hash(config, settings.browser) != first


async def test_pinned_browser_refuses_font_configuration_without_glyphs(tmp_path):
    from playwright.async_api import async_playwright

    fontconfig = tmp_path / "fonts.conf"
    fontconfig.write_text(
        '<?xml version="1.0"?><fontconfig>'
        f"<dir>{tmp_path / 'no-fonts'}</dir><cachedir>{tmp_path / 'cache'}</cachedir>"
        "</fontconfig>"
    )
    settings = get_settings()
    async with async_playwright() as playwright:
        with pytest.raises(ValueError, match="Browser fonts cannot render text"):
            await capture_module._launch_browser(
                playwright,
                "cft",
                expected_build=settings.browser.cft_version,
                executable=settings.browser.cft_executable,
                fontconfig_file=fontconfig,
            )
    assert fontconfig.is_file(), "Negative-control artifacts must be preserved"


def test_effective_style_hash_binds_font_configuration(tmp_path):
    settings = get_settings()
    if capture_module.sys.platform != "linux":
        with pytest.raises(ValueError, match="supported only on Linux"):
            capture_module.fontconfig_digest(tmp_path / "fonts.conf")
        return
    fontconfig = tmp_path / "fonts.conf"
    fontconfig.write_text("<fontconfig><dir>/usr/share/fonts</dir></fontconfig>")
    configured = replace(settings.browser, fontconfig_file=fontconfig)
    config = CaptureConfig(url="https://example.com")
    first = capture_module.effective_screenshot_hash(config, configured)
    fontconfig.write_text("<fontconfig><dir>/usr/local/share/fonts</dir></fontconfig>")
    assert capture_module.effective_screenshot_hash(config, configured) != first
