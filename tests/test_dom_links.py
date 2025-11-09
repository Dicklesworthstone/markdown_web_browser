from __future__ import annotations

from app.dom_links import (
    LinkRecord,
    blend_dom_with_ocr,
    extract_dom_text_overlays,
    extract_headings_from_html,
    extract_links_from_markdown,
    serialize_links,
)


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
    assert records[0].domain == "example.com"
    assert records[0].kind == "markdown"


def test_blend_dom_with_ocr_marks_deltas() -> None:
    dom_links = [
        LinkRecord(
            text="Docs",
            href="https://example.com/docs",
            source="DOM",
            delta="✓",
            rel=("noopener",),
            target="_blank",
            domain="example.com",
        ),
    ]
    ocr_links = [
        LinkRecord(text="Docs - Updated", href="https://example.com/docs", source="OCR", delta="OCR only"),
        LinkRecord(text="Extra", href="https://example.com/extra", source="OCR", delta="OCR only"),
    ]

    blended = blend_dom_with_ocr(dom_links=dom_links, ocr_links=ocr_links)

    assert any(link.source == "DOM+OCR" and link.delta in {"✓", "text mismatch"} for link in blended)
    assert any(link.source == "OCR" and link.delta == "OCR only" for link in blended)
    dom_hybrid = next(link for link in blended if link.source == "DOM+OCR")
    assert dom_hybrid.target == "_blank"
    assert dom_hybrid.rel == ("noopener",)


def test_extract_headings_from_html_returns_outline() -> None:
    html = """
    <html>
      <body>
        <h1>Intro</h1>
        <section>
          <h4> Deep Dive </h4>
          <h2>Summary &amp; Next Steps</h2>
        </section>
      </body>
    </html>
    """

    headings = extract_headings_from_html(html)

    assert [h.level for h in headings] == [1, 4, 2]
    assert headings[1].normalized == "deep dive"
    assert headings[2].normalized == "summary next steps"


def test_extract_dom_text_overlays_includes_captions() -> None:
    html = """
    <main>
      <h2> Chart Title </h2>
      <figure>
        <img src="chart.png" />
        <figcaption> Revenue Q4 </figcaption>
      </figure>
    </main>
    """

    overlays = extract_dom_text_overlays(html)

    assert any(entry.normalized == "chart title" for entry in overlays)
    assert any(entry.normalized == "revenue q4" for entry in overlays)


def test_serialize_links_includes_metadata() -> None:
    record = LinkRecord(
        text="Docs",
        href="https://example.com/docs",
        source="DOM",
        delta="✓",
        rel=("noopener", "nofollow"),
        target="_blank",
        kind="anchor",
        domain="example.com",
    )

    payload = serialize_links([record])

    assert payload[0]["rel"] == ["noopener", "nofollow"]
    assert payload[0]["target"] == "_blank"
    assert payload[0]["domain"] == "example.com"
    assert payload[0]["markdown"] == "[Docs](https://example.com/docs)"
