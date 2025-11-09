"""Stitch OCR chunks into Markdown with provenance + DOM assist markers."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import difflib
import re
from typing import Sequence
from urllib.parse import quote_plus

from app.dom_links import DomHeading, DomTextOverlay, normalize_heading_text
from app.tiler import TileSlice

_HEADING_RE = re.compile(r"^(#{1,6})(\s+.+)")
_HEADING_PREFIX_RE = re.compile(r"^\s*(?:>+\s*)*(?:#{1,6}\s+)")
_ORDERED_LIST_PREFIX_RE = re.compile(r"^\s*(?:>+\s*)*(?:\d+\.\s+)")
_UNORDERED_LIST_PREFIX_RE = re.compile(r"^\s*(?:>+\s*)*(?:[-+*]\s+)")
_BLOCKQUOTE_PREFIX_RE = re.compile(r"^\s*>+\s*")
_LEADING_WS_RE = re.compile(r"^\s+")
_SPACED_LETTERS_RE = re.compile(r"(?:[A-Za-z]\s+){3,}[A-Za-z]")
_CODE_FENCE_PREFIXES = ("```", "~~~")


@dataclass(slots=True)
class DomAssistEntry:
    tile_index: int
    line: int
    reason: str
    dom_text: str
    original_text: str


@dataclass(slots=True)
class SeamMarkerEvent:
    prev_tile_index: int
    curr_tile_index: int
    seam_hash: str | None
    prev_overlap_hash: str | None
    curr_overlap_hash: str | None


@dataclass(slots=True)
class StitchResult:
    markdown: str
    dom_assists: list[DomAssistEntry]
    seam_marker_events: list[SeamMarkerEvent]


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
        self._map: dict[str, deque[DomTextOverlay]] = {}
        if overlays:
            for entry in overlays:
                if not entry.normalized:
                    continue
                queue = self._map.setdefault(entry.normalized, deque())
                queue.append(entry)

    def lookup(self, normalized: str) -> DomTextOverlay | None:
        if not normalized:
            return None
        queue = self._map.get(normalized)
        if not queue:
            return None
        overlay = queue.popleft()
        if not queue:
            self._map.pop(normalized, None)
        return overlay


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
        return StitchResult(markdown="", dom_assists=[], seam_marker_events=[])

    processed: list[str] = []
    last_heading_level = 0
    last_table_signature: str | None = None
    previous_tile: TileSlice | None = None
    heading_guide = HeadingGuide(dom_headings) if dom_headings else None
    overlay_index = DomOverlayIndex(dom_overlays)
    dom_assists: list[DomAssistEntry] = []
    seam_events: list[SeamMarkerEvent] = []

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

        if tile and previous_tile:
            overlap_match, used_seam_marker = _tiles_share_overlap(previous_tile, tile)
            if overlap_match:
                if used_seam_marker:
                    seam_events.append(
                        SeamMarkerEvent(
                            prev_tile_index=previous_tile.index,
                            curr_tile_index=tile.index,
                            seam_hash=previous_tile.seam_bottom_hash or tile.seam_top_hash,
                            prev_overlap_hash=previous_tile.bottom_overlap_sha256,
                            curr_overlap_hash=tile.top_overlap_sha256,
                        )
                    )
                processed.append(_format_seam_marker(previous_tile, tile))
        if tile:
            processed.append(_format_provenance(tile, job_id=job_id))
        for original in heading_changes:
            processed.append(f"<!-- normalized-heading: {original} -->")
        if table_trim:
            processed.append(_format_table_trim_comment(table_trim))

        body = "\n".join(lines).strip("\n")
        if body and body.strip():
            processed.append(body)
        previous_tile = tile if tile else previous_tile

    return StitchResult(
        markdown="\n\n".join(processed),
        dom_assists=dom_assists,
        seam_marker_events=seam_events,
    )


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

    extraction = _extract_table_header_signature(lines)
    if not extraction:
        return lines, last_signature, None

    header_signature, header_start = extraction
    header_end = header_start + 2
    prefix = lines[:header_start]
    suffix = lines[header_end:]

    overlap_match, _ = _tiles_share_overlap(prev_tile, tile)
    identical = header_signature == last_signature and overlap_match
    trimmed_info: TrimmedHeaderInfo | None = None

    if identical:
        trimmed = prefix + suffix
        trimmed_info = TrimmedHeaderInfo(reason="identical")
        return trimmed, header_signature, trimmed_info

    similarity = None
    if overlap_match and last_signature:
        similarity = _header_similarity(header_signature, last_signature)
        if similarity >= 0.92:
            trimmed = prefix + suffix
            trimmed_info = TrimmedHeaderInfo(reason="similar", similarity=similarity)
            return trimmed, header_signature, trimmed_info

    return lines, header_signature, None


def _extract_table_header_signature(lines: list[str]) -> tuple[str, int] | None:
    start = _locate_table_header_start(lines)
    if start is None or start + 1 >= len(lines):
        return None
    header = lines[start].strip()
    separator = lines[start + 1].strip()
    if "|" not in header or "|" not in separator:
        return None
    if "---" not in separator:
        return None
    signature = f"{header}\n{separator}"
    return signature, start


def _locate_table_header_start(lines: list[str]) -> int | None:
    idx = 0
    limit = len(lines) - 1
    while idx < limit:
        stripped = lines[idx].strip()
        if not stripped:
            idx += 1
            continue
        if _is_inline_comment(stripped):
            idx += 1
            continue
        next_line = lines[idx + 1].strip()
        if "|" in stripped and "|" in next_line and "---" in next_line:
            return idx
        return None
    return None


def _is_inline_comment(line: str) -> bool:
    return line.startswith("<!--") and line.endswith("-->")


def _header_similarity(sig_a: str, sig_b: str) -> float:
    return difflib.SequenceMatcher(a=sig_a, b=sig_b).ratio()


def _tiles_share_overlap(prev_tile: TileSlice | None, tile: TileSlice | None) -> tuple[bool, bool]:
    if not prev_tile or not tile:
        return False, False
    if prev_tile.bottom_overlap_sha256 and tile.top_overlap_sha256:
        if prev_tile.bottom_overlap_sha256 == tile.top_overlap_sha256:
            return True, False
    if prev_tile.seam_bottom_hash and tile.seam_top_hash:
        if prev_tile.seam_bottom_hash == tile.seam_top_hash:
            return True, True
    return False, False


def _format_seam_marker(prev_tile: TileSlice, tile: TileSlice) -> str:
    overlap_hash = prev_tile.bottom_overlap_sha256 or tile.top_overlap_sha256 or "unknown"
    parts = [
        "<!-- seam-marker:",
        f"prev=tile_{prev_tile.index:04d}",
        f"curr=tile_{tile.index:04d}",
        f"overlap_hash={overlap_hash}",
    ]
    if prev_tile.seam_bottom_hash and tile.seam_top_hash:
        parts.append(f"seam_hash={prev_tile.seam_bottom_hash}")
    parts.append("-->")
    return " ".join(parts)


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
    skip_next = False
    in_code_fence = False
    code_delimiter: str | None = None
    for line_idx, line in enumerate(lines):
        if skip_next:
            skip_next = False
            continue
        stripped_line = line.strip()
        fence = _detect_code_fence(stripped_line)
        if fence:
            if in_code_fence and fence == code_delimiter:
                in_code_fence = False
                code_delimiter = None
            else:
                in_code_fence = True
                code_delimiter = fence
            updated.append(line)
            continue
        if in_code_fence or _is_indented_code_block(line):
            updated.append(line)
            continue
        next_line = lines[line_idx + 1] if line_idx + 1 < len(lines) else None
        reason = _line_issue(line, next_line)
        if reason:
            stripped = line.lstrip("# ").strip()
            normalized = ""
            consume_next = False
            original_text = line.strip()
            if stripped:
                if reason == "hyphen-break" and next_line:
                    suffix = next_line.lstrip()
                    combined = f"{stripped.rstrip('-')} {suffix}".strip()
                    normalized = normalize_heading_text(combined)
                    consume_next = bool(suffix)
                    original_tail = next_line.strip()
                    if original_tail:
                        original_text = f"{original_text} {original_tail}".strip()
                else:
                    if reason == "spaced-letters":
                        stripped_candidate = re.sub(r"\s+", "", stripped)
                        normalized = normalize_heading_text(stripped_candidate)
                    else:
                        normalized = normalize_heading_text(stripped)
            if normalized:
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
                            original_text=original_text,
                        )
                    )
                    if consume_next:
                        skip_next = True
                    continue
        updated.append(line)
    return updated, assists


def _line_issue(line: str, next_line: str | None) -> str | None:
    stripped = line.strip()
    if not stripped:
        return None
    if "�" in stripped:
        return "replacement-char"
    if _SPACED_LETTERS_RE.search(stripped):
        return "spaced-letters"
    noisy = sum(1 for char in stripped if char in "!?…")
    if noisy >= 3:
        return "punctuation"
    if any(char.isdigit() for char in stripped) and any(char.isalpha() for char in stripped):
        return "mixed-numeric"
    alpha = sum(1 for char in stripped if char.isalpha())
    ratio = alpha / max(1, len(stripped))
    if ratio < 0.45 and len(stripped) >= 6:
        return "low-alpha"
    if stripped.endswith("-") and next_line:
        next_token = next_line.lstrip()[:1]
        if next_token.islower():
            return "hyphen-break"
    return None


def _merge_overlay(line: str, dom_text: str) -> str:
    prefix = _extract_markdown_prefix(line)
    return f"{prefix}{dom_text}"


def _extract_markdown_prefix(line: str) -> str:
    for pattern in (_HEADING_PREFIX_RE, _ORDERED_LIST_PREFIX_RE, _UNORDERED_LIST_PREFIX_RE, _BLOCKQUOTE_PREFIX_RE):
        match = pattern.match(line)
        if match:
            return match.group(0)
    whitespace = _LEADING_WS_RE.match(line)
    return whitespace.group(0) if whitespace else ""


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


def _detect_code_fence(line: str) -> str | None:
    if not line:
        return None
    for prefix in _CODE_FENCE_PREFIXES:
        if line.startswith(prefix):
            return prefix
    return None


def _is_indented_code_block(line: str) -> bool:
    if not line:
        return False
    if line.startswith("\t"):
        return True
    if line.startswith("    "):
        stripped = line.lstrip()
        if stripped.startswith(("-", "*", "+")):
            return False
        if re.match(r"\d+\.", stripped):
            return False
        return True
    return False
