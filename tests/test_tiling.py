"""Tests for tiler helpers."""

from __future__ import annotations

import hashlib

import pytest

try:  # pragma: no cover - exercised only when libvips missing
    import pyvips as _pyvips  # type: ignore
except Exception as exc:  # noqa: BLE001
    pytest.skip(f"pyvips unavailable: {exc}", allow_module_level=True)
else:
    if not hasattr(_pyvips, "Image"):
        pytest.skip("pyvips.Image not available", allow_module_level=True)
    pyvips = _pyvips  # type: Any
    if not hasattr(getattr(pyvips, "Image", None), "black"):
        pytest.skip("pyvips stub lacks Image.black; skipping tiler tests", allow_module_level=True)

from app.tiler import TileSlice, validate_tiles


def _png_bytes(width: int = 10, height: int = 10) -> bytes:
    image = pyvips.Image.black(width, height).add(1)  # type: ignore[attr-defined]
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
