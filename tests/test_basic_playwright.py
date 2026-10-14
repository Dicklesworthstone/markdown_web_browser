#!/usr/bin/env python3
"""Native browser screenshot and sweep smoke against local known pixels."""

from importlib import metadata
from pathlib import Path

import pyvips
import pytest
from playwright.async_api import ViewportSize, async_playwright

COLORS = ((230, 20, 30), (20, 210, 40), (20, 80, 220))
VIEWPORT: ViewportSize = {"width": 320, "height": 240}


def _assert_screenshot(path: Path, expected_color: tuple[int, int, int]) -> None:
    assert path.is_file(), "Screenshot file missing"
    image = pyvips.Image.new_from_file(str(path))
    assert (image.width, image.height) == (VIEWPORT["width"], VIEWPORT["height"])
    assert tuple(image.getpoint(10, 20)[:3]) == expected_color, "Known screenshot pixels absent"


def _assert_sweep_coverage(captures: list[tuple[int, Path]], scroll_height: int) -> None:
    assert captures, "Viewport sweep missing"
    assert captures[0][0] == 0, "Viewport sweep must include the page top"
    previous_end = 0
    for offset, path in captures:
        assert offset <= previous_end, "Viewport sweep left an uncaptured gap"
        _assert_screenshot(path, COLORS[(offset + 20) // VIEWPORT["height"]])
        previous_end = offset + VIEWPORT["height"]
    assert previous_end == scroll_height, "Viewport sweep must include the page bottom"


@pytest.mark.asyncio
async def test_basic_screenshot(tmp_path: Path) -> None:
    """A real local navigation produces the expected image and viewport."""
    fixture = tmp_path / "screenshot.html"
    fixture.write_text(
        """<!doctype html><html><head><title>Native screenshot fixture</title>
        <style>body { margin: 0; background: rgb(20, 80, 220); }</style>
        </head><body><h1>Known local screenshot content</h1></body></html>""",
        encoding="utf-8",
    )
    output_path = tmp_path / "test_screenshot.png"
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            assert browser.version and all(part.isdigit() for part in browser.version.split("."))
            print(f"Native Chromium={browser.version}; Playwright={metadata.version('playwright')}")
            context = await browser.new_context(viewport=VIEWPORT, device_scale_factor=1)
            page = await context.new_page()
            await page.goto(fixture.as_uri(), wait_until="domcontentloaded")
            assert await page.title() == "Native screenshot fixture"
            assert await page.locator("h1").inner_text() == "Known local screenshot content"
            assert await page.evaluate("[innerWidth, innerHeight, devicePixelRatio]") == [
                320,
                240,
                1,
            ]
            png = await page.screenshot(
                path=output_path, full_page=False, animations="disabled", caret="hide"
            )
        finally:
            await browser.close()

    assert output_path.read_bytes() == png
    _assert_screenshot(output_path, COLORS[2])
    with pytest.raises(AssertionError, match="Screenshot file missing"):
        _assert_screenshot(tmp_path / "missing.png", COLORS[2])
    blank_path = tmp_path / "blank-control.png"
    blank = pyvips.Image.black(320, 240, bands=3).new_from_image([255, 255, 255])
    blank_path.write_bytes(blank.pngsave_buffer(compression=9, interlace=False))
    with pytest.raises(AssertionError, match="Known screenshot pixels absent"):
        _assert_screenshot(blank_path, COLORS[2])


@pytest.mark.asyncio
async def test_viewport_sweep(tmp_path: Path) -> None:
    """Real scrolling covers every known section through the page bottom."""
    fixture = tmp_path / "sweep.html"
    fixture.write_text(
        """<!doctype html><html><head><style>
        body { margin: 0; } section { height: 240px; }
        #top { background: rgb(230, 20, 30); }
        #middle { background: rgb(20, 210, 40); }
        #bottom { background: rgb(20, 80, 220); }
        </style></head><body><section id="top">Top section</section>
        <section id="middle">Middle section</section>
        <section id="bottom">Bottom section</section></body></html>""",
        encoding="utf-8",
    )
    captures: list[tuple[int, Path]] = []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport=VIEWPORT, device_scale_factor=1)
            page = await context.new_page()
            await page.goto(fixture.as_uri(), wait_until="domcontentloaded")
            scroll_height = await page.evaluate("document.scrollingElement.scrollHeight")
            assert scroll_height == 720
            offset = 0
            while True:
                await page.evaluate("offset => window.scrollTo(0, offset)", offset)
                assert await page.evaluate("scrollY") == offset
                path = tmp_path / f"sweep_{len(captures):02d}.png"
                await page.screenshot(
                    path=path, full_page=False, animations="disabled", caret="hide"
                )
                captures.append((offset, path))
                if offset + VIEWPORT["height"] >= scroll_height:
                    break
                offset = min(offset + 180, scroll_height - VIEWPORT["height"])
        finally:
            await browser.close()

    assert [offset for offset, _ in captures] == [0, 180, 360, 480]
    _assert_sweep_coverage(captures, scroll_height)
    with pytest.raises(AssertionError, match="page bottom"):
        _assert_sweep_coverage(captures[:-1], scroll_height)
    with pytest.raises(AssertionError, match="page top"):
        _assert_sweep_coverage(captures[1:], scroll_height)
