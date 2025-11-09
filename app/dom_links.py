"""DOM + OCR link harvesting and hybrid text recovery helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Sequence
from urllib.parse import urlparse

from bs4 import BeautifulSoup


@dataclass(slots=True)
class LinkRecord:
    """Structured representation of an extracted anchor or form."""

    text: str
    href: str
    source: str  # "DOM" | "OCR" | "DOM+OCR"
    delta: str
    rel: tuple[str, ...] = ()
    target: str | None = None
    kind: str = "anchor"
    domain: str = ""


@dataclass(slots=True)
class DomHeading:
    """Heading extracted from the DOM snapshot with a normalized key."""

    text: str
    level: int
    normalized: str


@dataclass(slots=True)
class DomTextOverlay:
    """Small DOM text fragments that can patch low-confidence OCR output."""

    text: str
    normalized: str
    source: str


def extract_headings_from_html(html: bytes | str) -> Sequence[DomHeading]:
    """Parse a DOM snapshot and return a sequential heading outline."""

    if not html:
        return []
    markup = html.decode("utf-8", errors="ignore") if isinstance(html, bytes) else html
    soup = BeautifulSoup(markup, "html.parser")
    headings: List[DomHeading] = []
    for tag in soup.find_all(re.compile(r"^h[1-6]$", re.IGNORECASE)):
        text = tag.get_text(strip=True)
        if not text:
            continue
        try:
            level = int(tag.name[1])  # type: ignore[index]
        except (ValueError, TypeError, IndexError):
            continue
        normalized = normalize_heading_text(text)
        if not normalized:
            continue
        headings.append(DomHeading(text=text, level=level, normalized=normalized))
    return headings


def extract_dom_text_overlays(html: bytes | str) -> Sequence[DomTextOverlay]:
    """Collect DOM fragments (headings, captions, role-based labels)."""

    if not html:
        return []
    markup = html.decode("utf-8", errors="ignore") if isinstance(html, bytes) else html
    soup = BeautifulSoup(markup, "html.parser")
    overlays: List[DomTextOverlay] = []
    selectors = [
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "figcaption",
        "caption",
        "[role='heading']",
    ]
    for selector in selectors:
        for element in soup.select(selector):
            text = element.get_text(" ", strip=True)
            if not text:
                continue
            normalized = normalize_heading_text(text)
            if not normalized:
                continue
            overlays.append(DomTextOverlay(text=text, normalized=normalized, source=selector))
    return overlays


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
        rel = _normalize_rel(anchor.get("rel"))
        target = anchor.get("target")
        records.append(
            LinkRecord(
                text=text,
                href=href,
                source="DOM",
                delta="✓",
                rel=rel,
                target=target,
                kind="anchor",
                domain=_derive_domain(href),
            )
        )

    for form in soup.find_all("form"):
        action = form.get("action") or ""
        if not action:
            continue
        label = form.get("aria-label") or form.get("name") or "[form]"
        rel = _normalize_rel(form.get("rel"))
        target = form.get("target")
        records.append(
            LinkRecord(
                text=label,
                href=action,
                source="DOM",
                delta="✓",
                rel=rel,
                target=target,
                kind="form",
                domain=_derive_domain(action),
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
        href_value = href.strip()
        records.append(
            LinkRecord(
                text=text.strip(),
                href=href_value,
                source="OCR",
                delta="OCR only",
                kind="markdown",
                domain=_derive_domain(href_value),
            )
        )
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
            rel=record.rel,
            target=record.target,
            kind=record.kind,
            domain=record.domain,
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
                rel=record.rel,
                target=record.target,
                kind=record.kind,
                domain=record.domain,
            )

    return list(results.values())


def serialize_links(records: Iterable[LinkRecord]) -> list[dict[str, object]]:
    """Convert link records to JSON-serializable dictionaries."""

    payload: list[dict[str, object]] = []
    for record in records:
        rel_values = list(record.rel)
        domain = record.domain or _derive_domain(record.href)
        markdown_text = record.text or record.href
        payload.append(
            {
                "text": record.text,
                "href": record.href,
                "source": record.source,
                "delta": record.delta,
                "rel": rel_values,
                "target": record.target,
                "kind": record.kind,
                "domain": domain,
                "markdown": f"[{markdown_text}]({record.href})",
            }
        )
    return payload


def demo_dom_links() -> list[LinkRecord]:
    """Provide deterministic sample DOM links for UI/tests."""

    return [
        LinkRecord(
            text="Example Docs",
            href="https://example.com/docs",
            source="DOM",
            delta="✓",
            target="_blank",
            rel=("noopener",),
            domain="example.com",
        ),
        LinkRecord(text="Support", href="https://example.com/support", source="DOM", delta="✓", domain="example.com"),
    ]


def demo_ocr_links() -> list[LinkRecord]:
    """Provide deterministic sample OCR-only or conflicting links."""

    return [
        LinkRecord(
            text="Unknown link",
            href="https://demo.invalid",
            source="OCR",
            delta="Δ +1",
            kind="markdown",
            domain="demo.invalid",
        ),
    ]


def normalize_heading_text(text: str) -> str:
    """Normalize heading text for matching between DOM + OCR content."""

    collapsed = re.sub(r"\s+", " ", text).strip().lower()
    stripped = re.sub(r"[^a-z0-9\s]", "", collapsed)
    return re.sub(r"\s+", " ", stripped).strip()


def _normalize_rel(value: object | None) -> tuple[str, ...]:
    if not value:
        return ()
    if isinstance(value, (list, tuple, set)):
        tokens = [str(token).strip().lower() for token in value]
    else:
        tokens = [segment.strip().lower() for segment in str(value).split()]
    return tuple(token for token in tokens if token)


def _derive_domain(href: str) -> str:
    if not href:
        return "(unknown)"
    parsed = urlparse(href)
    if parsed.hostname:
        return parsed.hostname.lower()
    if href.startswith("#"):
        return "(fragment)"
    if parsed.scheme and not parsed.hostname:
        return f"{parsed.scheme}:"
    return "(relative)"
