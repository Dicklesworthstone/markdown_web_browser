"""Tests for tiler helpers."""

from __future__ import annotations

import hashlib
import math

import pytest
import pyvips

from app.tiler import TileSlice, slice_into_tiles, validate_tiles


def _png_bytes(width: int = 10, height: int = 10) -> bytes:
    image = pyvips.Image.black(width, height).add(1)
    return image.write_to_buffer(".png")


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


@pytest.mark.asyncio()
async def test_slice_into_tiles_downscales_wide_capture() -> None:
    png = _png_bytes(width=2000, height=600)

    tiles = await slice_into_tiles(png, target_long_side_px=1000)

    assert len(tiles) == 1  # height fits in a single tile
    tile = tiles[0]
    assert tile.width == 1000  # downscaled from 2000
    assert tile.scale == pytest.approx(0.5)
    assert tile.height == 300  # both axes must use the same scale
    assert tile.source_rect == (0, 0, 2000, 600)


@pytest.mark.asyncio()
async def test_slice_into_tiles_records_overlap_hashes() -> None:
    png = _png_bytes(width=800, height=2800)

    tiles = await slice_into_tiles(png, overlap_px=120, target_long_side_px=1000)

    assert len(tiles) >= 3  # tall image yields multiple tiles
    assert tiles[0].top_overlap_sha256 is None
    assert tiles[-1].bottom_overlap_sha256 is None
    for previous, current in zip(tiles, tiles[1:]):
        assert current.top_overlap_px == previous.bottom_overlap_px == 120
        assert previous.bottom_overlap_sha256 is not None
        assert previous.bottom_overlap_sha256 == current.top_overlap_sha256


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
        # Sample known cell centers, away from text and interpolation boundaries.
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
            ).pngsave_buffer(compression=9, interlace=False)
        ).hexdigest()
        expected_current = hashlib.sha256(
            current_image.crop(0, 0, current.width, overlap_rows).pngsave_buffer(
                compression=9, interlace=False
            )
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
