"""Tile slicing helpers backed by pyvips."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from typing import Any, Iterable, List, Optional, TYPE_CHECKING

try:  # pragma: no cover - exercised indirectly via helper
    import pyvips as _pyvips
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
    source_y_offset: float
    viewport_y_offset: int
    overlap_px: int
    top_overlap_sha256: Optional[str]
    bottom_overlap_sha256: Optional[str]
    seam_top_hash: Optional[str] = None
    seam_bottom_hash: Optional[str] = None
    source_rect: tuple[float, float, float, float] | None = None
    scale_x: float | None = None
    scale_y: float | None = None
    device_scale_factor: int = 1
    top_overlap_px: int = 0
    bottom_overlap_px: int = 0


async def slice_into_tiles(
    image_bytes: bytes,
    *,
    overlap_px: int = 120,
    tile_index_offset: int = 0,
    viewport_y_offset: int = 0,
    target_long_side_px: int = 1288,
    device_scale_factor: int = 1,
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
        device_scale_factor,
    )


def _slice_sync(
    image_bytes: bytes,
    overlap_px: int,
    tile_index_offset: int,
    viewport_y_offset: int,
    target_long_side_px: int,
    device_scale_factor: int,
) -> List[TileSlice]:
    if target_long_side_px < 1 or device_scale_factor < 1:
        raise ValueError("Tile size and device scale factor must be positive")
    if not 0 <= overlap_px < target_long_side_px:
        raise ValueError("Overlap must be nonnegative and smaller than tile size")
    vips = _require_pyvips()
    # Avoid sequential access which can cause issues with some PNG data
    try:
        image = vips.Image.new_from_buffer(image_bytes, "")
    except Exception:
        # Fallback to sequential if needed
        image = vips.Image.new_from_buffer(image_bytes, "", access="sequential")

    source_width, source_height = image.width, image.height
    # Resize both axes together. Cropping the uniformly resized image then
    # bounds each tile without stretching the source content.
    scale = 1.0
    if image.width > target_long_side_px:
        scale = target_long_side_px / image.width
        image = image.resize(scale)

    tiles: List[TileSlice] = []
    height = image.height
    width = image.width
    scale_x, scale_y = width / source_width, height / source_height

    step = max(1, target_long_side_px - overlap_px)
    cursor = 0
    tile_idx = 0

    while cursor < height:
        remaining = height - cursor
        tile_height = target_long_side_px if remaining > target_long_side_px else remaining

        cropped = image.crop(0, cursor, width, tile_height)
        # Use pngsave_buffer for more reliable PNG encoding
        png_bytes = cropped.pngsave_buffer(**_PNG_ENCODE_ARGS)
        tiles.append(
            TileSlice(
                index=tile_index_offset + tile_idx,
                png_bytes=png_bytes,
                sha256=hashlib.sha256(png_bytes).hexdigest(),
                width=width,
                height=tile_height,
                scale=scale,
                # Offsets are CSS document pixels; source_rect is relative to
                # this screenshot in physical pixels. Rounded resize dimensions
                # give separate effective axes for exact inverse crop mapping.
                source_y_offset=viewport_y_offset + cursor / scale_y / device_scale_factor,
                viewport_y_offset=viewport_y_offset,
                overlap_px=0,
                top_overlap_sha256=None,
                bottom_overlap_sha256=None,
                source_rect=(0.0, cursor / scale_y, width / scale_x, tile_height / scale_y),
                scale_x=scale_x,
                scale_y=scale_y,
                device_scale_factor=device_scale_factor,
            )
        )

        if cursor + tile_height >= height:
            break

        cursor += step
        tile_idx += 1

    for previous, current in zip(tiles, tiles[1:]):
        set_tile_overlap(previous, current)
    return tiles


def set_tile_overlap(previous: TileSlice, current: TileSlice) -> None:
    """Hash actual common document rows, including shortened final sweeps."""
    previous_scale = (previous.scale_y or previous.scale) * previous.device_scale_factor
    current_scale = (current.scale_y or current.scale) * current.device_scale_factor
    common_css = (
        previous.source_y_offset + previous.height / previous_scale - current.source_y_offset
    )
    previous_px = min(previous.height, max(0, round(common_css * previous_scale)))
    current_px = min(current.height, max(0, round(common_css * current_scale)))
    previous.bottom_overlap_px = previous_px
    current.top_overlap_px = current_px
    current.overlap_px = current_px
    vips = _require_pyvips()
    previous_image = vips.Image.new_from_buffer(previous.png_bytes, "")
    current_image = vips.Image.new_from_buffer(current.png_bytes, "")
    previous.bottom_overlap_sha256 = _overlap_sha(
        previous_image, position="bottom", overlap_px=previous_px
    )
    current.top_overlap_sha256 = _overlap_sha(current_image, position="top", overlap_px=current_px)


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

    # Use pngsave_buffer for more reliable PNG encoding
    png_bytes = strip.pngsave_buffer(**_PNG_ENCODE_ARGS)
    return hashlib.sha256(png_bytes).hexdigest()


def validate_tiles(tiles: Iterable[TileSlice]) -> None:
    """Ensure each tile's checksum matches bytes and pyvips can decode it."""

    vips = _require_pyvips()
    for tile in tiles:
        if hashlib.sha256(tile.png_bytes).hexdigest() != tile.sha256:
            raise ValueError(f"Tile {tile.index} checksum mismatch")
        try:
            # Remove sequential access which can cause issues with some PNG data
            image = vips.Image.new_from_buffer(tile.png_bytes, "")
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
