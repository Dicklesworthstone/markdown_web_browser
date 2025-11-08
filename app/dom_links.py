"""DOM + OCR link harvesting and hybrid text recovery helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable, Sequence

from bs4 import BeautifulSoup


@dataclass(slots=True)
class LinkRecord:
    """Structured representation of an extracted anchor or form."""

    text: str
    href: str
    source: str  # "DOM" | "OCR" | "DOM+OCR"
    delta: str


def extract_links_from_dom(dom_snapshot: Path) -> Sequence[LinkRecord]:
    """Parse the DOM snapshot and return structured link records."""

    html = dom_snapshot.read_text(encoding="utf-8")
    soup = BeautifulSoup(html, "html.parser")
    records: list[LinkRecord] = []

    for anchor in soup.find_all("a"):
        href = anchor.get("href")
        if not href:
            continue
        text = anchor.get_text(strip=True)
        records.append(
            LinkRecord(
                text=text,
                href=href,
                source="DOM",
                delta="✓",
            )
        )

    for form in soup.find_all("form"):
        action = form.get("action") or ""
        if not action:
            continue
        label = form.get("aria-label") or form.get("name") or "[form]"
        records.append(
            LinkRecord(
                text=label,
                href=action,
                source="DOM",
                delta="✓",
            )
        )

    return records


_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")


def extract_links_from_markdown(markdown: str) -> Sequence[LinkRecord]:
    """Heuristically parse Markdown links (e.g., `[text](https://example.com)`)."""

    if not markdown:
        return []
    records: list[LinkRecord] = []
    for match in _MARKDOWN_LINK_RE.finditer(markdown):
        text, href = match.groups()
        records.append(LinkRecord(text=text.strip(), href=href.strip(), source="OCR", delta="OCR only"))
    return records


def blend_dom_with_ocr(
    *,
    dom_links: Sequence[LinkRecord],
    ocr_links: Sequence[LinkRecord],
) -> Sequence[LinkRecord]:
    """Merge DOM + OCR signals to flag mismatches for the Links tab."""

    results: dict[str, LinkRecord] = {}
    for record in dom_links:
        key = record.href
        results[key] = LinkRecord(
            text=record.text,
            href=record.href,
            source="DOM",
            delta="DOM only",
        )

    for record in ocr_links:
        key = record.href
        if key in results:
            combined = results[key]
            combined.source = "DOM+OCR"
            if not combined.text:
                combined.text = record.text
            if combined.text and record.text and combined.text != record.text:
                combined.delta = "text mismatch"
            else:
                combined.delta = "✓"
        else:
            results[key] = LinkRecord(
                text=record.text,
                href=record.href,
                source="OCR",
                delta="OCR only",
            )

    return list(results.values())


def serialize_links(records: Iterable[LinkRecord]) -> list[dict[str, str]]:
    """Convert link records to JSON-serializable dictionaries."""

    return [asdict(record) for record in records]


def demo_dom_links() -> list[LinkRecord]:
    """Provide deterministic sample DOM links for UI/tests."""

    return [
        LinkRecord(text="Example Docs", href="https://example.com/docs", source="DOM", delta="✓"),
        LinkRecord(text="Support", href="https://example.com/support", source="DOM", delta="✓"),
    ]


def demo_ocr_links() -> list[LinkRecord]:
    """Provide deterministic sample OCR-only or conflicting links."""

    return [
        LinkRecord(text="Unknown link", href="https://demo.invalid", source="OCR", delta="Δ +1"),
    ]
