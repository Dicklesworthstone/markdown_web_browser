"""Tile slicing helpers backed by pyvips."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from typing import Any, Iterable, List, Optional, TYPE_CHECKING

try:  # pragma: no cover - exercised indirectly via helper
    import pyvips as _pyvips  # type: ignore[import-not-found]
except Exception as exc:  # pragma: no cover - missing native dependency
    _PYVIPS = None
    _PYVIPS_IMPORT_ERROR = exc
else:  # pragma: no cover - import succeeds during normal runtime
    _PYVIPS = _pyvips
    _PYVIPS_IMPORT_ERROR = None

if TYPE_CHECKING:  # pragma: no cover - typing aide only
    import pyvips  # noqa: F401

# PNG encode defaults that match PLAN §19.3 guidance.
_PNG_ENCODE_ARGS = {
    "compression": 9,
    "interlace": False,
}


@dataclass(slots=True)
class TileSlice:
    """A single OCR-ready tile plus provenance metadata."""

    index: int
    png_bytes: bytes
    sha256: str
    width: int
    height: int
    scale: float
    source_y_offset: int
    viewport_y_offset: int
    overlap_px: int
    top_overlap_sha256: Optional[str]
    bottom_overlap_sha256: Optional[str]


async def slice_into_tiles(
    image_bytes: bytes,
    *,
    overlap_px: int = 120,
    tile_index_offset: int = 0,
    viewport_y_offset: int = 0,
    target_long_side_px: int = 1288,
) -> List[TileSlice]:
    """Slice a viewport-sized screenshot into <=1288 px-long tiles.

    The longest dimension for each resulting tile is capped at ``target_long_side_px``
    while keeping ≈``overlap_px`` pixels of vertical overlap so OCR stitching can
    deduplicate seams deterministically.
    """

    return await asyncio.to_thread(
        _slice_sync,
        image_bytes,
        overlap_px,
        tile_index_offset,
        viewport_y_offset,
        target_long_side_px,
    )


def _slice_sync(
    image_bytes: bytes,
    overlap_px: int,
    tile_index_offset: int,
    viewport_y_offset: int,
    target_long_side_px: int,
) -> List[TileSlice]:
    vips = _require_pyvips()
    image = vips.Image.new_from_buffer(image_bytes, "", access="sequential")

    # Downscale only when the width exceeds the 1288 px guidance; height is handled via tiling.
    scale = 1.0
    if image.width > target_long_side_px:
        scale = target_long_side_px / image.width
        image = image.resize(scale)

    tiles: List[TileSlice] = []
    height = image.height
    width = image.width

    overlap_px = min(overlap_px, target_long_side_px)

    if height <= target_long_side_px:
        png_bytes = image.write_to_buffer(".png", **_PNG_ENCODE_ARGS)
        top_sha = _overlap_sha(image, position="top", overlap_px=overlap_px)
        bottom_sha = _overlap_sha(image, position="bottom", overlap_px=overlap_px)
        tiles.append(
            TileSlice(
                index=tile_index_offset,
                png_bytes=png_bytes,
                sha256=hashlib.sha256(png_bytes).hexdigest(),
                width=width,
                height=height,
                scale=scale,
                source_y_offset=int(viewport_y_offset),
                viewport_y_offset=viewport_y_offset,
                overlap_px=overlap_px,
                top_overlap_sha256=top_sha,
                bottom_overlap_sha256=bottom_sha,
            )
        )
        return tiles

    step = max(1, target_long_side_px - overlap_px)
    cursor = 0
    tile_idx = 0

    while cursor < height:
        remaining = height - cursor
        tile_height = target_long_side_px if remaining > target_long_side_px else remaining

        # Ensure the final tile ends exactly at the bottom to avoid tiny slivers.
        if remaining < target_long_side_px and cursor > 0:
            cursor = max(0, height - target_long_side_px)
            tile_height = height - cursor

        cropped = image.crop(0, cursor, width, tile_height)
        png_bytes = cropped.write_to_buffer(".png", **_PNG_ENCODE_ARGS)
        top_sha = _overlap_sha(cropped, position="top", overlap_px=overlap_px)
        bottom_sha = _overlap_sha(cropped, position="bottom", overlap_px=overlap_px)
        tiles.append(
            TileSlice(
                index=tile_index_offset + tile_idx,
                png_bytes=png_bytes,
                sha256=hashlib.sha256(png_bytes).hexdigest(),
                width=width,
                height=tile_height,
                scale=scale,
                source_y_offset=int(viewport_y_offset + _unscale(cursor, scale)),
                viewport_y_offset=viewport_y_offset,
                overlap_px=overlap_px,
                top_overlap_sha256=top_sha,
                bottom_overlap_sha256=bottom_sha,
            )
        )

        if cursor + tile_height >= height:
            break

        cursor += step
        tile_idx += 1

    return tiles


def _unscale(value: int, scale: float) -> float:
    if scale == 0:
        return float(value)
    return value / scale


def _overlap_sha(image: Any, *, position: str, overlap_px: int) -> Optional[str]:
    if overlap_px <= 0:
        return None

    sample_height = min(overlap_px, image.height)
    if sample_height <= 0:
        return None

    if position == "top":
        strip = image.crop(0, 0, image.width, sample_height)
    else:
        y = max(0, image.height - sample_height)
        strip = image.crop(0, y, image.width, sample_height)

    png_bytes = strip.write_to_buffer(".png", **_PNG_ENCODE_ARGS)
    return hashlib.sha256(png_bytes).hexdigest()


def validate_tiles(tiles: Iterable[TileSlice]) -> None:
    """Ensure each tile's checksum matches bytes and pyvips can decode it."""

    vips = _require_pyvips()
    for tile in tiles:
        if hashlib.sha256(tile.png_bytes).hexdigest() != tile.sha256:
            raise ValueError(f"Tile {tile.index} checksum mismatch")
        try:
            image = vips.Image.new_from_buffer(tile.png_bytes, "", access="sequential")
        except Exception as exc:  # pragma: no cover - depends on corrupted data
            raise ValueError(f"Tile {tile.index} PNG decode failed") from exc
        if image.width != tile.width or image.height != tile.height:
            raise ValueError(
                f"Tile {tile.index} dimension mismatch: expected {tile.width}x{tile.height},"
                f" got {image.width}x{image.height}"
            )


def _require_pyvips():
    if _PYVIPS is None:
        message = (
            "pyvips/libvips is not installed or failed to load. "
            "Install the libvips shared library (see PLAN §19.3) to enable tiling."
        )
        raise RuntimeError(message) from _PYVIPS_IMPORT_ERROR
    return _PYVIPS
