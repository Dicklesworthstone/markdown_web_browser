"""Stitch OCR chunks into Markdown with provenance + DOM assist markers."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import difflib
import re
from typing import Sequence
from urllib.parse import quote_plus

from app.dedup import (
    DeduplicationResult,
    _tiles_share_overlap as _dedup_tiles_share_overlap,
    deduplicate_tile_overlap,
)
from app.dom_links import DomHeading, DomTextOverlay, normalize_heading_text
from app.settings import get_settings
from app.tiler import TileSlice

_HEADING_RE = re.compile(r"^(#{1,6})(\s+.+)")
_HEADING_PREFIX_RE = re.compile(r"^\s*(?:>+\s*)*(?:#{1,6}\s+)")
_ORDERED_LIST_PREFIX_RE = re.compile(r"^\s*(?:>+\s*)*(?:\d+\.\s+)")
_UNORDERED_LIST_PREFIX_RE = re.compile(r"^\s*(?:>+\s*)*(?:[-+*]\s+)")
_BLOCKQUOTE_PREFIX_RE = re.compile(r"^\s*>+\s*")
_LEADING_WS_RE = re.compile(r"^\s+")
_SPACED_LETTERS_RE = re.compile(r"(?:[A-Za-z]\s+){3,}[A-Za-z]")
_CODE_FENCE_PREFIXES = ("```", "~~~")

_OLMOCR_FRONTMATTER_KEYS = frozenset(
    {
        "primary_language",
        "is_rotation_valid",
        "rotation_correction",
        "is_table",
        "is_diagram",
        "natural_text",
    }
)
_YAML_KEY_LINE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$")
_BLANK_TILE_PROSE_RE = re.compile(
    r"^(?:"
    r"(?:this|the)\s+(?:image|page|tile|screenshot|document)\s+"
    r"(?:appears\s+to\s+be\s+|is\s+|seems\s+to\s+be\s+)?"
    r"(?:completely\s+|entirely\s+)?(?:blank|empty|white|void)"
    r"(?:\s+with\s+no\s+visible\s+(?:text|content))?\.?"
    r"|(?:there\s+is\s+)?no\s+(?:visible\s+)?(?:text|content)\s+"
    r"(?:is\s+visible\s+|found\s+|present\s+)?in\s+this\s+(?:image|page|tile|screenshot)\.?"
    r")$",
    re.IGNORECASE,
)
_DELEET_TABLE = str.maketrans(
    {
        "0": "o",
        "1": "l",
        "3": "e",
        "4": "a",
        "5": "s",
        "7": "t",
        "8": "b",
    }
)


def _deleet_text(text: str) -> str:
    """Normalize common OCR digit-for-letter substitutions inside words."""
    tokens: list[str] = []
    for tok in text.split():
        if any(c.isdigit() for c in tok) and any(c.isalpha() for c in tok):
            tokens.append(tok.translate(_DELEET_TABLE))
        else:
            tokens.append(tok)
    return " ".join(tokens)


def clean_ocr_chunk(chunk: str) -> str:
    """Strip olmOCR YAML front-matter scaffolding and blank-tile refusal prose.

    Preserves legitimate user YAML front-matter (any block containing keys outside
    the olmOCR metadata schema), fenced code blocks, and Markdown horizontal rules.
    """
    if not chunk or not chunk.strip():
        return ""

    text = chunk.strip("\r\n")
    lines = text.splitlines()

    # Check if the entire chunk is wrapped in an outer ```markdown / ```yaml fence
    # whose inner content starts with olmOCR YAML front-matter.
    if (
        len(lines) >= 4
        and lines[0].strip().lower() in ("```markdown", "```md", "```yaml", "```")
        and lines[-1].strip() == "```"
    ):
        inner_lines = lines[1:-1]
        if inner_lines and inner_lines[0].strip() == "---":
            cleaned_inner = clean_ocr_chunk("\n".join(inner_lines))
            if cleaned_inner != "\n".join(inner_lines):
                return cleaned_inner

    # Strip leading olmOCR YAML front-matter if present
    start_idx = 0
    while start_idx < len(lines) and not lines[start_idx].strip():
        start_idx += 1

    if start_idx < len(lines) and lines[start_idx].strip() == "---":
        end_idx: int | None = None
        keys_seen: list[str] = []
        all_olmocr_keys = True
        in_multiline_natural = False
        natural_lines: list[str] = []

        for i in range(start_idx + 1, len(lines)):
            raw_line = lines[i]
            stripped = raw_line.strip()
            if stripped == "---":
                end_idx = i
                break
            if not stripped:
                if in_multiline_natural:
                    natural_lines.append("")
                continue
            if in_multiline_natural and (raw_line.startswith(" ") or raw_line.startswith("\t")):
                natural_lines.append(raw_line.lstrip())
                continue
            in_multiline_natural = False
            match = _YAML_KEY_LINE_RE.match(stripped)
            if not match:
                all_olmocr_keys = False
                break
            key, val = match.group(1), match.group(2).strip()
            if key not in _OLMOCR_FRONTMATTER_KEYS:
                all_olmocr_keys = False
                break
            keys_seen.append(key)
            if key == "natural_text":
                if val in ("|", ">", "|-", ">-"):
                    in_multiline_natural = True
                elif val and val.lower() not in ("null", "none", "~", '""', "''"):
                    unquoted = val.strip("\"'")
                    if unquoted:
                        natural_lines.append(unquoted)

        if end_idx is not None and all_olmocr_keys and keys_seen:
            remaining = "\n".join(lines[end_idx + 1 :]).strip()
            if remaining:
                text = remaining
            elif natural_lines:
                text = "\n".join(natural_lines).strip()
            else:
                text = ""
        elif end_idx is None and all_olmocr_keys and keys_seen and set(keys_seen) <= _OLMOCR_FRONTMATTER_KEYS:
            # Unclosed olmOCR YAML header with no body (e.g. truncated after natural_text: null)
            if len(keys_seen) >= 2:
                text = "\n".join(natural_lines).strip() if natural_lines else ""

    stripped_body = text.strip()
    if not stripped_body:
        return ""
    if stripped_body.lower() in ("null", "none"):
        return ""
    if "\n" not in stripped_body and _BLANK_TILE_PROSE_RE.match(stripped_body):
        return ""

    return text


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
    dedup_events: list[DeduplicationResult] = field(default_factory=list)


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


def _overlay_matches_tile_bounds(overlay: DomTextOverlay, tile: TileSlice | None) -> bool:
    """Return True if overlay has no y coordinate or falls within tile's vertical span."""
    if tile is None or overlay.y is None:
        return True
    ov_y0 = float(overlay.y)
    ov_y1 = ov_y0 + max(0.0, float(overlay.height or 0.0))
    margin = float(max(60, tile.overlap_px))

    src_y0 = float(tile.source_y_offset) - margin
    src_span = float(max(tile.source_crop_height, tile.height))
    src_y1 = float(tile.source_y_offset) + src_span + margin
    if ov_y1 >= src_y0 and ov_y0 <= src_y1:
        return True

    if tile.device_scale_factor > 1.0:
        css_margin = margin / float(tile.device_scale_factor)
        css_y0 = float(tile.css_y_offset) - css_margin
        css_y1 = float(tile.css_y_offset + tile.css_height) + css_margin
        if ov_y1 >= css_y0 and ov_y0 <= css_y1:
            return True

    return False


class DomOverlayIndex:
    def __init__(self, overlays: Sequence[DomTextOverlay] | None) -> None:
        self._map: dict[str, deque[DomTextOverlay]] = {}
        if overlays:
            for entry in overlays:
                if not entry.normalized:
                    continue
                queue = self._map.setdefault(entry.normalized, deque())
                queue.append(entry)

    def _pop_matching_from_queue(
        self,
        key: str,
        queue: deque[DomTextOverlay],
        tile: TileSlice | None,
    ) -> DomTextOverlay | None:
        for idx, candidate in enumerate(queue):
            if _overlay_matches_tile_bounds(candidate, tile):
                del queue[idx]
                if not queue:
                    self._map.pop(key, None)
                return candidate
        return None

    def lookup(
        self,
        normalized: str,
        *,
        tile: TileSlice | None = None,
        allow_fuzzy: bool = False,
    ) -> DomTextOverlay | None:
        if not normalized:
            return None
        queue = self._map.get(normalized)
        if queue:
            hit = self._pop_matching_from_queue(normalized, queue, tile)
            if hit is not None:
                return hit

        if not allow_fuzzy:
            return None

        deleet_target = _deleet_text(normalized)
        if deleet_target != normalized:
            d_queue = self._map.get(deleet_target)
            if d_queue:
                hit = self._pop_matching_from_queue(deleet_target, d_queue, tile)
                if hit is not None:
                    return hit

        target_words = deleet_target.split()
        best_key: str | None = None
        best_ratio = 0.0
        for cand_key, cand_queue in list(self._map.items()):
            if not cand_queue:
                continue
            if not any(_overlay_matches_tile_bounds(c, tile) for c in cand_queue):
                continue
            cand_words = cand_key.split()
            if len(cand_words) != len(target_words):
                continue
            if abs(len(cand_key) - len(deleet_target)) > 3:
                continue
            ratio = difflib.SequenceMatcher(None, deleet_target, cand_key).ratio()
            if ratio >= 0.84 and ratio > best_ratio:
                best_ratio = ratio
                best_key = cand_key

        if best_key is not None:
            return self._pop_matching_from_queue(best_key, self._map[best_key], tile)
        return None


def stitch_markdown(
    chunks: Sequence[str],
    tiles: Sequence[TileSlice] | None = None,
    *,
    dom_headings: Sequence[DomHeading] | None = None,
    dom_overlays: Sequence[DomTextOverlay] | None = None,
    job_id: str | None = None,
    deduplicate_overlaps: bool | None = None,
) -> StitchResult:
    """Join OCR-derived Markdown segments with provenance + DOM assists."""

    if not chunks:
        return StitchResult(markdown="", dom_assists=[], seam_marker_events=[])

    # Get deduplication settings
    settings = get_settings()
    dedup_enabled = (
        deduplicate_overlaps if deduplicate_overlaps is not None else settings.deduplication.enabled
    )

    processed: list[str] = []
    last_heading_level = 0
    last_table_signature: str | None = None
    previous_tile: TileSlice | None = None
    previous_chunk: str = ""
    heading_guide = HeadingGuide(dom_headings) if dom_headings else None
    overlay_index = DomOverlayIndex(dom_overlays)
    dom_assists: list[DomAssistEntry] = []
    seam_events: list[SeamMarkerEvent] = []
    dedup_events: list[DeduplicationResult] = []

    for idx, raw_chunk in enumerate(chunks):
        chunk = clean_ocr_chunk(raw_chunk)
        lines = _split_lines(chunk)
        tile = tiles[idx] if tiles and idx < len(tiles) else None

        # Deduplicate overlaps before other processing
        if dedup_enabled and tile and previous_tile and previous_chunk:
            prev_lines = _split_lines(previous_chunk)
            lines, dedup_result = deduplicate_tile_overlap(
                prev_lines=prev_lines,
                curr_lines=lines,
                prev_tile=previous_tile,
                curr_tile=tile,
                enabled=True,
                min_overlap_lines=settings.deduplication.min_overlap_lines,
                sequence_similarity_threshold=settings.deduplication.sequence_similarity_threshold,
                fuzzy_line_threshold=settings.deduplication.fuzzy_line_threshold,
                max_search_window=settings.deduplication.max_search_window,
            )

            # Track all overlap deduplication attempts on overlapping tile pairs
            if dedup_result.method not in ("disabled", "no_overlap"):
                dedup_events.append(dedup_result)
            if dedup_result.lines_removed > 0 and settings.deduplication.log_events:
                processed.append(_format_dedup_comment(dedup_result))

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
            lines, assists = _apply_dom_overlays(
                lines,
                overlay_index,
                tile_index=tile.index if tile else idx,
                tile=tile,
            )
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
        if chunk and chunk.strip():
            previous_chunk = chunk  # Save cleaned chunk for next iteration's deduplication

    return StitchResult(
        markdown="\n\n".join(processed),
        dom_assists=dom_assists,
        seam_marker_events=seam_events,
        dedup_events=dedup_events,
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
    return _dedup_tiles_share_overlap(prev_tile, tile)


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
        "height_coordinate_space=tile_pixels",
        f"height_css={tile.height / ((tile.scale_y or tile.scale) * tile.device_scale_factor):.8g}",
        f"sha256={tile.sha256}",
        f"scale={tile.scale:.2f}",
        f"scale_x={tile.scale_x or tile.scale:.8g}",
        f"scale_y={tile.scale_y or tile.scale:.8g}",
        f"dpr={tile.device_scale_factor}",
        "coordinates=css_document",
        f"source_rect={tile.source_rect}",
        f"viewport_y={tile.viewport_y_offset}",
        f"css_y={tile.css_y_offset}",
        f"dpr={tile.device_scale_factor:g}",
        f"crop={tile.source_crop_x},{tile.source_crop_y},{tile.source_crop_width},{tile.source_crop_height}",
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


_BENIGN_ALNUM_TOKEN_RE = re.compile(
    r"^(?:"
    r"\d+(?:[.,:/-]\d+)*(?:st|nd|rd|th|px|ms|ns|us|s|m|h|d|w|y|yr|yrs|k|b|t|kb|mb|gb|tb|pb|hz|khz|mhz|ghz|fps|dpi|ppi|pt|pc|em|rem|vh|vw|vmin|vmax|deg|rad|x|p|c|f|v|a|mah|wh|kwh|bit|bits|byte|bytes|am|pm)?"
    r"|(?:v|ver|version|q|h|p|s|sec|step|part|ch|fig|tab|col|row|tier|level|lvl|phase|stage|sha|md|utf|iso|rfc|http|ipv|tls|oauth|web|fp|int|uint|bf|float|mp|ec|s3|k8s|i18n|l10n|a11y|x86|arm|b|rc|alpha|beta)\d+(?:[._-]\d+)*"
    r"|0x[0-9a-f]+"
    r"|[0-9a-f]{7,64}"
    r"|[a-z]{1,4}-\d+(?:[._-]\d+)*"
    r")$",
    re.IGNORECASE,
)


def _has_mixed_numeric_token(text: str) -> bool:
    """Return True only when an alphabetic word contains OCR-corrupted inline digits."""
    # Strip inline code spans and markdown links before checking tokens
    cleaned = re.sub(r"`[^`]*`", " ", text)
    cleaned = re.sub(r"https?://\S+", " ", cleaned)
    for raw_tok in cleaned.split():
        tok = raw_tok.strip(".,;:!?'\"()[]{}*_~<>|/#+-=$%^&")
        if len(tok) < 3:
            continue
        if not (any(c.isdigit() for c in tok) and any(c.isalpha() for c in tok)):
            continue
        if _BENIGN_ALNUM_TOKEN_RE.match(tok):
            continue
        return True
    return False


def _apply_dom_overlays(
    lines: list[str],
    overlay_index: DomOverlayIndex,
    *,
    tile_index: int,
    tile: TileSlice | None = None,
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
                allow_fuzzy = reason in ("mixed-numeric", "replacement-char", "low-alpha")
                overlay = overlay_index.lookup(
                    normalized,
                    tile=tile,
                    allow_fuzzy=allow_fuzzy,
                )
                if overlay:
                    replacement = _merge_overlay(line, overlay.text)
                    if not consume_next and replacement == line:
                        updated.append(line)
                        continue
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
    if "\ufffd" in stripped:
        return "replacement-char"
    if _SPACED_LETTERS_RE.search(stripped):
        return "spaced-letters"
    noisy = sum(1 for char in stripped if char in "!?…")
    if noisy >= 3:
        return "punctuation"
    if _has_mixed_numeric_token(stripped):
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
    for pattern in (
        _HEADING_PREFIX_RE,
        _ORDERED_LIST_PREFIX_RE,
        _UNORDERED_LIST_PREFIX_RE,
        _BLOCKQUOTE_PREFIX_RE,
    ):
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


def _format_dedup_comment(result: DeduplicationResult) -> str:
    """Format deduplication event as HTML comment."""
    parts = [
        "overlap-dedup:",
        f"prev=tile_{result.prev_tile_index:04d}",
        f"curr=tile_{result.curr_tile_index:04d}",
        f"removed={result.lines_removed}",
        f"method={result.method}",
    ]
    if result.similarity is not None:
        parts.append(f"similarity={result.similarity:.3f}")
    if result.overlap_hash:
        parts.append(f"hash={result.overlap_hash[:8]}")
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
