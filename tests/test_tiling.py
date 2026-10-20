"""Tests for tiler helpers."""

from __future__ import annotations

import hashlib
import math

import pytest
import pyvips

from app.tiler import _PNG_ENCODE_ARGS, TileSlice, slice_into_tiles, validate_tiles


def _png_bytes(width: int = 10, height: int = 10) -> bytes:
    image = pyvips.Image.black(width, height).add(1)
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


def test_validate_tiles_raises_on_checksum_mismatch() -> None:
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

    try:
        validate_tiles([tile])
    except ValueError as exc:
        assert "checksum" in str(exc)
    else:  # pragma: no cover - safety net
        raise AssertionError("Expected checksum mismatch ValueError")


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


def _assert_downscaled_wide_tile(tile: TileSlice) -> None:
    assert tile.width == 1000
    assert tile.height == 300
    assert tile.scale == pytest.approx(0.5)
    assert tile.scale_x == pytest.approx(0.5)
    assert tile.scale_y == pytest.approx(0.5)
    assert tile.source_rect == pytest.approx((0, 0, 2000, 600))
    assert tile.source_crop_box == (0, 0, 2000, 600)


@pytest.mark.asyncio()
async def test_slice_into_tiles_downscales_wide_capture() -> None:
    png = _png_bytes(width=2000, height=600)

    tiles = await slice_into_tiles(png, target_long_side_px=1000)

    assert len(tiles) == 1
    _assert_downscaled_wide_tile(tiles[0])


@pytest.mark.asyncio()
async def test_slice_into_tiles_downscales_wide_capture_preserving_aspect_ratio() -> None:
    png = _patterned_png_bytes(width=2000, height=600)

    tiles = await slice_into_tiles(png, target_long_side_px=1000)

    assert len(tiles) == 1
    _assert_downscaled_wide_tile(tiles[0])
    validate_tiles(tiles)


@pytest.mark.asyncio()
async def test_slice_into_tiles_records_overlap_hashes() -> None:
    png = _png_bytes(width=800, height=2800)

    tiles = await slice_into_tiles(png, overlap_px=120, target_long_side_px=1000)

    assert len(tiles) >= 3
    assert tiles[0].top_overlap_sha256 is None
    assert tiles[-1].bottom_overlap_sha256 is None
    for previous, current in zip(tiles, tiles[1:]):
        assert current.top_overlap_px == previous.bottom_overlap_px == 120
        assert previous.bottom_overlap_sha256 is not None
        assert previous.bottom_overlap_sha256 == current.top_overlap_sha256


@pytest.mark.asyncio()
async def test_slice_into_tiles_records_overlap_hashes_on_patterned_image() -> None:
    """Patterned rows prove matching overlap hashes are the same pixels, not a flat fill."""
    png = _patterned_png_bytes(width=800, height=2800)

    tiles = await slice_into_tiles(png, overlap_px=120, target_long_side_px=1000)

    assert len(tiles) == 4
    validate_tiles(tiles)
    assert tiles[0].top_overlap_sha256 is None
    assert tiles[-1].bottom_overlap_sha256 is None
    for tile in tiles:
        assert max(tile.width, tile.height) <= 1000
    for previous, current in zip(tiles, tiles[1:]):
        assert current.top_overlap_px == previous.bottom_overlap_px == 120
        assert previous.bottom_overlap_sha256 is not None
        assert previous.bottom_overlap_sha256 == current.top_overlap_sha256


@pytest.mark.asyncio()
@pytest.mark.parametrize("dpr", [1.0, 2.0, 3.0])
async def test_patterned_geometry_inverse_crop_mapping_and_highlights_across_dpr(dpr: float) -> None:
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

    # 2000x2800 scaled by 0.5 -> 1000x1400. Nominal 880px step, short final tile.
    # Tile 0: resized [0, 1000] -> source [0, 2000]
    # Tile 1: resized [880, 1400] -> source [1760, 2800], overlap 120 tile px.
    assert len(tiles) == 2
    validate_tiles(tiles)

    assert tiles[0].source_crop_box == (0, 0, 2000, 2000)
    assert tiles[1].source_crop_box == (0, 1760, 2000, 1040)
    assert tiles[0].source_rect == pytest.approx((0.0, 0.0, 2000.0, 2000.0))
    assert tiles[1].source_rect == pytest.approx((0.0, 1760.0, 2000.0, 1040.0))
    assert tiles[0].bottom_overlap_px == 120
    assert tiles[1].top_overlap_px == 120
    assert tiles[0].bottom_overlap_sha256 == tiles[1].top_overlap_sha256

    assert tiles[0].source_crop_y == 0
    assert tiles[-1].source_crop_y + tiles[-1].source_crop_height == src_h
    for tile in tiles:
        assert max(tile.width, tile.height) <= 1000
        assert 0 <= tile.source_crop_y < src_h
        assert 0 < tile.source_crop_y + tile.source_crop_height <= src_h
        assert tile.device_scale_factor == pytest.approx(dpr)
        assert tile.source_rect is not None
        assert tile.source_y_offset == pytest.approx(css_viewport_y + tile.source_rect[1] / dpr)
        assert tile.css_y_offset == int(round(tile.source_y_offset))

    src_point = tiles[1].tile_to_source_px(250, 260)
    assert src_point == (500, 2280)
    assert tiles[1].source_px_to_tile(*src_point) == (250, 260)

    highlight = tiles[1].highlight_band_for_source_px(1960, 2160)
    assert highlight == (100, 200)


@pytest.mark.asyncio()
async def test_slice_into_tiles_refuses_invalid_inputs() -> None:
    png = _png_bytes(20, 20)
    with pytest.raises(ValueError, match="non-empty"):
        await slice_into_tiles(b"")
    with pytest.raises(ValueError, match="target_long_side_px"):
        await slice_into_tiles(png, target_long_side_px=0)
    with pytest.raises(ValueError, match="overlap_px"):
        await slice_into_tiles(png, overlap_px=-5)


def _labelled_grid(width: int, height: int, dpr: int) -> bytes:
    cells = []
    for row in range(math.ceil(height / 100)):
        for column in range(math.ceil(width / 100)):
            color = f"rgb({row * 37 % 256},{column * 53 % 256},127)"
            cells.append(
                f'<rect x="{column * 100}" y="{row * 100}" width="100" height="100" fill="{color}"/>'
                f'<text x="{column * 100 + 5}" y="{row * 100 + 20}" font-size="12">R{row} C{column}</text>'
            )
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width * dpr}" height="{height * dpr}" viewBox="0 0 {width} {height}">'
        + "".join(cells)
        + "</svg>"
    )
    return pyvips.Image.new_from_buffer(svg.encode(), "").pngsave_buffer()


@pytest.mark.parametrize("dpr", [1, 2, 3])
@pytest.mark.parametrize("width,height", [(2000, 1400), (400, 3000), (400, 1000), (400, 1125)])
async def test_real_labelled_pixels_cover_source_and_map_back_to_css(width, height, dpr):
    """Independent row/column colors catch stretched axes and wrong DPR offsets."""
    source = _labelled_grid(width, height, dpr)
    tiles = await slice_into_tiles(
        source,
        target_long_side_px=1000,
        overlap_px=120,
        viewport_y_offset=300,
        device_scale_factor=dpr,
    )
    assert tiles and tiles[0].source_rect is not None and tiles[0].source_rect[1] == 0
    covered_end = 0.0
    for tile in tiles:
        assert tile.device_scale_factor == dpr
        assert tile.source_rect is not None and tile.scale_x and tile.scale_y
        x, y, w, h = tile.source_rect
        assert x == 0 and w == pytest.approx(width * dpr)
        assert 0 <= y <= covered_end + 1e-6
        covered_end = max(covered_end, y + h)
        assert covered_end <= height * dpr + 1e-6
        assert tile.source_y_offset == pytest.approx(300 + y / dpr)
        assert tile.width == pytest.approx(w * tile.scale_x)
        assert tile.height == pytest.approx(h * tile.scale_y)
        assert max(tile.width, tile.height) <= 1000
        assert abs(tile.scale_x - tile.scale_y) <= 1 / min(width * dpr, height * dpr)
        image = pyvips.Image.new_from_buffer(tile.png_bytes, "").copy_memory()
        for row in range(math.ceil(y / dpr / 100), math.floor((y + h) / dpr / 100)):
            css_y = row * 100 + 60
            if not y / dpr <= css_y < (y + h) / dpr:
                continue
            for column in range(width // 100):
                px = int((column * 100 + 60) * dpr * tile.scale_x)
                py = int((css_y * dpr - y) * tile.scale_y)
                assert image.getpoint(px, py)[:3] == pytest.approx(
                    [row * 37 % 256, column * 53 % 256, 127], abs=1
                )
        assert hashlib.sha256(tile.png_bytes).hexdigest() == tile.sha256
    assert covered_end == pytest.approx(height * dpr)
    for previous, current in zip(tiles, tiles[1:]):
        assert current.source_rect is not None and previous.source_rect is not None
        assert current.scale_y is not None
        common = previous.source_rect[1] + previous.source_rect[3] - current.source_rect[1]
        assert current.top_overlap_px == pytest.approx(common * current.scale_y, abs=1)
        assert current.top_overlap_px == previous.bottom_overlap_px
        overlap_rows = round(common * current.scale_y)
        assert overlap_rows > 0
        previous_image = pyvips.Image.new_from_buffer(previous.png_bytes, "")
        current_image = pyvips.Image.new_from_buffer(current.png_bytes, "")
        expected_previous = hashlib.sha256(
            previous_image.crop(
                0, previous.height - overlap_rows, previous.width, overlap_rows
            ).pngsave_buffer(**_PNG_ENCODE_ARGS)
        ).hexdigest()
        expected_current = hashlib.sha256(
            current_image.crop(0, 0, current.width, overlap_rows).pngsave_buffer(**_PNG_ENCODE_ARGS)
        ).hexdigest()
        assert previous.bottom_overlap_sha256 == expected_previous
        assert current.top_overlap_sha256 == expected_current
        assert previous.bottom_overlap_sha256 == current.top_overlap_sha256


@pytest.mark.parametrize("size,overlap,dpr", [(0, 0, 1), (100, 100, 1), (100, -1, 1), (100, 0, 0)])
async def test_invalid_geometry_refused_before_decode(size, overlap, dpr):
    with pytest.raises(ValueError):
        await slice_into_tiles(
            b"not a PNG", target_long_side_px=size, overlap_px=overlap, device_scale_factor=dpr
        )


async def test_short_final_viewport_uses_actual_common_pixels():
    """The final scroll step can overlap much more than the nominal 120 pixels."""
    from app.tiler import set_tile_overlap

    image = (pyvips.Image.xyz(200, 1000)[1] % 251).cast("uchar")
    first = (
        await slice_into_tiles(image.crop(0, 0, 200, 600).pngsave_buffer(), viewport_y_offset=0)
    )[0]
    last = (
        await slice_into_tiles(image.crop(0, 400, 200, 600).pngsave_buffer(), viewport_y_offset=400)
    )[0]
    set_tile_overlap(first, last)
    assert first.bottom_overlap_px == last.top_overlap_px == last.overlap_px == 200
    assert first.bottom_overlap_sha256 == last.top_overlap_sha256
    assert first.bottom_overlap_sha256 is not None
