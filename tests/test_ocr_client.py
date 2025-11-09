"""Test OCR client with REAL OCR API calls - no mocks."""

from __future__ import annotations

import asyncio
import base64
import os
from pathlib import Path
from typing import Any, Iterator

import httpx
import pytest
from PIL import Image
import io

from app.ocr_client import OCRBatchTelemetry, OCRRequest, reset_quota_tracker, submit_tiles
from app.settings import get_settings, Settings


@pytest.fixture(autouse=True)
def _reset_quota_tracker_fixture() -> Iterator[None]:
    reset_quota_tracker()
    yield
    reset_quota_tracker()


def create_real_test_image(width: int = 1280, height: int = 720, text: str = "Test") -> bytes:
    """Create a real PNG image with text for testing."""
    from PIL import Image, ImageDraw, ImageFont

    # Create white image
    img = Image.new('RGB', (width, height), color='white')
    draw = ImageDraw.Draw(img)

    # Try to use a font, fall back to default if not available
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 48)
    except:
        font = ImageFont.load_default()

    # Add some text
    draw.text((50, 50), text, fill='black', font=font)
    draw.text((50, 150), "This is a real test image", fill='black', font=font)
    draw.text((50, 250), "Generated for testing OCR", fill='black', font=font)

    # Convert to PNG bytes
    img_bytes = io.BytesIO()
    img.save(img_bytes, format='PNG')
    return img_bytes.getvalue()


@pytest.mark.asyncio
@pytest.mark.skipif(not os.getenv("OCR_API_KEY"), reason="Requires real OCR API key")
async def test_real_ocr_api_single_image():
    """Test with real OCR API using a real image."""
    settings = get_settings()

    # Create a real test image
    test_image = create_real_test_image(text="Hello OCR API")

    # Create OCR request
    request = OCRRequest(
        tile_id="test_tile_001",
        tile_bytes=test_image,
        tile_index=0
    )

    # Submit to real OCR API
    result = await submit_tiles(
        requests=[request],
        settings=settings
    )

    # Verify real response
    assert result.telemetry is not None
    assert result.telemetry.total_requests == 1
    assert result.telemetry.total_tiles == 1
    assert result.markdown_sections is not None
    assert len(result.markdown_sections) == 1

    # The real OCR should detect some text
    markdown = result.markdown_sections[0]
    assert len(markdown) > 0
    # Real OCR might detect "Hello" or "OCR" or "API" from our test image
    # but we can't predict exact output


@pytest.mark.asyncio
@pytest.mark.skipif(not os.getenv("OCR_API_KEY"), reason="Requires real OCR API key")
async def test_real_ocr_api_multiple_images():
    """Test with real OCR API using multiple real images."""
    settings = get_settings()

    # Create multiple real test images
    requests = []
    for i in range(3):
        test_image = create_real_test_image(text=f"Page {i+1}")
        requests.append(OCRRequest(
            tile_id=f"tile_{i:03d}",
            tile_bytes=test_image,
            tile_index=i
        ))

    # Submit to real OCR API
    result = await submit_tiles(
        requests=requests,
        settings=settings
    )

    # Verify real response
    assert result.telemetry is not None
    assert result.telemetry.total_tiles == 3
    assert result.markdown_sections is not None
    assert len(result.markdown_sections) == 3

    # Each section should have some content from real OCR
    for section in result.markdown_sections:
        assert len(section) > 0


@pytest.mark.asyncio
@pytest.mark.skipif(not os.getenv("OCR_API_KEY"), reason="Requires real OCR API key")
async def test_real_ocr_api_with_actual_webpage_screenshot():
    """Test with a real screenshot from an actual website."""
    settings = get_settings()

    # Use playwright to capture a real website
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()

        # Navigate to a real website
        await page.goto("https://example.com")

        # Take a real screenshot
        screenshot_bytes = await page.screenshot(full_page=True)

        await browser.close()

    # Submit real screenshot to OCR
    request = OCRRequest(
        tile_id="real_webpage_001",
        tile_bytes=screenshot_bytes,
        tile_index=0
    )

    result = await submit_tiles(
        requests=[request],
        settings=settings
    )

    # Verify real response
    assert result.telemetry is not None
    assert result.markdown_sections is not None
    assert len(result.markdown_sections) == 1

    # Real OCR should detect content from example.com
    markdown = result.markdown_sections[0]
    assert len(markdown) > 0
    # example.com has "Example Domain" text that should be detected


@pytest.mark.asyncio
@pytest.mark.skipif(not os.getenv("OCR_API_KEY"), reason="Requires real OCR API key")
async def test_real_ocr_api_error_handling():
    """Test error handling with real OCR API."""
    settings = get_settings()

    # Create an invalid image (too small, might cause issues)
    tiny_image = create_real_test_image(width=1, height=1)

    request = OCRRequest(
        tile_id="tiny_tile",
        tile_bytes=tiny_image,
        tile_index=0
    )

    # Submit to real OCR API - might fail or return empty
    result = await submit_tiles(
        requests=[request],
        settings=settings
    )

    # Should handle gracefully even with weird input
    assert result.telemetry is not None


@pytest.mark.asyncio
@pytest.mark.skipif(not os.getenv("OCR_API_KEY"), reason="Requires real OCR API key")
async def test_real_ocr_api_concurrent_requests():
    """Test concurrent requests to real OCR API."""
    settings = get_settings()

    # Create multiple images for concurrent processing
    requests = []
    for i in range(5):
        test_image = create_real_test_image(
            width=1280,
            height=720,
            text=f"Concurrent Test {i}"
        )
        requests.append(OCRRequest(
            tile_id=f"concurrent_{i:03d}",
            tile_bytes=test_image,
            tile_index=i
        ))

    # Submit all at once - tests real concurrency handling
    result = await submit_tiles(
        requests=requests,
        settings=settings
    )

    # Verify all were processed
    assert result.telemetry is not None
    assert result.telemetry.total_tiles == 5
    assert len(result.markdown_sections) == 5

    # All should have content
    for section in result.markdown_sections:
        assert len(section) > 0