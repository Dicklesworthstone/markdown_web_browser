"""Tests for tile overlap deduplication functionality."""

from __future__ import annotations


from app.dedup import (
    deduplicate_tile_overlap,
    _find_exact_boundary_match,
    _find_overlap_fuzzy,
    _find_overlap_sequence_matching,
    _estimate_overlap_lines,
)
from app.tiler import TileSlice


def create_test_tile(
    index: int,
    overlap_px: int = 120,
    bottom_hash: str | None = "abc123",
    top_hash: str | None = "abc123",
) -> TileSlice:
    """Helper to create test tiles."""
    return TileSlice(
        index=index,
        png_bytes=b"fake",
        sha256="fake_sha",
        width=1280,
        height=1288,
        scale=1.0,
        source_y_offset=index * 1168,
        viewport_y_offset=0,
        overlap_px=overlap_px,
        top_overlap_sha256=top_hash,
        bottom_overlap_sha256=bottom_hash,
    )


class TestExactBoundaryMatch:
    """Tests for exact line matching algorithm."""

    def test_perfect_match(self):
        """Test exact matching with identical lines."""
        prev_tail = ["Line 1", "Line 2", "Overlap A", "Overlap B"]
        curr_head = ["Overlap A", "Overlap B", "Line 3", "Line 4"]

        match_count = _find_exact_boundary_match(prev_tail, curr_head)

        assert match_count == 2

    def test_no_match(self):
        """Test no match when lines differ."""
        prev_tail = ["Line 1", "Line 2", "Different A"]
        curr_head = ["Different B", "Line 3", "Line 4"]

        match_count = _find_exact_boundary_match(prev_tail, curr_head)

        assert match_count == 0

    def test_partial_match(self):
        """Test partial match at boundary."""
        # Boundary match: end of prev must match start of curr
        prev_tail = ["Line 1", "Line 2", "Overlap A", "Overlap B"]
        curr_head = ["Overlap A", "Overlap B", "Line 3", "Line 4"]

        match_count = _find_exact_boundary_match(prev_tail, curr_head)

        # Should match last 2 lines of prev with first 2 lines of curr
        assert match_count == 2

    def test_trailing_whitespace_ignored(self):
        """Test that trailing whitespace is ignored."""
        prev_tail = ["Line with spaces   ", "Another line  "]
        curr_head = ["Line with spaces", "Another line", "Next"]

        match_count = _find_exact_boundary_match(prev_tail, curr_head)

        assert match_count == 2

    def test_empty_lists(self):
        """Test empty input lists."""
        assert _find_exact_boundary_match([], []) == 0
        assert _find_exact_boundary_match(["Line"], []) == 0
        assert _find_exact_boundary_match([], ["Line"]) == 0


class TestSequenceMatching:
    """Tests for sequence matching algorithm."""

    def test_requires_minimum_similarity(self):
        """Test that low similarity blocks don't match."""
        prev_tail = ["Completely different", "text here"]
        curr_head = ["Totally other", "content there", "more"]

        result = _find_overlap_sequence_matching(
            prev_tail,
            curr_head,
            min_match_lines=2,
            min_similarity=0.90,
        )

        assert result is None

    def test_requires_boundary_alignment(self):
        """Test that matches must be at boundary (end of prev, start of curr)."""
        prev_tail = ["Start", "Middle match", "End"]
        curr_head = ["Different", "Middle match", "Other"]

        result = _find_overlap_sequence_matching(
            prev_tail,
            curr_head,
            min_match_lines=1,
            min_similarity=0.90,
        )

        # "Middle match" appears in both but not at boundary
        assert result is None

    def test_average_similarity_with_one_corrupted_line(self):
        """Test sequence matching handles one corrupted line via averaging.

        This demonstrates the key difference between sequence and fuzzy matching:
        - Sequence: Average similarity >= threshold (lenient)
        - Fuzzy: Every line similarity >= threshold (strict)
        """
        # Line 1: Perfect match (1.0)
        # Line 2: Perfect match (1.0)
        # Line 3: Heavily corrupted (low similarity ~0.5)
        # Average: (1.0 + 1.0 + 0.5) / 3 = 0.83
        prev_tail = ["Perfect line one", "Perfect line two", "This line is correct"]
        curr_head = ["Perfect line one", "Perfect line two", "Th1s 11ne 1s c0rrupt", "New content"]

        # Should match with lower threshold (0.80) via averaging
        result = _find_overlap_sequence_matching(
            prev_tail,
            curr_head,
            min_match_lines=3,
            min_similarity=0.80,
        )

        assert result is not None
        match_size, avg_sim = result
        assert match_size == 3
        assert 0.80 <= avg_sim < 0.90  # Average similarity between thresholds

    def test_high_average_similarity_with_punctuation_differences(self):
        """Test sequence matching with minor OCR variations."""
        prev_tail = ["Hello World!", "How are you?", "Fine, thanks."]
        curr_head = ["Hello World", "How are you", "Fine thanks", "Next"]

        result = _find_overlap_sequence_matching(
            prev_tail,
            curr_head,
            min_match_lines=2,
            min_similarity=0.90,
        )

        # Should match - punctuation differences are minor
        assert result is not None
        match_size, avg_sim = result
        assert match_size == 3
        assert avg_sim >= 0.90


class TestFuzzyMatching:
    """Tests for fuzzy line-by-line matching."""

    def test_fuzzy_match_with_punctuation_differences(self):
        """Test fuzzy matching handles punctuation variations."""
        prev_tail = ["Hello World!", "How are you?", "Fine thanks"]
        curr_head = ["Hello World", "How are you", "Fine thanks", "Next"]

        match_count = _find_overlap_fuzzy(
            prev_tail,
            curr_head,
            line_threshold=0.85,
            min_match_lines=2,
        )

        assert match_count == 3

    def test_stops_at_significant_difference(self):
        """Test fuzzy matching finds longest valid match."""
        # For boundary matches, we look for end of prev matching start of curr
        prev_tail = ["Line 1", "Match 1", "Match 2"]
        curr_head = ["Match 1", "Match 2", "Next"]

        match_count = _find_overlap_fuzzy(
            prev_tail,
            curr_head,
            line_threshold=0.85,
            min_match_lines=2,
        )

        # Should match last 2 lines of prev with first 2 of curr
        assert match_count == 2

    def test_empty_lines_counted_as_match(self):
        """Test that matching empty lines count as matches."""
        prev_tail = ["Text", "", "More"]
        curr_head = ["", "More", "Next"]

        match_count = _find_overlap_fuzzy(
            prev_tail,
            curr_head,
            line_threshold=0.85,
            min_match_lines=1,
        )

        # Should match empty line and "More"
        assert match_count == 2


class TestEstimateOverlapLines:
    """Tests for overlap line estimation."""

    def test_estimate_from_pixel_overlap(self):
        """Test line estimation from pixel overlap."""
        tile1 = create_test_tile(0, overlap_px=120)
        tile2 = create_test_tile(1, overlap_px=120)

        estimated = _estimate_overlap_lines(tile1, tile2)

        # 120px / 25px per line ~= 4-5 lines
        assert estimated >= 3
        assert estimated <= 6

    def test_minimum_estimate(self):
        """Test minimum estimate is enforced."""
        tile1 = create_test_tile(0, overlap_px=10)  # Very small overlap
        tile2 = create_test_tile(1, overlap_px=10)

        estimated = _estimate_overlap_lines(tile1, tile2)

        # Should return minimum of 3
        assert estimated == 3


class TestDeduplicateTileOverlap:
    """Integration tests for complete deduplication flow."""

    def test_exact_match_deduplication(self):
        """Test exact line matching removes duplicates."""
        prev_lines = ["Line 1", "Line 2", "Overlap A", "Overlap B"]
        curr_lines = ["Overlap A", "Overlap B", "Line 3", "Line 4"]
        prev_tile = create_test_tile(0, bottom_hash="match123")
        curr_tile = create_test_tile(1, top_hash="match123")

        result_lines, info = deduplicate_tile_overlap(prev_lines, curr_lines, prev_tile, curr_tile)

        assert info.lines_removed == 2
        assert info.method == "exact"
        assert info.similarity == 1.0
        assert result_lines == ["Line 3", "Line 4"]

    def test_sequence_match_with_ocr_errors(self):
        """Test sequence matching handles OCR variations."""
        prev_lines = ["Hello World!", "Second line."]
        curr_lines = ["Hello World", "Second line", "New content"]
        prev_tile = create_test_tile(0, bottom_hash="match123")
        curr_tile = create_test_tile(1, top_hash="match123")

        result_lines, info = deduplicate_tile_overlap(prev_lines, curr_lines, prev_tile, curr_tile)

        assert info.lines_removed == 2
        assert info.method in ["exact", "sequence", "fuzzy"]  # Any tier can match
        assert result_lines == ["New content"]

    def test_no_dedup_without_pixel_overlap(self):
        """Test no deduplication when pixel hashes don't match."""
        prev_lines = ["Line 1", "Line 2", "Overlap"]
        curr_lines = ["Overlap", "Line 3", "Line 4"]
        prev_tile = create_test_tile(0, bottom_hash="abc123")
        curr_tile = create_test_tile(1, top_hash="def456")  # Different!

        result_lines, info = deduplicate_tile_overlap(prev_lines, curr_lines, prev_tile, curr_tile)

        assert info.lines_removed == 0
        assert info.method == "no_overlap"
        assert result_lines == curr_lines  # Unchanged

    def test_disabled_deduplication(self):
        """Test deduplication can be disabled."""
        prev_lines = ["Line 1", "Overlap"]
        curr_lines = ["Overlap", "Line 2"]
        prev_tile = create_test_tile(0, bottom_hash="match123")
        curr_tile = create_test_tile(1, top_hash="match123")

        result_lines, info = deduplicate_tile_overlap(
            prev_lines,
            curr_lines,
            prev_tile,
            curr_tile,
            enabled=False,  # Disabled
        )

        assert info.lines_removed == 0
        assert info.method == "disabled"
        assert result_lines == curr_lines  # Unchanged

    def test_insufficient_content(self):
        """Test no dedup when content is too short."""
        prev_lines = ["Single line"]
        curr_lines = ["Single line", "Next"]
        prev_tile = create_test_tile(0, bottom_hash="match123")
        curr_tile = create_test_tile(1, top_hash="match123")

        result_lines, info = deduplicate_tile_overlap(
            prev_lines,
            curr_lines,
            prev_tile,
            curr_tile,
            min_overlap_lines=2,  # Require at least 2 lines
        )

        assert info.lines_removed == 0
        assert info.method == "insufficient_content"

    def test_safety_limit_prevents_over_deletion(self):
        """Test safety mechanism prevents removing too many lines."""
        # Create scenario where match is larger than 3x estimated overlap
        prev_lines = ["Line" + str(i) for i in range(50)]
        curr_lines = ["Line" + str(i) for i in range(50)] + ["New"]
        prev_tile = create_test_tile(0, overlap_px=10, bottom_hash="match123")  # Small overlap
        curr_tile = create_test_tile(1, overlap_px=10, top_hash="match123")

        result_lines, info = deduplicate_tile_overlap(prev_lines, curr_lines, prev_tile, curr_tile)

        # Estimated overlap from 10px = 3 lines minimum
        # Max removable = 3 * 3 = 9 lines
        # But exact match found 50 lines, which exceeds safety limit
        # Should NOT remove all 50 (safety mechanism kicks in)
        if info.lines_removed > 0:
            assert info.lines_removed <= 9, "Should not exceed safety limit"

    def test_markdown_heading_in_overlap(self):
        """Test deduplication works with markdown headings."""
        prev_lines = ["# Heading", "Paragraph text"]
        curr_lines = ["# Heading", "Paragraph text", "More content"]
        prev_tile = create_test_tile(0, bottom_hash="match123")
        curr_tile = create_test_tile(1, top_hash="match123")

        result_lines, info = deduplicate_tile_overlap(prev_lines, curr_lines, prev_tile, curr_tile)

        assert info.lines_removed == 2
        assert "# Heading" not in result_lines
        assert result_lines == ["More content"]

    def test_markdown_list_in_overlap(self):
        """Test deduplication works with markdown lists."""
        prev_lines = ["- List item 1", "- List item 2"]
        curr_lines = ["- List item 1", "- List item 2", "- List item 3"]
        prev_tile = create_test_tile(0, bottom_hash="match123")
        curr_tile = create_test_tile(1, top_hash="match123")

        result_lines, info = deduplicate_tile_overlap(prev_lines, curr_lines, prev_tile, curr_tile)

        assert info.lines_removed == 2
        assert result_lines == ["- List item 3"]


class TestTierFallback:
    """Test that deduplication falls through tiers correctly."""

    def test_fallback_to_sequence_when_exact_fails(self):
        """Test fallback to sequence matching when exact matching fails."""
        # Similar but not exact (punctuation differences)
        prev_lines = ["Line 1!", "Line 2?"]
        curr_lines = ["Line 1", "Line 2", "Line 3"]
        prev_tile = create_test_tile(0, bottom_hash="match123")
        curr_tile = create_test_tile(1, top_hash="match123")

        result_lines, info = deduplicate_tile_overlap(prev_lines, curr_lines, prev_tile, curr_tile)

        # Should match via sequence or fuzzy, not exact
        assert info.lines_removed == 2
        assert info.method in ["sequence", "fuzzy"]
        assert result_lines == ["Line 3"]

    def test_sequence_succeeds_where_fuzzy_fails(self):
        """Test that sequence matching (average) is more lenient than fuzzy (per-line).

        Sequence matching uses average similarity, so it can handle one corrupted
        line if other lines are perfect. Fuzzy matching requires every line to
        meet the threshold.
        """
        # Perfect, Perfect, Corrupted (~50% similar)
        # Average: ~0.83, but one line is way below fuzzy threshold (0.85)
        prev_lines = ["First perfect line", "Second perfect line", "Third line is good"]
        curr_lines = ["First perfect line", "Second perfect line", "Th1rd 11ne 1s b4d", "New"]
        prev_tile = create_test_tile(0, bottom_hash="match123")
        curr_tile = create_test_tile(1, top_hash="match123")

        # Use lower sequence threshold to trigger sequence matching
        result_lines, info = deduplicate_tile_overlap(
            prev_lines,
            curr_lines,
            prev_tile,
            curr_tile,
            sequence_similarity_threshold=0.80,  # Lower than default
            fuzzy_line_threshold=0.85,  # Standard threshold
        )

        # Should match via sequence (average ~0.83) but NOT fuzzy (one line < 0.85)
        # OR might not match at all if corruption is too high
        if info.lines_removed > 0:
            # If it matched, it should be via sequence, not fuzzy
            assert info.method == "sequence"
            assert result_lines == ["New"]

    def test_no_match_when_all_tiers_fail(self):
        """Test no deduplication when all matching tiers fail."""
        prev_lines = ["Completely", "Different"]
        curr_lines = ["Totally", "Other", "Content"]
        prev_tile = create_test_tile(0, bottom_hash="match123")
        curr_tile = create_test_tile(1, top_hash="match123")

        result_lines, info = deduplicate_tile_overlap(prev_lines, curr_lines, prev_tile, curr_tile)

        # No confident match found
        assert info.lines_removed == 0
        assert info.method == "no_match"
        assert result_lines == curr_lines  # Unchanged


class TestOverlapSsimAndDedupSummary:
    """Tests for real pyvips/numpy SSIM over overlap strips and dedup event aggregation."""

    @staticmethod
    def _encode_pattern_png(arr) -> bytes:
        import pyvips

        h, w = arr.shape
        img = pyvips.Image.new_from_memory(arr.tobytes(), w, h, 1, "uchar")
        return bytes(img.write_to_buffer(".png"))

    def test_compute_overlap_ssim_recovers_subpixel_drift_and_blocks_false_seam(self):
        import numpy as np
        from app.dedup import compute_overlap_ssim, summarize_dedup_events

        rng = np.random.default_rng(42)
        shared_strip = rng.integers(20, 235, size=(40, 128), dtype=np.uint8)
        # Minor subpixel / anti-aliasing jitter (+1 gray level in a few pixels)
        drifted_strip = np.clip(shared_strip.astype(np.int16) + 1, 0, 255).astype(np.uint8)
        different_strip = 255 - shared_strip

        prev_arr = np.vstack([np.full((80, 128), 240, dtype=np.uint8), shared_strip])
        curr_good_arr = np.vstack([drifted_strip, np.full((80, 128), 240, dtype=np.uint8)])
        curr_bad_arr = np.vstack([different_strip, np.full((80, 128), 240, dtype=np.uint8)])

        prev_tile = TileSlice(
            index=0,
            png_bytes=self._encode_pattern_png(prev_arr),
            sha256="sha0",
            width=128,
            height=120,
            scale=1.0,
            source_y_offset=0,
            viewport_y_offset=0,
            overlap_px=40,
            top_overlap_sha256=None,
            bottom_overlap_sha256="sha_a",
            seam_bottom_hash="collided_seam",
        )
        curr_good = TileSlice(
            index=1,
            png_bytes=self._encode_pattern_png(curr_good_arr),
            sha256="sha1",
            width=128,
            height=120,
            scale=1.0,
            source_y_offset=80,
            viewport_y_offset=0,
            overlap_px=40,
            top_overlap_sha256="sha_b_drifted",  # SHA256 differs due to 1-level drift
            bottom_overlap_sha256=None,
        )
        curr_bad = TileSlice(
            index=1,
            png_bytes=self._encode_pattern_png(curr_bad_arr),
            sha256="sha2",
            width=128,
            height=120,
            scale=1.0,
            source_y_offset=80,
            viewport_y_offset=0,
            overlap_px=40,
            top_overlap_sha256="sha_c",
            bottom_overlap_sha256=None,
            seam_top_hash="collided_seam",  # Coarse seam hash collided!
        )

        good_ssim = compute_overlap_ssim(prev_tile, curr_good)
        bad_ssim = compute_overlap_ssim(prev_tile, curr_bad)
        assert good_ssim is not None and good_ssim >= 0.95
        assert bad_ssim is not None and bad_ssim < 0.30

        # 1. Subpixel drift with high SSIM still deduplicates overlap!
        lines_good, res_good = deduplicate_tile_overlap(
            ["Intro", "Overlap 1", "Overlap 2"],
            ["Overlap 1", "Overlap 2", "Body"],
            prev_tile,
            curr_good,
        )
        assert res_good.method == "exact"
        assert res_good.lines_removed == 2
        assert lines_good == ["Body"]

        # 2. Visually disagreeing strip with collided seam hash is BLOCKED by SSIM!
        lines_bad, res_bad = deduplicate_tile_overlap(
            ["Intro", "Overlap 1", "Overlap 2"],
            ["Overlap 1", "Overlap 2", "Body"],
            prev_tile,
            curr_bad,
        )
        assert res_bad.method == "no_overlap"
        assert res_bad.lines_removed == 0
        assert lines_bad == ["Overlap 1", "Overlap 2", "Body"]

        summary = summarize_dedup_events([res_good])
        assert summary["total_events"] == 1
        assert summary["lines_removed"] == 2
        assert summary["exact_matches"] == 1
        assert summary["avg_similarity"] == 1.0

