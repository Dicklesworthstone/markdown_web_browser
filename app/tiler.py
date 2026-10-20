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

# PNG encode defaults that match PLAN §19.3 and AGENTS.md guidance (Q=9, palette off, non-interlaced).
_PNG_ENCODE_ARGS = {
    "compression": 9,
    "interlace": False,
    "palette": False,
    "strip": True,
}


@dataclass(slots=True)
class TileSlice:
    """A single OCR-ready tile plus provenance and inverse-mapping metadata."""

    index: int
    png_bytes: bytes
    sha256: str
    width: int
    height: int
    scale: float
    # CSS document pixels. set_tile_overlap derives shared rows from this offset.
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
    device_scale_factor: float = 1
    source_crop_x: int = 0
    source_crop_y: int = 0
    source_crop_width: int = 0
    source_crop_height: int = 0
    source_width: int = 0
    source_height: int = 0
    css_y_offset: int = 0
    css_height: int = 0
    top_overlap_px: int = 0
    bottom_overlap_px: int = 0
    coordinate_space: str = "CSS document offsets; screenshot physical-pixel source_rect"

    def __post_init__(self) -> None:
        if self.scale_x is None or self.scale_x <= 0:
            self.scale_x = self.scale if self.scale > 0 else 1.0
        if self.scale_y is None or self.scale_y <= 0:
            self.scale_y = self.scale if self.scale > 0 else 1.0
        if not self.device_scale_factor or self.device_scale_factor <= 0:
            self.device_scale_factor = 1.0
        if self.source_crop_width <= 0:
            self.source_crop_width = max(1, int(round(self.width / self.scale_x)))
        if self.source_crop_height <= 0:
            self.source_crop_height = max(1, int(round(self.height / self.scale_y)))
        if self.source_width <= 0:
            self.source_width = self.source_crop_width
        if self.source_height <= 0:
            self.source_height = max(
                self.source_crop_y + self.source_crop_height,
                self.source_crop_height,
            )
        if self.css_y_offset == 0 and (self.viewport_y_offset != 0 or self.source_crop_y != 0):
            self.css_y_offset = int(round(self.source_y_offset))
        if self.css_height <= 0:
            self.css_height = max(
                1, int(round(self.source_crop_height / self.device_scale_factor))
            )
        if self.top_overlap_px == 0 and self.overlap_px > 0:
            self.top_overlap_px = min(self.overlap_px, self.height)
        if self.bottom_overlap_px == 0 and self.overlap_px > 0:
            self.bottom_overlap_px = min(self.overlap_px, self.height)
        if self.source_rect is None and self.source_crop_width > 0 and self.source_crop_height > 0:
            self.source_rect = (
                float(self.source_crop_x),
                float(self.source_crop_y),
                float(self.source_crop_width),
                float(self.source_crop_height),
            )

    @property
    def source_crop_box(self) -> tuple[int, int, int, int]:
        """Return `(x, y, width, height)` in source screenshot pixels."""
        return (
            self.source_crop_x,
            self.source_crop_y,
            self.source_crop_width,
            self.source_crop_height,
        )

    def tile_to_source_px(self, tile_x: float, tile_y: float) -> tuple[int, int]:
        """Map `(tile_x, tile_y)` in tile pixels back to `(src_x, src_y)` in screenshot pixels."""
        clamped_x = min(max(0.0, float(tile_x)), max(0.0, float(self.width - 1)))
        clamped_y = min(max(0.0, float(tile_y)), max(0.0, float(self.height - 1)))
        ratio_x = clamped_x / self.width if self.width > 0 else 0.0
        ratio_y = clamped_y / self.height if self.height > 0 else 0.0
        src_x = self.source_crop_x + int(round(ratio_x * self.source_crop_width))
        src_y = self.source_crop_y + int(round(ratio_y * self.source_crop_height))
        src_x = min(max(self.source_crop_x, src_x), max(0, self.source_width - 1))
        src_y = min(max(self.source_crop_y, src_y), max(0, self.source_height - 1))
        return (src_x, src_y)

    def source_px_to_tile(self, src_x: float, src_y: float) -> tuple[int, int]:
        """Map `(src_x, src_y)` in screenshot pixels within this tile's crop box to `(tile_x, tile_y)`."""
        rel_x = (float(src_x) - self.source_crop_x) / max(1, self.source_crop_width)
        rel_y = (float(src_y) - self.source_crop_y) / max(1, self.source_crop_height)
        tile_x = int(round(rel_x * self.width))
        tile_y = int(round(rel_y * self.height))
        tile_x = min(max(0, tile_x), max(0, self.width - 1))
        tile_y = min(max(0, tile_y), max(0, self.height - 1))
        return (tile_x, tile_y)

    def highlight_band_for_source_px(self, src_y0: float, src_y1: float) -> tuple[int, int]:
        """Map a vertical band `[src_y0, src_y1]` in source screenshot pixels to `[y0, y1]` in tile pixels."""
        _, y0 = self.source_px_to_tile(self.source_crop_x, src_y0)
        rel_end = (float(src_y1) - self.source_crop_y) / max(1, self.source_crop_height)
        y1 = min(self.height, max(y0 + 1, int(round(rel_end * self.height))))
        return (y0, y1)


async def slice_into_tiles(
    image_bytes: bytes,
    *,
    overlap_px: int = 120,
    tile_index_offset: int = 0,
    viewport_y_offset: int = 0,
    target_long_side_px: int = 1288,
    device_scale_factor: float = 1,
) -> List[TileSlice]:
    """Slice a viewport-sized screenshot into <=1288 px-long aspect-preserving tiles.

    The longest dimension for each resulting tile is capped at ``target_long_side_px``
    while keeping ≈``overlap_px`` pixels of vertical overlap so OCR stitching can
    deduplicate seams deterministically. A short final tile keeps the nominal step;
    cross-sweep overlap larger than that step is recorded by ``set_tile_overlap``.
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
    device_scale_factor: float,
) -> List[TileSlice]:
    if not image_bytes:
        raise ValueError("image_bytes must be non-empty")
    if target_long_side_px < 1 or device_scale_factor < 1:
        raise ValueError("target_long_side_px and device scale factor must be positive")
    if not 0 <= overlap_px < target_long_side_px:
        raise ValueError("overlap_px must be nonnegative and smaller than tile size")
    vips = _require_pyvips()
    # Avoid sequential access which can cause issues with some PNG data
    try:
        image = vips.Image.new_from_buffer(image_bytes, "")
    except Exception:
        # Fallback to sequential if needed
        image = vips.Image.new_from_buffer(image_bytes, "", access="sequential")

    source_width, source_height = image.width, image.height
    if source_width <= 0 or source_height <= 0:
        raise ValueError(f"Invalid source image dimensions: {source_width}x{source_height}")
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
        png_bytes = cropped.pngsave_buffer(**_PNG_ENCODE_ARGS)
        src_y = cursor / scale_y
        src_h = tile_height / scale_y
        src_w = width / scale_x
        source_y_offset = viewport_y_offset + src_y / device_scale_factor
        src_crop_y = 0 if cursor == 0 else max(0, int(round(src_y)))
        src_crop_height = max(1, int(round(src_h)))
        if src_crop_y + src_crop_height > source_height:
            src_crop_height = max(1, source_height - src_crop_y)
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
                source_y_offset=source_y_offset,
                viewport_y_offset=viewport_y_offset,
                overlap_px=0,
                top_overlap_sha256=None,
                bottom_overlap_sha256=None,
                source_rect=(0.0, src_y, src_w, src_h),
                scale_x=scale_x,
                scale_y=scale_y,
                device_scale_factor=device_scale_factor,
                source_crop_x=0,
                source_crop_y=src_crop_y,
                source_crop_width=int(source_width),
                source_crop_height=src_crop_height,
                source_width=int(source_width),
                source_height=int(source_height),
                css_y_offset=int(round(source_y_offset)),
                css_height=max(1, int(round(src_h / device_scale_factor))),
                coordinate_space="CSS document offsets; screenshot physical-pixel source_rect",
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


def align_cross_sweep_overlap(prev_tile: TileSlice, curr_tile: TileSlice) -> None:
    """Fill cross-sweep overlap only when set_tile_overlap has not recorded it yet.

    Same-viewport pairs and pairs that already carry strip hashes are left unchanged
    so a preceding set_tile_overlap is not hashed a second time.
    """

    if prev_tile.viewport_y_offset == curr_tile.viewport_y_offset:
        return
    if (
        prev_tile.bottom_overlap_sha256
        and curr_tile.top_overlap_sha256
        and prev_tile.bottom_overlap_px > 0
        and curr_tile.top_overlap_px > 0
    ):
        return
    set_tile_overlap(prev_tile, curr_tile)


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

    png_bytes = strip.pngsave_buffer(**_PNG_ENCODE_ARGS)
    return hashlib.sha256(png_bytes).hexdigest()


def validate_tiles(tiles: Iterable[TileSlice]) -> None:
    """Ensure each tile's checksum matches bytes and pyvips can decode it within bounds."""

    vips = _require_pyvips()
    for tile in tiles:
        if hashlib.sha256(tile.png_bytes).hexdigest() != tile.sha256:
            raise ValueError(f"Tile {tile.index} checksum mismatch")
        try:
            image = vips.Image.new_from_buffer(tile.png_bytes, "")
        except Exception as exc:  # pragma: no cover - depends on corrupted data
            raise ValueError(f"Tile {tile.index} PNG decode failed") from exc
        if image.width != tile.width or image.height != tile.height:
            raise ValueError(
                f"Tile {tile.index} dimension mismatch: expected {tile.width}x{tile.height},"
                f" got {image.width}x{image.height}"
            )
        if tile.source_crop_y < 0 or (tile.source_crop_y + tile.source_crop_height) > tile.source_height:
            raise ValueError(
                f"Tile {tile.index} source crop out of bounds: "
                f"y={tile.source_crop_y}..{tile.source_crop_y + tile.source_crop_height} > {tile.source_height}"
            )


def _require_pyvips():
    if _PYVIPS is None:
        message = (
            "pyvips/libvips is not installed or failed to load. "
            "Install the libvips shared library (see PLAN §19.3) to enable tiling."
        )
        raise RuntimeError(message) from _PYVIPS_IMPORT_ERROR
    return _PYVIPS
