from __future__ import annotations

import asyncio
import sys
import types
from collections.abc import AsyncGenerator
from datetime import datetime
from typing import Any, cast
from pathlib import Path
from threading import Event

from dataclasses import replace

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # pragma: no cover
    sys.path.append(str(ROOT))

from app.capture import (  # noqa: E402
    CaptureConfig,
    CaptureManifest,
    CaptureResult,
    ScrollPolicy,
    SweepStats,
)


import app.jobs as jobs_module  # noqa: E402
from app.jobs import JobManager, JobState  # noqa: E402
from app.auth import AUTH_CONTEXT, AuthContext  # noqa: E402
from app.schemas import JobCreateRequest  # noqa: E402
from app.store import StorageConfig, Store  # noqa: E402
from app.ocr_client import OCRBatchTelemetry, OCRQuotaStatus, SubmitTilesResult  # noqa: E402
from app.tiler import TileSlice  # noqa: E402


async def _fake_runner(*, job_id: str, url: str, store: Store, config=None, request=None):  # noqa: ANN001
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
    return CaptureResult(tiles=tiles, manifest=manifest), artifacts


@pytest.mark.parametrize("served", ["olmOCR-2-7B-1025", "google/gemma-4-31B-it", None])
async def test_manifest_preserves_provider_model_identity_and_substitution_warning(
    tmp_path, served
):
    """Telemetry contract; fixture metadata does not prove model availability."""
    store = Store(StorageConfig(tmp_path / "cache", tmp_path / "runs.db"))
    store.allocate_run(job_id="identity", url="https://example.com", started_at=datetime.now())
    capture_result, _ = await _fake_runner(
        job_id="identity", url="https://example.com", store=store
    )
    result = SubmitTilesResult(
        markdown_chunks=["known content"],
        batches=[
            OCRBatchTelemetry(
                tile_ids=("identity-tile-0000",),
                latency_ms=20,
                status_code=200,
                request_id="identity-request",
                payload_bytes=100,
                attempts=1,
                requested_model="olmOCR-2-7B-1025",
                served_model=served,
            )
        ],
        quota=OCRQuotaStatus(limit=None, used=1, threshold_ratio=0.7, warning_triggered=False),
    )
    jobs_module._apply_ocr_metadata(capture_result.manifest, result)
    store.write_manifest(job_id="identity", manifest=capture_result.manifest)
    persisted = store.read_manifest("identity")
    assert persisted is not None
    batch = persisted["ocr_batches"][0]
    assert batch["requested_model"] == "olmOCR-2-7B-1025"
    assert batch["served_model"] == served
    assert batch["request_id"] == "identity-request"
    warnings = [w for w in persisted["warnings"] if w["code"] == "ocr-model-substitution"]
    assert bool(warnings) == (served == "google/gemma-4-31B-it")
    if warnings:
        assert "google/gemma-4-31B-it" in warnings[0]["message"]
        assert "does not establish availability" in warnings[0]["message"]
    store.engine.dispose()


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
    assert final_snapshot is not None
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
    await manager.flush_events()

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

    await manager.flush_events()
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
        return await _fake_runner(
            job_id=job_id, url=url, store=store, config=config, request=request
        )

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
async def test_capture_stage_callbacks_reach_global_and_job_streams(tmp_path: Path):
    """Controlled capture callbacks prove lifecycle wiring, not hosted OCR quality."""
    entered, release = asyncio.Event(), asyncio.Event()

    async def runner(*, job_id: str, url: str, store: Store, config: CaptureConfig, request):
        for stage in ("BROWSER_STARTING", "NAVIGATING", "CAPTURING", "TILING", "OCR_SUBMITTING"):
            config.report_stage(stage)
        config.report_stage("OCR_WAITING", 0, 1)
        entered.set()
        await release.wait()
        config.report_stage("OCR_WAITING", 1, 1)
        config.report_stage("STITCHING")
        config.report_stage("PERSISTING")
        return await _fake_runner(
            job_id=job_id, url=url, store=store, config=config, request=request
        )

    store = Store(StorageConfig(tmp_path / "cache", tmp_path / "runs.db"))
    manager = JobManager(store=store, runner=runner)
    global_queue = manager.subscribe_all()
    snapshot = await manager.create_job(
        JobCreateRequest(url="https://example.com/stages", reuse_cache=False)
    )
    job_id = snapshot["id"]
    task = manager._tasks[job_id]
    queue = manager.subscribe(job_id)
    second_queue = manager.subscribe(job_id)
    await asyncio.wait_for(entered.wait(), 2)
    waiting = manager.get_snapshot(job_id)
    assert waiting is not None and waiting["state"] == "OCR_WAITING"
    assert waiting["progress"] == {"done": 0, "total": 1}
    release.set()
    await asyncio.wait_for(task, 2)
    events = manager.get_events(job_id)
    stages = [event["snapshot"]["state"] for event in events if event["event"] == "snapshot"]
    for stage in (
        "NAVIGATING",
        "TILING",
        "OCR_SUBMITTING",
        "OCR_WAITING",
        "STITCHING",
        "PERSISTING",
        "DONE",
    ):
        assert stage in stages
    assert stages.index("STITCHING") < stages.index("PERSISTING") < stages.index("DONE")
    assert [event["sequence"] for event in events] == list(range(len(events)))
    observed = []
    while not global_queue.empty():
        observed.append(global_queue.get_nowait())
    assert observed == events
    updates = []
    while not queue.empty():
        updates.append(queue.get_nowait())
    assert updates[-1]["state"] == "DONE"
    assert updates[-1]["progress"] == {"done": 1, "total": 1}
    assert updates[-1]["sequence"] == events[-1]["sequence"]
    second_updates = []
    while not second_queue.empty():
        second_updates.append(second_queue.get_nowait())
    assert second_updates == updates
    record = store.fetch_run(job_id)
    assert record is not None and record.status == "DONE"
    reopened = JobManager(store=Store(store.config))
    persisted = reopened.get_snapshot(job_id)
    assert persisted is not None and persisted["state"] == "DONE"
    assert persisted["progress"] == {"done": 1, "total": 1}
    manager.unsubscribe_all(global_queue)
    manager.unsubscribe(job_id, queue)
    manager.unsubscribe(job_id, second_queue)
    assert not manager._global_subscribers and not manager._subscribers


@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("write_fails", [False, True])
async def test_capture_artifact_write_keeps_api_responsive_and_drains_before_cancel(
    tmp_path, monkeypatch, cancel, write_fails
):
    """Actual execute/store boundary; upstream capture/OCR are explicit fixtures."""
    from app import main

    fixture_store = Store(StorageConfig(tmp_path / "fixture-cache", tmp_path / "fixture.db"))
    fixture_store.allocate_run(
        job_id="fixture", url="https://example.com/artifacts", started_at=datetime.now()
    )
    fixture_result, _ = await _fake_runner(
        job_id="fixture", url="https://example.com/artifacts", store=fixture_store
    )
    fixture_store.engine.dispose()
    entered, release, finished = Event(), Event(), Event()

    class GatedStore(Store):
        def write_tiles(self, *, job_id, tiles):
            entered.set()
            try:
                assert release.wait(3), "artifact barrier was not released"
                if write_fails:
                    raise OSError("controlled artifact write failure")
                return super().write_tiles(job_id=job_id, tiles=tiles)
            finally:
                finished.set()

    async def fixture_capture(config):
        return fixture_result

    async def fixture_ocr(**kwargs):
        return "# Artifact fixture\n\nInvoice 7319: 42.", 1, 1, []

    monkeypatch.setattr(jobs_module, "capture_tiles", fixture_capture)
    monkeypatch.setattr(jobs_module, "_run_ocr_pipeline", fixture_ocr)
    store = GatedStore(StorageConfig(tmp_path / "cache", tmp_path / "runs.db"))
    manager = JobManager(store=store)
    monkeypatch.setattr(main, "JOB_MANAGER", manager)
    snapshot = await manager.create_job(
        JobCreateRequest(url="https://example.com/artifacts", reuse_cache=False)
    )
    job_id = snapshot["id"]
    task = manager._tasks[job_id]
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        assert not task.done() and not finished.is_set()
        # Exercise the actual API handler while the real filesystem/DB writer
        # is waiting. An inline persistence mutant finishes/fails before here.
        response = await asyncio.wait_for(main.fetch_job(job_id), 0.2)
        assert response.state == "PERSISTING"
        if cancel:
            assert manager.cancel_job(job_id)
            await asyncio.sleep(0)
            assert not manager.cancel_job(job_id)  # API cancellation is idempotent.
            task.cancel()  # Shutdown may cancel an already draining task again.
            await asyncio.sleep(0)
            assert not task.done() and not finished.is_set()
            assert (await main.fetch_job(job_id)).state == "CANCELLED"
    finally:
        release.set()
    try:
        if cancel:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
            expected = "CANCELLED"
        elif write_fails:
            with pytest.raises(OSError, match="controlled artifact"):
                await asyncio.wait_for(task, 2)
            expected = "FAILED"
        else:
            await asyncio.wait_for(task, 2)
            expected = "DONE"
            assert "Invoice 7319: 42." in store.read_markdown(job_id)
            assert store.read_manifest(job_id)["tiles_total"] == 1
        assert finished.is_set()
        record = store.fetch_run(job_id)
        assert record is not None and record.status == expected and record.finished_at is not None
        final_snapshot = manager.get_snapshot(job_id)
        assert final_snapshot is not None and final_snapshot["state"] == expected
        assert job_id not in manager._tasks
        if cancel:
            assert not any(
                entry.get("snapshot", {}).get("state") == "DONE"
                for entry in manager.get_events(job_id)
            )
    finally:
        store.engine.dispose()


@pytest.mark.parametrize("terminal", ["DONE", "CANCELLED", "FAILED"])
async def test_per_job_event_history_reopens_with_identical_sequences_and_suffix(
    tmp_path, terminal
):
    store = Store(StorageConfig(tmp_path / "cache", tmp_path / "runs.db"))
    manager = JobManager(store=store, runner=_fake_runner)
    snapshot = await manager.create_job(
        JobCreateRequest(url="https://example.com/restart", reuse_cache=False)
    )
    job_id = snapshot["id"]
    await manager._tasks[job_id]
    if terminal != "DONE":
        store.update_status(job_id=job_id, status=terminal, finished_at=datetime.now())
        manager._set_state(job_id, JobState(terminal))
        await manager.flush_events()
    original = manager.get_events(job_id)
    cursor = original[1]["sequence"]
    reopened_store = Store(store.config)
    reopened = JobManager(store=reopened_store)
    try:
        await reopened.restore_job_history(job_id)
        assert reopened.get_events(job_id) == original
        restored = reopened.get_snapshot(job_id)
        assert restored is not None and restored["state"] == terminal
        backlog, queue = reopened.subscribe_events(job_id, min_sequence=cursor)
        assert backlog == original[2:]
        assert queue.empty()
        reopened.unsubscribe_events(job_id, queue)
    finally:
        reopened_store.engine.dispose()
        store.engine.dispose()


async def test_event_publication_waits_for_commit_and_pending_memory_is_bounded(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(jobs_module, "_PENDING_EVENT_LIMIT", 3)
    entered, release = Event(), Event()

    class GatedStore(Store):
        gate = False

        def append_job_event(self, **kwargs):
            if self.gate:
                entered.set()
                assert release.wait(4), "journal barrier was not released"
            return super().append_job_event(**kwargs)

    store = GatedStore(StorageConfig(tmp_path / "cache", tmp_path / "runs.db"))
    manager = JobManager(store=store, runner=_fake_runner)
    job = await manager.create_job(
        JobCreateRequest(url="https://example.com/journal", reuse_cache=False)
    )
    await manager._tasks[job["id"]]
    original = manager.get_events(job["id"])
    cursor = original[-1]["sequence"]
    _, queue = manager.subscribe_events(job["id"], min_sequence=cursor)
    global_queue = manager.subscribe_all()
    store.gate = True
    try:
        manager._record_custom_event(job["id"], "probe", {"value": "admitted"})
        assert await asyncio.to_thread(entered.wait, 2)
        assert queue.empty() and global_queue.empty()
        assert store.read_job_events(job["id"], owner=None) == original
        for value in range(3):
            manager._record_custom_event(job["id"], "probe", {"value": value})
        assert manager._pending_events.qsize() == 3
        with pytest.raises(jobs_module.EventPersistenceError, match="backlog"):
            manager._record_custom_event(job["id"], "probe", {"value": "overflow"})
        assert manager._pending_events.qsize() == 3
    finally:
        release.set()
        await manager.flush_events()
        manager.unsubscribe_events(job["id"], queue)
        manager.unsubscribe_all(global_queue)
    persisted = store.read_job_events(job["id"], owner=None)
    assert [entry["data"]["value"] for entry in persisted[len(original) :]] == ["admitted", 0, 1, 2]
    assert queue.qsize() == global_queue.qsize() == 4
    assert manager._pending_events.empty()
    assert manager._event_writer is not None and manager._event_writer.done()
    store.engine.dispose()


async def test_failed_event_commit_refuses_admission_and_never_broadcasts_success(tmp_path):
    class FailingStore(Store):
        def append_job_event(self, **kwargs):
            raise OSError("controlled event disk failure")

    store = FailingStore(StorageConfig(tmp_path / "cache", tmp_path / "runs.db"))
    manager = JobManager(store=store, runner=_fake_runner)
    global_queue = manager.subscribe_all()
    try:
        with pytest.raises(jobs_module.EventPersistenceError, match="journal"):
            await manager.create_job(
                JobCreateRequest(url="https://example.com/no-admission", reuse_cache=False)
            )
        assert manager._tasks == {} and global_queue.empty()
        assert store.read_global_events(owner=None)[2] == []
        assert manager._event_writer is not None and manager._event_writer.done()
    finally:
        manager.unsubscribe_all(global_queue)
        store.engine.dispose()


async def test_restore_cancellation_cannot_resurrect_as_capturing_and_purged_runs_stay_missing(
    tmp_path,
):
    store = Store(StorageConfig(tmp_path / "cache", tmp_path / "runs.db"))
    store.allocate_run(
        job_id="cancelled", url="https://example.com/cancelled", started_at=datetime.now()
    )
    store.update_status(job_id="cancelled", status="CAPTURING")
    store.append_job_event(
        job_id="cancelled",
        owner=None,
        entry={
            "event": "snapshot",
            "snapshot": {
                "id": "cancelled",
                "url": "https://example.com/cancelled",
                "state": "CANCELLED",
                "progress": {"done": 0, "total": 1},
            },
        },
    )
    manager = JobManager(store=store)
    snapshot = manager.get_snapshot("cancelled")
    assert snapshot is not None and snapshot["state"] == "CANCELLED"
    # Model a metadata purge without invoking any filesystem deletion API.
    from sqlalchemy import delete
    from sqlmodel import col
    from app.store import RunRecord

    with store.session() as session:
        session.exec(delete(RunRecord).where(col(RunRecord.id) == "cancelled"))
        session.commit()
    reopened = JobManager(store=Store(store.config))
    try:
        await reopened.restore_job_history("cancelled")
        assert reopened.get_snapshot("cancelled") is None
        with pytest.raises(KeyError):
            reopened.get_events("cancelled")
    finally:
        reopened.store.engine.dispose()
        store.engine.dispose()


async def test_event_cursor_replays_exact_successor_and_refuses_lost_history(tmp_path):
    store = Store(StorageConfig(tmp_path / "cache", tmp_path / "runs.db"))
    manager = JobManager(store=store, runner=_fake_runner)
    snapshot = await manager.create_job(
        JobCreateRequest(url="https://example.com/reconnect", reuse_cache=False)
    )
    job_id = snapshot["id"]
    await manager._tasks[job_id]
    original = manager.get_events(job_id)
    cursor = original[1]["sequence"]
    backlog, queue = manager.subscribe_events(job_id, min_sequence=cursor)
    try:
        assert backlog == original[2:]
        manager._record_custom_event(job_id, "probe", {"value": "live"})
        await manager.flush_events()
        live = queue.get_nowait()
        assert live["sequence"] == original[-1]["sequence"] + 1
        assert live["data"] == {"value": "live"}
    finally:
        manager.unsubscribe_events(job_id, queue)
    for index in range(650):
        manager._record_custom_event(job_id, "probe", {"value": index})
    await manager.flush_events()
    retained = manager.get_events(job_id)
    assert len(retained) == 500
    with pytest.raises(jobs_module.EventHistoryGap) as lost:
        manager.subscribe_events(job_id, min_sequence=cursor)
    assert lost.value.detail["first_available"] == retained[0]["sequence"]
    assert lost.value.detail["last_available"] == retained[-1]["sequence"]
    assert job_id not in manager._event_subscribers
    with pytest.raises(jobs_module.EventHistoryGap):
        manager.get_events(job_id, min_sequence=retained[-1]["sequence"] + 1)
    assert manager.get_events(job_id, min_sequence=retained[-1]["sequence"]) == []
    store.engine.dispose()


@pytest.mark.parametrize("event_count", [150, 650])
async def test_ndjson_stalled_consumer_recovers_or_reports_history_gap(
    tmp_path, monkeypatch, event_count
):
    import json
    from collections.abc import AsyncGenerator
    from starlette.requests import Request
    from app import main

    store = Store(StorageConfig(tmp_path / "cache", tmp_path / "runs.db"))
    manager = JobManager(store=store, runner=_fake_runner)
    snapshot = await manager.create_job(
        JobCreateRequest(url="https://example.com/slow-events", reuse_cache=False)
    )
    job_id = snapshot["id"]
    await manager._tasks[job_id]
    cursor = manager.get_events(job_id)[-1]["sequence"]
    monkeypatch.setattr(main, "JOB_MANAGER", manager)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    request = Request({"type": "http", "method": "GET", "path": "/", "headers": []}, receive)
    response = await main.job_events(job_id, request, after=cursor)
    for index in range(event_count):
        manager._record_custom_event(job_id, "probe", {"value": index})
    await manager.flush_events()
    queue = manager._event_subscribers[job_id][0]
    assert queue.qsize() == 128
    iterator = aiter(response.body_iterator)
    assert isinstance(iterator, AsyncGenerator)

    async def next_entry():
        fragment = await anext(iterator)
        return json.loads(fragment if isinstance(fragment, str) else bytes(fragment))

    try:
        if event_count == 150:
            recovered = [await next_entry() for _ in range(event_count)]
            assert [e["sequence"] for e in recovered] == list(
                range(cursor + 1, cursor + event_count + 1)
            )
            assert [e["data"]["value"] for e in recovered] == list(range(event_count))
        else:
            gap = await next_entry()
            assert gap["event"] == "gap" and gap["code"] == "event-history-gap"
            assert gap["after"] == cursor
            assert gap["first_available"] == cursor + event_count - 499
            with pytest.raises(StopAsyncIteration):
                await anext(iterator)
    finally:
        await iterator.aclose()
        store.engine.dispose()
    assert manager._event_subscribers == {}


@pytest.mark.parametrize("transport", ["html", "global"])
@pytest.mark.parametrize("event_count", [150, 650])
async def test_sse_stalled_consumer_replays_or_explicitly_reports_gap(
    tmp_path, monkeypatch, transport, event_count
):
    import json
    from starlette.requests import Request
    from app import main

    store = Store(StorageConfig(tmp_path / "cache", tmp_path / "runs.db"))
    manager = JobManager(store=store, runner=_fake_runner)
    snapshot = await manager.create_job(
        JobCreateRequest(url="https://example.com/sse-recovery", reuse_cache=False)
    )
    job_id = snapshot["id"]
    await manager._tasks[job_id]
    cursor = manager.get_events(job_id)[-1]["sequence"]
    global_cursor = manager.global_cursor()
    monkeypatch.setattr(main, "JOB_MANAGER", manager)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    request = Request({"type": "http", "method": "GET", "path": "/", "headers": []}, receive)
    if transport == "html":
        response = await main.job_stream(job_id, request, after=cursor)
    else:
        response = await main.jobs_global_stream(request, after=global_cursor)
    for index in range(event_count):
        manager._record_custom_event(job_id, "probe", {"value": index})
    await manager.flush_events()
    queue = (
        manager._event_subscribers[job_id][0]
        if transport == "html"
        else next(iter(manager._global_subscribers))
    )
    assert queue.qsize() == 128
    iterator = aiter(response.body_iterator)
    assert isinstance(iterator, AsyncGenerator)

    async def next_entry():
        while True:
            frame = await anext(iterator)
            assert isinstance(frame, str)
            if frame.startswith("event: gap"):
                assert "\nid:" not in frame
                return json.loads(frame.split("data: ", 1)[1])
            if transport == "html" and frame.startswith("event: job-event"):
                entry = json.loads(frame.split("data: ", 1)[1])
                assert f"\nid: {entry['sequence']}\n" in frame
                return entry
            if transport == "global" and frame.startswith("id:"):
                entry = json.loads(frame.split("data: ", 1)[1])
                if entry["event"] != "subscribed":
                    assert frame.startswith(f"id: {entry['cursor']}\n")
                    return entry

    try:
        if event_count == 150:
            recovered = [await next_entry() for _ in range(event_count)]
            assert [entry["sequence"] for entry in recovered] == list(
                range(cursor + 1, cursor + event_count + 1)
            )
            assert [entry["data"]["value"] for entry in recovered] == list(range(event_count))
        else:
            gap = await next_entry()
            assert gap["code"] == "event-history-gap"
            assert gap["after"] == (cursor if transport == "html" else global_cursor)
            with pytest.raises(StopAsyncIteration):
                await anext(iterator)
    finally:
        await iterator.aclose()
        store.engine.dispose()
    assert manager._event_subscribers == {} and manager._global_subscribers == {}


async def test_html_stream_commits_cursor_after_raw_and_replays_incomplete_group(
    tmp_path, monkeypatch
):
    from starlette.requests import Request
    from app import main

    store = Store(StorageConfig(tmp_path / "cache", tmp_path / "runs.db"))
    manager = JobManager(store=store, runner=_fake_runner)
    snapshot = await manager.create_job(
        JobCreateRequest(url="https://example.com/sse-group", reuse_cache=False)
    )
    job_id = snapshot["id"]
    await manager._tasks[job_id]
    store.write_markdown(job_id=job_id, content="# Sample output\n\nInvoice 7319: 42.")
    # Locate the real DONE entry in the shared event log, then disconnect after
    # state delivery but before the Markdown and cursor commit have arrived.
    done = next(
        entry
        for entry in manager.get_events(job_id)
        if entry.get("snapshot", {}).get("state") == "DONE"
    )
    cursor = done["sequence"] - 1
    monkeypatch.setattr(main, "JOB_MANAGER", manager)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    request = Request({"type": "http", "method": "GET", "path": "/", "headers": []}, receive)
    response = await main.job_stream(job_id, request, after=cursor)
    iterator = aiter(response.body_iterator)
    assert isinstance(iterator, AsyncGenerator)
    first = await anext(iterator)
    assert isinstance(first, str)
    assert first == "event: state\ndata: DONE\n\n" and "id:" not in first
    await iterator.aclose()
    assert manager._event_subscribers == {}

    # Native reconnect's Last-Event-ID overrides the original query cursor.
    reconnect = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [(b"last-event-id", str(cursor).encode())],
        },
        receive,
    )
    response = await main.job_stream(job_id, reconnect, after=-1)
    iterator = aiter(response.body_iterator)
    assert isinstance(iterator, AsyncGenerator)
    frames = []
    try:
        while True:
            frame = await anext(iterator)
            assert isinstance(frame, str)
            frames.append(frame)
            if str(frame).startswith("event: job-event"):
                break
        assert frames[0] == first
        raw_index = next(
            index for index, frame in enumerate(frames) if str(frame).startswith("event: raw")
        )
        assert "Sample output" in frames[raw_index]
        assert raw_index < len(frames) - 1
        assert all("\nid:" not in frame for frame in frames[:-1])
        assert f"\nid: {done['sequence']}\n" in frames[-1]
    finally:
        await iterator.aclose()
        store.engine.dispose()


def test_sse_error_text_is_escaped_and_cannot_inject_protocol_fields():
    from app import main

    error = '<img src=x onerror="window.injected=1">\r\n\r\nevent: forged\rdata: attack'
    event, payload = main._snapshot_events({"state": "FAILED", "error": error})[-1]
    assert event == "log" and "<img" not in payload and "&lt;img" in payload
    frame = main._sse_frame(event, payload)
    lines = frame.rstrip("\n").split("\n")
    assert lines[0] == "event: log"
    assert all(line.startswith("data: ") for line in lines[1:])
    assert "\nevent: forged" not in frame


async def test_global_cursors_survive_restart_and_refuse_foreign_and_future(tmp_path):
    store = Store(StorageConfig(tmp_path / "cache", tmp_path / "runs.db"))
    manager = JobManager(store=store, runner=_fake_runner)
    alice = AuthContext(
        api_key_id=1, api_key_name="Alice", api_key_prefix="a", owner="alice", rate_limit=100
    )
    bob = AuthContext(
        api_key_id=2, api_key_name="Bob", api_key_prefix="b", owner="bob", rate_limit=100
    )
    cursors = []
    for owner in (alice, bob):
        token = AUTH_CONTEXT.set(owner)
        try:
            await manager.restore_global_history()
            initial = manager.global_cursor()
            job = await manager.create_job(
                JobCreateRequest(url="https://example.com/owner", reuse_cache=False)
            )
            await manager._tasks[job["id"]]
            events = manager.get_global_events(after=initial)
            assert events and all(entry["job_id"] == job["id"] for entry in events)
            assert events == manager.get_events(job["id"])
            assert manager.get_global_events(after=manager.global_cursor()) == []
            cursors.append(manager.global_cursor())
        finally:
            AUTH_CONTEXT.reset(token)
    token = AUTH_CONTEXT.set(bob)
    try:
        with pytest.raises(jobs_module.GlobalEventHistoryGap):
            manager.subscribe_global_events(after=cursors[0])
        future = cursors[1].split(":")[0] + ":999999"
        with pytest.raises(jobs_module.GlobalEventHistoryGap):
            manager.subscribe_global_events(after=future)
        reopened = JobManager(store=store)
        await reopened.restore_global_history()
        assert reopened.global_cursor() == cursors[1]
        backlog, queue, cursor = reopened.subscribe_global_events(after=cursors[1])
        assert backlog == [] and cursor == cursors[1] and queue.empty()
        reopened.unsubscribe_all(queue)
        resumed = await reopened.create_job(
            JobCreateRequest(url="https://example.com/restarted", reuse_cache=False)
        )
        await reopened._tasks[resumed["id"]]
        suffix = reopened.get_global_events(after=cursors[1])
        assert suffix and all(entry["job_id"] == resumed["id"] for entry in suffix)
        assert not manager._global_subscribers and not reopened._global_subscribers
        for invalid in (
            "",
            "123",
            "x:1",
            "0" * 32 + ":-2",
            "0" * 32 + ":1:2",
            "0" * 32 + ":" + "9" * 5000,
        ):
            with pytest.raises(ValueError, match="Invalid global"):
                manager.subscribe_global_events(after=invalid)
    finally:
        AUTH_CONTEXT.reset(token)
        store.engine.dispose()


@pytest.mark.asyncio
async def test_cancelled_runner_cannot_report_late_success(tmp_path: Path):
    entered = asyncio.Event()

    async def runner(*, job_id: str, url: str, store: Store, config: CaptureConfig, request):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            config.report_stage("OCR_WAITING", 1, 1)
            return await _fake_runner(
                job_id=job_id, url=url, store=store, config=config, request=request
            )

    store = Store(StorageConfig(tmp_path / "cache", tmp_path / "runs.db"))
    manager = JobManager(store=store, runner=runner)
    snapshot = await manager.create_job(
        JobCreateRequest(url="https://example.com/cancel", reuse_cache=False)
    )
    job_id = snapshot["id"]
    task = manager._tasks[job_id]
    await asyncio.wait_for(entered.wait(), 2)
    assert manager.cancel_job(job_id)
    with pytest.raises(asyncio.CancelledError):
        await task
    final = manager.get_snapshot(job_id)
    assert final is not None and final["state"] == "CANCELLED"
    record = store.fetch_run(job_id)
    assert record is not None and record.status == "CANCELLED" and record.finished_at
    assert not manager.cancel_job(job_id)
    assert not manager.cancel_job("unknown")
    assert job_id not in manager._tasks


@pytest.mark.asyncio
async def test_cancellation_during_allocation_leaves_no_queued_ghost(tmp_path: Path):
    entered, release = Event(), Event()

    class DelayedStore(Store):
        def allocate_run(self, **kwargs):
            entered.set()
            if not release.wait(3):
                raise TimeoutError("allocation barrier was not released")
            return super().allocate_run(**kwargs)

    store = DelayedStore(StorageConfig(tmp_path / "cache", tmp_path / "runs.db"))
    manager = JobManager(store=store, runner=_fake_runner)
    snapshot = await manager.create_job(
        JobCreateRequest(url="https://example.com/allocation", reuse_cache=False)
    )
    job_id = snapshot["id"]
    task = manager._tasks[job_id]
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        assert manager.cancel_job(job_id)
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    record = store.fetch_run(job_id)
    assert record is not None and record.status == "CANCELLED"
    assert record.finished_at and record.tiles_total is None


@pytest.mark.asyncio
async def test_allocation_failure_surfaces_without_stuck_task(tmp_path: Path):
    class FailingStore(Store):
        def allocate_run(self, **kwargs):
            raise OSError("controlled allocation failure")

    store = FailingStore(StorageConfig(tmp_path / "cache", tmp_path / "runs.db"))
    manager = JobManager(store=store, runner=_fake_runner)
    snapshot = await manager.create_job(
        JobCreateRequest(url="https://example.com/failure", reuse_cache=False)
    )
    job_id = snapshot["id"]
    with pytest.raises(OSError, match="controlled allocation failure"):
        await manager._tasks[job_id]
    final = manager.get_snapshot(job_id)
    assert final is not None and final["state"] == "FAILED"
    assert final["error"] == "controlled allocation failure"
    assert store.fetch_run(job_id) is None and job_id not in manager._tasks


@pytest.mark.asyncio
async def test_global_stream_is_bounded_and_owner_scoped(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(jobs_module, "_SUBSCRIBER_BACKLOG", 3)
    store = Store(StorageConfig(tmp_path / "cache", tmp_path / "runs.db"))
    manager = JobManager(store=store, runner=_fake_runner)
    alice = AuthContext(
        api_key_id=1, api_key_name="Alice", api_key_prefix="mdwb_a", owner="alice", rate_limit=100
    )
    bob = AuthContext(
        api_key_id=2, api_key_name="Bob", api_key_prefix="mdwb_b", owner="bob", rate_limit=100
    )
    token = AUTH_CONTEXT.set(alice)
    try:
        alice_queue = manager.subscribe_all()
        snapshot = await manager.create_job(
            JobCreateRequest(url="https://example.com/private", reuse_cache=False)
        )
        await manager._tasks[snapshot["id"]]
    finally:
        AUTH_CONTEXT.reset(token)
    token = AUTH_CONTEXT.set(bob)
    try:
        bob_queue = manager.subscribe_all()
        assert manager.get_snapshot(snapshot["id"]) is None
    finally:
        AUTH_CONTEXT.reset(token)
    for index in range(20):
        manager._record_custom_event(snapshot["id"], "bounded", {"index": index})
    await manager.flush_events()
    assert alice_queue.qsize() == 3 and bob_queue.empty()
    assert [alice_queue.get_nowait()["data"]["index"] for _ in range(3)] == [17, 18, 19]
    manager.unsubscribe_all(alice_queue)
    manager.unsubscribe_all(bob_queue)
    assert not manager._global_subscribers


def test_manifest_replay_preserves_capture_and_policy_options():
    options = JobCreateRequest(
        url="https://example.com/options",
        profile_id="research",
        viewport_width=800,
        viewport_height=900,
        device_scale_factor=3,
        color_scheme="dark",
        ocr_policy="olmOCR-2-7B-1025",
        semantic_post_enabled=True,
        semantic_post_max_chars=1200,
        reuse_cache=False,
        tags=["dataset:real"],
    )
    rebuilt = jobs_module.request_from_manifest(
        {"url": options.url, "request_options": options.model_dump()}
    )
    assert rebuilt == options
    for invalid in (
        {"url": options.url, "request_options": {"url": "https://different.example"}},
        {"url": options.url, "request_options": []},
        {"url": options.url, "profile_id": "../escape"},
        {"url": options.url, "requested_ocr_policy": "unknown-policy"},
    ):
        with pytest.raises(ValueError):
            jobs_module.request_from_manifest(invalid)


def test_semantic_processing_changes_cache_identity():
    request = JobCreateRequest(url="https://example.com/semantic-cache")
    settings = jobs_module.global_settings
    plain = jobs_module._build_capture_config(request, settings)
    enabled = jobs_module._build_capture_config(
        request.model_copy(update={"semantic_post_enabled": True}), settings
    )
    bounded = jobs_module._build_capture_config(
        request.model_copy(update={"semantic_post_enabled": True, "semantic_post_max_chars": 1200}),
        settings,
    )
    assert (
        len(
            {
                jobs_module._build_cache_key(config=config, settings=settings)
                for config in (plain, enabled, bounded)
            }
        )
        == 3
    )


async def test_done_is_not_published_before_terminal_database_write(tmp_path, monkeypatch):
    store = Store(StorageConfig(tmp_path / "cache", tmp_path / "runs.db"))
    entered, release = Event(), Event()
    update = store.update_status

    def gated_update(**kwargs):
        if kwargs.get("status") == JobState.DONE:
            entered.set()
            assert release.wait(5), "test did not release final persistence"
        return update(**kwargs)

    monkeypatch.setattr(store, "update_status", gated_update)
    manager = JobManager(store=store, runner=_fake_runner)
    snapshot = await manager.create_job(JobCreateRequest(url="https://example.com/persist"))
    task = manager._tasks[snapshot["id"]]
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        current = manager.get_snapshot(snapshot["id"])
        record = store.fetch_run(snapshot["id"])
        assert current is not None and current["state"] != JobState.DONE
        assert record is not None and record.status != JobState.DONE
    finally:
        release.set()
        await task
        store.engine.dispose()
    final = manager.get_snapshot(snapshot["id"])
    assert final is not None and final["state"] == JobState.DONE


@pytest.mark.parametrize("blocked_status", [JobState.CAPTURING, JobState.DONE])
async def test_cancellation_drains_status_write_before_terminal_commit(
    tmp_path, monkeypatch, blocked_status
):
    store = Store(StorageConfig(tmp_path / "cache", tmp_path / "runs.db"))
    entered, release = Event(), Event()
    update = store.update_status

    def gated_update(**kwargs):
        if kwargs.get("status") == blocked_status:
            entered.set()
            assert release.wait(5), "test did not release status write"
        return update(**kwargs)

    monkeypatch.setattr(store, "update_status", gated_update)
    manager = JobManager(store=store, runner=_fake_runner)
    snapshot = await manager.create_job(
        JobCreateRequest(url="https://example.com/cancel-write", reuse_cache=False)
    )
    job_id = snapshot["id"]
    task = manager._tasks[job_id]
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        assert manager.cancel_job(job_id)
        # A second cancellation must not interrupt the draining wait and allow
        # the blocked CAPTURING/DONE transaction to overwrite CANCELLED later.
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(task), 0.05)
        assert not task.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    reopened = Store(store.config)
    try:
        record = reopened.fetch_run(job_id)
        assert record is not None and record.status == "CANCELLED" and record.finished_at
        events = manager.get_events(job_id)
        states = [e["snapshot"]["state"] for e in events if e["event"] == "snapshot"]
        assert states[-1] == "CANCELLED" and "DONE" not in states
        assert manager._tasks == {}
    finally:
        reopened.engine.dispose()
        store.engine.dispose()


async def test_shutdown_deadline_prevents_late_success_from_cancellation_swallowing_runner(
    tmp_path,
):
    store = Store(StorageConfig(tmp_path / "cache", tmp_path / "runs.db"))
    started, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def stubborn_runner(**kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
        return await _fake_runner(**kwargs)

    manager = JobManager(store=store, runner=stubborn_runner, shutdown_timeout_seconds=0.02)
    snapshot = await manager.create_job(JobCreateRequest(url="https://example.com/shutdown"))
    task = manager._tasks[snapshot["id"]]
    try:
        await asyncio.wait_for(started.wait(), 5)
        await asyncio.wait_for(manager.stop_watchdog(), 1)
        assert cancelled.is_set() and not task.done()
        current = manager.get_snapshot(snapshot["id"])
        assert current is not None and current["state"] == JobState.CANCELLED
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    record = store.fetch_run(snapshot["id"])
    assert record is not None and record.status == JobState.CANCELLED
    assert manager._tasks == {}
    store.engine.dispose()


async def test_shutdown_cancels_and_reaps_tracked_webhook_tasks(tmp_path):
    store = Store(StorageConfig(tmp_path / "cache", tmp_path / "runs.db"))
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def sender(url, payload):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    manager = JobManager(store=store, runner=_fake_runner, webhook_sender=sender)
    snapshot = await manager.create_job(JobCreateRequest(url="https://example.com/webhook"))
    await manager._tasks[snapshot["id"]]
    manager.register_webhook(snapshot["id"], url="https://example.com/hook", events=["DONE"])
    manager._set_state(snapshot["id"], JobState.DONE)
    await asyncio.wait_for(started.wait(), 5)
    assert len(manager._webhook_tasks) == 1
    await manager.stop_watchdog()
    assert cancelled.is_set() and manager._webhook_tasks == set()
    store.engine.dispose()


async def test_shutdown_deadline_includes_watchdog_cleanup(tmp_path):
    store = Store(StorageConfig(tmp_path / "cache", tmp_path / "runs.db"))
    started, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def draining_watchdog():
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()

    manager = JobManager(store=store, shutdown_timeout_seconds=0.02)
    watchdog = asyncio.create_task(draining_watchdog())
    manager._watchdog_task = watchdog
    try:
        await asyncio.wait_for(started.wait(), 1)
        await asyncio.wait_for(manager.stop_watchdog(), 0.5)
        assert cancelled.is_set() and not watchdog.done()
    finally:
        release.set()
        await asyncio.gather(watchdog, return_exceptions=True)
        store.engine.dispose()
