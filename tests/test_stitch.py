from __future__ import annotations

from app.stitch import stitch_markdown
from app.tiler import TileSlice


def _tile(index: int, viewport_y: int) -> TileSlice:
    return TileSlice(
        index=index,
        png_bytes=b"",
        sha256=f"sha{index}",
        width=100,
        height=100,
        scale=2.0,
        source_y_offset=viewport_y,
        viewport_y_offset=viewport_y,
        overlap_px=120,
        top_overlap_sha256=None,
        bottom_overlap_sha256=None,
    )


def test_stitch_inserts_provenance_comments() -> None:
    tiles = [_tile(0, 0), _tile(1, 400)]
    chunks = ["First chunk", "Second chunk"]

    output = stitch_markdown(chunks, tiles)

    assert "<!-- source: tile_0000, y=0, sha256=sha0, scale=2.00 -->" in output
    assert "<!-- source: tile_0001, y=400, sha256=sha1, scale=2.00 -->" in output


def test_stitch_normalizes_heading_and_notes_original() -> None:
    tiles = [_tile(0, 0)]
    output = stitch_markdown(["#### Deep Heading"], tiles)

    assert "## Deep Heading" in output
    assert "<!-- normalized-heading: #### Deep Heading -->" in output


def test_stitch_dedupes_table_headers() -> None:
    tiles = [_tile(0, 0), _tile(1, 400)]
    chunk1 = "| Col |\n| --- |\n| A |\n"
    chunk2 = "| Col |\n| --- |\n| B |\n"

    output = stitch_markdown([chunk1, chunk2], tiles)

    assert output.count("| Col |") == 1  # header only once
    assert "<!-- table-header-trimmed -->" in output
