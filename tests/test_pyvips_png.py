#!/usr/bin/env python3
"""Verify the actual libvips PNG encoding and pixel round-trip contracts."""

import pyvips
import pytest


@pytest.mark.parametrize("bands", [1, 3, 4])
@pytest.mark.parametrize("encoder", ["write_to_buffer", "pngsave_buffer"])
def test_png_formats(bands: int, encoder: str) -> None:
    """Supported encoders preserve dimensions, channels and nonblank pixels."""
    image = pyvips.Image.black(100, 100, bands=bands).new_from_image(list(range(10, 10 + bands)))
    if encoder == "write_to_buffer":
        png_bytes = image.write_to_buffer(".png", compression=9, interlace=False)
    else:
        png_bytes = image.pngsave_buffer(compression=9, interlace=False)

    assert png_bytes.startswith(b"\x89PNG\r\n\x1a\n")
    assert png_bytes[28] == 0, "PNG output must be non-interlaced"
    reloaded = pyvips.Image.new_from_buffer(png_bytes, "")
    assert (reloaded.width, reloaded.height, reloaded.bands) == (100, 100, bands)
    assert reloaded.write_to_memory() == image.write_to_memory()
    re_encoded = reloaded.pngsave_buffer(compression=9, interlace=False)
    round_trip = pyvips.Image.new_from_buffer(re_encoded, "")
    assert round_trip.write_to_memory() == image.write_to_memory()
