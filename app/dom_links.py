"""DOM + OCR link harvesting and hybrid text recovery helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, List, Mapping, Sequence
from urllib.parse import urljoin, urlparse

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
    y: float | None = None
    height: float | None = None
    x: float | None = None
    width: float | None = None
    href: str | None = None


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
            level = int(tag.name[1])
        except (ValueError, TypeError, IndexError):
            continue
        normalized = normalize_heading_text(text)
        if not normalized:
            continue
        headings.append(DomHeading(text=text, level=level, normalized=normalized))
    return headings


def _parse_float_attr(element: Any, *attr_names: str) -> float | None:
    for name in attr_names:
        raw = element.get(name)
        if raw is None:
            continue
        if isinstance(raw, list):
            raw = raw[0] if raw else None
        if raw is None:
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            continue
    return None


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
            y_val = _parse_float_attr(element, "data-mdwb-src-y", "data-mdwb-css-y")
            h_val = _parse_float_attr(element, "data-mdwb-src-h", "data-mdwb-css-h")
            x_val = _parse_float_attr(element, "data-mdwb-src-x", "data-mdwb-css-x")
            w_val = _parse_float_attr(element, "data-mdwb-src-w", "data-mdwb-css-w")
            overlays.append(
                DomTextOverlay(
                    text=text,
                    normalized=normalized,
                    source=selector,
                    y=y_val,
                    height=h_val,
                    x=x_val,
                    width=w_val,
                )
            )
    return overlays


def overlays_from_dom_boxes(boxes: Sequence[Mapping[str, Any]]) -> list[DomTextOverlay]:
    """Convert viewport-harvested DOM bounding boxes into DomTextOverlay items."""

    result: list[DomTextOverlay] = []
    for item in boxes:
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        normalized = normalize_heading_text(text)
        if not normalized:
            continue
        source = str(item.get("source") or item.get("tag") or "dom")
        src_box = item.get("screenshot_box")
        css_box = item.get("css_box")
        box = src_box if isinstance(src_box, Mapping) else (css_box if isinstance(css_box, Mapping) else {})
        y_val = box.get("y") if isinstance(box, Mapping) else item.get("y")
        h_val = box.get("height") if isinstance(box, Mapping) else item.get("height")
        x_val = box.get("x") if isinstance(box, Mapping) else item.get("x")
        w_val = box.get("width") if isinstance(box, Mapping) else item.get("width")
        href_raw = item.get("href")
        result.append(
            DomTextOverlay(
                text=text,
                normalized=normalized,
                source=source,
                y=float(y_val) if isinstance(y_val, (int, float)) else None,
                height=float(h_val) if isinstance(h_val, (int, float)) else None,
                x=float(x_val) if isinstance(x_val, (int, float)) else None,
                width=float(w_val) if isinstance(w_val, (int, float)) else None,
                href=str(href_raw) if isinstance(href_raw, str) and href_raw else None,
            )
        )
    return result


def _resolve_href(href: str, base_url: str | None) -> str:
    cleaned = href.strip()
    if not cleaned or not base_url:
        return cleaned
    lowered = cleaned.lower()
    if lowered.startswith(("http://", "https://", "#", "mailto:", "tel:", "javascript:", "data:")):
        return cleaned
    try:
        return urljoin(base_url, cleaned)
    except Exception:
        return cleaned


def extract_links_from_dom(
    dom_snapshot: Path | bytes | str,
    *,
    base_url: str | None = None,
) -> Sequence[LinkRecord]:
    """Parse the DOM snapshot and return structured link records."""

    if isinstance(dom_snapshot, Path):
        html = dom_snapshot.read_text(encoding="utf-8", errors="ignore")
    elif isinstance(dom_snapshot, bytes):
        html = dom_snapshot.decode("utf-8", errors="ignore")
    else:
        html = str(dom_snapshot)
    soup = BeautifulSoup(html, "html.parser")
    effective_base = base_url
    base_tag = soup.find("base")
    if base_tag is not None:
        base_href_attr = base_tag.get("href")
        base_href = base_href_attr[0] if isinstance(base_href_attr, list) else base_href_attr
        if isinstance(base_href, str) and base_href.strip():
            effective_base = (
                urljoin(base_url, base_href.strip()) if base_url else base_href.strip()
            )
    records: list[LinkRecord] = []

    for anchor in soup.find_all("a"):
        href_attr = anchor.get("href")
        if not href_attr:
            continue
        # BeautifulSoup can return list for multi-value attrs; take first if list
        raw_href = href_attr[0] if isinstance(href_attr, list) else href_attr
        href = _resolve_href(str(raw_href), effective_base)
        text = anchor.get_text(strip=True)
        rel = _normalize_rel(anchor.get("rel"))
        target_attr = anchor.get("target")
        target = target_attr[0] if isinstance(target_attr, list) else target_attr
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
        action_attr = form.get("action") or ""
        if not action_attr:
            continue
        # BeautifulSoup can return list for multi-value attrs; take first if list
        raw_action = action_attr[0] if isinstance(action_attr, list) else action_attr
        action = _resolve_href(str(raw_action), effective_base)
        label_attr = form.get("aria-label") or form.get("name") or "[form]"
        label = label_attr[0] if isinstance(label_attr, list) else label_attr
        rel = _normalize_rel(form.get("rel"))
        target_attr = form.get("target")
        target = target_attr[0] if isinstance(target_attr, list) else target_attr
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


_MARKDOWN_LINK_RE = re.compile(r"(?<!!)\[([^\]]+)\]\(([^)\s]+)\)")


def _resolve_ocr_link_target(
    text: str,
    raw_href: str,
    *,
    base_url: str | None = None,
    dom_links: Sequence[LinkRecord] | None = None,
) -> str | None:
    href_value = raw_href.strip()
    if not href_value:
        return None
    if href_value.startswith(("http://", "https://")):
        return href_value

    norm_text = normalize_heading_text(text)
    if dom_links and norm_text:
        for dom_rec in dom_links:
            if normalize_heading_text(dom_rec.text) == norm_text and dom_rec.href:
                return dom_rec.href
            if dom_rec.href.endswith("/" + href_value.lstrip("./")):
                return dom_rec.href

    if base_url and (
        href_value.startswith(("/", "./", "../", "?"))
        or "." in href_value
        or "/" in href_value
        or normalize_heading_text(href_value) != norm_text
    ):
        return _resolve_href(href_value, base_url)

    return None


def resolve_markdown_links(
    markdown: str,
    *,
    base_url: str | None = None,
    dom_links: Sequence[LinkRecord] | None = None,
) -> str:
    """Resolve relative or placeholder `[Text](Text)` links in Markdown against DOM links and base_url."""
    if not markdown or (not base_url and not dom_links):
        return markdown

    def _replace(match: re.Match[str]) -> str:
        text, raw_href = match.groups()
        if raw_href.startswith(("http://", "https://", "#", "mailto:", "tel:")):
            return match.group(0)
        resolved = _resolve_ocr_link_target(
            text, raw_href, base_url=base_url, dom_links=dom_links
        )
        if resolved:
            return f"[{text}]({resolved})"
        return match.group(0)

    out_lines: list[str] = []
    in_fence = False
    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            in_fence = not in_fence
            out_lines.append(line)
            continue
        if in_fence:
            out_lines.append(line)
            continue
        out_lines.append(_MARKDOWN_LINK_RE.sub(_replace, line))
    return "\n".join(out_lines)


def extract_links_from_markdown(
    markdown: str,
    *,
    base_url: str | None = None,
    dom_links: Sequence[LinkRecord] | None = None,
) -> Sequence[LinkRecord]:
    """Heuristically parse Markdown links (e.g., `[text](https://example.com)`)."""

    if not markdown:
        return []
    records: list[LinkRecord] = []
    in_fence = False
    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if stripped == "## Links Appendix":
            break
        for match in _MARKDOWN_LINK_RE.finditer(line):
            text, raw_href = match.groups()
            resolved = _resolve_ocr_link_target(
                text,
                raw_href,
                base_url=base_url,
                dom_links=dom_links,
            )
            if not resolved:
                continue
            records.append(
                LinkRecord(
                    text=text.strip(),
                    href=resolved,
                    source="OCR",
                    delta="OCR only",
                    kind="markdown",
                    domain=_derive_domain(resolved),
                )
            )
    return records


def format_links_appendix(records: Sequence[LinkRecord]) -> str:
    """Format a `## Links Appendix` section with DOM-vs-OCR delta notes."""
    if not records:
        return ""
    matched = sum(1 for r in records if r.source == "DOM+OCR" and r.delta == "✓")
    mismatched = sum(
        1 for r in records if r.source == "DOM+OCR" and r.delta != "✓"
    )
    dom_only = sum(1 for r in records if r.source == "DOM")
    ocr_only = sum(1 for r in records if r.source == "OCR")

    lines = [
        "## Links Appendix",
        (
            f"<!-- links-appendix: total={len(records)}, matched={matched}, "
            f"mismatched={mismatched}, dom_only={dom_only}, ocr_only={ocr_only} -->"
        ),
    ]
    for rec in records:
        label = (rec.text or rec.href).strip() or rec.href
        lines.append(f"- [{label}]({rec.href}) — source: {rec.source} ({rec.delta})")
    return "\n".join(lines)


def append_links_appendix(markdown: str, records: Sequence[LinkRecord]) -> str:
    """Append the `## Links Appendix` section to stitched Markdown when links exist."""
    if not records:
        return markdown
    if "## Links Appendix" in markdown:
        return markdown
    appendix = format_links_appendix(records)
    if not appendix:
        return markdown
    base = markdown.rstrip()
    if not base:
        return appendix
    return f"{base}\n\n{appendix}"


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
        LinkRecord(
            text="Support",
            href="https://example.com/support",
            source="DOM",
            delta="✓",
            domain="example.com",
        ),
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
