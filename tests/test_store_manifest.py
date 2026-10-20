from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import hashlib
import io
import json
import tarfile

import pytest
from sqlmodel import select
import zstandard as zstd

from app.store import LinkRecord, StorageConfig, Store, _build_runtime_cache_key
from app.tiler import TileSlice
from app.schemas import (
    ConcurrencyWindow,
    ManifestEnvironment,
    ManifestMetadata,
    ManifestOCRBatch,
    ManifestOCRQuota,
    ManifestSweepStats,
    ManifestTimings,
    ViewportSettings,
)


def test_event_journal_is_atomic_durable_and_owner_scoped(tmp_path):
    from app.auth import AUTH_CONTEXT, AuthContext, claim_resource

    store = Store(StorageConfig(tmp_path / "cache", tmp_path / "runs.db"))
    owner = AuthContext(
        api_key_id=1, api_key_name="Alice", api_key_prefix="a", owner="alice", rate_limit=100
    )
    token = AUTH_CONTEXT.set(owner)
    try:
        claim_resource(store, "job:journal")
        store.allocate_run(
            job_id="journal",
            url="https://example.com/journal",
            started_at=datetime.now(timezone.utc),
        )
        first = store.append_job_event(
            job_id="journal",
            owner=owner.owner_key,
            entry={"event": "snapshot", "snapshot": {"state": "NAVIGATING"}},
        )
        second = store.append_job_event(
            job_id="journal",
            owner=owner.owner_key,
            entry={"event": "probe", "data": {"invoice": 7319}},
        )
        assert first["sequence"] == first["snapshot"]["sequence"] == 0
        assert second["sequence"] == 1
        assert first["cursor"].split(":")[0] == second["cursor"].split(":")[0]
        assert second["cursor"].endswith(":1")
        reopened = Store(store.config)
        try:
            assert reopened.read_job_events("journal", owner=owner.owner_key) == [first, second]
            epoch, sequence, events = reopened.read_global_events(owner=owner.owner_key)
            assert sequence == 2 and events == [first, second] and second["cursor"] == f"{epoch}:1"
            assert reopened.read_job_events("journal", owner="owner:bob") == []
            with pytest.raises(PermissionError):
                reopened.append_job_event(
                    job_id="journal", owner="owner:bob", entry={"event": "forged"}
                )
            with pytest.raises(ValueError):
                reopened.append_job_event(
                    job_id="journal",
                    owner=owner.owner_key,
                    entry={"event": "invalid", "value": float("nan")},
                )
            for limit in (0, 501):
                with pytest.raises(ValueError):
                    reopened.read_job_events("journal", owner=owner.owner_key, limit=limit)
                with pytest.raises(ValueError):
                    reopened.read_global_events(owner=owner.owner_key, limit=limit)
            assert reopened.read_global_events(owner=owner.owner_key) == (epoch, sequence, events)
        finally:
            reopened.engine.dispose()
    finally:
        AUTH_CONTEXT.reset(token)
        store.engine.dispose()


def test_competing_journal_writers_allocate_unique_dense_sequences(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    config = StorageConfig(tmp_path / "cache", tmp_path / "runs.db")
    store = Store(config)
    store.allocate_run(
        job_id="shared", url="https://example.com/shared", started_at=datetime.now(timezone.utc)
    )
    writers = [Store(config) for _ in range(4)]
    barrier = Barrier(4)

    def append(worker):
        barrier.wait(timeout=5)
        return [
            writers[worker].append_job_event(
                job_id="shared",
                owner=None,
                entry={"event": "probe", "data": {"worker": worker, "value": index}},
            )
            for index in range(12)
        ]

    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(append, range(4)))
        records = [entry for group in results for entry in group]
        assert sorted(entry["sequence"] for entry in records) == list(range(48))
        assert sorted(int(entry["cursor"].split(":")[1]) for entry in records) == list(range(48))
        assert len({entry["cursor"].split(":")[0] for entry in records}) == 1
        persisted = store.read_job_events("shared", owner=None)
        assert [entry["sequence"] for entry in persisted] == list(range(48))
        assert {(entry["data"]["worker"], entry["data"]["value"]) for entry in persisted} == {
            (worker, value) for worker in range(4) for value in range(12)
        }
    finally:
        for writer in writers:
            writer.engine.dispose()
        store.engine.dispose()


@pytest.mark.parametrize("terminal", ["DONE", "FAILED", "CANCELLED"])
def test_terminal_crash_recovery_appends_one_current_observation(tmp_path, terminal):
    from concurrent.futures import ThreadPoolExecutor

    store = Store(StorageConfig(tmp_path / "cache", tmp_path / "runs.db"))
    store.allocate_run(
        job_id="crash", url="https://example.com/crash", started_at=datetime.now(timezone.utc)
    )
    previous = store.append_job_event(
        job_id="crash",
        owner=None,
        entry={
            "event": "snapshot",
            "snapshot": {
                "id": "crash",
                "url": "https://example.com/crash",
                "state": "PERSISTING",
                "progress": {"done": 1, "total": 1},
            },
        },
    )
    store.update_status(job_id="crash", status=terminal, finished_at=datetime.now(timezone.utc))
    peers = [Store(store.config) for _ in range(2)]
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(
                    lambda peer: peer.recover_terminal_event(job_id="crash", owner=None), peers
                )
            )
        recovered = [result for result in results if result is not None]
        assert len(recovered) == 1
        event = recovered[0]
        assert event["sequence"] == previous["sequence"] + 1
        assert event["snapshot"]["state"] == terminal
        assert event["data"] == {
            "reason": "terminal-record-recovery",
            "previous_sequence": previous["sequence"],
        }
        assert store.read_job_events("crash", owner=None) == [previous, event]
        assert store.recover_terminal_event(job_id="crash", owner=None) is None
        assert store.recover_terminal_event(job_id="crash", owner="owner:foreign") is None
    finally:
        for peer in peers:
            peer.engine.dispose()
        store.engine.dispose()


def test_acknowledged_cancellation_recovery_updates_run_without_duplicate_event(tmp_path):
    store = Store(StorageConfig(tmp_path / "cache", tmp_path / "runs.db"))
    store.allocate_run(
        job_id="cancel-window",
        url="https://example.com/cancel",
        started_at=datetime.now(timezone.utc),
    )
    store.update_status(job_id="cancel-window", status="CAPTURING")
    cancelled = store.append_job_event(
        job_id="cancel-window",
        owner=None,
        entry={"event": "snapshot", "snapshot": {"state": "CANCELLED"}},
    )
    try:
        assert store.recover_terminal_event(job_id="cancel-window", owner=None) is None
        record = store.fetch_run("cancel-window")
        assert (
            record is not None and record.status == "CANCELLED" and record.finished_at is not None
        )
        assert store.read_job_events("cancel-window", owner=None) == [cancelled]
    finally:
        store.engine.dispose()


def _storage(tmp_path: Path) -> Store:
    cache_root = tmp_path / "cache"
    db_path = tmp_path / "runs.db"
    config = StorageConfig(cache_root=cache_root, db_path=db_path)
    return Store(config=config)


def _allocate_run(
    store: Store,
    *,
    job_id: str = "run-123",
    started_at: datetime | None = None,
    cache_key: str | None = None,
) -> tuple[str, Path]:
    started = started_at or datetime(2025, 11, 8, 8, 0, tzinfo=timezone.utc)
    paths = store.allocate_run(
        job_id=job_id,
        url="https://example.com",
        started_at=started,
        cache_key=cache_key,
    )
    return job_id, paths.root


def test_allocate_run_uses_cache_key_path(tmp_path: Path) -> None:
    store = _storage(tmp_path)
    started = datetime(2025, 11, 9, 12, 0, tzinfo=timezone.utc)
    cache_key = "ABCDEF1234567890"
    paths = store.allocate_run(
        job_id="run-cache",
        url="https://example.com/cache-path",
        started_at=started,
        cache_key=cache_key,
    )

    relative = paths.root.relative_to(tmp_path / "cache")
    parts = relative.parts
    assert parts[2] == "cache"
    assert parts[3] == cache_key[:2].lower()
    assert parts[4] == cache_key.lower()
    assert parts[-1] == started.astimezone(timezone.utc).strftime("%Y-%m-%d_%H%M%S")


def test_allocate_run_without_cache_key_preserves_timestamp_layout(tmp_path: Path) -> None:
    store = _storage(tmp_path)
    started = datetime(2025, 11, 9, 12, 30, tzinfo=timezone.utc)
    paths = store.allocate_run(
        job_id="run-nocache",
        url="https://example.com/no-cache",
        started_at=started,
    )

    relative = paths.root.relative_to(tmp_path / "cache")
    parts = relative.parts
    assert "cache" not in parts[:3]
    assert parts[-1] == started.astimezone(timezone.utc).strftime("%Y-%m-%d_%H%M%S")


def test_store_records_profile_id(tmp_path: Path) -> None:
    store = _storage(tmp_path)
    job_id = "run-profile"
    profile_id = "agent.alpha"
    store.allocate_run(
        job_id=job_id,
        url="https://example.com/profile",
        started_at=datetime(2025, 11, 8, 7, 0, tzinfo=timezone.utc),
        profile_id=profile_id,
    )
    manifest = {
        "profile_id": profile_id,
        "environment": {"cft_version": "chrome-130", "viewport": {"device_scale_factor": 2}},
    }
    store.write_manifest(job_id=job_id, manifest=manifest)

    record = store.fetch_run(job_id)
    assert record is not None
    assert record.profile_id == profile_id


def test_store_read_artifacts(tmp_path: Path) -> None:
    store = _storage(tmp_path)
    job_id = "run-artifacts"
    store.allocate_run(
        job_id=job_id,
        url="https://example.com/art",
        started_at=datetime(2025, 11, 8, 6, 0, tzinfo=timezone.utc),
    )
    tiles = [
        TileSlice(
            index=0,
            png_bytes=b"tile",
            sha256="sha0",
            width=100,
            height=200,
            scale=1.0,
            source_y_offset=0,
            viewport_y_offset=0,
            overlap_px=0,
            top_overlap_sha256=None,
            bottom_overlap_sha256=None,
        )
    ]
    store.write_tiles(job_id=job_id, tiles=tiles)

    artifacts = store.read_artifacts(job_id)
    assert artifacts and artifacts[0]["index"] == 0


def test_store_persists_sweep_and_validation_metadata(tmp_path: Path) -> None:
    store = _storage(tmp_path)
    store.allocate_run(
        job_id="run-123",
        url="https://example.com",
        started_at=datetime(2025, 11, 8, 8, 0, tzinfo=timezone.utc),
    )

    manifest = {
        "environment": {
            "cft_version": "chrome-130",
            "cft_label": "Stable-1",
            "server_runtime": "granian",
            "playwright_channel": "cft",
            "playwright_version": "1.55.0",
            "browser_transport": "cdp",
            "viewport": {
                "width": 1280,
                "height": 2000,
                "device_scale_factor": 2,
                "color_scheme": "light",
            },
            "viewport_overlap_px": 120,
            "tile_overlap_px": 120,
            "scroll_settle_ms": 350,
            "max_viewport_sweeps": 200,
            "screenshot_style_hash": "demo",
            "screenshot_mask_selectors": [],
            "ocr_model": "olmOCR-2-7B-1025-FP8",
            "ocr_provider": "olmocr",
            "ocr_use_fp8": True,
            "ocr_concurrency": {"min": 2, "max": 8},
            "ocr_backend_id": "olmocr-remote-openai",
            "ocr_backend_mode": "openai-compatible",
            "ocr_hardware_path": "remote",
            "ocr_fallback_chain": ["olmocr-remote-openai"],
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
        "seam_markers": [
            {"tile_index": 0, "position": "top", "hash": "aaa111"},
            {"tile_index": 1, "position": "bottom", "hash": "bbb222"},
            {"tile_index": 2, "position": "top", "hash": "bbb222"},
        ],
    }

    store.write_manifest(job_id="run-123", manifest=manifest)
    record = store.fetch_run("run-123")
    assert record is not None
    assert record.server_runtime == "granian"
    assert record.backend_id == "olmocr-remote-openai"
    assert record.backend_mode == "openai-compatible"
    assert record.hardware_path == "remote"
    assert record.fallback_chain == ["olmocr-remote-openai"]
    assert record.sweep_shrink_events == 2
    assert record.sweep_retry_attempts == 1
    assert record.sweep_overlap_pairs == 6
    assert record.overlap_match_ratio == 0.87
    assert record.validation_failure_count == 2
    assert record.seam_marker_count == 3
    assert record.seam_hash_count == 2
    assert getattr(record, "seam_markers_summary") == {
        "count": 3,
        "unique_tiles": 3,
        "unique_hashes": 2,
        "sample": [
            {"tile_index": 0, "position": "top", "hash": "aaa111"},
            {"tile_index": 1, "position": "bottom", "hash": "bbb222"},
            {"tile_index": 2, "position": "top", "hash": "bbb222"},
        ],
    }


def _sample_manifest_metadata() -> ManifestMetadata:
    return ManifestMetadata(
        environment=ManifestEnvironment(
            cft_version="chrome-130",
            cft_label="Stable-1",
            playwright_channel="cft",
            playwright_version="1.55.0",
            browser_transport="cdp",
            viewport=ViewportSettings(
                width=1280, height=2000, device_scale_factor=2, color_scheme="light"
            ),
            viewport_overlap_px=120,
            tile_overlap_px=120,
            scroll_settle_ms=350,
            max_viewport_sweeps=200,
            screenshot_style_hash="demo",
            screenshot_mask_selectors=(),
            ocr_model="olmOCR-2-7B-1025-FP8",
            ocr_provider="olmocr",
            ocr_use_fp8=True,
            ocr_concurrency=ConcurrencyWindow(min=2, max=8),
            ocr_backend_id="olmocr-remote-openai",
            ocr_backend_mode="openai-compatible",
            ocr_hardware_path="remote",
            ocr_fallback_chain=("olmocr-remote-openai",),
        ),
        sweep_stats=ManifestSweepStats(
            sweep_count=3,
            total_scroll_height=13000,
            shrink_events=1,
            retry_attempts=0,
            overlap_pairs=4,
            overlap_match_ratio=0.91,
        ),
        tiles_total=10,
        long_side_px=1288,
        overlap_match_ratio=0.91,
        validation_failures=["tile 0002 checksum mismatch"],
        timings=ManifestTimings(capture_ms=2100, ocr_ms=4200, stitch_ms=500),
        ocr_batches=[
            ManifestOCRBatch(
                tile_ids=["tile_0001"],
                latency_ms=900,
                status_code=200,
                request_id="req-1",
                payload_bytes=1_200_000,
                attempts=1,
            )
        ],
        ocr_quota=ManifestOCRQuota(
            limit=100000, used=6000, threshold_ratio=0.7, warning_triggered=False
        ),
        seam_markers=[
            {"tile_index": 0, "position": "top", "hash": "ccc111"},
            {"tile_index": 1, "position": "bottom", "hash": "ddd222"},
        ],
    )


def test_store_accepts_pydantic_manifest(tmp_path: Path) -> None:
    store = _storage(tmp_path)
    job_id = "run-meta"
    store.allocate_run(
        job_id=job_id,
        url="https://example.com/docs",
        started_at=datetime(2025, 11, 8, 9, 0, tzinfo=timezone.utc),
    )
    metadata = _sample_manifest_metadata()

    manifest_path = store.write_manifest(job_id=job_id, manifest=metadata)
    assert manifest_path.exists()

    record = store.fetch_run(job_id)
    assert record is not None
    assert record.long_side_px == 1288
    assert record.device_scale_factor == 2
    assert record.tiles_total == 10
    assert record.capture_ms == 2100
    assert record.ocr_ms == 4200
    assert record.overlap_match_ratio == 0.91
    assert record.validation_failure_count == 1
    assert record.backend_id == "olmocr-remote-openai"
    assert record.backend_mode == "openai-compatible"
    assert record.hardware_path == "remote"
    assert record.fallback_chain == ["olmocr-remote-openai"]
    assert record.seam_marker_count == 2
    assert record.seam_hash_count == 2
    assert getattr(record, "seam_markers_summary") == {
        "count": 2,
        "unique_tiles": 2,
        "unique_hashes": 2,
        "sample": [
            {"tile_index": 0, "position": "top", "hash": "ccc111"},
            {"tile_index": 1, "position": "bottom", "hash": "ddd222"},
        ],
    }

    saved = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert saved["environment"]["viewport"]["device_scale_factor"] == 2
    assert saved["sweep_stats"]["overlap_match_ratio"] == 0.91


def test_store_read_manifest_roundtrip(tmp_path: Path) -> None:
    store = _storage(tmp_path)
    job_id = "run-read"
    store.allocate_run(
        job_id=job_id,
        url="https://example.org",
        started_at=datetime(2025, 11, 8, 9, 30, tzinfo=timezone.utc),
    )
    manifest_dict = {
        "environment": {"cft_version": "chrome-131", "viewport": {"device_scale_factor": 2}},
        "timings": {"capture_ms": 1000},
        "tiles_total": 4,
        "long_side_px": 1200,
    }
    store.write_manifest(job_id=job_id, manifest=manifest_dict)

    loaded = store.read_manifest(job_id)
    assert loaded["environment"]["cft_version"] == "chrome-131"
    assert loaded["timings"]["capture_ms"] == 1000


def test_store_infers_backend_fields_for_legacy_manifest(tmp_path: Path) -> None:
    store = _storage(tmp_path)
    job_id = "run-legacy-backend"
    store.allocate_run(
        job_id=job_id,
        url="https://example.org/legacy",
        started_at=datetime(2025, 11, 8, 9, 35, tzinfo=timezone.utc),
    )
    manifest_dict = {
        "environment": {
            "cft_version": "chrome-131",
            "ocr_model": "olmOCR-2-7B-1025-FP8",
            "viewport": {"device_scale_factor": 2},
        }
    }
    store.write_manifest(job_id=job_id, manifest=manifest_dict)

    record = store.fetch_run(job_id)
    assert record is not None
    assert record.backend_id == "olmocr-remote-openai"
    assert record.backend_mode == "openai-compatible"
    assert record.hardware_path == "remote"
    assert record.fallback_chain == ["olmocr-remote-openai"]


def test_store_recomputes_cache_key_from_runtime_backend(tmp_path: Path) -> None:
    store = _storage(tmp_path)
    job_id = "run-runtime-cache"
    store.allocate_run(
        job_id=job_id,
        url="https://example.org/runtime-cache",
        started_at=datetime(2025, 11, 8, 9, 50, tzinfo=timezone.utc),
        cache_key="stale-pre-ocr-key",
    )
    manifest_dict = {
        "url": "https://example.org/runtime-cache",
        "cache_seed": "seed-xyz",
        "cache_key": "stale-pre-ocr-key",
        "backend_id": "glm-ocr-remote-openai",
        "backend_mode": "openai-compatible",
        "hardware_path": "remote",
        "fallback_chain": ["glm-ocr-local-openai", "glm-ocr-remote-openai"],
        "environment": {
            "cft_version": "chrome-131",
            "viewport": {"device_scale_factor": 2},
        },
    }
    store.write_manifest(job_id=job_id, manifest=manifest_dict)

    expected = _build_runtime_cache_key(
        cache_seed="seed-xyz",
        backend_id="glm-ocr-remote-openai",
        backend_mode="openai-compatible",
        hardware_path="remote",
        fallback_chain=["glm-ocr-local-openai", "glm-ocr-remote-openai"],
    )
    record = store.fetch_run(job_id)
    assert record is not None
    assert record.cache_key == expected
    loaded = store.read_manifest(job_id)
    assert loaded["cache_key"] == expected


def test_store_preserves_cache_key_for_legacy_manifests_without_seed(tmp_path: Path) -> None:
    store = _storage(tmp_path)
    job_id = "run-legacy-cache-key"
    store.allocate_run(
        job_id=job_id,
        url="https://example.org/legacy-cache",
        started_at=datetime(2025, 11, 8, 9, 55, tzinfo=timezone.utc),
        cache_key="legacy-cache-key",
    )
    manifest_dict = {
        "url": "https://example.org/legacy-cache",
        "cache_key": "legacy-cache-key",
        "backend_id": "olmocr-remote-openai",
        "backend_mode": "openai-compatible",
        "hardware_path": "remote",
        "fallback_chain": ["olmocr-remote-openai"],
    }
    store.write_manifest(job_id=job_id, manifest=manifest_dict)

    record = store.fetch_run(job_id)
    assert record is not None
    assert record.cache_key == "legacy-cache-key"
    loaded = store.read_manifest(job_id)
    assert loaded["cache_key"] == "legacy-cache-key"


def test_store_persists_ocr_batches_and_quota(tmp_path: Path) -> None:
    store = _storage(tmp_path)
    job_id = "run-ocr"
    store.allocate_run(
        job_id=job_id,
        url="https://example.com/ocr",
        started_at=datetime(2025, 11, 8, 10, 0, tzinfo=timezone.utc),
    )
    manifest = {
        "environment": {
            "cft_version": "chrome-130",
            "viewport": {
                "width": 1280,
                "height": 2000,
                "device_scale_factor": 2,
                "color_scheme": "light",
            },
        },
        "timings": {"capture_ms": 1800, "ocr_ms": 3600, "stitch_ms": 700},
        "ocr_batches": [
            {
                "tile_ids": ["tile_0001", "tile_0002"],
                "latency_ms": 950,
                "status_code": 200,
                "request_id": "req-123",
                "payload_bytes": 2456789,
                "attempts": 1,
            },
            {
                "tile_ids": ["tile_0003"],
                "latency_ms": 1250,
                "status_code": 429,
                "request_id": "req-124",
                "payload_bytes": 1056789,
                "attempts": 2,
            },
        ],
        "ocr_quota": {
            "limit": 100000,
            "used": 70000,
            "threshold_ratio": 0.7,
            "warning_triggered": True,
        },
    }

    store.write_manifest(job_id=job_id, manifest=manifest)
    record = store.fetch_run(job_id)
    assert record is not None
    assert record.ocr_ms == 3600

    loaded = store.read_manifest(job_id)
    assert len(loaded["ocr_batches"]) == 2
    assert loaded["ocr_batches"][0]["request_id"] == "req-123"
    assert loaded["ocr_batches"][1]["status_code"] == 429
    assert loaded["ocr_quota"]["warning_triggered"] is True


def test_resolve_artifact_allows_descendant_paths(tmp_path: Path) -> None:
    store = _storage(tmp_path)
    job_id, root = _allocate_run(store)
    target = root / "artifact" / "tiles"
    target.mkdir(parents=True, exist_ok=True)
    tile = target / "tile.png"
    tile.write_text("data", encoding="utf-8")

    resolved = store.resolve_artifact(job_id, "artifact/tiles/tile.png")

    assert resolved == tile


def test_resolve_artifact_blocks_path_escape(tmp_path: Path) -> None:
    store = _storage(tmp_path)
    job_id, root = _allocate_run(store)
    sibling = root.parent / f"{root.name}-sibling"
    sibling.mkdir(parents=True, exist_ok=True)
    secret = sibling / "secret.txt"
    secret.write_text("secret", encoding="utf-8")

    with pytest.raises(FileNotFoundError):
        store.resolve_artifact(job_id, f"../{sibling.name}/secret.txt")


def _tar_members(bundle: Path) -> list[str]:
    dctx = zstd.ZstdDecompressor()
    with open(bundle, "rb") as bundle_file:
        with dctx.stream_reader(bundle_file) as reader:
            decompressed = reader.read()
    with tarfile.open(fileobj=io.BytesIO(decompressed), mode="r:") as tar:
        return sorted(member.name for member in tar.getmembers())


def _seed_run_files(root: Path) -> None:
    (root / "out.md").write_text("markdown", encoding="utf-8")
    manifest = root / "manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    dom = root / "artifact" / "dom.html"
    dom.parent.mkdir(parents=True, exist_ok=True)
    dom.write_text("<html></html>", encoding="utf-8")
    tile_dir = root / "artifact" / "tiles"
    tile_dir.mkdir(parents=True, exist_ok=True)
    (tile_dir / "tile_0000.png").write_bytes(b"tile-data")


def test_build_bundle_includes_tiles_by_default(tmp_path: Path) -> None:
    store = _storage(tmp_path)
    job_id, root = _allocate_run(store, job_id="run-bundle")
    _seed_run_files(root)

    bundle = store.build_bundle(job_id=job_id)

    names = _tar_members(bundle)
    assert any(name.endswith("tile_0000.png") for name in names)
    assert any(name.endswith("out.md") for name in names)


def test_build_bundle_excludes_tiles_when_requested(tmp_path: Path) -> None:
    store = _storage(tmp_path)
    job_id, root = _allocate_run(store, job_id="run-bundle-no-tiles")
    _seed_run_files(root)

    bundle = store.build_bundle(job_id=job_id, include_tiles=False)

    names = _tar_members(bundle)
    assert not any("tiles" in name for name in names)
    assert any(name.endswith("out.md") for name in names)


def test_artifact_inventory_fresh_and_cached_matches_sha_and_bundle_replays_independently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.testclient import TestClient
    import app.main as main_mod
    from app.dom_links import LinkRecord as DomLink, blend_dom_with_ocr, serialize_links
    from app.jobs import JobManager

    store = _storage(tmp_path)
    jm = JobManager(store=store)
    monkeypatch.setattr(main_mod, "store", store)
    monkeypatch.setattr(main_mod, "JOB_MANAGER", jm)

    job_id, root = _allocate_run(store, job_id="run-fresh", cache_key="inv-key-1")
    tile_bytes = b"\x89PNG\r\n\x1a\nfaketile"
    tile_sha = hashlib.sha256(tile_bytes).hexdigest()
    store.write_tiles(
        job_id=job_id,
        tiles=[
            TileSlice(
                index=0,
                png_bytes=tile_bytes,
                width=1280,
                height=1000,
                scale=1.0,
                source_y_offset=0,
                viewport_y_offset=0,
                overlap_px=120,
                top_overlap_px=0,
                bottom_overlap_px=0,
                sha256=tile_sha,
                top_overlap_sha256=None,
                bottom_overlap_sha256=None,
            )
        ],
    )
    store.write_dom_snapshot(job_id=job_id, html=b"<html><body><a href='https://a.example'>A</a></body></html>")
    blended = blend_dom_with_ocr(
        dom_links=[DomLink(text="Alpha", href="https://a.example", source="DOM", delta="✓")],
        ocr_links=[
            DomLink(text="Alpha", href="https://a.example", source="OCR", delta="✓"),
            DomLink(text="Beta", href="https://b.example", source="OCR", delta="OCR only"),
        ],
    )
    store.write_links(job_id=job_id, links=serialize_links(blended))
    store.write_markdown(job_id=job_id, content="# Overview\n\nBody text with [Alpha](https://a.example).\n")
    store.write_manifest(
        job_id=job_id,
        manifest={
            "url": "https://example.com/fresh",
            "cft_label": "Stable-1",
            "cft_version": "chrome-130.0.6723.69",
            "playwright_version": "1.55.0",
            "browser_transport": "cdp",
            "screenshot_style_hash": "style-hash-1",
            "long_side_px": 1288,
            "tiles_total": 1,
            "capture_ms": 100,
            "ocr_ms": 200,
            "stitch_ms": 10,
        },
    )
    store.update_status(job_id=job_id, status="DONE", finished_at=datetime.now(timezone.utc))

    source_rec = store.fetch_run(job_id)
    assert source_rec is not None
    store.register_cached_run(job_id="run-cached", source=source_rec)

    client = TestClient(main_mod.app)
    resp_fresh = client.get("/jobs/run-fresh/artifacts")
    assert resp_fresh.status_code == 200
    body_fresh = resp_fresh.json()
    assert body_fresh["file_count"] >= 5
    assert body_fresh["artifact_root"] == "/jobs/run-fresh/artifact"
    assert str(tmp_path) not in json.dumps(body_fresh)
    assert body_fresh["shared_with_job_ids"] == ["run-cached"]
    assert body_fresh["purge_at"] is not None

    resp_cached = client.get("/jobs/run-cached/artifacts")
    assert resp_cached.status_code == 200
    body_cached = resp_cached.json()
    assert body_cached["cache_hit"] is True
    assert body_cached["cache_source_job_id"] == "run-fresh"
    assert body_cached["shared_with_job_ids"] == ["run-fresh"]
    assert {f["sha256"] for f in body_fresh["files"]} == {f["sha256"] for f in body_cached["files"]}

    # Verify pagination
    resp_page = client.get("/jobs/run-fresh/artifacts?limit=2&offset=1")
    assert resp_page.status_code == 200
    page_body = resp_page.json()
    assert page_body["file_count"] == body_fresh["file_count"]
    assert len(page_body["files"]) == 2
    assert page_body["has_more"] is True

    # On-demand bundle download and independent bundle inspection
    bundle_resp = client.get("/jobs/run-fresh/artifact/bundle.tar.zst")
    assert bundle_resp.status_code == 200
    bundle_file = tmp_path / "downloaded-bundle.tar.zst"
    bundle_file.write_bytes(bundle_resp.content)
    loaded_bundle = Store.load_bundle(bundle_file)
    assert loaded_bundle["manifest"]["url"] == "https://example.com/fresh"
    assert "Overview" in loaded_bundle["markdown"]
    assert len(loaded_bundle["links"]) == 2


def test_artifact_inventory_and_bundle_reject_corrupt_files_with_typed_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.testclient import TestClient
    import app.main as main_mod
    from app.jobs import JobManager

    store = _storage(tmp_path)
    jm = JobManager(store=store)
    monkeypatch.setattr(main_mod, "store", store)
    monkeypatch.setattr(main_mod, "JOB_MANAGER", jm)

    job_id, root = _allocate_run(store, job_id="run-corrupt")
    _seed_run_files(root)
    (root / "artifact" / "artifacts.json").write_text(
        json.dumps([{"index": 0, "path": "artifact/tiles/tile_0000.png", "sha256": "deadbeef"}]),
        encoding="utf-8",
    )

    client = TestClient(main_mod.app)
    # Corrupt tile SHA mismatch -> 422
    resp = client.get(f"/jobs/{job_id}/artifacts")
    assert resp.status_code == 422
    assert "SHA256 mismatch" in resp.json()["detail"]

    bundle_path = store.build_bundle(job_id=job_id)
    with pytest.raises(ValueError, match="SHA256 mismatch"):
        Store.load_bundle(bundle_path)

    # Corrupt links.json -> 422
    (root / "links.json").write_text("{corrupt-json", encoding="utf-8")
    links_resp = client.get(f"/jobs/{job_id}/links.json")
    assert links_resp.status_code == 422

    # Corrupt manifest.json -> 422
    (root / "manifest.json").write_text("{not-valid-json", encoding="utf-8")
    manifest_resp = client.get(f"/jobs/{job_id}/manifest.json")
    assert manifest_resp.status_code == 422
    art_resp = client.get(f"/jobs/{job_id}/artifacts")
    assert art_resp.status_code == 422


def test_all_nine_links_endpoints_and_sqlite_link_records_work_on_list_payloads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.testclient import TestClient
    import app.main as main_mod
    from app.dom_links import LinkRecord as DomLink, blend_dom_with_ocr, serialize_links
    from app.jobs import JobManager

    store = _storage(tmp_path)
    jm = JobManager(store=store)
    monkeypatch.setattr(main_mod, "store", store)
    monkeypatch.setattr(main_mod, "JOB_MANAGER", jm)

    job_id, _ = _allocate_run(store, job_id="run-links-1", cache_key="links-key-1")
    other_id, _ = _allocate_run(store, job_id="run-links-2", cache_key="links-key-2")
    blended = blend_dom_with_ocr(
        dom_links=[
            DomLink(text="Docs", href="https://example.com/docs", source="DOM", delta="✓"),
            DomLink(text="Shared", href="https://example.com/shared", source="DOM", delta="✓"),
        ],
        ocr_links=[
            DomLink(text="Shared", href="https://example.com/shared", source="OCR", delta="✓"),
            DomLink(text="OCR Only", href="https://example.com/ocr", source="OCR", delta="OCR only"),
        ],
    )
    store.write_links(job_id=job_id, links=serialize_links(blended))
    store.write_markdown(job_id=job_id, content="# Guide\n\nSee [Docs](https://example.com/docs).\n")
    store.write_manifest(job_id=job_id, manifest={"url": "https://example.com"})
    store.update_status(job_id=job_id, status="DONE", finished_at=datetime.now(timezone.utc))

    store.write_links(
        job_id=other_id,
        links=[{"text": "Shared", "href": "https://example.com/shared", "source": "DOM"}],
    )
    store.write_markdown(job_id=other_id, content="# Guide\n\nUpdated.\n")
    store.write_manifest(job_id=other_id, manifest={"url": "https://example.com"})
    store.update_status(job_id=other_id, status="DONE", finished_at=datetime.now(timezone.utc))

    # Verify LinkRecord rows persisted in SQLite
    with store.session() as session:
        rows = list(session.exec(select(LinkRecord).where(LinkRecord.run_id == job_id)))
        assert len(rows) == 3

    client = TestClient(main_mod.app)
    # 1. GET /jobs/{id}/result.json (JobManager.get_structured_result)
    r1 = client.get(f"/jobs/{job_id}/result.json")
    assert r1.status_code == 200
    assert len(r1.json()["links"]) == 3

    # 2. POST /jobs/{id}/diff
    r2 = client.post(f"/jobs/{job_id}/diff", json={"other_job_id": other_id, "include_links": True})
    assert r2.status_code == 200
    assert "https://example.com/shared" in r2.json()["links_common"]
    assert "https://example.com/docs" in r2.json()["links_a_only"]

    # 3. GET /jobs/{id}/headings/{heading}
    r3 = client.get(f"/jobs/{job_id}/headings/Guide")
    assert r3.status_code == 200
    assert "https://example.com/docs" in r3.json()["outbound_links"]

    # 4. POST /jobs/{id}/queries
    r4 = client.post(f"/jobs/{job_id}/queries", json={"queries": ["links", "toc", "summary"]})
    assert r4.status_code == 200
    assert len(r4.json()["results"]["links"]) == 3
    assert "https://example.com/docs" in r4.json()["results"]["toc"]["outbound_links"]

    # 5. GET /jobs/{id}/extract
    r5 = client.get(f"/jobs/{job_id}/extract")
    assert r5.status_code == 200
    assert len(r5.json()["links"]) == 3

    # 6. GET /jobs/{id}/links/{query}
    r6 = client.get(f"/jobs/{job_id}/links/docs")
    assert r6.status_code == 200
    assert len(r6.json()["matches"]) == 1

    # 7. GET /jobs/{id}/toc
    r7 = client.get(f"/jobs/{job_id}/toc")
    assert r7.status_code == 200
    assert len(r7.json()["outbound_links"]) == 3

    # 8. GET /jobs/{id}/summary
    r8 = client.get(f"/jobs/{job_id}/summary")
    assert r8.status_code == 200
    assert r8.json()["outbound_link_count"] == 3

    # 9. GET /jobs/{id}/links
    r9 = client.get(f"/jobs/{job_id}/links")
    assert r9.status_code == 200
    b9 = r9.json()
    assert b9["total"] == 3
    assert b9["counts"]["dom"] == 1
    assert b9["counts"]["ocr"] == 1
    assert b9["counts"]["both"] == 1


def test_retention_delete_and_admin_prune_preserve_shared_cache_and_cleanup_db_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import timedelta
    from fastapi.testclient import TestClient
    import app.main as main_mod
    from app.jobs import JobManager
    from app.store import WebhookRecord

    store = _storage(tmp_path)
    jm = JobManager(store=store)
    monkeypatch.setattr(main_mod, "store", store)
    monkeypatch.setattr(main_mod, "JOB_MANAGER", jm)

    old_dt = datetime.now(timezone.utc) - timedelta(days=15)
    src_id, src_root = _allocate_run(store, job_id="run-old-src", started_at=old_dt, cache_key="shared-k")
    _seed_run_files(src_root)
    store.write_links(job_id=src_id, links=[{"href": "https://example.com/a", "text": "A", "source": "DOM"}])
    store.write_markdown(job_id=src_id, content="# Shared Heading\n\nShared content body.\n")
    store.write_manifest(job_id=src_id, manifest={"url": "https://example.com"})
    store.update_status(job_id=src_id, status="DONE", finished_at=old_dt)
    with store.session() as session:
        session.add(WebhookRecord(job_id=src_id, url="https://hooks.example.com/cb", events=["DONE"]))
        session.commit()

    src_rec = store.fetch_run(src_id)
    assert src_rec is not None
    # Register a recent cached alias that shares src_root
    store.register_cached_run(job_id="run-recent-alias", source=src_rec)
    store.update_status(
        job_id="run-recent-alias",
        status="DONE",
        finished_at=datetime.now(timezone.utc),
    )

    client = TestClient(main_mod.app)
    # 1. Dry-run prune identifies run-old-src and notes it is shared-protected by run-recent-alias
    prune_dry = client.post("/admin/jobs/prune", json={"older_than_days": 7, "dry_run": True})
    assert prune_dry.status_code == 200
    pd_body = prune_dry.json()
    assert pd_body["candidates"] == 1
    assert pd_body["deleted"] == 0
    assert pd_body["candidate_job_ids"] == ["run-old-src"]
    assert pd_body["shared_protected_job_ids"] == ["run-old-src"]

    # 2. Execute prune (dry_run=False) -> awaits job_delete, cleans up SQLite rows for run-old-src, preserves shared files
    prune_exec = client.post("/admin/jobs/prune", json={"older_than_days": 7, "dry_run": False})
    assert prune_exec.status_code == 200
    pe_body = prune_exec.json()
    assert pe_body["candidates"] == 1
    assert pe_body["deleted"] == 1

    assert store.fetch_run("run-old-src") is None
    with store.session() as session:
        assert list(session.exec(select(LinkRecord).where(LinkRecord.run_id == "run-old-src"))) == []
        assert list(session.exec(select(WebhookRecord).where(WebhookRecord.job_id == "run-old-src"))) == []

    # 3. Alias run still has full access to all shared files on disk!
    alias_art = client.get("/jobs/run-recent-alias/artifacts")
    assert alias_art.status_code == 200
    assert alias_art.json()["file_count"] >= 4
    alias_md = client.get("/jobs/run-recent-alias/result.md")
    assert alias_md.status_code == 200
    assert "Shared Heading" in alias_md.text


@pytest.mark.asyncio
async def test_async_session_task_isolation_cancellation_and_engine_disposal(
    tmp_path: Path,
) -> None:
    import asyncio
    import sqlite3
    from app.store import RunRecord

    store = _storage(tmp_path)
    # Verify workload-driven indexes exist in SQLite schema
    with sqlite3.connect(store.db_path) as conn:
        idx_rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'"
        ).fetchall()
    idx_names = {r[0] for r in idx_rows}
    assert {
        "idx_runs_status_started_at",
        "idx_runs_owner_started_at",
        "idx_runs_cache_lookup",
        "idx_links_run_id",
        "idx_webhooks_job_id",
        "idx_section_embedding_meta_model_dim",
    }.issubset(idx_names)

    seen_session_ids: set[int] = set()

    async def _worker(idx: int) -> str:
        job_id = f"async-sess-{idx}"
        await asyncio.to_thread(
            store.allocate_run,
            job_id=job_id,
            url=f"https://example.com/{idx}",
            started_at=datetime.now(timezone.utc),
        )
        async with store.async_session() as session:
            seen_session_ids.add(id(session))
            await asyncio.sleep(0.01)
            rec = await session.get(RunRecord, job_id)
            assert rec is not None
            return rec.url

    urls = await asyncio.gather(*(_worker(i) for i in range(8)))
    assert len(urls) == 8
    assert len(seen_session_ids) == 8
    assert store.session_sharing_violations == 0
    assert len(store._active_task_sessions) == 0

    # Verify task cancellation inside async_session rolls back uncommitted changes and cleans up tracking
    entered = asyncio.Event()

    async def _cancelled_writer() -> None:
        async with store.async_session() as session:
            session.add(
                RunRecord(
                    id="cancelled-run",
                    url="https://example.com/cancelled",
                    status="PENDING",
                    started_at=datetime.now(timezone.utc),
                    manifest_path=str(tmp_path / "m.json"),
                    cache_path=str(tmp_path),
                )
            )
            await session.flush()
            entered.set()
            await asyncio.sleep(10.0)

    task = asyncio.create_task(_cancelled_writer())
    await entered.wait()
    assert len(store._active_task_sessions) == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(store._active_task_sessions) == 0
    assert await store.afetch_run("cancelled-run") is None

    await store.aclose()


@pytest.mark.asyncio
async def test_slow_db_writer_and_slow_embedder_do_not_stall_health_or_sse_streams(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    import sqlite3
    import threading
    import time
    from typing import Any
    import httpx
    import app.main as main_mod
    import app.embedders as embedders_mod
    from app.jobs import JobManager

    store = _storage(tmp_path)
    jm = JobManager(store=store)
    jm.start_watchdog()
    monkeypatch.setattr(main_mod, "store", store)
    monkeypatch.setattr(main_mod, "JOB_MANAGER", jm)

    job_id, _ = _allocate_run(store, job_id="run-concurrent")
    store.write_markdown(
        job_id=job_id,
        content="# Concurrency Test\n\n FastAPI event loop stays responsive under load.\n",
    )
    store.write_manifest(job_id=job_id, manifest={"url": "https://example.com/concurrent"})
    store.update_status(job_id=job_id, status="DONE", finished_at=datetime.now(timezone.utc))

    orig_get_embedder = embedders_mod.get_embedder

    class _SlowEmbedder:
        dim = 1536

        def __init__(self, base: Any) -> None:
            self._base = base

        def embed(self, text: str) -> list[float]:
            time.sleep(0.28)
            return self._base.embed(text)

    monkeypatch.setattr(
        embedders_mod,
        "get_embedder",
        lambda name="hash-bucket-v1": _SlowEmbedder(orig_get_embedder(name)),
    )

    lock_acquired = threading.Event()
    release_lock = threading.Event()

    def _hold_write_lock() -> None:
        conn = sqlite3.connect(store.db_path, timeout=30.0)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE runs SET status = 'DONE' WHERE id = ?",
                (job_id,),
            )
            lock_acquired.set()
            release_lock.wait(timeout=2.0)
            conn.rollback()
        finally:
            conn.close()

    writer_thread = threading.Thread(target=_hold_write_lock, daemon=True)
    writer_thread.start()
    assert lock_acquired.wait(timeout=2.0)

    try:
        transport = httpx.ASGITransport(app=main_mod.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # Launch slow embedder call in the background while DB write lock is ALSO held
            slow_embed_task = asyncio.create_task(
                client.post(
                    "/embeddings/text",
                    json={"text": "slow embedding computation", "model": "hash-bucket-v1"},
                )
            )
            await asyncio.sleep(0.02)

            t0 = time.perf_counter()
            live_resp, ready_resp = await asyncio.gather(
                client.get("/health/live"),
                client.get("/health/ready"),
            )
            health_elapsed_ms = (time.perf_counter() - t0) * 1000.0
            assert live_resp.status_code == 200
            assert ready_resp.status_code == 200
            assert health_elapsed_ms < 80.0, f"Health probes stalled for {health_elapsed_ms:.1f}ms"

            # Verify SSE /jobs/stream yields its initial subscription frame promptly while lock + slow embedder are active
            sse_t0 = time.perf_counter()
            async with client.stream("GET", "/jobs/stream") as sse_resp:
                assert sse_resp.status_code == 200
                first_line = await asyncio.wait_for(sse_resp.aiter_lines().__anext__(), timeout=0.5)
                assert "subscribed" in first_line
            sse_elapsed_ms = (time.perf_counter() - sse_t0) * 1000.0
            assert sse_elapsed_ms < 100.0, f"SSE stream stalled for {sse_elapsed_ms:.1f}ms"

            release_lock.set()
            embed_resp = await slow_embed_task
            assert embed_resp.status_code == 200
            assert embed_resp.json()["dim"] == 1536
    finally:
        release_lock.set()
        writer_thread.join(timeout=2.0)
        await jm.stop_watchdog()
        await store.aclose()


@pytest.mark.parametrize(
    "payload",
    [
        [{"href": "https://example.com/guide", "text": "Guide"}],
        {"anchors": [{"href": "https://example.com/guide", "text": "Guide"}], "total": 1},
    ],
)
def test_links_roundtrip_shapes_survive_store_reopen(tmp_path, payload):
    store = _storage(tmp_path)
    job_id, _ = _allocate_run(store)
    path = store.write_links(job_id=job_id, links=payload)
    assert json.loads(path.read_text()) == payload
    reopened = Store(store.config)
    assert reopened.read_links(job_id) == payload
    reopened.engine.dispose()
    store.engine.dispose()


@pytest.mark.parametrize("payload", [None, 1, "wrong", ["wrong"], [None]])
def test_invalid_links_document_refuses(tmp_path, payload):
    store = _storage(tmp_path)
    job_id, root = _allocate_run(store)
    (root / "links.json").write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="Invalid links payload"):
        store.read_links(job_id)


def test_read_paths_dont_create_directories_or_follow_outside_links(tmp_path):
    store = _storage(tmp_path)
    job_id, root = _allocate_run(store)
    outside = tmp_path / "secret.md"
    outside.write_text("private")
    (root / "out.md").symlink_to(outside)
    with pytest.raises(FileNotFoundError, match="escapes"):
        store.read_markdown(job_id)
    assert outside.read_text() == "private"
