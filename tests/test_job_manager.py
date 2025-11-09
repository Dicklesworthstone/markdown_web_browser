from __future__ import annotations

import asyncio
import sys
import types
from datetime import datetime
from typing import Any, cast
from pathlib import Path

from dataclasses import dataclass, replace

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # pragma: no cover
    sys.path.append(str(ROOT))

if "pyvips" not in sys.modules:
    pyvips_stub = types.ModuleType("pyvips")
    pyvips_stub.Image = object  # type: ignore[attr-defined]
    sys.modules["pyvips"] = pyvips_stub

try:
    from app.capture import (  # noqa: E402
        CaptureConfig,
        CaptureManifest,
        CaptureResult,
        ScrollPolicy,
        SweepStats,
    )
except OSError:  # pyvips missing in CI
    @dataclass
    class ScrollPolicy:  # type: ignore[override]
        settle_ms: int
        max_steps: int
        viewport_overlap_px: int
        viewport_step_px: int

    @dataclass
    class SweepStats:  # type: ignore[override]
        sweep_count: int
        total_scroll_height: int
        shrink_events: int
        retry_attempts: int
        overlap_pairs: int
        overlap_match_ratio: float

    @dataclass
    class CaptureManifest:  # type: ignore[override]
        url: str
        cft_label: str
        cft_version: str
        playwright_channel: str
        playwright_version: str
        browser_transport: str
        screenshot_style_hash: str
        viewport_width: int
        viewport_height: int
        device_scale_factor: int
        long_side_px: int
        capture_ms: int
        tiles_total: int
        scroll_policy: ScrollPolicy
        sweep_stats: SweepStats
        user_agent: str
        shrink_retry_limit: int
        blocklist_version: str
        blocklist_hits: dict
        warnings: list
        overlap_match_ratio: float
        validation_failures: list
        profile_id: str | None = None
        cache_key: str | None = None
        cache_hit: bool = False
        ocr_ms: int | None = None
        stitch_ms: int | None = None
        ocr_batches: list | None = None
        ocr_quota: dict | None = None

    @dataclass
    class CaptureResult:  # type: ignore[override]
        tiles: list
        manifest: CaptureManifest

    @dataclass
    class CaptureConfig:  # type: ignore[override]
        url: str
        viewport_width: int = 1280
        viewport_height: int = 2000
        device_scale_factor: int = 2
        color_scheme: str = "light"
        reduced_motion: bool = True
        profile_id: str | None = None
        long_side_px: int | None = None
        cache_key: str | None = None

from app.jobs import (  # noqa: E402
    JobManager,
    JobState,
    _build_cache_key,
    global_settings as job_global_settings,
)
from app.schemas import JobCreateRequest  # noqa: E402
from app.store import StorageConfig, Store  # noqa: E402
from app.tiler import TileSlice  # noqa: E402


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
        overlap_match_ratio=0.0,
        validation_failures=[],
        ocr_ms=1200,
        stitch_ms=300,
        ocr_batches=[
            {
                "tile_ids": [f"{job_id}-tile-0000"],
                "latency_ms": 850,
                "status_code": 200,
                "attempts": 1,
                "payload_bytes": 2048,
                "request_id": "req-demo",
            }
        ],
        ocr_quota={
            "limit": 10,
            "used": 3,
            "threshold_ratio": 0.7,
            "warning_triggered": False,
        },
        seam_markers=[
            {"tile_index": 0, "position": "top", "hash": "aaa111"},
            {"tile_index": 1, "position": "bottom", "hash": "bbb222"},
        ],
        profile_id=getattr(config, "profile_id", None),
        cache_key=getattr(config, "cache_key", None),
    )
    tiles = [
        TileSlice(
            index=0,
            png_bytes=b"tile",
            sha256="sha0",
            width=100,
            height=100,
            scale=1.0,
            source_y_offset=0,
            viewport_y_offset=0,
            overlap_px=0,
            top_overlap_sha256=None,
            bottom_overlap_sha256=None,
        )
    ]
    artifacts = store.write_tiles(job_id=job_id, tiles=tiles)
    store.write_manifest(job_id=job_id, manifest=manifest)
    return CaptureResult(tiles=tiles, manifest=manifest), artifacts


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


@pytest.mark.asyncio
async def test_job_manager_event_log_records_history(tmp_path: Path):
    config = StorageConfig(cache_root=tmp_path / "cache", db_path=tmp_path / "runs.db")
    manager = JobManager(store=Store(config), runner=_fake_runner)
    snapshot = await manager.create_job(JobCreateRequest(url="https://example.com/one"))
    job_id = snapshot["id"]
    task = manager._tasks[job_id]
    await task

    history = manager.get_events(job_id)

    assert history
    assert history[0]["snapshot"]["state"] == JobState.BROWSER_STARTING.value
    assert history[-1]["snapshot"]["state"] == JobState.DONE.value


@pytest.mark.asyncio
async def test_job_manager_snapshot_includes_seam_counts(tmp_path: Path):
    config = StorageConfig(cache_root=tmp_path / "cache", db_path=tmp_path / "runs.db")
    manager = JobManager(store=Store(config), runner=_fake_runner)
    snapshot = await manager.create_job(JobCreateRequest(url="https://example.com/seams"))
    job_id = snapshot["id"]
    task = manager._tasks[job_id]
    await task

    final_snapshot = manager.get_snapshot(job_id)
    assert final_snapshot.get("seam_marker_count") == 2
    assert final_snapshot.get("seam_hash_count") == 2


@pytest.mark.asyncio
async def test_job_manager_event_log_clamps_length(monkeypatch, tmp_path: Path):
    from app import jobs as jobs_module

    monkeypatch.setattr(jobs_module, "_EVENT_HISTORY_LIMIT", 3)
    config = StorageConfig(cache_root=tmp_path / "cache", db_path=tmp_path / "runs.db")
    manager = JobManager(store=Store(config), runner=_fake_runner)
    snapshot = await manager.create_job(JobCreateRequest(url="https://example.org/two"))
    job_id = snapshot["id"]
    task = manager._tasks[job_id]
    await task

    manager._set_state(job_id, JobState.NAVIGATING)
    manager._set_state(job_id, JobState.CAPTURING)
    manager._set_state(job_id, JobState.DONE)

    history = manager.get_events(job_id)

    assert len(history) == 3
    assert history[0]["snapshot"]["state"] == JobState.NAVIGATING.value


@pytest.mark.asyncio
async def test_job_manager_events_since(tmp_path: Path):
    config = StorageConfig(cache_root=tmp_path / "cache", db_path=tmp_path / "runs.db")
    manager = JobManager(store=Store(config), runner=_fake_runner)
    snapshot = await manager.create_job(JobCreateRequest(url="https://example.com/events"))
    job_id = snapshot["id"]

    await manager._tasks[job_id]
    events = manager.get_events(job_id)
    assert events, "expected events to be recorded"
    assert events[-1]["snapshot"]["state"] == JobState.DONE.value

    last_timestamp = datetime.fromisoformat(events[-1]["timestamp"])
    filtered = manager.get_events(job_id, since=last_timestamp)
    assert filtered
    assert filtered[0]["timestamp"] == events[-1]["timestamp"]


@pytest.mark.asyncio
async def test_job_manager_events_sequence_filter(tmp_path: Path):
    config = StorageConfig(cache_root=tmp_path / "cache", db_path=tmp_path / "runs.db")
    manager = JobManager(store=Store(config), runner=_fake_runner)
    snapshot = await manager.create_job(JobCreateRequest(url="https://example.com/seq"))
    job_id = snapshot["id"]
    await manager._tasks[job_id]

    events = manager.get_events(job_id)
    assert events
    last_seq = events[-1]["sequence"]

    assert manager.get_events(job_id, min_sequence=last_seq) == []

    manager._set_state(job_id, JobState.NAVIGATING)

    new_events = manager.get_events(job_id, min_sequence=last_seq)
    assert new_events
    assert new_events[0]["sequence"] > last_seq


@pytest.mark.asyncio
async def test_job_manager_emits_ocr_event(tmp_path: Path):
    config = StorageConfig(cache_root=tmp_path / "cache", db_path=tmp_path / "runs.db")
    manager = JobManager(store=Store(config), runner=_fake_runner)
    snapshot = await manager.create_job(JobCreateRequest(url="https://example.com/ocr"))
    job_id = snapshot["id"]
    await manager._tasks[job_id]

    events = manager.get_events(job_id)

    assert any(entry.get("event") == "ocr_telemetry" for entry in events)


@pytest.mark.asyncio
async def test_job_manager_subscribe_events_stream(tmp_path: Path):
    config = StorageConfig(cache_root=tmp_path / "cache", db_path=tmp_path / "runs.db")
    manager = JobManager(store=Store(config), runner=_fake_runner)
    snapshot = await manager.create_job(JobCreateRequest(url="https://example.com/sub"))
    job_id = snapshot["id"]

    await manager._tasks[job_id]
    backlog, queue = manager.subscribe_events(job_id)
    try:
        assert backlog, "Expected backlog events to replay"
        last_sequence = backlog[-1]["sequence"]

        manager._set_state(job_id, JobState.NAVIGATING)
        event = await asyncio.wait_for(queue.get(), timeout=1)

        assert event["snapshot"]["state"] == JobState.NAVIGATING.value
        assert event["sequence"] > last_sequence
    finally:
        manager.unsubscribe_events(job_id, queue)


@pytest.mark.asyncio
async def test_job_manager_passes_profile_id_to_runner(tmp_path: Path):
    config = StorageConfig(cache_root=tmp_path / "cache", db_path=tmp_path / "runs.db")
    captured: dict[str, Any] = {}

    async def runner(*, job_id: str, url: str, store: Store, config=None):  # noqa: ANN001
        captured["profile_id"] = getattr(config, "profile_id", None)
        return await _fake_runner(job_id=job_id, url=url, store=store, config=config)

    manager = JobManager(store=Store(config), runner=runner)
    snapshot = await manager.create_job(
        JobCreateRequest(url="https://example.com/profile", profile_id="agent-alpha")
    )
    job_id = snapshot["id"]
    await manager._tasks[job_id]

    assert snapshot["profile_id"] == "agent-alpha"
    assert captured["profile_id"] == "agent-alpha"
    record = manager.store.fetch_run(job_id)
    assert record is not None and record.profile_id == "agent-alpha"


@pytest.mark.asyncio
async def test_job_manager_reuses_cache(tmp_path: Path):
    config = StorageConfig(cache_root=tmp_path / "cache", db_path=tmp_path / "runs.db")
    manager = JobManager(store=Store(config), runner=_fake_runner)

    first = await manager.create_job(JobCreateRequest(url="https://example.com/cache", reuse_cache=False))
    await manager._tasks[first["id"]]
    source_record = manager.store.fetch_run(first["id"])
    assert source_record is not None
    assert source_record.cache_key is not None
    assert manager.store.find_cache_hit(source_record.cache_key) is not None

    second = await manager.create_job(JobCreateRequest(url="https://example.com/cache"))

    assert second["state"] == JobState.DONE.value
    assert second["cache_hit"] is True
    assert second["progress"]["done"] == second["progress"]["total"]
    assert second["artifacts"], "Cache hit should include artifact metadata"
    assert second["id"] not in manager._tasks


@pytest.mark.asyncio
async def test_cache_key_respects_overrides(tmp_path: Path):
    config = StorageConfig(cache_root=tmp_path / "cache", db_path=tmp_path / "runs.db")
    manager = JobManager(store=Store(config), runner=_fake_runner)

    first = await manager.create_job(
        JobCreateRequest(url="https://example.com/override", viewport_width=1400, reuse_cache=False)
    )
    await manager._tasks[first["id"]]

    same_override = await manager.create_job(
        JobCreateRequest(url="https://example.com/override", viewport_width=1400)
    )
    assert same_override["cache_hit"] is True

    different_override = await manager.create_job(
        JobCreateRequest(url="https://example.com/override", viewport_width=1200)
    )
    assert different_override["state"] == JobState.BROWSER_STARTING
    await manager._tasks[different_override["id"]]


@pytest.mark.asyncio
async def test_job_manager_webhook_delivery(tmp_path: Path):
    config = StorageConfig(cache_root=tmp_path / "cache", db_path=tmp_path / "runs.db")
    sent: list[dict] = []

    async def _sender(url: str, payload: dict):  # noqa: ANN001
        sent.append({"url": url, "payload": payload})

    manager = JobManager(store=Store(config), runner=_fake_runner, webhook_sender=_sender)
    snapshot = await manager.create_job(JobCreateRequest(url="https://example.com/hook"))
    job_id = snapshot["id"]

    manager.register_webhook(job_id, url="https://example.com/webhook", events=[JobState.DONE.value])
    await manager._tasks[job_id]
    await asyncio.sleep(0)

    assert sent, "webhook sender should be invoked"
    assert sent[-1]["payload"]["state"] == JobState.DONE.value


@pytest.mark.asyncio
async def test_register_webhook_persists_to_store(tmp_path: Path):
    config = StorageConfig(cache_root=tmp_path / "cache", db_path=tmp_path / "runs.db")
    store = Store(config)
    manager = JobManager(store=store, runner=_fake_runner)
    snapshot = await manager.create_job(JobCreateRequest(url="https://example.com/web"))
    job_id = snapshot["id"]
    await manager._tasks[job_id]

    manager.register_webhook(job_id, url="https://example.com/hook", events=[JobState.DONE.value])

    records = store.list_webhooks(job_id)
    assert len(records) == 1
    assert records[0].url == "https://example.com/hook"
    assert records[0].events == [JobState.DONE.value]


@pytest.mark.asyncio
async def test_delete_webhook_removes_from_store(tmp_path: Path):
    config = StorageConfig(cache_root=tmp_path / "cache", db_path=tmp_path / "runs.db")
    store = Store(config)
    manager = JobManager(store=store, runner=_fake_runner)
    snapshot = await manager.create_job(JobCreateRequest(url="https://example.com/web"))
    job_id = snapshot["id"]
    await manager._tasks[job_id]
    manager.register_webhook(job_id, url="https://example.com/hook", events=[JobState.DONE.value])
    records = store.list_webhooks(job_id)
    assert records
    deleted = manager.delete_webhook(job_id, url="https://example.com/hook")
    assert deleted == 1
    assert store.list_webhooks(job_id) == []


@pytest.mark.asyncio
async def test_delete_webhook_after_job_cleanup(tmp_path: Path):
    config = StorageConfig(cache_root=tmp_path / "cache", db_path=tmp_path / "runs.db")
    store = Store(config)
    manager = JobManager(store=store, runner=_fake_runner)
    snapshot = await manager.create_job(JobCreateRequest(url="https://example.com/web"))
    job_id = snapshot["id"]
    await manager._tasks[job_id]
    manager.register_webhook(job_id, url="https://example.com/hook", events=[JobState.DONE.value])
    # simulate job cleanup (no snapshot in memory)
    manager._snapshots.pop(job_id, None)
    deleted = manager.delete_webhook(job_id, url="https://example.com/hook")
    assert deleted == 1
    assert store.list_webhooks(job_id) == []


def test_delete_webhook_removes_records(tmp_path: Path):
    config = StorageConfig(cache_root=tmp_path / "cache", db_path=tmp_path / "runs.db")
    store = Store(config)
    job_id = "job-cleanup"
    store.allocate_run(job_id=job_id, url="https://example.com", started_at=datetime.now())
    store.register_webhook(job_id=job_id, url="https://example.com/webhook", events=["DONE"])

    deleted = store.delete_webhook(job_id, url="https://example.com/webhook")

    assert deleted == 1
    assert store.list_webhooks(job_id) == []


@pytest.mark.asyncio
async def test_delete_webhook_requires_both_identifiers(tmp_path: Path):
    """When both id and url are provided, both must match before removal."""

    config = StorageConfig(cache_root=tmp_path / "cache", db_path=tmp_path / "runs.db")
    store = Store(config)
    manager = JobManager(store=store, runner=_fake_runner)
    snapshot = await manager.create_job(JobCreateRequest(url="https://example.com/web"))
    job_id = snapshot["id"]
    await manager._tasks[job_id]
    manager.register_webhook(job_id, url="https://example.com/hook", events=[JobState.DONE.value])
    records = store.list_webhooks(job_id)
    assert len(records) == 1
    webhook_id = records[0].id
    assert webhook_id is not None

    deleted = manager.delete_webhook(job_id, webhook_id=webhook_id + 1, url="https://example.com/hook")

    assert deleted == 0
    # Store still has the record because the ID mismatch prevented deletion.
    assert len(store.list_webhooks(job_id)) == 1
    assert manager._webhooks[job_id], "cached webhook list should remain intact"


def test_cache_key_changes_when_cft_version_differs():
    """Changing the CfT build should invalidate the cache key."""

    capture_config = CaptureConfig(url="https://example.com/cache-cft")
    default_key = _build_cache_key(config=capture_config, settings=job_global_settings)
    mutated_settings = replace(
        job_global_settings,
        browser=replace(job_global_settings.browser, cft_version="chrome-999.0.0"),
    )

    mutated_key = _build_cache_key(config=capture_config, settings=mutated_settings)

    assert mutated_key != default_key


def test_cache_key_changes_when_ocr_model_differs():
    """Switching OCR models must produce a distinct cache key."""

    capture_config = CaptureConfig(url="https://example.com/cache-ocr")
    default_key = _build_cache_key(config=capture_config, settings=job_global_settings)
    mutated_settings = replace(
        job_global_settings,
        ocr=replace(job_global_settings.ocr, model="olmOCR-2-7B-NEW"),
    )

    mutated_key = _build_cache_key(config=capture_config, settings=mutated_settings)

    assert mutated_key != default_key


class _DeleteStubStore:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int | None, str | None]] = []

    def delete_webhooks(self, *, job_id: str, webhook_id: int | None = None, url: str | None = None) -> int:
        self.calls.append((job_id, webhook_id, url))
        raise KeyError("Run not allocated yet")


def test_delete_webhook_handles_pending_entries():
    store = _DeleteStubStore()
    manager = JobManager(store=cast(Store, store), runner=_fake_runner)
    job_id = "pending-job"
    manager._snapshots[job_id] = {"id": job_id, "url": "https://example.com", "state": JobState.CAPTURING}
    manager._webhooks[job_id] = [{"url": "https://example.com/hook"}]
    manager._pending_webhooks[job_id] = [{"url": "https://example.com/hook"}]

    deleted = manager.delete_webhook(job_id, url="https://example.com/hook")

    assert deleted == 1
    assert manager._webhooks.get(job_id) is None
    assert manager._pending_webhooks.get(job_id) is None
    assert store.calls == [(job_id, None, "https://example.com/hook")]


def test_delete_webhook_unknown_job_propagates_keyerror():
    store = _DeleteStubStore()
    manager = JobManager(store=cast(Store, store), runner=_fake_runner)

    with pytest.raises(KeyError):
        manager.delete_webhook("missing-job", url="https://example.com/hook")
