"""Stitch OCR chunks into Markdown with provenance + DOM assist markers."""

from __future__ import annotations

from dataclasses import dataclass
import difflib
import re
from typing import Sequence
from urllib.parse import quote_plus

from app.dom_links import DomHeading, DomTextOverlay, normalize_heading_text
from app.tiler import TileSlice

_HEADING_RE = re.compile(r"^(#{1,6})(\s+.+)")


@dataclass(slots=True)
class DomAssistEntry:
    tile_index: int
    line: int
    reason: str
    dom_text: str
    original_text: str


@dataclass(slots=True)
class StitchResult:
    markdown: str
    dom_assists: list[DomAssistEntry]


@dataclass(slots=True)
class TrimmedHeaderInfo:
    reason: str
    similarity: float | None = None


class HeadingGuide:
    """DOM-aware helper that aligns OCR headings with the source outline."""

    def __init__(self, headings: Sequence[DomHeading]) -> None:
        self._headings = list(headings)
        self._cursor = 0

    def target_level(self, heading_text: str) -> int | None:
        normalized = normalize_heading_text(heading_text)
        if not normalized:
            return None
        for idx in range(self._cursor, len(self._headings)):
            candidate = self._headings[idx]
            if candidate.normalized == normalized:
                self._cursor = idx + 1
                return candidate.level
        return None


class DomOverlayIndex:
    def __init__(self, overlays: Sequence[DomTextOverlay] | None) -> None:
        self._map: dict[str, DomTextOverlay] = {}
        if overlays:
            for entry in overlays:
                self._map.setdefault(entry.normalized, entry)

    def lookup(self, normalized: str) -> DomTextOverlay | None:
        if not normalized:
            return None
        return self._map.get(normalized)


def stitch_markdown(
    chunks: Sequence[str],
    tiles: Sequence[TileSlice] | None = None,
    *,
    dom_headings: Sequence[DomHeading] | None = None,
    dom_overlays: Sequence[DomTextOverlay] | None = None,
    job_id: str | None = None,
) -> StitchResult:
    """Join OCR-derived Markdown segments with provenance + DOM assists."""

    if not chunks:
        return StitchResult(markdown="", dom_assists=[])

    processed: list[str] = []
    last_heading_level = 0
    last_table_signature: str | None = None
    previous_tile: TileSlice | None = None
    heading_guide = HeadingGuide(dom_headings) if dom_headings else None
    overlay_index = DomOverlayIndex(dom_overlays)
    dom_assists: list[DomAssistEntry] = []

    for idx, chunk in enumerate(chunks):
        lines = _split_lines(chunk)
        tile = tiles[idx] if tiles and idx < len(tiles) else None
        lines, last_heading_level, heading_changes = _normalize_headings(
            lines, last_heading_level, heading_guide
        )
        lines, last_table_signature, table_trim = _trim_duplicate_table_header(
            lines,
            last_table_signature,
            previous_tile,
            tile,
        )

        if overlay_index:
            lines, assists = _apply_dom_overlays(lines, overlay_index, tile_index=tile.index if tile else idx)
            if assists:
                dom_assists.extend(assists)
                for assist in assists:
                    processed.append(_format_dom_assist_comment(assist))

        if tile and previous_tile and _tiles_share_overlap(previous_tile, tile):
            processed.append(_format_seam_marker(previous_tile, tile))
        if tile:
            processed.append(_format_provenance(tile, job_id=job_id))
        for original in heading_changes:
            processed.append(f"<!-- normalized-heading: {original} -->")
        if table_trim:
            processed.append(_format_table_trim_comment(table_trim))

        body = "\n".join(lines).strip()
        if body:
            processed.append(body)
        previous_tile = tile if tile else previous_tile

    return StitchResult(markdown="\n\n".join(processed), dom_assists=dom_assists)


def _split_lines(chunk: str) -> list[str]:
    if not chunk:
        return []
    return chunk.splitlines()


def _normalize_headings(
    lines: list[str],
    last_level: int,
    guide: HeadingGuide | None,
) -> tuple[list[str], int, list[str]]:
    """Clamp heading jumps to ±1 level and record the original line."""

    normalized: list[str] = []
    changed_headings: list[str] = []
    for line in lines:
        match = _HEADING_RE.match(line.strip())
        if not match:
            normalized.append(line)
            continue
        level = len(match.group(1))
        heading_text = match.group(2).strip()
        dom_level = guide.target_level(heading_text) if guide else None
        target_level = dom_level or level
        if dom_level is None:
            if last_level:
                target_level = min(level, last_level + 1)
            else:
                target_level = min(level, 2)
        if target_level != level:
            hashes = "#" * target_level
            remainder = match.group(2)
            normalized.append(f"{hashes}{remainder}")
            changed_headings.append(line.strip())
            last_level = target_level
        else:
            normalized.append(line)
            last_level = level
    return normalized, last_level, changed_headings


def _trim_duplicate_table_header(
    lines: list[str],
    last_signature: str | None,
    prev_tile: TileSlice | None,
    tile: TileSlice | None,
) -> tuple[list[str], str | None, TrimmedHeaderInfo | None]:
    """Drop repeated Markdown table header rows emitted across tiles."""

    header_signature = _extract_table_header_signature(lines)
    if not header_signature:
        return lines, last_signature, None

    overlap_match = _tiles_share_overlap(prev_tile, tile)
    identical = header_signature == last_signature and overlap_match
    trimmed_info: TrimmedHeaderInfo | None = None

    if identical:
        trimmed = lines[2:]
        trimmed_info = TrimmedHeaderInfo(reason="identical")
        return trimmed, header_signature, trimmed_info

    similarity = None
    if overlap_match and last_signature:
        similarity = _header_similarity(header_signature, last_signature)
        if similarity >= 0.92:
            trimmed = lines[2:]
            trimmed_info = TrimmedHeaderInfo(reason="similar", similarity=similarity)
            return trimmed, header_signature, trimmed_info

    return lines, header_signature, None


def _extract_table_header_signature(lines: list[str]) -> str | None:
    if len(lines) < 2:
        return None
    header = lines[0].strip()
    separator = lines[1].strip()
    if "|" not in header or "|" not in separator:
        return None
    if "---" not in separator:
        return None
    return f"{header}\n{separator}"


def _header_similarity(sig_a: str, sig_b: str) -> float:
    return difflib.SequenceMatcher(a=sig_a, b=sig_b).ratio()


def _tiles_share_overlap(prev_tile: TileSlice | None, tile: TileSlice | None) -> bool:
    if not prev_tile or not tile:
        return False
    if not prev_tile.bottom_overlap_sha256 or not tile.top_overlap_sha256:
        return False
    return prev_tile.bottom_overlap_sha256 == tile.top_overlap_sha256


def _format_seam_marker(prev_tile: TileSlice, tile: TileSlice) -> str:
    overlap_hash = prev_tile.bottom_overlap_sha256 or tile.top_overlap_sha256 or "unknown"
    return (
        "<!-- seam-marker: "
        f"prev=tile_{prev_tile.index:04d}, curr=tile_{tile.index:04d}, overlap_hash={overlap_hash}"
        " -->"
    )


def _format_provenance(tile: TileSlice, *, job_id: str | None = None) -> str:
    path = f"artifact/tiles/tile_{tile.index:04d}.png"
    parts = [
        f"tile_{tile.index:04d}",
        f"y={tile.source_y_offset}",
        f"height={tile.height}",
        f"sha256={tile.sha256}",
        f"scale={tile.scale:.2f}",
        f"viewport_y={tile.viewport_y_offset}",
        f"overlap_px={tile.overlap_px}",
        f"path={path}",
    ]
    if job_id:
        highlight = _build_highlight_url(job_id=job_id, tile_path=path, start=0, end=tile.height)
        parts.append(f"highlight={highlight}")
    return f"<!-- source: {', '.join(parts)} -->"


def _build_highlight_url(*, job_id: str, tile_path: str, start: int, end: int) -> str:
    start = max(0, start)
    end = max(start + 1, end)
    query = f"tile={quote_plus(tile_path)}&y0={start}&y1={end}"
    return f"/jobs/{job_id}/artifact/highlight?{query}"


def _apply_dom_overlays(
    lines: list[str],
    overlay_index: DomOverlayIndex,
    *,
    tile_index: int,
) -> tuple[list[str], list[DomAssistEntry]]:
    updated: list[str] = []
    assists: list[DomAssistEntry] = []
    for line_idx, line in enumerate(lines):
        next_line = lines[line_idx + 1] if line_idx + 1 < len(lines) else None
        reason = _line_issue(line, next_line)
        if reason:
            stripped = line.lstrip("# ")
            normalized = normalize_heading_text(stripped)
            overlay = overlay_index.lookup(normalized)
            if overlay:
                replacement = _merge_overlay(line, overlay.text)
                updated.append(replacement)
                assists.append(
                    DomAssistEntry(
                        tile_index=tile_index,
                        line=line_idx,
                        reason=reason,
                        dom_text=overlay.text,
                        original_text=line.strip(),
                    )
                )
                continue
        updated.append(line)
    return updated, assists


def _line_issue(line: str, next_line: str | None) -> str | None:
    stripped = line.strip()
    if not stripped:
        return None
    if "�" in stripped:
        return "replacement-char"
    noisy = sum(1 for char in stripped if char in "!?…")
    if noisy >= 3:
        return "punctuation"
    if any(char.isdigit() for char in stripped) and any(char.isalpha() for char in stripped):
        return "mixed-numeric"
    alpha = sum(1 for char in stripped if char.isalpha())
    ratio = alpha / max(1, len(stripped))
    if ratio < 0.45 and len(stripped) >= 6:
        return "low-alpha"
    if stripped.endswith("-") and next_line and next_line[:1].islower():
        return "hyphen-break"
    return None


def _merge_overlay(line: str, dom_text: str) -> str:
    prefix = ""
    stripped = line.lstrip()
    if stripped.startswith("#"):
        hashes, _, remainder = stripped.partition(" ")
        prefix = f"{hashes} "
    return f"{prefix}{dom_text}"


def _format_dom_assist_comment(entry: DomAssistEntry) -> str:
    return (
        f"<!-- dom-assist: tile={entry.tile_index}, line={entry.line}, reason={entry.reason}, "
        f"replacement={entry.dom_text!r} -->"
    )


def _format_table_trim_comment(info: TrimmedHeaderInfo) -> str:
    parts = ["table-header-trimmed", f"reason={info.reason}"]
    if info.similarity is not None:
        parts.append(f"similarity={info.similarity:.2f}")
    return f"<!-- {' '.join(parts)} -->"
