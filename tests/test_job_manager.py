from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # pragma: no cover
    sys.path.append(str(ROOT))

try:
    from app.capture import CaptureManifest, CaptureResult, ScrollPolicy, SweepStats  # noqa: E402
    from app.jobs import JobManager, JobState  # noqa: E402
    from app.schemas import JobCreateRequest  # noqa: E402
    from app.store import StorageConfig, Store  # noqa: E402
except OSError as exc:  # pyvips missing in CI
    pytest.skip(f"capture dependencies unavailable: {exc}", allow_module_level=True)


async def _fake_runner(*, job_id: str, url: str, store: Store, config=None):  # noqa: ANN001
    manifest = CaptureManifest(
        url=url,
        cft_label="Stable-1",
        cft_version="chrome-130",
        playwright_channel="chrome",
        playwright_version="1.55.0",
        browser_transport="cdp",
        screenshot_style_hash="demo",
        viewport_width=1280,
        viewport_height=2000,
        device_scale_factor=2,
        long_side_px=1288,
        capture_ms=100,
        tiles_total=1,
        scroll_policy=ScrollPolicy(
            settle_ms=300,
            max_steps=10,
            viewport_overlap_px=120,
            viewport_step_px=1080,
        ),
        sweep_stats=SweepStats(
            sweep_count=1,
            total_scroll_height=2000,
            shrink_events=0,
            retry_attempts=0,
            overlap_pairs=0,
            overlap_match_ratio=0.0,
        ),
        user_agent="Demo",
        shrink_retry_limit=2,
        blocklist_version="demo",
        blocklist_hits={},
        warnings=[],
    )
    return CaptureResult(tiles=[], manifest=manifest), []


@pytest.mark.asyncio
async def test_job_manager_snapshot_queue(tmp_path: Path):
    config = StorageConfig(cache_root=tmp_path / "cache", db_path=tmp_path / "runs.db")
    manager = JobManager(store=Store(config), runner=_fake_runner)
    snapshot = await manager.create_job(JobCreateRequest(url="https://example.com"))
    job_id = snapshot["id"]

    queue = manager.subscribe(job_id)
    states: list[str] = []
    while True:
        update = await asyncio.wait_for(queue.get(), timeout=1)
        states.append(update["state"])
        if update["state"] == JobState.DONE.value:
            break
    manager.unsubscribe(job_id, queue)

    assert JobState.BROWSER_STARTING.value in states
    assert states[-1] == JobState.DONE.value
