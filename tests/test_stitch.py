from __future__ import annotations

from app.dom_links import DomHeading, DomTextOverlay
from app.stitch import stitch_markdown
from app.tiler import TileSlice


def _tile(
    index: int,
    viewport_y: int,
    *,
    top_sha: str | None = None,
    bottom_sha: str | None = None,
    height: int = 100,
    source_y: int | None = None,
) -> TileSlice:
    return TileSlice(
        index=index,
        png_bytes=b"",
        sha256=f"sha{index}",
        width=100,
        height=height,
        scale=2.0,
        source_y_offset=viewport_y if source_y is None else source_y,
        viewport_y_offset=viewport_y,
        overlap_px=120,
        top_overlap_sha256=top_sha,
        bottom_overlap_sha256=bottom_sha,
    )


def test_stitch_inserts_provenance_comments() -> None:
    tiles = [_tile(0, 0), _tile(1, 400)]
    chunks = ["First chunk", "Second chunk"]

    result = stitch_markdown(chunks, tiles)
    output = result.markdown

    assert "viewport_y=0" in output
    assert "viewport_y=400" in output
    assert "overlap_px=120" in output


def test_stitch_normalizes_heading_and_notes_original() -> None:
    tiles = [_tile(0, 0)]
    result = stitch_markdown(["#### Deep Heading"], tiles)
    output = result.markdown

    assert "## Deep Heading" in output
    assert "<!-- normalized-heading: #### Deep Heading -->" in output


def test_stitch_uses_dom_outline_for_heading_levels() -> None:
    tiles = [_tile(0, 0)]
    dom_headings = [DomHeading(text="Long Heading", level=4, normalized="long heading")]

    result = stitch_markdown(["###### Long Heading"], tiles, dom_headings=dom_headings)
    output = result.markdown

    assert "#### Long Heading" in output  # DOM level honored even when deeper than clamp
    assert "<!-- normalized-heading: ###### Long Heading -->" in output


def test_stitch_dedupes_table_headers_with_overlap_match() -> None:
    tiles = [
        _tile(0, 0, bottom_sha="aaa"),
        _tile(1, 400, top_sha="aaa"),
    ]
    chunk1 = "| Col |\n| --- |\n| A |\n"
    chunk2 = "| Col |\n| --- |\n| B |\n"

    result = stitch_markdown([chunk1, chunk2], tiles)
    output = result.markdown

    assert output.count("| Col |") == 1  # header only once
    assert "table-header-trimmed reason=identical" in output


def test_stitch_keeps_table_headers_without_overlap_match() -> None:
    tiles = [
        _tile(0, 0, bottom_sha="aaa"),
        _tile(1, 400, top_sha="bbb"),
    ]
    chunk1 = "| Col |\n| --- |\n| A |\n"
    chunk2 = "| Col |\n| --- |\n| B |\n"

    result = stitch_markdown([chunk1, chunk2], tiles)
    output = result.markdown

    assert output.count("| Col |") == 2  # second header retained without overlap agreement


def test_stitch_emits_seam_marker_for_matching_overlap() -> None:
    tiles = [
        _tile(0, 0, bottom_sha="aaa"),
        _tile(1, 400, top_sha="aaa"),
    ]
    result = stitch_markdown(["chunk A", "chunk B"], tiles)
    output = result.markdown

    assert "<!-- seam-marker: prev=tile_0000, curr=tile_0001" in output


def test_table_header_similarity_trim() -> None:
    tiles = [
        _tile(0, 0, bottom_sha="aaa"),
        _tile(1, 400, top_sha="aaa"),
    ]
    chunk1 = """| Col |
| --- |
| A |
"""
    chunk2 = """|  Col  |
| --- |
| B |
"""

    result = stitch_markdown([chunk1, chunk2], tiles)
    output = result.markdown

    assert output.count("| Col") == 1
    assert "table-header-trimmed reason=similar" in output
def test_dom_assist_overlays_low_confidence_line() -> None:
    tiles = [_tile(0, 0)]
    overlays = [DomTextOverlay(text="Revenue Q4", normalized="revenue q4", source="figcaption")]
    chunk = "Revenue Q4???"

    result = stitch_markdown([chunk], tiles, dom_overlays=overlays)

    assert "Revenue Q4" in result.markdown
    assert result.dom_assists[0].reason == "punctuation"
