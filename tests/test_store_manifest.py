from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from app.store import StorageConfig, Store


def _storage(tmp_path: Path) -> Store:
    cache_root = tmp_path / "cache"
    db_path = tmp_path / "runs.db"
    config = StorageConfig(cache_root=cache_root, db_path=db_path)
    return Store(config=config)


def test_store_persists_sweep_and_validation_metadata(tmp_path: Path) -> None:
    store = _storage(tmp_path)
    started = datetime(2025, 11, 8, 8, 0, tzinfo=timezone.utc)
    store.allocate_run(job_id="run-123", url="https://example.com", started_at=started)

    manifest = {
        "environment": {
            "cft_version": "chrome-130",
            "cft_label": "Stable-1",
            "playwright_channel": "cft",
            "playwright_version": "1.55.0",
            "browser_transport": "cdp",
            "viewport": {"width": 1280, "height": 2000, "device_scale_factor": 2, "color_scheme": "light"},
            "viewport_overlap_px": 120,
            "tile_overlap_px": 120,
            "scroll_settle_ms": 350,
            "max_viewport_sweeps": 200,
            "screenshot_style_hash": "demo",
            "screenshot_mask_selectors": [],
            "ocr_model": "olmOCR-2-7B-1025-FP8",
            "ocr_use_fp8": True,
            "ocr_concurrency": {"min": 2, "max": 8},
        },
        "timings": {"capture_ms": 1500, "ocr_ms": 3200, "stitch_ms": 400},
        "tiles_total": 8,
        "long_side_px": 1288,
        "sweep_stats": {
            "sweep_count": 5,
            "total_scroll_height": 14000,
            "shrink_events": 2,
            "retry_attempts": 1,
            "overlap_pairs": 6,
            "overlap_match_ratio": 0.87,
        },
        "overlap_match_ratio": 0.87,
        "validation_failures": ["tile 3 checksum mismatch", "tile 5 decode failed"],
    }

    store.write_manifest(job_id="run-123", manifest=manifest)
    record = store.fetch_run("run-123")
    assert record is not None
    assert record.sweep_shrink_events == 2
    assert record.sweep_retry_attempts == 1
    assert record.sweep_overlap_pairs == 6
    assert record.overlap_match_ratio == 0.87
    assert record.validation_failure_count == 2
