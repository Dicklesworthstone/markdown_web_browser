from __future__ import annotations

from app.dom_links import DomHeading, DomTextOverlay, normalize_heading_text
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

    assert "seam-marker: prev=tile_0000" in output


def test_seam_marker_includes_hash_when_present() -> None:
    tiles = [
        _tile(0, 0, bottom_sha=None),
        _tile(1, 400, top_sha=None),
    ]
    tiles[0].seam_bottom_hash = "abc123"
    tiles[1].seam_top_hash = "abc123"

    result = stitch_markdown(["chunk A", "chunk B"], tiles)
    output = result.markdown

    assert "seam_hash=abc123" in output


def test_seam_marker_events_recorded_when_seam_hint_used() -> None:
    tiles = [
        _tile(0, 0, bottom_sha="mismatch-a"),
        _tile(1, 400, top_sha="mismatch-b"),
    ]
    tiles[0].seam_bottom_hash = "seam99"
    tiles[1].seam_top_hash = "seam99"

    result = stitch_markdown(["chunk A", "chunk B"], tiles)

    seam_events = getattr(result, "seam_marker_events")
    assert seam_events, "expected seam marker usage to be recorded"
    event = seam_events[0]
    assert event.prev_tile_index == 0
    assert event.curr_tile_index == 1
    assert event.seam_hash == "seam99"


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


def test_table_header_trim_skips_leading_blank_lines_and_comments() -> None:
    tiles = [
        _tile(0, 0, bottom_sha="aaa"),
        _tile(1, 400, top_sha="aaa"),
    ]
    chunk1 = """| Name | Value |
| ---- | ----- |
| A | 1 |
"""
    chunk2 = """
<!-- normalized-heading: ### Stats -->
| Name | Value |
| ---- | ----- |
| B | 2 |
"""

    result = stitch_markdown([chunk1, chunk2], tiles)
    output = result.markdown

    assert output.count("| Name | Value |") == 1
    assert "table-header-trimmed reason=identical" in output


def test_dom_assist_overlays_low_confidence_line() -> None:
    tiles = [_tile(0, 0)]
    overlays = [DomTextOverlay(text="Revenue Q4", normalized="revenue q4", source="figcaption")]
    chunk = "Revenue Q4???"

    result = stitch_markdown([chunk], tiles, dom_overlays=overlays)

    assert "Revenue Q4" in result.markdown
    assert result.dom_assists[0].reason == "punctuation"


def test_dom_assist_hyphen_break_merges_lines() -> None:
    tiles = [_tile(0, 0)]
    overlay_text = "Revenue Growth"
    overlays = [
        DomTextOverlay(
            text=overlay_text,
            normalized=normalize_heading_text(overlay_text),
            source="h2",
        )
    ]
    chunk = "## Revenue-\n growth"

    result = stitch_markdown([chunk], tiles, dom_overlays=overlays)

    assert "## Revenue Growth" in result.markdown
    assert "\n growth" not in result.markdown
    assert result.dom_assists[0].reason == "hyphen-break"
    assert "Revenue-" in result.dom_assists[0].original_text


def test_dom_assist_preserves_list_prefix() -> None:
    tiles = [_tile(0, 0)]
    overlays = [DomTextOverlay(text="Revenue Growth", normalized="revenue growth", source="li")]
    chunk = "- Revenue Growth???"

    result = stitch_markdown([chunk], tiles, dom_overlays=overlays)

    assert "- Revenue Growth" in result.markdown


def test_dom_assist_queue_consumes_duplicate_normalized_entries() -> None:
    tiles = [_tile(0, 0), _tile(1, 400)]
    overlays = [
        DomTextOverlay(text="Revenue Growth", normalized="revenue growth", source="h2"),
        DomTextOverlay(text="Revenue Growth!!", normalized="revenue growth", source="h2"),
    ]
    chunks = ["## Revenue-\n growth", "## Revenue-\n growth"]

    result = stitch_markdown(chunks, tiles, dom_overlays=overlays)

    assert "Revenue Growth!!" in result.markdown
    dom_texts = [entry.dom_text for entry in result.dom_assists]
    assert dom_texts == ["Revenue Growth", "Revenue Growth!!"]


def test_dom_assist_skips_code_fence_blocks() -> None:
    tiles = [_tile(0, 0)]
    overlays = [DomTextOverlay(text="Function foo", normalized="function foo", source="code")]
    chunk = """```
function foo???
```"""

    result = stitch_markdown([chunk], tiles, dom_overlays=overlays)

    assert not result.dom_assists
    assert "function foo???" in result.markdown


def test_dom_assist_detects_spaced_letters() -> None:
    tiles = [_tile(0, 0)]
    overlays = [DomTextOverlay(text="Revenue", normalized="revenue", source="heading")]
    chunk = "R e v e n u e"

    result = stitch_markdown([chunk], tiles, dom_overlays=overlays)

    assert "Revenue" in result.markdown
    assert result.dom_assists[0].reason == "spaced-letters"
