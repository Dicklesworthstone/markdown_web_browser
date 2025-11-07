"""Capture warning heuristics for canvas/video-heavy pages and overlays."""

from __future__ import annotations

from dataclasses import dataclass
from typing import List

from playwright.async_api import Page

from app.settings import WarningSettings


@dataclass(slots=True)
class WarningStats:
    """Lightweight DOM metrics used to drive capture warnings."""

    canvas_count: int
    video_count: int
    sticky_count: int
    dialog_count: int


@dataclass(slots=True)
class CaptureWarningEntry:
    """Structured warning surfaced in manifests + telemetry."""

    code: str
    message: str
    count: float
    threshold: float


async def collect_warning_stats(page: Page) -> WarningStats:
    """Inspect the DOM and return counts that drive warning decisions."""

    result = await page.evaluate(
        """
        () => {
            const stickySelectors =
                "[style*='position:fixed'],[style*='position: sticky'],header[style*='position']";
            const sticky = document.querySelectorAll(stickySelectors).length;
            const canvas = document.querySelectorAll('canvas').length;
            const video = document.querySelectorAll('video').length;
            const dialog = document.querySelectorAll('[role=\"dialog\"], [aria-modal=\"true\"]').length;
            return { sticky, canvas, video, dialog };
        }
        """
    )
    return WarningStats(
        canvas_count=int(result.get("canvas", 0)),
        video_count=int(result.get("video", 0)),
        sticky_count=int(result.get("sticky", 0)),
        dialog_count=int(result.get("dialog", 0)),
    )


def build_warnings(
    stats: WarningStats,
    *,
    settings: WarningSettings,
    sticky_threshold: int = 3,
    dialog_threshold: int = 1,
) -> List[CaptureWarningEntry]:
    """Convert DOM stats into structured warnings."""

    warnings: List[CaptureWarningEntry] = []

    if settings.canvas_warning_threshold > 0 and stats.canvas_count >= settings.canvas_warning_threshold:
        warnings.append(
            CaptureWarningEntry(
                code="canvas-heavy",
                message="High canvas count may hide chart labels or overlay content.",
                count=stats.canvas_count,
                threshold=settings.canvas_warning_threshold,
            )
        )

    if settings.video_warning_threshold > 0 and stats.video_count >= settings.video_warning_threshold:
        warnings.append(
            CaptureWarningEntry(
                code="video-heavy",
                message="Multiple video elements detected; screenshots may capture transient frames.",
                count=stats.video_count,
                threshold=settings.video_warning_threshold,
            )
        )

    sticky_condition = stats.sticky_count >= sticky_threshold
    dialog_condition = stats.dialog_count >= dialog_threshold
    if sticky_condition or dialog_condition:
        warnings.append(
            CaptureWarningEntry(
                code="sticky-chrome",
                message="Detected fixed or modal overlays that can occlude content.",
                count=stats.sticky_count + stats.dialog_count,
                threshold=sticky_threshold,
            )
        )

    return warnings


async def collect_capture_warnings(page: Page, settings: WarningSettings) -> List[CaptureWarningEntry]:
    """Gather DOM stats and produce warning entries using configured thresholds."""

    stats = await collect_warning_stats(page)
    return build_warnings(stats, settings=settings)


def build_sweep_warning(
    *,
    shrink_events: int,
    overlap_pairs: int,
    overlap_match_ratio: float,
    settings: WarningSettings,
) -> List[CaptureWarningEntry]:
    """Emit warnings derived from sweep stats (shrink + overlap ratios)."""

    warnings: List[CaptureWarningEntry] = []

    if settings.shrink_warning_threshold > 0 and shrink_events >= settings.shrink_warning_threshold:
        warnings.append(
            CaptureWarningEntry(
                code="scroll-shrink",
                message="Repeated scroll-height shrink events detected; viewport sweep retried.",
                count=shrink_events,
                threshold=settings.shrink_warning_threshold,
            )
        )

    if (
        overlap_pairs > 0
        and settings.overlap_warning_ratio > 0
        and overlap_match_ratio < settings.overlap_warning_ratio
    ):
        warnings.append(
            CaptureWarningEntry(
                code="overlap-low",
                message="Tile overlap match ratio is below the configured threshold; seams may misalign.",
                count=overlap_match_ratio,
                threshold=settings.overlap_warning_ratio,
            )
        )

    if (
        overlap_pairs >= settings.seam_warning_min_pairs
        and settings.seam_warning_ratio > 0
        and overlap_match_ratio >= settings.seam_warning_ratio
    ):
        warnings.append(
            CaptureWarningEntry(
                code="duplicate-seam",
                message="High overlap match ratio suggests duplicate tiling seams.",
                count=overlap_pairs,
                threshold=settings.seam_warning_ratio,
            )
        )

    return warnings
