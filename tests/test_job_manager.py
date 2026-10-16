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
        CaptureManifest,
        CaptureResult,
        ScrollPolicy,
        SweepStats,
    )
except OSError:  # pyvips missing in CI

    @dataclass
    class ScrollPolicy:
        settle_ms: int
        max_steps: int
        viewport_overlap_px: int
        viewport_step_px: int

    @dataclass
    class SweepStats:
        sweep_count: int
        total_scroll_height: int
        shrink_events: int
        retry_attempts: int
        overlap_pairs: int
        overlap_match_ratio: float

    @dataclass
    class CaptureManifest:
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
        cache_seed: str | None = None
        cache_key: str | None = None
        cache_hit: bool = False
        backend_id: str | None = None
        backend_mode: str | None = None
        hardware_path: str | None = None
        backend_reason_codes: list | None = None
        backend_reevaluate_after_s: int | None = None
        fallback_chain: list | None = None
        ocr_ms: int | None = None
        stitch_ms: int | None = None
        ocr_batches: list | None = None
        ocr_quota: dict | None = None
        ocr_local_service: dict | None = None
        ocr_failover_events: list | None = None

    @dataclass
    class CaptureResult:
        tiles: list
        manifest: CaptureManifest


import app.jobs as jobs_module  # noqa: E402
from app.jobs import JobManager, JobState  # noqa: E402
from app.schemas import JobCreateRequest  # noqa: E402
from app.store import StorageConfig, Store  # noqa: E402
from app.tiler import TileSlice  # noqa: E402


async def _fake_runner(*, job_id: str, url: str, store: Store, config=None, request=None):  # noqa: ANN001
    manifest_cls = cast(Any, CaptureManifest)
    scroll_policy_cls = cast(Any, ScrollPolicy)
    sweep_stats_cls = cast(Any, SweepStats)
    manifest = manifest_cls(
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
        scroll_policy=scroll_policy_cls(
            settle_ms=300,
            max_steps=10,
            viewport_overlap_px=120,
            viewport_step_px=1080,
        ),
        sweep_stats=sweep_stats_cls(
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
        ocr_local_service={
            "enabled": True,
            "endpoint": "http://localhost:8001/v1",
            "healthy": True,
            "action": "reused",
            "launch_attempts": 0,
            "restart_count": 0,
        },
        ocr_failover_events=[
            {
                "event": "backend_failed",
                "backend_id": "glm-ocr-local-openai",
                "reason_code": "runtime.failover.local-unhealthy",
            },
            {
                "event": "backend_selected",
                "backend_id": "glm-ocr-remote-openai",
                "reason_code": "runtime.failover.promoted",
            },
        ],
        seam_markers=[
            {"tile_index": 0, "position": "top", "hash": "aaa111"},
            {"tile_index": 1, "position": "bottom", "hash": "bbb222"},
        ],
        profile_id=getattr(config, "profile_id", None),
        cache_seed=getattr(config, "cache_seed", None),
        cache_key=getattr(config, "cache_key", None),
        backend_id="olmocr-remote-openai",
        backend_mode="openai-compatible",
        hardware_path="remote",
        backend_reason_codes=["policy.remote.fallback"],
        backend_reevaluate_after_s=120,
        fallback_chain=["olmocr-remote-openai"],
        hardware_capabilities={
            "cpu_logical_cores": 8,
            "gpu_count": 0,
            "has_gpu": False,
            "preferred_hardware_path": "cpu",
        },
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
    result_cls = cast(Any, CaptureResult)
    return result_cls(tiles=tiles, manifest=manifest), artifacts


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
    seam_summary = final_snapshot.get("seam_markers")
    if isinstance(seam_summary, dict):
        assert seam_summary.get("count") == 2
        assert seam_summary.get("unique_hashes") == 2
    else:
        assert isinstance(seam_summary, list)
        assert len(seam_summary) == 2


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
    ocr_events = [entry for entry in events if entry.get("event") == "ocr_telemetry"]
    assert ocr_events[-1]["data"]["backend_id"] == "olmocr-remote-openai"
    assert ocr_events[-1]["data"]["backend_mode"] == "openai-compatible"
    assert ocr_events[-1]["data"]["backend_reason_codes"] == ["policy.remote.fallback"]
    assert ocr_events[-1]["data"]["backend_reevaluate_after_s"] == 120
    assert ocr_events[-1]["data"]["gpu_count"] == 0
    assert ocr_events[-1]["data"]["local_service"]["action"] == "reused"
    assert ocr_events[-1]["data"]["failover"]["event_count"] == 2


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

    async def runner(*, job_id: str, url: str, store: Store, config=None, request=None):  # noqa: ANN001
        captured["profile_id"] = getattr(config, "profile_id", None)
        return await _fake_runner(job_id=job_id, url=url, store=store, config=config, request=request)

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
async def test_job_manager_reuses_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    config = StorageConfig(cache_root=tmp_path / "cache", db_path=tmp_path / "runs.db")
    manager = JobManager(store=Store(config), runner=_fake_runner)
    base_settings = jobs_module.global_settings
    monkeypatch.setattr(
        jobs_module,
        "global_settings",
        replace(base_settings, ocr=replace(base_settings.ocr, local_url="")),
    )

    first = await manager.create_job(
        JobCreateRequest(url="https://example.com/cache", reuse_cache=False)
    )
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
async def test_cache_key_respects_overrides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    config = StorageConfig(cache_root=tmp_path / "cache", db_path=tmp_path / "runs.db")
    manager = JobManager(store=Store(config), runner=_fake_runner)
    base_settings = jobs_module.global_settings
    monkeypatch.setattr(
        jobs_module,
        "global_settings",
        replace(base_settings, ocr=replace(base_settings.ocr, local_url="")),
    )

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

    manager.register_webhook(
        job_id, url="https://example.com/webhook", events=[JobState.DONE.value]
    )
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

    deleted = manager.delete_webhook(
        job_id, webhook_id=webhook_id + 1, url="https://example.com/hook"
    )

    assert deleted == 0
    # Store still has the record because the ID mismatch prevented deletion.
    assert len(store.list_webhooks(job_id)) == 1
    assert manager._webhooks[job_id], "cached webhook list should remain intact"


@pytest.mark.asyncio
async def test_cache_key_changes_when_cft_version_differs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Changing the CfT build should invalidate the cache key."""

    config = StorageConfig(cache_root=tmp_path / "cache", db_path=tmp_path / "runs.db")
    manager = JobManager(store=Store(config), runner=_fake_runner)
    url = "https://example.com/cache-cft"
    first = await manager.create_job(JobCreateRequest(url=url, reuse_cache=False))
    await manager._tasks[first["id"]]

    base_settings = jobs_module.global_settings
    mutated_settings = replace(
        base_settings,
        browser=replace(base_settings.browser, cft_version="chrome-999.0.0"),
    )
    monkeypatch.setattr(jobs_module, "global_settings", mutated_settings)
    try:
        second = await manager.create_job(JobCreateRequest(url=url))
        assert second["state"] != JobState.DONE.value
        await manager._tasks[second["id"]]
    finally:
        monkeypatch.setattr(jobs_module, "global_settings", base_settings)


@pytest.mark.asyncio
async def test_cache_key_changes_when_ocr_model_differs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Switching OCR models must produce a distinct cache key."""

    config = StorageConfig(cache_root=tmp_path / "cache", db_path=tmp_path / "runs.db")
    manager = JobManager(store=Store(config), runner=_fake_runner)
    url = "https://example.com/cache-ocr"
    first = await manager.create_job(JobCreateRequest(url=url, reuse_cache=False))
    await manager._tasks[first["id"]]

    base_settings = jobs_module.global_settings
    mutated_settings = replace(
        base_settings,
        ocr=replace(base_settings.ocr, model="olmOCR-2-7B-NEW"),
    )
    monkeypatch.setattr(jobs_module, "global_settings", mutated_settings)
    try:
        second = await manager.create_job(JobCreateRequest(url=url))
        assert second["state"] != JobState.DONE.value
        await manager._tasks[second["id"]]
    finally:
        monkeypatch.setattr(jobs_module, "global_settings", base_settings)


def test_refresh_manifest_cache_key_uses_runtime_backend_path() -> None:
    manifest = types.SimpleNamespace(
        cache_seed="seed-abc",
        backend_id="glm-ocr-remote-openai",
        backend_mode="openai-compatible",
        hardware_path="remote",
        fallback_chain=["glm-ocr-local-openai", "glm-ocr-remote-openai"],
        cache_key="stale-key",
    )

    jobs_module._refresh_manifest_cache_key(cast(Any, manifest))

    assert manifest.cache_key == jobs_module._build_runtime_cache_key(
        cache_seed="seed-abc",
        backend_id="glm-ocr-remote-openai",
        backend_mode="openai-compatible",
        hardware_path="remote",
        fallback_chain=["glm-ocr-local-openai", "glm-ocr-remote-openai"],
    )


class _DeleteStubStore:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int | None, str | None]] = []

    def delete_webhooks(
        self, *, job_id: str, webhook_id: int | None = None, url: str | None = None
    ) -> int:
        self.calls.append((job_id, webhook_id, url))
        raise KeyError("Run not allocated yet")


def test_delete_webhook_handles_pending_entries():
    store = _DeleteStubStore()
    manager = JobManager(store=cast(Store, store), runner=_fake_runner)
    job_id = "pending-job"
    manager._snapshots[job_id] = {
        "id": job_id,
        "url": "https://example.com",
        "state": JobState.CAPTURING,
    }
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


@pytest.mark.asyncio
async def test_job_manager_stage_progress_and_global_stream(tmp_path: Path):
    """Multi-tile capture emits monotonic stage + tile progress to per-job and global subscribers."""
    config = StorageConfig(cache_root=tmp_path / "cache", db_path=tmp_path / "runs.db")
    store = Store(config)

    async def _staged_runner(*, job_id: str, url: str, store: Store, config=None, request=None):  # noqa: ANN001
        cb = getattr(config, "on_stage", None)
        if callable(cb):
            cb("NAVIGATING", {"done": 0, "total": 0})
            cb("SCROLLING", {"done": 0, "total": 0, "sweep_count": 2})
            cb("CAPTURING", {"done": 0, "total": 0})
            cb("TILING", {"done": 0, "total": 3})
            cb("OCR_SUBMITTING", {"done": 0, "total": 3})
            for idx in range(1, 4):
                cb("OCR_WAITING", {"done": idx, "total": 3})
            cb("STITCHING", {"done": 3, "total": 3})
        res, artifacts = await _fake_runner(
            job_id=job_id, url=url, store=store, config=config, request=request
        )
        res.manifest.tiles_total = 3
        return res, artifacts

    manager = JobManager(store=store, runner=_staged_runner)
    global_queue = manager.subscribe_global()
    try:
        snapshot = await manager.create_job(JobCreateRequest(url="https://example.com/staged"))
        job_id = snapshot["id"]
        assert "job_id" not in snapshot  # canonical JobSnapshot key is "id"
        await manager._tasks[job_id]

        events = manager.get_events(job_id)
        states_seen = [
            e["snapshot"]["state"] for e in events if e.get("event") == "snapshot"
        ]
        for expected_state in (
            JobState.BROWSER_STARTING.value,
            JobState.NAVIGATING.value,
            JobState.SCROLLING.value,
            JobState.CAPTURING.value,
            JobState.TILING.value,
            JobState.OCR_SUBMITTING.value,
            JobState.OCR_WAITING.value,
            JobState.STITCHING.value,
            JobState.DONE.value,
        ):
            assert expected_state in states_seen, f"missing {expected_state} in {states_seen}"

        ocr_waiting_done = [
            e["snapshot"]["progress"]["done"]
            for e in events
            if e.get("event") == "snapshot"
            and e["snapshot"]["state"] == JobState.OCR_WAITING.value
        ]
        assert ocr_waiting_done == [1, 2, 3]

        sequences = [e["sequence"] for e in events]
        assert sequences == list(range(len(events)))

        # Verify global subscriber received events with job_id + monotonic sequences
        global_events: list[dict[str, Any]] = []
        while not global_queue.empty():
            global_events.append(global_queue.get_nowait())
        assert len(global_events) == len(events)
        assert all(ge["job_id"] == job_id for ge in global_events)
        assert [ge["sequence"] for ge in global_events] == sequences

        # Verify subscribe_events(min_sequence=...) cursor continuity
        mid_seq = sequences[len(sequences) // 2]
        backlog, sub_q = manager.subscribe_events(job_id, min_sequence=mid_seq)
        manager.unsubscribe_events(job_id, sub_q)
        assert [e["sequence"] for e in backlog] == [s for s in sequences if s > mid_seq]
    finally:
        manager.unsubscribe_global(global_queue)


@pytest.mark.asyncio
async def test_subscriber_queue_is_bounded_and_drops_oldest(tmp_path: Path):
    """Stalled subscribers cannot cause unbounded queue growth."""
    config = StorageConfig(cache_root=tmp_path / "cache", db_path=tmp_path / "runs.db")
    manager = JobManager(store=Store(config), runner=_fake_runner)
    global_q = manager.subscribe_global()
    try:
        for idx in range(jobs_module._SUBSCRIBER_QUEUE_MAXSIZE + 40):
            jobs_module._put_bounded(global_q, {"seq": idx})
        assert global_q.qsize() == jobs_module._SUBSCRIBER_QUEUE_MAXSIZE
        first_retained = global_q.get_nowait()
        assert first_retained["seq"] == 40
    finally:
        manager.unsubscribe_global(global_q)


@pytest.mark.asyncio
async def test_job_cancellation_prevents_done_overwrite_and_persists(tmp_path: Path):
    """Cancelling an in-flight job marks CANCELLED in memory and Store and cleans up task."""
    config = StorageConfig(cache_root=tmp_path / "cache", db_path=tmp_path / "runs.db")
    store = Store(config)
    started = asyncio.Event()

    async def _slow_runner(*, job_id: str, url: str, store: Store, config=None, request=None):  # noqa: ANN001
        started.set()
        await asyncio.sleep(10.0)
        return await _fake_runner(
            job_id=job_id, url=url, store=store, config=config, request=request
        )

    manager = JobManager(store=store, runner=_slow_runner)
    snapshot = await manager.create_job(JobCreateRequest(url="https://example.com/cancel-me"))
    job_id = snapshot["id"]
    await asyncio.wait_for(started.wait(), timeout=2.0)

    cancelled = manager.cancel_job(job_id, reason="user requested abort")
    assert cancelled is True

    task = manager._tasks.get(job_id)
    if task is not None:
        with pytest.raises(asyncio.CancelledError):
            await task

    final_snap = manager.get_snapshot(job_id)
    assert final_snap is not None
    assert final_snap["state"] == JobState.CANCELLED.value
    assert final_snap["error"] == "user requested abort"
    assert job_id not in manager._tasks

    record = store.fetch_run(job_id)
    assert record is not None
    assert record.status == JobState.CANCELLED.value


@pytest.mark.asyncio
async def test_job_timeout_marks_failed_and_persists(tmp_path: Path):
    """Jobs exceeding job_timeout_seconds transition to FAILED and persist status."""
    config = StorageConfig(cache_root=tmp_path / "cache", db_path=tmp_path / "runs.db")
    store = Store(config)

    async def _hanging_runner(*, job_id: str, url: str, store: Store, config=None, request=None):  # noqa: ANN001
        await asyncio.sleep(5.0)
        return await _fake_runner(
            job_id=job_id, url=url, store=store, config=config, request=request
        )

    manager = JobManager(store=store, runner=_hanging_runner, job_timeout_seconds=1)
    snapshot = await manager.create_job(JobCreateRequest(url="https://example.com/timeout"))
    job_id = snapshot["id"]
    await asyncio.wait_for(manager._tasks[job_id], timeout=3.0)

    final_snap = manager.get_snapshot(job_id)
    assert final_snap is not None
    assert final_snap["state"] == JobState.FAILED.value
    assert "timeout" in (final_snap.get("error") or "").lower()
    record = store.fetch_run(job_id)
    assert record is not None
    assert record.status == JobState.FAILED.value


@pytest.mark.asyncio
async def test_job_manager_shutdown_cancels_active_tasks(tmp_path: Path):
    """JobManager.shutdown cancels all in-flight tasks and stops the watchdog."""
    config = StorageConfig(cache_root=tmp_path / "cache", db_path=tmp_path / "runs.db")
    store = Store(config)
    started = asyncio.Event()

    async def _blocking_runner(*, job_id: str, url: str, store: Store, config=None, request=None):  # noqa: ANN001
        started.set()
        await asyncio.sleep(30.0)
        return await _fake_runner(
            job_id=job_id, url=url, store=store, config=config, request=request
        )

    manager = JobManager(store=store, runner=_blocking_runner)
    manager.start_watchdog()
    snap = await manager.create_job(JobCreateRequest(url="https://example.com/shutdown"))
    job_id = snap["id"]
    await asyncio.wait_for(started.wait(), timeout=2.0)

    await manager.shutdown(timeout_s=2.0)
    assert manager._tasks == {}
    final_snap = manager.get_snapshot(job_id)
    assert final_snap is not None
    assert final_snap["state"] == JobState.CANCELLED.value


@pytest.mark.asyncio
async def test_cache_lifecycle_accounting_invalidation_and_clear(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Verify miss -> hit -> refresh accounting, URL invalidation, and clear without deleting files."""
    from fastapi.testclient import TestClient
    import app.main as main_module

    base_settings = jobs_module.global_settings
    monkeypatch.setattr(
        jobs_module,
        "global_settings",
        replace(base_settings, ocr=replace(base_settings.ocr, local_url="")),
    )
    config = StorageConfig(cache_root=tmp_path / "cache", db_path=tmp_path / "runs.db")
    store = Store(config)
    manager = JobManager(store=store, runner=_fake_runner)
    monkeypatch.setattr(main_module, "store", store)
    monkeypatch.setattr(main_module, "JOB_MANAGER", manager)

    url = "https://example.com/cache-lifecycle"

    # 1. Cold miss
    miss_snap = await manager.create_job(JobCreateRequest(url=url))
    assert miss_snap["cache_hit"] is False
    await manager._tasks[miss_snap["id"]]
    miss_final = manager.get_snapshot(miss_snap["id"])
    assert miss_final is not None
    assert miss_final["cache_hit"] is False
    manifest_file = Path(miss_final["manifest_path"])
    assert manifest_file.exists()

    # 2. Warm hit
    hit_snap = await manager.create_job(JobCreateRequest(url=url))
    assert hit_snap["state"] == JobState.DONE.value
    assert hit_snap["cache_hit"] is True
    assert hit_snap["cache_source_job_id"] == miss_snap["id"]
    assert hit_snap["manifest"]["cache_hit"] is True
    assert hit_snap["manifest"]["cache_source_job_id"] == miss_snap["id"]

    # 3. Forced refresh (reuse_cache=False) -> miss
    refresh_snap = await manager.create_job(JobCreateRequest(url=url, reuse_cache=False))
    assert refresh_snap["cache_hit"] is False
    await manager._tasks[refresh_snap["id"]]

    # Verify list_jobs and /metrics/cache agree
    jobs_list, total_jobs = manager.list_jobs(limit=20)
    assert total_jobs == 3
    by_id = {item["id"]: item for item in jobs_list}
    assert by_id[miss_snap["id"]]["cache_hit"] is False
    assert by_id[hit_snap["id"]]["cache_hit"] is True
    assert by_id[hit_snap["id"]]["cache_source_job_id"] == miss_snap["id"]
    assert by_id[refresh_snap["id"]]["cache_hit"] is False

    with TestClient(main_module.app) as client:
        metrics_resp = client.get("/metrics/cache")
        assert metrics_resp.status_code == 200
        metrics_data = metrics_resp.json()
        assert metrics_data["enabled"] is True
        assert metrics_data["entries"] == 1
        assert metrics_data["hits"] == 1
        assert metrics_data["misses"] == 2
        assert abs(metrics_data["hit_rate"] - (1 / 3)) < 1e-6

        # 4. Invalidate URL via JSON body and confirm next request is a miss while files remain on disk
        inv_resp = client.post("/admin/cache/invalidate", json={"url": url})
        assert inv_resp.status_code == 200
        inv_data = inv_resp.json()
        assert inv_data["removed"] is True
        assert inv_data["invalidated_entries"] >= 1
        assert manifest_file.exists(), "Cache invalidation must never delete artifact files"

        metrics_after_inv = client.get("/metrics/cache").json()
        assert metrics_after_inv["entries"] == 0

        post_inv_snap = await manager.create_job(JobCreateRequest(url=url))
        assert post_inv_snap["cache_hit"] is False
        await manager._tasks[post_inv_snap["id"]]

        # 5. Clear all cache entries and confirm non-destructive metadata invalidation
        clear_resp = client.post("/admin/cache/clear")
        assert clear_resp.status_code == 200
        clear_data = clear_resp.json()
        assert clear_data["cleared_entries"] >= 1
        assert manifest_file.exists(), "Cache clear must never delete artifact files"
        assert client.get("/metrics/cache").json()["entries"] == 0


def test_cache_key_sensitive_to_style_blocklist_prompt_semantic_and_profile_state(
    tmp_path: Path,
) -> None:
    """Verify cache seed/key changes across all capture, blocklist, OCR, semantic-post, and profile inputs."""
    from app.blocklist import cached_blocklist
    from app.cache import compute_cache_key_from_config
    from app.capture import CaptureConfig

    bl_v1 = tmp_path / "blocklist_v1.json"
    bl_v1.write_text(
        '{"version": "v1", "global": ["#cookie"], "domains": {"example.com": [".ad-v1"]}}',
        encoding="utf-8",
    )
    bl_v2 = tmp_path / "blocklist_v2.json"
    bl_v2.write_text(
        '{"version": "v2", "global": ["#cookie"], "domains": {"example.com": [".ad-v2"]}}',
        encoding="utf-8",
    )
    cached_blocklist.cache_clear()

    profiles_root = tmp_path / "profiles"
    profile_state_file = profiles_root / "prof1" / "storage_state.json"
    profile_state_file.parent.mkdir(parents=True, exist_ok=True)
    profile_state_file.write_text('{"cookies": [{"name": "sid", "value": "111"}]}', encoding="utf-8")

    base_settings = replace(
        jobs_module.global_settings,
        browser=replace(
            jobs_module.global_settings.browser,
            blocklist_path=bl_v1,
        ),
        storage=replace(
            jobs_module.global_settings.storage,
            profiles_root=profiles_root,
        ),
        ocr=replace(jobs_module.global_settings.ocr, local_url=""),
    )
    base_cfg = CaptureConfig(url="https://example.com/page", profile_id="prof1")
    base_key = compute_cache_key_from_config(base_cfg, base_settings)

    # 1. Blocklist selector/version change
    settings_bl2 = replace(
        base_settings,
        browser=replace(base_settings.browser, blocklist_path=bl_v2),
    )
    assert compute_cache_key_from_config(base_cfg, settings_bl2) != base_key

    # 2. Scroll settle ms change
    settings_settle = replace(
        base_settings,
        browser=replace(base_settings.browser, scroll_settle_ms=999),
    )
    assert compute_cache_key_from_config(base_cfg, settings_settle) != base_key

    # 3. Screenshot style hash change
    settings_style = replace(
        base_settings,
        browser=replace(base_settings.browser, screenshot_style_hash="custom-style-v9"),
    )
    assert compute_cache_key_from_config(base_cfg, settings_style) != base_key

    # 4. OCR policy / prompt template change
    cfg_glm = replace(base_cfg, ocr_policy="glm-ocr", ocr_model="glm-ocr")
    assert compute_cache_key_from_config(cfg_glm, base_settings) != base_key

    # 5. Semantic post-processing settings change
    cfg_sem1 = replace(base_cfg, semantic_post_enabled=True, semantic_post_max_chars=4000)
    cfg_sem2 = replace(base_cfg, semantic_post_enabled=True, semantic_post_max_chars=8000)
    key_sem1 = compute_cache_key_from_config(cfg_sem1, base_settings)
    key_sem2 = compute_cache_key_from_config(cfg_sem2, base_settings)
    assert key_sem1 != base_key
    assert key_sem1 != key_sem2

    # 6. Profile storage_state.json mutation on disk
    profile_state_file.write_text('{"cookies": [{"name": "sid", "value": "222"}]}', encoding="utf-8")
    assert compute_cache_key_from_config(base_cfg, base_settings) != base_key


@pytest.mark.asyncio
async def test_cache_hit_rejects_failed_truncated_and_expired_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Runs with validation failures, truncated sweeps, or expired TTL must never hit cache."""
    base_settings = jobs_module.global_settings
    monkeypatch.setattr(
        jobs_module,
        "global_settings",
        replace(base_settings, ocr=replace(base_settings.ocr, local_url="")),
    )
    config = StorageConfig(cache_root=tmp_path / "cache", db_path=tmp_path / "runs.db")
    store = Store(config)

    mode = {"kind": "validation_failure"}

    async def _flawed_runner(*, job_id: str, url: str, store: Store, config=None, request=None):  # noqa: ANN001
        res, artifacts = await _fake_runner(
            job_id=job_id, url=url, store=store, config=config, request=request
        )
        if mode["kind"] == "validation_failure":
            res.manifest.validation_failures = ["tile 0 checksum mismatch"]
        elif mode["kind"] == "truncated":
            res.manifest.sweep_stats.truncated_by_max_steps = True
        store.write_manifest(job_id=job_id, manifest=res.manifest)
        return res, artifacts

    manager = JobManager(store=store, runner=_flawed_runner)
    url = "https://example.com/flawed"

    # 1. Run with validation failure -> not eligible for cache hit
    first = await manager.create_job(JobCreateRequest(url=url, reuse_cache=False))
    await manager._tasks[first["id"]]
    rec1 = store.fetch_run(first["id"])
    assert rec1 is not None
    assert store.find_cache_hit(rec1.cache_key) is None

    # 2. Run with truncated_by_max_steps -> not eligible for cache hit
    mode["kind"] = "truncated"
    second = await manager.create_job(JobCreateRequest(url=url))
    assert second["cache_hit"] is False
    await manager._tasks[second["id"]]
    rec2 = store.fetch_run(second["id"])
    assert rec2 is not None
    assert store.find_cache_hit(rec2.cache_key) is None

    # 3. Clean run -> eligible, then expired when TTL=0
    mode["kind"] = "clean"
    third = await manager.create_job(JobCreateRequest(url=url))
    assert third["cache_hit"] is False
    await manager._tasks[third["id"]]
    rec3 = store.fetch_run(third["id"])
    assert rec3 is not None
    assert store.find_cache_hit(rec3.cache_key) is not None

    store.cache_ttl_hours = 0
    assert store.find_cache_hit(rec3.cache_key) is None


def test_failover_served_model_partitions_runtime_cache_key() -> None:
    """When served_ocr_model differs from requested ocr_model, runtime cache key is partitioned."""
    primary_manifest = types.SimpleNamespace(
        cache_seed="seed-123",
        backend_id="olmocr-remote-openai",
        backend_mode="openai-compatible",
        hardware_path="remote",
        fallback_chain=["olmocr-remote-openai"],
        ocr_model="olmOCR-2-7B-1025-FP8",
        served_ocr_model="olmOCR-2-7B-1025-FP8",
        cache_key="initial",
    )
    failover_manifest = types.SimpleNamespace(
        cache_seed="seed-123",
        backend_id="olmocr-remote-openai",
        backend_mode="openai-compatible",
        hardware_path="remote",
        fallback_chain=["olmocr-remote-openai"],
        ocr_model="olmOCR-2-7B-1025-FP8",
        served_ocr_model="glm-ocr-fallback",
        cache_key="initial",
    )
    jobs_module._refresh_manifest_cache_key(cast(Any, primary_manifest))
    jobs_module._refresh_manifest_cache_key(cast(Any, failover_manifest))
    assert primary_manifest.cache_key != failover_manifest.cache_key


@pytest.mark.asyncio
async def test_inflight_duplicate_request_reuses_primary_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Concurrent identical requests with reuse_cache=True coalesce onto the primary in-flight run."""
    base_settings = jobs_module.global_settings
    monkeypatch.setattr(
        jobs_module,
        "global_settings",
        replace(base_settings, ocr=replace(base_settings.ocr, local_url="")),
    )
    config = StorageConfig(cache_root=tmp_path / "cache", db_path=tmp_path / "runs.db")
    store = Store(config)
    gate = asyncio.Event()
    run_calls = 0

    async def _gated_runner(*, job_id: str, url: str, store: Store, config=None, request=None):  # noqa: ANN001
        nonlocal run_calls
        run_calls += 1
        await gate.wait()
        return await _fake_runner(
            job_id=job_id, url=url, store=store, config=config, request=request
        )

    manager = JobManager(store=store, runner=_gated_runner)
    url = "https://example.com/inflight-coalesce"

    first = await manager.create_job(JobCreateRequest(url=url, reuse_cache=True))
    second = await manager.create_job(JobCreateRequest(url=url, reuse_cache=True))

    assert second["cache_source_job_id"] == first["id"]
    gate.set()

    await asyncio.gather(
        manager._tasks[first["id"]],
        manager._tasks[second["id"]],
    )

    assert run_calls == 1
    first_final = manager.get_snapshot(first["id"])
    second_final = manager.get_snapshot(second["id"])
    assert first_final is not None and first_final["state"] == JobState.DONE.value
    assert first_final["cache_hit"] is False
    assert second_final is not None and second_final["state"] == JobState.DONE.value
    assert second_final["cache_hit"] is True
    assert second_final["cache_source_job_id"] == first["id"]

    stats = store.cache_stats()
    assert stats["misses"] == 1
    assert stats["hits"] == 1
    assert stats["entries"] == 1

