from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import io
import json
import tarfile

import pytest
import zstandard as zstd

from app.store import StorageConfig, Store
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


def _storage(tmp_path: Path) -> Store:
    cache_root = tmp_path / "cache"
    db_path = tmp_path / "runs.db"
    config = StorageConfig(cache_root=cache_root, db_path=db_path)
    return Store(config=config)


def _allocate_run(store: Store, *, job_id: str = "run-123") -> tuple[str, Path]:
    started = datetime(2025, 11, 8, 8, 0, tzinfo=timezone.utc)
    paths = store.allocate_run(job_id=job_id, url="https://example.com", started_at=started)
    return job_id, paths.root


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
    store.allocate_run(job_id=job_id, url="https://example.com/art", started_at=datetime(2025, 11, 8, 6, 0, tzinfo=timezone.utc))
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
    store.allocate_run(job_id="run-123", url="https://example.com", started_at=datetime(2025, 11, 8, 8, 0, tzinfo=timezone.utc))

    manifest = {
        "environment": {
            "cft_version": "chrome-130",
            "cft_label": "Stable-1",
            "server_runtime": "granian",
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
    assert record.sweep_shrink_events == 2
    assert record.sweep_retry_attempts == 1
    assert record.sweep_overlap_pairs == 6
    assert record.overlap_match_ratio == 0.87
    assert record.validation_failure_count == 2
    assert record.seam_marker_count == 3
    assert record.seam_hash_count == 2


def _sample_manifest_metadata() -> ManifestMetadata:
    return ManifestMetadata(
        environment=ManifestEnvironment(
            cft_version="chrome-130",
            cft_label="Stable-1",
            playwright_channel="cft",
            playwright_version="1.55.0",
            browser_transport="cdp",
            viewport=ViewportSettings(width=1280, height=2000, device_scale_factor=2, color_scheme="light"),
            viewport_overlap_px=120,
            tile_overlap_px=120,
            scroll_settle_ms=350,
            max_viewport_sweeps=200,
            screenshot_style_hash="demo",
            screenshot_mask_selectors=(),
            ocr_model="olmOCR-2-7B-1025-FP8",
            ocr_use_fp8=True,
            ocr_concurrency=ConcurrencyWindow(min=2, max=8),
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
        ocr_quota=ManifestOCRQuota(limit=100000, used=6000, threshold_ratio=0.7, warning_triggered=False),
        seam_markers=[
            {"tile_index": 0, "position": "top", "hash": "ccc111"},
            {"tile_index": 1, "position": "bottom", "hash": "ddd222"},
        ],
    )


def test_store_accepts_pydantic_manifest(tmp_path: Path) -> None:
    store = _storage(tmp_path)
    job_id = "run-meta"
    store.allocate_run(job_id=job_id, url="https://example.com/docs", started_at=datetime(2025, 11, 8, 9, 0, tzinfo=timezone.utc))
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
    assert record.seam_marker_count == 2
    assert record.seam_hash_count == 2

    saved = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert saved["environment"]["viewport"]["device_scale_factor"] == 2
    assert saved["sweep_stats"]["overlap_match_ratio"] == 0.91


def test_store_read_manifest_roundtrip(tmp_path: Path) -> None:
    store = _storage(tmp_path)
    job_id = "run-read"
    store.allocate_run(job_id=job_id, url="https://example.org", started_at=datetime(2025, 11, 8, 9, 30, tzinfo=timezone.utc))
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


def test_store_persists_ocr_batches_and_quota(tmp_path: Path) -> None:
    store = _storage(tmp_path)
    job_id = "run-ocr"
    store.allocate_run(job_id=job_id, url="https://example.com/ocr", started_at=datetime(2025, 11, 8, 10, 0, tzinfo=timezone.utc))
    manifest = {
        "environment": {
            "cft_version": "chrome-130",
            "viewport": {"width": 1280, "height": 2000, "device_scale_factor": 2, "color_scheme": "light"},
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
