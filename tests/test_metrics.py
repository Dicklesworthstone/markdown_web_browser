from __future__ import annotations

from typing import Any, Mapping

import pytest

from app import metrics
from app.schemas import (
    ConcurrencyWindow,
    ManifestEnvironment,
    ManifestMetadata,
    ManifestTimings,
    ManifestWarning,
    ViewportSettings,
)


def _base_environment() -> ManifestEnvironment:
    return ManifestEnvironment(
        cft_version="chrome-130.0.6723.69",
        cft_label="Stable-1",
        playwright_channel="cft",
        playwright_version="1.50.0",
        browser_transport="cdp",
        viewport=ViewportSettings(width=1280, height=2000, device_scale_factor=2, color_scheme="light"),
        viewport_overlap_px=120,
        tile_overlap_px=120,
        scroll_settle_ms=350,
        max_viewport_sweeps=200,
        screenshot_style_hash="dev-sweeps-v1",
        screenshot_mask_selectors=(),
        ocr_model="olmOCR-2-7B-1025-FP8",
        ocr_use_fp8=True,
        ocr_concurrency=ConcurrencyWindow(min=2, max=8),
    )


def _sample_value(metric: Any, sample_name: str, labels: Mapping[str, str] | None = None) -> float:
    for collected in metric.collect():
        for sample in collected.samples:
            if sample.name != sample_name:
                continue
            if labels is not None and sample.labels != labels:
                continue
            return float(sample.value)
    return 0.0


def test_observe_manifest_metrics_updates_histograms_and_counters() -> None:
    manifest = ManifestMetadata(
        environment=_base_environment(),
        timings=ManifestTimings(capture_ms=1500, ocr_ms=3200, stitch_ms=900, total_ms=5600),
        blocklist_hits={"#cookie-banner": 2},
        warnings=[
            ManifestWarning(code="canvas-heavy", message="canvas", count=3, threshold=2),
            ManifestWarning(code="scroll-shrink", message="shrink", count=1, threshold=1),
        ],
    )

    before_capture_count = _sample_value(
        metrics.CAPTURE_DURATION_SECONDS, "mdwb_capture_duration_seconds_count"
    )
    before_capture_sum = _sample_value(
        metrics.CAPTURE_DURATION_SECONDS, "mdwb_capture_duration_seconds_sum"
    )
    before_ocr_sum = _sample_value(metrics.OCR_DURATION_SECONDS, "mdwb_ocr_duration_seconds_sum")
    before_stitch_sum = _sample_value(
        metrics.STITCH_DURATION_SECONDS, "mdwb_stitch_duration_seconds_sum"
    )
    before_canvas = _sample_value(
        metrics.WARNING_COUNTER, "mdwb_capture_warnings_total", labels={"code": "canvas-heavy"}
    )
    before_shrink = _sample_value(
        metrics.WARNING_COUNTER, "mdwb_capture_warnings_total", labels={"code": "scroll-shrink"}
    )
    before_blocklist = _sample_value(
        metrics.BLOCKLIST_COUNTER,
        "mdwb_blocklist_hits_total",
        labels={"selector": "#cookie-banner"},
    )

    metrics.observe_manifest_metrics(manifest)

    after_capture_count = _sample_value(
        metrics.CAPTURE_DURATION_SECONDS, "mdwb_capture_duration_seconds_count"
    )
    after_capture_sum = _sample_value(
        metrics.CAPTURE_DURATION_SECONDS, "mdwb_capture_duration_seconds_sum"
    )
    after_ocr_sum = _sample_value(metrics.OCR_DURATION_SECONDS, "mdwb_ocr_duration_seconds_sum")
    after_stitch_sum = _sample_value(
        metrics.STITCH_DURATION_SECONDS, "mdwb_stitch_duration_seconds_sum"
    )
    after_canvas = _sample_value(
        metrics.WARNING_COUNTER, "mdwb_capture_warnings_total", labels={"code": "canvas-heavy"}
    )
    after_shrink = _sample_value(
        metrics.WARNING_COUNTER, "mdwb_capture_warnings_total", labels={"code": "scroll-shrink"}
    )
    after_blocklist = _sample_value(
        metrics.BLOCKLIST_COUNTER,
        "mdwb_blocklist_hits_total",
        labels={"selector": "#cookie-banner"},
    )

    assert after_capture_count == pytest.approx(before_capture_count + 1)
    assert after_capture_sum == pytest.approx(before_capture_sum + 1.5)
    assert after_ocr_sum == pytest.approx(before_ocr_sum + 3.2)
    assert after_stitch_sum == pytest.approx(before_stitch_sum + 0.9)
    assert after_canvas == pytest.approx(before_canvas + 3)
    assert after_shrink == pytest.approx(before_shrink + 1)
    assert after_blocklist == pytest.approx(before_blocklist + 2)


def test_sse_and_job_metrics_increment() -> None:
    before_heartbeat = _sample_value(metrics.SSE_HEARTBEAT_COUNTER, "mdwb_sse_heartbeat_total")
    metrics.increment_sse_heartbeat()
    after_heartbeat = _sample_value(metrics.SSE_HEARTBEAT_COUNTER, "mdwb_sse_heartbeat_total")
    assert after_heartbeat == pytest.approx(before_heartbeat + 1)

    before_done = _sample_value(
        metrics.JOB_COMPLETIONS, "mdwb_job_completions_total", labels={"state": "DONE"}
    )
    metrics.record_job_completion("DONE")
    after_done = _sample_value(
        metrics.JOB_COMPLETIONS, "mdwb_job_completions_total", labels={"state": "DONE"}
    )
    assert after_done == pytest.approx(before_done + 1)
