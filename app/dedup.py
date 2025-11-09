"""Tile overlap deduplication using multi-tier matching strategy.

This module implements a research-backed approach to removing duplicate content
from overlapping OCR tiles, based on Google's text stitching patents and proven
diff algorithms.

Multi-tier strategy:
1. Pixel-level overlap detection (SHA256 hashes)
2. Exact line matching (O(n) - fast path)
3. Sequence matching (O(n*m) - difflib.SequenceMatcher)
4. Fuzzy line-by-line matching (O(n*c) - fallback)
"""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.tiler import TileSlice


@dataclass(slots=True)
class DeduplicationResult:
    """Result of overlap deduplication attempt."""

    lines_removed: int
    method: str  # "disabled", "no_overlap", "exact", "sequence", "fuzzy", "no_match", etc.
    similarity: float | None
    prev_tile_index: int
    curr_tile_index: int
    overlap_hash: str | None


def deduplicate_tile_overlap(
    prev_lines: list[str],
    curr_lines: list[str],
    prev_tile: "TileSlice",
    curr_tile: "TileSlice",
    *,
    enabled: bool = True,
    min_overlap_lines: int = 2,
    sequence_similarity_threshold: float = 0.90,
    fuzzy_line_threshold: float = 0.85,
    max_search_window: int = 40,
) -> tuple[list[str], DeduplicationResult]:
    """
    Remove duplicate content from overlapping tiles using multi-tier strategy.

    This function implements a conservative, research-backed approach:
    1. Only deduplicates when pixel-level overlap is confirmed
    2. Tries exact matching first (fast path)
    3. Falls back to sequence matching for OCR variations
    4. Finally tries fuzzy line-by-line matching
    5. Never removes more than 3x estimated overlap (safety)

    Args:
        prev_lines: Lines from previous tile's markdown
        curr_lines: Lines from current tile's markdown
        prev_tile: Previous tile metadata (for overlap detection)
        curr_tile: Current tile metadata (for overlap detection)
        enabled: Whether deduplication is enabled globally
        min_overlap_lines: Minimum lines required to attempt deduplication
        sequence_similarity_threshold: Minimum similarity for sequence matching (0.0-1.0)
        fuzzy_line_threshold: Minimum similarity for fuzzy line matching (0.0-1.0)
        max_search_window: Maximum lines to search in each tile

    Returns:
        (deduplicated_lines, deduplication_result)
        - deduplicated_lines: Current tile lines with duplicates removed
        - deduplication_result: Metadata about what was removed and how
    """

    # Fast exit: deduplication disabled
    if not enabled:
        return curr_lines, DeduplicationResult(
            lines_removed=0,
            method="disabled",
            similarity=None,
            prev_tile_index=prev_tile.index,
            curr_tile_index=curr_tile.index,
            overlap_hash=None,
        )

    # Tier 1: Check pixel-level overlap
    overlap_match, _ = _tiles_share_overlap(prev_tile, curr_tile)
    if not overlap_match:
        return curr_lines, DeduplicationResult(
            lines_removed=0,
            method="no_overlap",
            similarity=None,
            prev_tile_index=prev_tile.index,
            curr_tile_index=curr_tile.index,
            overlap_hash=None,
        )

    overlap_hash = prev_tile.bottom_overlap_sha256 or curr_tile.top_overlap_sha256

    # Fast exit: not enough content to deduplicate
    if len(prev_lines) < min_overlap_lines or len(curr_lines) < min_overlap_lines:
        return curr_lines, DeduplicationResult(
            lines_removed=0,
            method="insufficient_content",
            similarity=None,
            prev_tile_index=prev_tile.index,
            curr_tile_index=curr_tile.index,
            overlap_hash=overlap_hash,
        )

    # Estimate overlap size in lines
    est_overlap_lines = _estimate_overlap_lines(prev_tile, curr_tile)
    search_window = min(max(est_overlap_lines * 3, 15), max_search_window)

    # Extract search windows (tail of prev, head of curr)
    prev_tail = prev_lines[-search_window:] if len(prev_lines) > search_window else prev_lines
    curr_head = curr_lines[:search_window] if len(curr_lines) > search_window else curr_lines

    # Safety: max removable lines (never remove more than 3x estimated)
    max_removable = est_overlap_lines * 3

    # Tier 2: Try exact matching (fast path)
    exact_match_size = _find_exact_boundary_match(prev_tail, curr_head)
    if exact_match_size >= min_overlap_lines and exact_match_size <= max_removable:
        deduplicated = curr_lines[exact_match_size:]
        return deduplicated, DeduplicationResult(
            lines_removed=exact_match_size,
            method="exact",
            similarity=1.0,
            prev_tile_index=prev_tile.index,
            curr_tile_index=curr_tile.index,
            overlap_hash=overlap_hash,
        )

    # Tier 3: Try sequence matching (main path)
    sequence_result = _find_overlap_sequence_matching(
        prev_tail,
        curr_head,
        min_match_lines=min_overlap_lines,
        min_similarity=sequence_similarity_threshold,
    )
    if sequence_result:
        match_size, similarity = sequence_result
        # Validate: reasonable size and high confidence
        if match_size <= max_removable:
            deduplicated = curr_lines[match_size:]
            return deduplicated, DeduplicationResult(
                lines_removed=match_size,
                method="sequence",
                similarity=similarity,
                prev_tile_index=prev_tile.index,
                curr_tile_index=curr_tile.index,
                overlap_hash=overlap_hash,
            )

    # Tier 4: Try fuzzy line matching (fallback)
    fuzzy_match_size = _find_overlap_fuzzy(
        prev_tail,
        curr_head,
        line_threshold=fuzzy_line_threshold,
        min_match_lines=min_overlap_lines,
    )
    if fuzzy_match_size and fuzzy_match_size <= max_removable:
        deduplicated = curr_lines[fuzzy_match_size:]
        return deduplicated, DeduplicationResult(
            lines_removed=fuzzy_match_size,
            method="fuzzy",
            similarity=fuzzy_line_threshold,  # Approximate average
            prev_tile_index=prev_tile.index,
            curr_tile_index=curr_tile.index,
            overlap_hash=overlap_hash,
        )

    # No confident match found - keep everything
    return curr_lines, DeduplicationResult(
        lines_removed=0,
        method="no_match",
        similarity=None,
        prev_tile_index=prev_tile.index,
        curr_tile_index=curr_tile.index,
        overlap_hash=overlap_hash,
    )


def _tiles_share_overlap(prev_tile: "TileSlice" | None, tile: "TileSlice" | None) -> tuple[bool, bool]:
    """
    Check if tiles share pixel-level overlap via SHA256 hash comparison.

    This is the gate-keeper function - we only deduplicate when pixel hashes
    confirm that the tiles actually overlap.

    Returns:
        (has_overlap, used_seam_marker)
        - has_overlap: True if tiles share overlapping pixels
        - used_seam_marker: True if match was via seam marker (more robust)
    """
    if not prev_tile or not tile:
        return False, False

    # Method 1: Direct pixel hash comparison of overlap regions
    if prev_tile.bottom_overlap_sha256 and tile.top_overlap_sha256:
        if prev_tile.bottom_overlap_sha256 == tile.top_overlap_sha256:
            return True, False

    # Method 2: Seam marker hash comparison (watermark-based, more robust)
    if prev_tile.seam_bottom_hash and tile.seam_top_hash:
        if prev_tile.seam_bottom_hash == tile.seam_top_hash:
            return True, True

    return False, False


def _estimate_overlap_lines(prev_tile: "TileSlice", curr_tile: "TileSlice") -> int:
    """
    Estimate number of lines in overlap region based on pixel overlap.

    Uses heuristic: typical line height is ~25px at 2x device scale factor.

    Args:
        prev_tile: Previous tile metadata
        curr_tile: Current tile metadata

    Returns:
        Estimated number of lines in overlap region (minimum 3)
    """
    # Overlap in pixels
    overlap_px = prev_tile.overlap_px

    # Estimate lines per pixel (rough heuristic)
    # Typical line height: ~20-30px at 2x scale factor
    # Conservative estimate: 25px per line
    estimated_lines = max(int(overlap_px / 25), 3)

    return estimated_lines


def _find_exact_boundary_match(prev_tail: list[str], curr_head: list[str]) -> int:
    """
    Find exact matching lines at tile boundary (fast path).

    Finds the longest sequence where:
    - End of prev_tail matches start of curr_head

    For example:
    - prev_tail = ["A", "B", "C", "D"]
    - curr_head = ["C", "D", "E", "F"]
    - Returns 2 (C and D match)

    Args:
        prev_tail: Last N lines of previous tile
        curr_head: First N lines of current tile

    Returns:
        Number of exactly matching lines at boundary
    """
    max_match = 0
    max_compare = min(len(prev_tail), len(curr_head))

    # Try different overlap sizes, from largest to smallest
    for overlap_size in range(max_compare, 0, -1):
        # Check if last overlap_size lines of prev match first overlap_size lines of curr
        prev_segment = [line.rstrip() for line in prev_tail[-overlap_size:]]
        curr_segment = [line.rstrip() for line in curr_head[:overlap_size]]

        if prev_segment == curr_segment:
            max_match = overlap_size
            break  # Found longest match

    return max_match


def _find_overlap_sequence_matching(
    prev_tail: list[str],
    curr_head: list[str],
    *,
    min_match_lines: int,
    min_similarity: float,
) -> tuple[int, float] | None:
    """
    Find overlap using average similarity across the boundary region.

    This tier is more lenient than fuzzy matching - it accepts blocks where
    the AVERAGE similarity is high, even if individual lines have OCR errors.
    This handles cases like:
      - Most lines match perfectly
      - One line has significant OCR corruption
      - Average similarity is still high

    Difference from fuzzy matching:
      - Fuzzy: Requires EVERY line to meet threshold (strict)
      - Sequence: Requires AVERAGE to meet threshold (lenient)

    Args:
        prev_tail: Last N lines of previous tile
        curr_head: First N lines of current tile
        min_match_lines: Minimum lines required for a valid match
        min_similarity: Minimum average similarity ratio (0.0-1.0)

    Returns:
        (match_size, avg_similarity) if found, None otherwise
    """
    max_compare = min(len(prev_tail), len(curr_head))
    best_match: tuple[int, float] | None = None

    # Try different overlap sizes, from largest to smallest
    for overlap_size in range(max_compare, 0, -1):
        if overlap_size < min_match_lines:
            break  # No point trying smaller sizes

        # Extract boundary segments
        prev_segment = prev_tail[-overlap_size:]
        curr_segment = curr_head[:overlap_size]

        # Calculate average line-by-line similarity
        total_similarity = 0.0
        for prev_line, curr_line in zip(prev_segment, curr_segment):
            # Strip whitespace for comparison
            p_stripped = prev_line.strip()
            c_stripped = curr_line.strip()

            # Handle empty lines
            if not p_stripped and not c_stripped:
                # Both empty - perfect match
                total_similarity += 1.0
            elif not p_stripped or not c_stripped:
                # One empty, one not - no similarity
                total_similarity += 0.0
            else:
                # Both have content - fuzzy compare
                line_sim = SequenceMatcher(None, p_stripped, c_stripped).ratio()
                total_similarity += line_sim

        avg_similarity = total_similarity / overlap_size

        # Check if average similarity meets threshold
        if avg_similarity >= min_similarity:
            best_match = (overlap_size, avg_similarity)
            break  # Found longest match with sufficient average similarity

    return best_match


def _find_overlap_fuzzy(
    prev_tail: list[str],
    curr_head: list[str],
    *,
    line_threshold: float,
    min_match_lines: int,
) -> int | None:
    """
    Find overlap using fuzzy line-by-line comparison (fallback method).

    Searches for the longest sequence where end of prev_tail matches
    start of curr_head with fuzzy line matching. More STRICT than
    sequence matching because it requires EVERY line to meet the
    threshold individually.

    Difference from sequence matching:
      - Sequence: AVERAGE similarity >= threshold (lenient)
      - Fuzzy: EVERY line similarity >= threshold (strict)

    Args:
        prev_tail: Last N lines of previous tile
        curr_head: First N lines of current tile
        line_threshold: Minimum similarity per line (0.0-1.0)
        min_match_lines: Minimum lines required for valid match

    Returns:
        Number of matching lines if >= min_match_lines, None otherwise
    """
    max_compare = min(len(prev_tail), len(curr_head))
    best_match = 0

    # Try different overlap sizes
    for overlap_size in range(max_compare, 0, -1):
        if overlap_size < min_match_lines:
            break  # No point trying smaller sizes

        # Check if this overlap size matches fuzzily
        all_match = True
        for i in range(overlap_size):
            prev_line = prev_tail[-(overlap_size - i)].strip()
            curr_line = curr_head[i].strip()

            # Empty lines match each other
            if not prev_line and not curr_line:
                continue

            # One empty, one not → no match
            if not prev_line or not curr_line:
                all_match = False
                break

            # Fuzzy compare
            similarity = SequenceMatcher(None, prev_line, curr_line).ratio()
            if similarity < line_threshold:
                all_match = False
                break

        if all_match:
            best_match = overlap_size
            break  # Found longest fuzzy match

    return best_match if best_match >= min_match_lines else None
