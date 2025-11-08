"""Stitch OCR chunks into Markdown with provenance markers."""

from __future__ import annotations

import re
from typing import Sequence

from app.tiler import TileSlice

_HEADING_RE = re.compile(r"^(#{1,6})(\s+.+)")


def stitch_markdown(chunks: Sequence[str], tiles: Sequence[TileSlice] | None = None) -> str:
    """Join OCR-derived Markdown segments with provenance + light normalization."""

    if not chunks:
        return ""

    processed: list[str] = []
    last_heading_level = 0
    last_table_signature: str | None = None

    for idx, chunk in enumerate(chunks):
        lines = _split_lines(chunk)
        lines, last_heading_level, heading_changes = _normalize_headings(lines, last_heading_level)
        lines, last_table_signature, table_trimmed = _trim_duplicate_table_header(lines, last_table_signature)

        tile = tiles[idx] if tiles and idx < len(tiles) else None
        if tile:
            processed.append(_format_provenance(tile))
        for original in heading_changes:
            processed.append(f"<!-- normalized-heading: {original} -->")
        if table_trimmed:
            processed.append("<!-- table-header-trimmed -->")

        body = "\n".join(lines).strip()
        if body:
            processed.append(body)

    return "\n\n".join(processed)


def _split_lines(chunk: str) -> list[str]:
    if not chunk:
        return []
    return chunk.splitlines()


def _normalize_headings(
    lines: list[str],
    last_level: int,
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
        target_level = level
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
) -> tuple[list[str], str | None, bool]:
    """Drop repeated Markdown table header rows emitted across tiles."""

    header_signature = _extract_table_header_signature(lines)
    if header_signature and header_signature == last_signature:
        trimmed = lines[2:]
        return trimmed, header_signature, True
    if header_signature:
        return lines, header_signature, False
    return lines, last_signature, False


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


def _format_provenance(tile: TileSlice) -> str:
    return (
        f"<!-- source: tile_{tile.index:04d}, "
        f"y={tile.viewport_y_offset}, sha256={tile.sha256}, scale={tile.scale:.2f} -->"
    )
