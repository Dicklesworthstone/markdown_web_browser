"""Job state machine and capture orchestration helpers."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from importlib import metadata
from typing import TypedDict

from app.capture import CaptureConfig, CaptureResult, capture_tiles
from app.schemas import ManifestMetadata
from app.settings import Settings, settings as global_settings
from app.store import Store, build_store

try:  # Playwright is an optional dependency in some CI environments
    PLAYWRIGHT_VERSION = metadata.version("playwright")
except metadata.PackageNotFoundError:  # pragma: no cover - development convenience
    PLAYWRIGHT_VERSION = None


class JobState(str, Enum):
    """Enumerated lifecycle states for a capture job."""

    BROWSER_STARTING = "BROWSER_STARTING"
    NAVIGATING = "NAVIGATING"
    SCROLLING = "SCROLLING"
    CAPTURING = "CAPTURING"
    TILING = "TILING"
    OCR_SUBMITTING = "OCR_SUBMITTING"
    OCR_WAITING = "OCR_WAITING"
    STITCHING = "STITCHING"
    DONE = "DONE"
    FAILED = "FAILED"


class JobSnapshot(TypedDict, total=False):
    """Serialized view of a job for API responses and SSE events."""

    id: str
    state: JobState
    url: str
    progress: dict[str, int]
    manifest_path: str
    manifest: dict[str, object]
    error: str | None


def build_initial_snapshot(
    url: str,
    *,
    job_id: str,
    settings: Settings | None = None,
) -> JobSnapshot:
    """Construct a basic snapshot stub used before persistence wiring exists."""

    manifest = None
    active_settings = settings or global_settings
    if active_settings:
        manifest = ManifestMetadata(
            environment=active_settings.manifest_environment(playwright_version=PLAYWRIGHT_VERSION),
        )

    snapshot = JobSnapshot(
        id=job_id,
        url=url,
        state=JobState.BROWSER_STARTING,
        progress={"done": 0, "total": 0},
        manifest_path="",
        error=None,
    )
    if manifest:
        snapshot["manifest"] = manifest.model_dump()
    return snapshot


async def execute_capture_job(
    *,
    job_id: str,
    url: str,
    store: Store | None = None,
    config: CaptureConfig | None = None,
) -> tuple[CaptureResult, list[dict[str, object]]]:
    """Run the capture pipeline, persisting artifacts + manifest via ``Store``."""

    storage = store or build_store()
    started_at = datetime.now(timezone.utc)
    storage.allocate_run(job_id=job_id, url=url, started_at=started_at)
    storage.update_status(job_id=job_id, status=JobState.CAPTURING)

    capture_config = config or CaptureConfig(url=url)
    try:
        capture_result = await capture_tiles(capture_config)
        storage.write_manifest(job_id=job_id, manifest=capture_result.manifest)
        tile_artifacts = storage.write_tiles(job_id=job_id, tiles=capture_result.tiles)
    except Exception:
        storage.update_status(job_id=job_id, status=JobState.FAILED, finished_at=datetime.now(timezone.utc))
        raise

    storage.update_status(job_id=job_id, status=JobState.DONE, finished_at=datetime.now(timezone.utc))
    return capture_result, tile_artifacts
