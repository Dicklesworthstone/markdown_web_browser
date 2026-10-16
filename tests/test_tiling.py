"""Tests for tiler helpers."""

from __future__ import annotations

import hashlib

import pytest

try:  # pragma: no cover - exercised only when libvips missing
    import pyvips as _pyvips
except Exception as exc:  # noqa: BLE001
    pytest.skip(f"pyvips unavailable: {exc}", allow_module_level=True)
else:
    if not hasattr(_pyvips, "Image"):
        pytest.skip("pyvips.Image not available", allow_module_level=True)
    pyvips = _pyvips  # type: Any
    if not hasattr(getattr(pyvips, "Image", None), "black"):
        pytest.skip("pyvips stub lacks Image.black; skipping tiler tests", allow_module_level=True)

from app.tiler import _PNG_ENCODE_ARGS, TileSlice, slice_into_tiles, validate_tiles


def _png_bytes(width: int = 10, height: int = 10) -> bytes:
    image = pyvips.Image.black(width, height).add(1)  # type: ignore[attr-defined]
    return image.write_to_buffer(".png")


def _patterned_png_bytes(width: int, height: int) -> bytes:
    """Create a deterministic non-uniform patterned RGB PNG where every row/column has distinct pixel values."""
    import numpy as np

    y_idx = np.arange(height, dtype=np.uint16)[:, None]
    x_idx = np.arange(width, dtype=np.uint16)[None, :]
    r = ((y_idx * 7 + x_idx) % 251).astype(np.uint8)
    g = ((y_idx * 13 + x_idx * 3) % 253).astype(np.uint8)
    b = (((y_idx // 4) * 19 + (x_idx // 4) * 5) % 255).astype(np.uint8)
    rgb = np.stack([r, g, b], axis=2)
    image = pyvips.Image.new_from_memory(rgb.tobytes(), width, height, 3, "uchar")
    return image.pngsave_buffer(**_PNG_ENCODE_ARGS)


def test_png_encode_args_enforce_non_paletted_non_interlaced_q9() -> None:
    assert _PNG_ENCODE_ARGS["compression"] == 9
    assert _PNG_ENCODE_ARGS["interlace"] is False
    assert _PNG_ENCODE_ARGS["palette"] is False

    png = _patterned_png_bytes(120, 80)
    # PNG signature (8 bytes) + IHDR length (4 bytes) + 'IHDR' (4 bytes) + width (4) + height (4) + bit_depth (1) + color_type (1) + compression (1) + filter (1) + interlace (1)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert png[12:16] == b"IHDR"
    color_type = png[25]
    interlace_method = png[28]
    assert color_type != 3, "Expected non-paletted PNG (color_type != 3)"
    assert interlace_method == 0, "Expected non-interlaced PNG (interlace_method == 0)"


def test_validate_tiles_passes_for_valid_png() -> None:
    png = _png_bytes()
    tile = TileSlice(
        index=0,
        png_bytes=png,
        sha256=hashlib.sha256(png).hexdigest(),
        width=10,
        height=10,
        scale=1.0,
        source_y_offset=0,
        viewport_y_offset=0,
        overlap_px=0,
        top_overlap_sha256=None,
        bottom_overlap_sha256=None,
    )

    validate_tiles([tile])  # should not raise


def test_validate_tiles_raises_on_checksum_mismatch_and_out_of_bounds_crop() -> None:
    png = _png_bytes()
    tile = TileSlice(
        index=5,
        png_bytes=png,
        sha256="deadbeef",
        width=10,
        height=10,
        scale=1.0,
        source_y_offset=0,
        viewport_y_offset=0,
        overlap_px=0,
        top_overlap_sha256=None,
        bottom_overlap_sha256=None,
    )

    with pytest.raises(ValueError, match="checksum"):
        validate_tiles([tile])

    oob_tile = TileSlice(
        index=6,
        png_bytes=png,
        sha256=hashlib.sha256(png).hexdigest(),
        width=10,
        height=10,
        scale=1.0,
        source_y_offset=0,
        viewport_y_offset=0,
        overlap_px=0,
        top_overlap_sha256=None,
        bottom_overlap_sha256=None,
        source_crop_y=5,
        source_crop_height=10,
        source_height=10,
    )
    with pytest.raises(ValueError, match="out of bounds"):
        validate_tiles([oob_tile])


@pytest.mark.asyncio()
async def test_slice_into_tiles_downscales_wide_capture_preserving_aspect_ratio() -> None:
    png = _patterned_png_bytes(width=2000, height=600)

    tiles = await slice_into_tiles(png, target_long_side_px=1000)

    assert len(tiles) == 1
    tile = tiles[0]
    assert tile.width == 1000  # downscaled from 2000
    assert tile.height == 300  # aspect ratio preserved (600 * 0.5 == 300)
    assert tile.scale == pytest.approx(0.5)
    assert tile.scale_x == pytest.approx(0.5)
    assert tile.scale_y == pytest.approx(0.5)
    assert tile.source_crop_box == (0, 0, 2000, 600)
    validate_tiles(tiles)


@pytest.mark.asyncio()
async def test_slice_into_tiles_records_overlap_hashes_on_patterned_image() -> None:
    # Patterned image where every row is distinct; proves final-tile pulled-back overlap
    # (960px on tile 2->3 for height=2800, target=1000, overlap=120) hashes identical pixel strips.
    png = _patterned_png_bytes(width=800, height=2800)

    tiles = await slice_into_tiles(png, overlap_px=120, target_long_side_px=1000)

    assert len(tiles) == 4
    validate_tiles(tiles)
    for tile in tiles:
        assert max(tile.width, tile.height) <= 1000
        assert tile.top_overlap_sha256 is not None
        assert tile.bottom_overlap_sha256 is not None
    # Pairs (0,1) and (1,2) have nominal 120px overlap; pair (2,3) has pulled-back 960px overlap
    assert tiles[1].top_overlap_px == 120
    assert tiles[2].top_overlap_px == 120
    assert tiles[2].bottom_overlap_px == 960
    assert tiles[3].top_overlap_px == 960
    assert tiles[3].overlap_px == 960
    for previous, current in zip(tiles, tiles[1:]):
        assert previous.bottom_overlap_px == current.top_overlap_px
        assert previous.bottom_overlap_sha256 == current.top_overlap_sha256


@pytest.mark.asyncio()
@pytest.mark.parametrize("dpr", [1.0, 2.0, 3.0])
async def test_patterned_geometry_inverse_crop_mapping_and_highlights_across_dpr(dpr: float) -> None:
    # Wide + tall patterned capture: 2000x2800 screenshot at DPR 1/2/3, swept at CSS y_offset=600
    src_w, src_h = 2000, 2800
    png = _patterned_png_bytes(width=src_w, height=src_h)
    css_viewport_y = 600

    tiles = await slice_into_tiles(
        png,
        overlap_px=120,
        viewport_y_offset=css_viewport_y,
        target_long_side_px=1000,
        device_scale_factor=dpr,
    )

    # 2000x2800 uniformly scaled by 0.5 -> 1000x1400 -> 2 tiles:
    # Tile 0: resized [0, 1000] -> source [0, 2000]
    # Tile 1: resized [400, 1400] -> source [800, 2800] (actual overlap = 600 tile px = 1200 source px)
    assert len(tiles) == 2
    validate_tiles(tiles)

    assert tiles[0].source_crop_box == (0, 0, 2000, 2000)
    assert tiles[1].source_crop_box == (0, 800, 2000, 2000)
    assert tiles[0].bottom_overlap_px == 600
    assert tiles[1].top_overlap_px == 600
    assert tiles[0].bottom_overlap_sha256 == tiles[1].top_overlap_sha256

    # Full source coverage and no out-of-bounds rectangles
    assert tiles[0].source_crop_y == 0
    assert tiles[-1].source_crop_y + tiles[-1].source_crop_height == src_h
    for tile in tiles:
        assert max(tile.width, tile.height) <= 1000
        assert 0 <= tile.source_crop_y < src_h
        assert 0 < tile.source_crop_y + tile.source_crop_height <= src_h
        assert tile.device_scale_factor == pytest.approx(dpr)
        expected_screenshot_y = int(round(css_viewport_y * dpr)) + tile.source_crop_y
        expected_css_y = int(round(css_viewport_y + tile.source_crop_y / dpr))
        assert tile.source_y_offset == expected_screenshot_y
        assert tile.css_y_offset == expected_css_y

    # Inverse mapping and highlight band selection select the intended pixels
    src_point = tiles[1].tile_to_source_px(250, 500)
    assert src_point == (500, 1800)
    roundtrip_tile_pt = tiles[1].source_px_to_tile(*src_point)
    assert roundtrip_tile_pt == (250, 500)

    # Highlight band for source rows [1200, 1600] inside tile 1 (which starts at src_y=800, scale=0.5)
    # should select tile rows [200, 400]
    hl_y0, hl_y1 = tiles[1].highlight_band_for_source_px(1200, 1600)
    assert (hl_y0, hl_y1) == (200, 400)


@pytest.mark.asyncio()
async def test_slice_into_tiles_refuses_invalid_inputs() -> None:
    png = _png_bytes(20, 20)
    with pytest.raises(ValueError, match="non-empty"):
        await slice_into_tiles(b"")
    with pytest.raises(ValueError, match="target_long_side_px"):
        await slice_into_tiles(png, target_long_side_px=0)
    with pytest.raises(ValueError, match="overlap_px"):
        await slice_into_tiles(png, overlap_px=-5)

