from __future__ import annotations

from app.dom_links import LinkRecord, blend_dom_with_ocr, extract_links_from_markdown


def test_extract_links_from_markdown_parses_basic_links() -> None:
    markdown = """
    Welcome to [Docs](https://example.com/docs)!
    Need help? Try [Support](https://example.com/support).
    """
    records = extract_links_from_markdown(markdown)

    assert len(records) == 2
    assert records[0].href == "https://example.com/docs"
    assert records[0].source == "OCR"
    assert records[0].delta == "OCR only"


def test_blend_dom_with_ocr_marks_deltas() -> None:
    dom_links = [
        LinkRecord(text="Docs", href="https://example.com/docs", source="DOM", delta="✓"),
    ]
    ocr_links = [
        LinkRecord(text="Docs - Updated", href="https://example.com/docs", source="OCR", delta="OCR only"),
        LinkRecord(text="Extra", href="https://example.com/extra", source="OCR", delta="OCR only"),
    ]

    blended = blend_dom_with_ocr(dom_links=dom_links, ocr_links=ocr_links)

    assert any(link.source == "DOM+OCR" and link.delta in {"✓", "text mismatch"} for link in blended)
    assert any(link.source == "OCR" and link.delta == "OCR only" for link in blended)
