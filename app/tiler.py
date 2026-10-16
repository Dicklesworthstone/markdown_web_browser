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
    source_y_offset: int
    viewport_y_offset: int
    overlap_px: int
    top_overlap_sha256: Optional[str]
    bottom_overlap_sha256: Optional[str]
    seam_top_hash: Optional[str] = None
    seam_bottom_hash: Optional[str] = None
    scale_x: float = 0.0
    scale_y: float = 0.0
    device_scale_factor: float = 1.0
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
    coordinate_space: str = "screenshot_px"

    def __post_init__(self) -> None:
        if self.scale_x <= 0:
            self.scale_x = self.scale if self.scale > 0 else 1.0
        if self.scale_y <= 0:
            self.scale_y = self.scale if self.scale > 0 else 1.0
        if self.device_scale_factor <= 0:
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
            self.css_y_offset = int(
                round(self.viewport_y_offset + (self.source_crop_y / self.device_scale_factor))
            )
        if self.css_height <= 0:
            self.css_height = max(1, int(round(self.source_crop_height / self.device_scale_factor)))
        if self.top_overlap_px == 0 and self.overlap_px > 0:
            self.top_overlap_px = min(self.overlap_px, self.height)
        if self.bottom_overlap_px == 0 and self.overlap_px > 0:
            self.bottom_overlap_px = min(self.overlap_px, self.height)

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
    device_scale_factor: float = 1.0,
) -> List[TileSlice]:
    """Slice a viewport-sized screenshot into <=1288 px-long aspect-preserving tiles.

    The longest dimension for each resulting tile is capped at ``target_long_side_px``
    while keeping ≈``overlap_px`` pixels of vertical overlap (and exact final-tile overlap)
    so OCR stitching can deduplicate seams deterministically.
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
    device_scale_factor: float = 1.0,
) -> List[TileSlice]:
    if not image_bytes:
        raise ValueError("image_bytes must be non-empty")
    if target_long_side_px <= 0:
        raise ValueError("target_long_side_px must be > 0")
    if overlap_px < 0:
        raise ValueError("overlap_px must be >= 0")

    dpr = float(device_scale_factor) if device_scale_factor and device_scale_factor > 0 else 1.0
    vips = _require_pyvips()
    try:
        image = vips.Image.new_from_buffer(image_bytes, "")
    except Exception:
        image = vips.Image.new_from_buffer(image_bytes, "", access="sequential")

    source_width = int(image.width)
    source_height = int(image.height)
    if source_width <= 0 or source_height <= 0:
        raise ValueError(f"Invalid source image dimensions: {source_width}x{source_height}")

    # Preserve aspect ratio: when width exceeds target_long_side_px, downscale uniformly on both axes.
    scale = 1.0
    if source_width > target_long_side_px:
        scale = target_long_side_px / source_width
        image = image.resize(scale, vscale=scale)
        if image.width > target_long_side_px:
            image = image.crop(0, 0, target_long_side_px, image.height)

    height = int(image.height)
    width = int(image.width)
    scale_x = width / source_width
    scale_y = height / source_height
    viewport_y_screenshot_px = int(round(viewport_y_offset * dpr))

    # Precompute tile (cursor, tile_height) bands in resized tile coordinates
    bands: list[tuple[int, int]] = []
    if height <= target_long_side_px:
        bands.append((0, height))
    else:
        effective_overlap = min(overlap_px, max(0, target_long_side_px - 1))
        step = max(1, target_long_side_px - effective_overlap)
        cursor = 0
        while cursor < height:
            remaining = height - cursor
            tile_height = target_long_side_px if remaining > target_long_side_px else remaining
            if remaining < target_long_side_px and cursor > 0:
                cursor = max(0, height - target_long_side_px)
                tile_height = height - cursor
            bands.append((cursor, tile_height))
            if cursor + tile_height >= height:
                break
            cursor += step

    tiles: List[TileSlice] = []
    num_bands = len(bands)

    for tile_idx, (cursor, tile_height) in enumerate(bands):
        cropped = (
            image
            if (cursor == 0 and tile_height == height)
            else image.crop(0, cursor, width, tile_height)
        )
        png_bytes = cropped.pngsave_buffer(**_PNG_ENCODE_ARGS)

        # Compute actual pixel overlap with adjacent tiles in this sweep
        if tile_idx > 0:
            prev_cursor, prev_height = bands[tile_idx - 1]
            actual_top_overlap = max(0, min(tile_height, (prev_cursor + prev_height) - cursor))
        else:
            actual_top_overlap = min(overlap_px, tile_height) if overlap_px > 0 else 0

        if tile_idx + 1 < num_bands:
            next_cursor, _ = bands[tile_idx + 1]
            actual_bottom_overlap = max(0, min(tile_height, (cursor + tile_height) - next_cursor))
        else:
            actual_bottom_overlap = min(overlap_px, tile_height) if overlap_px > 0 else 0

        top_sha = _overlap_sha(cropped, position="top", overlap_px=actual_top_overlap)
        bottom_sha = _overlap_sha(cropped, position="bottom", overlap_px=actual_bottom_overlap)

        # Exact inverse crop mapping to source screenshot pixels [0, source_height]
        src_crop_y = 0 if cursor == 0 else min(source_height - 1, max(0, int(round(_unscale(cursor, scale_y)))))
        if cursor + tile_height >= height:
            src_end_y = source_height
        else:
            src_end_y = min(
                source_height,
                max(src_crop_y + 1, int(round(_unscale(cursor + tile_height, scale_y)))),
            )
        src_crop_height = max(1, src_end_y - src_crop_y)

        source_y_offset = viewport_y_screenshot_px + src_crop_y
        css_y_offset = int(round(viewport_y_offset + (src_crop_y / dpr)))
        css_height = max(1, int(round(src_crop_height / dpr)))

        tiles.append(
            TileSlice(
                index=tile_index_offset + tile_idx,
                png_bytes=png_bytes,
                sha256=hashlib.sha256(png_bytes).hexdigest(),
                width=width,
                height=tile_height,
                scale=scale,
                source_y_offset=source_y_offset,
                viewport_y_offset=viewport_y_offset,
                overlap_px=actual_top_overlap if tile_idx > 0 else actual_bottom_overlap,
                top_overlap_sha256=top_sha,
                bottom_overlap_sha256=bottom_sha,
                scale_x=scale_x,
                scale_y=scale_y,
                device_scale_factor=dpr,
                source_crop_x=0,
                source_crop_y=src_crop_y,
                source_crop_width=source_width,
                source_crop_height=src_crop_height,
                source_width=source_width,
                source_height=source_height,
                css_y_offset=css_y_offset,
                css_height=css_height,
                top_overlap_px=actual_top_overlap,
                bottom_overlap_px=actual_bottom_overlap,
                coordinate_space="screenshot_px",
            )
        )

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


def align_cross_sweep_overlap(prev_tile: TileSlice, curr_tile: TileSlice) -> None:
    """Align cross-sweep bottom/top overlap strips to the exact shared document y-range."""

    if prev_tile.viewport_y_offset == curr_tile.viewport_y_offset:
        return
    prev_bottom_src = prev_tile.source_y_offset + prev_tile.source_crop_height
    curr_top_src = curr_tile.source_y_offset
    shared_src_px = prev_bottom_src - curr_top_src
    if shared_src_px <= 0:
        return

    scale_y = (
        curr_tile.scale_y
        if curr_tile.scale_y > 0
        else (curr_tile.scale if curr_tile.scale > 0 else 1.0)
    )
    shared_tile_px = min(
        prev_tile.height,
        curr_tile.height,
        max(1, int(round(shared_src_px * scale_y))),
    )
    if shared_tile_px <= 0:
        return

    prev_tile.bottom_overlap_px = shared_tile_px
    curr_tile.top_overlap_px = shared_tile_px

    if _PYVIPS is None:
        return
    try:
        prev_img = _PYVIPS.Image.new_from_buffer(prev_tile.png_bytes, "")
        curr_img = _PYVIPS.Image.new_from_buffer(curr_tile.png_bytes, "")
    except Exception:
        return

    prev_sha = _overlap_sha(prev_img, position="bottom", overlap_px=shared_tile_px)
    curr_sha = _overlap_sha(curr_img, position="top", overlap_px=shared_tile_px)
    if prev_sha is not None and curr_sha is not None:
        prev_tile.bottom_overlap_sha256 = prev_sha
        curr_tile.top_overlap_sha256 = curr_sha


def _require_pyvips():
    if _PYVIPS is None:
        message = (
            "pyvips/libvips is not installed or failed to load. "
            "Install the libvips shared library (see PLAN §19.3) to enable tiling."
        )
        raise RuntimeError(message) from _PYVIPS_IMPORT_ERROR
    return _PYVIPS

