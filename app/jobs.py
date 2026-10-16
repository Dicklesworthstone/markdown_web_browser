"""Job orchestration helpers for capture requests."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from importlib import metadata
import time
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Sequence, TypedDict, cast
from uuid import uuid4

import hashlib
import hmac
import json
import re
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
import logging

import httpx

from app import metrics
from app.capture import CaptureConfig, CaptureManifest, CaptureResult, capture_tiles
from app.capture_warnings import CaptureWarningEntry
from app.dedup import summarize_dedup_events
from app.dom_links import (
    LinkRecord,
    append_links_appendix,
    blend_dom_with_ocr,
    extract_dom_text_overlays,
    extract_headings_from_html,
    extract_links_from_dom,
    extract_links_from_markdown,
    overlays_from_dom_boxes,
    resolve_markdown_links,
    serialize_links,
)
from app.hardware import get_host_capabilities
from app.ocr_client import OCRRequest, SubmitTilesResult, resolve_ocr_backend, submit_tiles
from app.ocr_policy import resolve_model_profile, resolve_prompt_template
from app.schemas import JobCreateRequest, ManifestMetadata
from app.settings import Settings, resolve_semantic_post_settings, settings as global_settings
from app.store import Store, build_store
from app.semantic_post import SemanticPostSettings, apply_semantic_post
from app.stitch import stitch_markdown
from app.warning_log import append_warning_log, summarize_dom_assists

LOGGER = logging.getLogger(__name__)

WebhookSender = Callable[[str, dict[str, Any]], Awaitable[None]]
_EVENT_HISTORY_LIMIT = 500
_SUBSCRIBER_QUEUE_MAXSIZE = 256


def _put_bounded(queue: asyncio.Queue[Any], item: Any) -> None:
    """Enqueue an item into a bounded subscriber queue, dropping oldest on overflow."""
    try:
        queue.put_nowait(item)
    except asyncio.QueueFull:
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
        try:
            queue.put_nowait(item)
        except asyncio.QueueFull:
            pass

try:  # Playwright may be missing in some CI environments
    PLAYWRIGHT_VERSION = metadata.version("playwright")
except metadata.PackageNotFoundError:  # pragma: no cover - dev fallback
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
    CANCELLED = "CANCELLED"


class JobSnapshot(TypedDict, total=False):
    """Serialized view of a job for API responses and SSE events."""

    id: str
    state: str  # JobState value name; enum coerced on read
    url: str
    progress: dict[str, int]
    manifest_path: str
    manifest: dict[str, object]
    artifacts: list[dict[str, object]]
    error: str | None
    profile_id: str | None
    owner: str | None
    cache_hit: bool
    cache_source_job_id: str | None
    seam_marker_count: int | None
    seam_hash_count: int | None
    seam_markers: list[dict[str, object]]
    tags: list[str]

def build_initial_snapshot(
    url: str,
    *,
    job_id: str,
    settings: Settings | None = None,
    profile_id: str | None = None,
    owner: str | None = None,
    cache_hit: bool = False,
    capture_config: CaptureConfig | None = None,
) -> JobSnapshot:
    """Construct a baseline snapshot before capture begins."""

    manifest = None
    active_settings = settings or global_settings
    if active_settings:
        effective_settings = _effective_settings_for_config(active_settings, capture_config)
        capability_snapshot = get_host_capabilities()
        backend = resolve_ocr_backend(effective_settings, capabilities=capability_snapshot)
        env = effective_settings.manifest_environment(playwright_version=PLAYWRIGHT_VERSION)
        if capture_config is not None:
            env.viewport.width = capture_config.viewport_width
            env.viewport.height = capture_config.viewport_height
            env.viewport.device_scale_factor = capture_config.device_scale_factor
            env.viewport.color_scheme = capture_config.color_scheme
            if capture_config.ocr_max_tiles_in_flight is not None:
                env.ocr_concurrency.max = max(
                    env.ocr_concurrency.min, capture_config.ocr_max_tiles_in_flight
                )
        selected_policy = (
            getattr(capture_config, "ocr_policy", None)
            or getattr(capture_config, "ocr_model", None)
            or effective_settings.ocr.model
        )
        manifest = ManifestMetadata(
            environment=env,
            backend_id=backend.backend_id,
            backend_mode=backend.backend_mode,
            hardware_path=backend.hardware_path,
            requested_ocr_policy=getattr(capture_config, "ocr_policy", None),
            selected_ocr_policy=selected_policy,
            ocr_model=effective_settings.ocr.model,
            served_ocr_model=effective_settings.ocr.model,
            ocr_use_fp8=effective_settings.ocr.use_fp8,
            ocr_prompt_template=resolve_prompt_template(effective_settings.ocr.model),
            ocr_max_tiles_in_flight=getattr(capture_config, "ocr_max_tiles_in_flight", None),
            long_side_px=(
                getattr(capture_config, "long_side_px", None)
                or effective_settings.browser.long_side_px
            ),
            backend_reason_codes=list(backend.reason_codes),
            backend_reevaluate_after_s=backend.reevaluate_after_s,
            fallback_chain=list(backend.fallback_chain),
            hardware_capabilities=capability_snapshot.to_dict(),
        )

    snapshot = JobSnapshot(
        id=job_id,
        url=url,
        state=JobState.BROWSER_STARTING,
        progress={"done": 0, "total": 0},
        manifest_path="",
        error=None,
    )
    if profile_id:
        snapshot["profile_id"] = profile_id
    if owner is not None:
        snapshot["owner"] = owner
    snapshot["cache_hit"] = bool(cache_hit)
    snapshot["cache_source_job_id"] = None
    snapshot["seam_marker_count"] = None
    snapshot["seam_hash_count"] = None
    snapshot["seam_markers"] = []
    if manifest:
        manifest.profile_id = profile_id
        manifest.cache_hit = cache_hit
        snapshot["manifest"] = manifest.model_dump()
    return snapshot


RunnerType = Callable[..., Awaitable[tuple[CaptureResult, list[dict[str, object]]]]]


class JobManager:
    """In-memory job registry backed by Store persistence."""

    def __init__(
        self,
        *,
        store: Store | None = None,
        runner: RunnerType | None = None,
        webhook_sender: WebhookSender | None = None,
        job_timeout_seconds: int = 600,  # 10 minutes default
    ) -> None:
        self.store = store or build_store()
        self._runner = runner or execute_capture_job
        self._snapshots: Dict[str, JobSnapshot] = {}
        self._tasks: Dict[str, asyncio.Task[None]] = {}
        self._subscribers: Dict[Any, List[asyncio.Queue[JobSnapshot]]] = {}
        self._event_logs: Dict[str, List[dict[str, Any]]] = {}
        self._event_sequences: Dict[str, int] = {}
        self._event_subscribers: Dict[str, List[asyncio.Queue[dict[str, Any]]]] = {}
        self._global_subscribers: List[asyncio.Queue[dict[str, Any]]] = []
        self._webhooks: Dict[str, List[dict[str, Any]]] = {}
        self._pending_webhooks: Dict[str, List[dict[str, Any]]] = {}
        self._webhook_sender = webhook_sender or _default_webhook_sender
        self._cache_keys: Dict[str, str | None] = {}
        self._job_timeout_seconds = int(job_timeout_seconds)
        self._job_tags: Dict[str, list[str]] = {}
        self._job_owners: Dict[str, str | None] = {}
        self._watchdog_task: asyncio.Task[None] | None = None
        self._started_at = datetime.now(timezone.utc)
        self._shutdown = False

    def get_owner(self, job_id: str) -> tuple[bool, str | None]:
        """Return (exists, owner) for a job across in-memory snapshots and Store."""
        if job_id in self._job_owners:
            return True, self._job_owners[job_id]
        snap = self._snapshots.get(job_id)
        if snap is not None and "owner" in snap:
            return True, snap.get("owner")
        try:
            record = self.store.fetch_run(job_id)
        except Exception:
            record = None
        if record is not None:
            return True, getattr(record, "owner", None)
        if snap is not None:
            return True, None
        return False, None

    def _complete_as_cache_hit(
        self,
        *,
        job_id: str,
        cache_record: Any,
        owner: str | None = None,
        tags: list[str] | None = None,
    ) -> None:
        """Populate snapshot and persist RunRecord for a job resolved via cache hit."""
        try:
            self.store.register_cached_run(
                job_id=job_id,
                source=cache_record,
                owner=owner,
                tags=list(tags) if tags else None,
            )
        except TypeError:
            self.store.register_cached_run(job_id=job_id, source=cache_record)
        snapshot = self._snapshots[job_id]
        manifest = self.store.read_manifest(cache_record.id)
        manifest["cache_hit"] = True
        manifest["cache_source_job_id"] = cache_record.id
        snapshot["cache_hit"] = True
        snapshot["cache_source_job_id"] = cache_record.id
        snapshot["manifest"] = manifest
        snapshot["manifest_path"] = cache_record.manifest_path
        total_tiles = cache_record.tiles_total or manifest.get("tiles_total") or 0
        snapshot["progress"] = {"done": total_tiles, "total": total_tiles}
        snapshot["artifacts"] = self.store.read_artifacts(cache_record.id)
        self._snapshots[job_id] = snapshot.copy()
        self._record_custom_event(
            job_id,
            "cache_hit",
            {
                "source_job_id": cache_record.id,
                "cache_key": cache_record.cache_key,
            },
        )
        self._set_state(job_id, JobState.DONE)

    async def _await_inflight_and_clone(
        self,
        *,
        job_id: str,
        source_job_id: str,
        url: str,
        config: CaptureConfig,
        request: JobCreateRequest,
        owner: str | None = None,
        tags: list[str] | None = None,
    ) -> None:
        """Wait for an identical in-flight primary job and reuse its result as a cache hit."""
        try:
            source_task = self._tasks.get(source_job_id)
            if source_task is not None and not source_task.done():
                try:
                    await asyncio.shield(source_task)
                except asyncio.CancelledError:
                    current_task = asyncio.current_task()
                    if current_task is not None and current_task.cancelled():
                        raise
                except Exception:
                    pass

            snapshot = self._snapshots.get(job_id)
            if snapshot is not None and snapshot.get("state") in (
                JobState.CANCELLED,
                JobState.CANCELLED.value,
                JobState.FAILED,
                JobState.FAILED.value,
            ):
                return

            cache_record = None
            source_record = None
            try:
                source_record = self.store.fetch_run(source_job_id)
            except Exception:
                source_record = None
            target_key = (
                getattr(source_record, "cache_key", None)
                or self._cache_keys.get(job_id)
                or config.cache_key
            )
            if target_key:
                try:
                    cache_record = self.store.find_cache_hit(target_key, owner=owner)
                except TypeError:
                    cache_record = self.store.find_cache_hit(target_key)
            if cache_record is not None:
                self._complete_as_cache_hit(
                    job_id=job_id,
                    cache_record=cache_record,
                    owner=owner,
                    tags=tags,
                )
                return

            # Fallback if primary in-flight job failed, cancelled, or was ineligible for cache reuse
            if snapshot is not None:
                snapshot["cache_source_job_id"] = None
            await self._run_job(job_id=job_id, url=url, config=config, request=request)
        except asyncio.CancelledError:
            snapshot = self._snapshots.get(job_id)
            if snapshot is not None and snapshot.get("state") not in (
                JobState.CANCELLED,
                JobState.CANCELLED.value,
                JobState.FAILED,
                JobState.FAILED.value,
            ):
                snapshot["error"] = snapshot.get("error") or "cancelled"
                self._set_state(job_id, JobState.CANCELLED)
            raise
        finally:
            self._tasks.pop(job_id, None)
            self._cache_keys.pop(job_id, None)

    async def create_job(
        self,
        request: JobCreateRequest,
        *,
        tags: list[str] | None = None,
        owner: str | None = None,
    ) -> JobSnapshot:
        """Submit a new job (or reuse cache hit).

        ``tags`` are persisted on the underlying RunRecord so agents can scope
        follow-up queries (e.g. all jobs in dataset:2026-q1) via GET /jobs?tag=...
        """
        job_id = uuid4().hex
        active_settings = global_settings
        capture_config = _build_capture_config(request, active_settings, owner=owner)
        cache_seed = _build_cache_seed(config=capture_config, settings=active_settings)
        capture_config.cache_seed = cache_seed
        cache_key = _build_cache_key(config=capture_config, settings=active_settings)
        capture_config.cache_key = cache_key
        snapshot = build_initial_snapshot(
            url=request.url,
            job_id=job_id,
            settings=active_settings,
            profile_id=capture_config.profile_id,
            owner=owner,
            cache_hit=False,
            capture_config=capture_config,
        )
        combined_tags: list[str] = []
        for raw_tag in [*(getattr(request, "tags", None) or []), *(tags or [])]:
            t = str(raw_tag).strip()
            if t and t not in combined_tags:
                combined_tags.append(t)
        snapshot["tags"] = list(combined_tags)
        self._snapshots[job_id] = snapshot.copy()
        self._job_owners[job_id] = owner
        self._event_logs[job_id] = []
        self._event_sequences[job_id] = 0
        self._cache_keys[job_id] = cache_key
        if combined_tags:
            self._job_tags[job_id] = list(combined_tags)
            snapshot["tags"] = list(combined_tags)
        self._broadcast(job_id)

        cache_record = None
        if request.reuse_cache:
            try:
                cache_record = self.store.find_cache_hit(cache_key, owner=owner)
            except TypeError:
                cache_record = self.store.find_cache_hit(cache_key)
        if cache_record:
            self._complete_as_cache_hit(
                job_id=job_id,
                cache_record=cache_record,
                owner=owner,
                tags=list(combined_tags) if combined_tags else None,
            )
            self._cache_keys.pop(job_id, None)
            return self._snapshot_payload(job_id)

        if request.reuse_cache and cache_key and getattr(self.store, "cache_ttl_hours", 24) > 0:
            inflight_job_id = None
            for existing_id, existing_task in list(self._tasks.items()):
                if existing_id == job_id or existing_task is None or existing_task.done():
                    continue
                if self._cache_keys.get(existing_id) != cache_key:
                    continue
                if self._job_owners.get(existing_id) != owner:
                    continue
                existing_snap = self._snapshots.get(existing_id) or {}
                if existing_snap.get("cache_source_job_id"):
                    continue
                inflight_job_id = existing_id
                break
            if inflight_job_id is not None:
                snapshot = self._snapshots[job_id]
                snapshot["cache_source_job_id"] = inflight_job_id
                self._snapshots[job_id] = snapshot.copy()
                task = asyncio.create_task(
                    self._await_inflight_and_clone(
                        job_id=job_id,
                        source_job_id=inflight_job_id,
                        url=request.url,
                        config=capture_config,
                        request=request,
                        owner=owner,
                        tags=list(combined_tags) if combined_tags else None,
                    )
                )
                self._tasks[job_id] = task
                return self._snapshot_payload(job_id)

        task = asyncio.create_task(
            self._run_job(job_id=job_id, url=request.url, config=capture_config, request=request)
        )
        self._tasks[job_id] = task
        return self._snapshot_payload(job_id)

    async def replay_job(
        self,
        manifest: Mapping[str, Any],
        *,
        owner: str | None = None,
    ) -> JobSnapshot:
        """Re-enqueue a job based on a stored manifest payload."""

        if not isinstance(manifest, Mapping):
            msg = "Manifest payload must be an object"
            raise ValueError(msg)

        # Validate required URL field
        url = manifest.get("url")
        if not isinstance(url, str) or not url.strip():
            msg = "Manifest is missing a valid 'url'"
            raise ValueError(msg)

        # Validate URL format
        try:
            parsed = urlparse(url)
        except Exception as exc:
            msg = f"Manifest URL '{url}' is malformed: {exc}"
            raise ValueError(msg) from exc

        if not parsed.scheme or not parsed.netloc:
            msg = f"Manifest URL '{url}' is not a valid URL"
            raise ValueError(msg)

        # Validate optional profile_id
        profile_id = manifest.get("profile_id")
        if profile_id is not None:
            if not isinstance(profile_id, str) or not profile_id.strip():
                profile_id = None
            elif len(profile_id) > 255:  # Reasonable length limit
                msg = f"Manifest profile_id '{profile_id}' is too long (max 255 chars)"
                raise ValueError(msg)

        # Validate environment metadata if present
        environment = manifest.get("environment")
        if environment is not None and not isinstance(environment, Mapping):
            msg = "Manifest environment field must be an object"
            raise ValueError(msg)

        ocr_policy = manifest.get("ocr_policy") or manifest.get("selected_ocr_policy")
        if not isinstance(ocr_policy, str) or not ocr_policy.strip():
            ocr_policy = None
        ocr_model = manifest.get("ocr_model")
        if not isinstance(ocr_model, str) or not ocr_model.strip():
            if isinstance(environment, Mapping) and isinstance(environment.get("ocr_model"), str):
                ocr_model = environment.get("ocr_model")
            else:
                ocr_model = None
        ocr_use_fp8 = manifest.get("ocr_use_fp8")
        if not isinstance(ocr_use_fp8, bool):
            if isinstance(environment, Mapping) and isinstance(environment.get("ocr_use_fp8"), bool):
                ocr_use_fp8 = environment.get("ocr_use_fp8")
            else:
                ocr_use_fp8 = None
        long_side_px = manifest.get("long_side_px")
        if not isinstance(long_side_px, int) or isinstance(long_side_px, bool):
            long_side_px = None
        viewport_width = manifest.get("viewport_width")
        viewport_height = manifest.get("viewport_height")
        device_scale_factor = manifest.get("device_scale_factor")
        color_scheme = None
        if isinstance(environment, Mapping):
            env_vp = environment.get("viewport")
            if isinstance(env_vp, Mapping):
                if not isinstance(viewport_width, int):
                    viewport_width = env_vp.get("width")
                if not isinstance(viewport_height, int):
                    viewport_height = env_vp.get("height")
                if not isinstance(device_scale_factor, int):
                    device_scale_factor = env_vp.get("device_scale_factor")
                if isinstance(env_vp.get("color_scheme"), str):
                    color_scheme = env_vp.get("color_scheme")

        snapshot = await self.create_job(
            JobCreateRequest(
                url=url,
                profile_id=profile_id,
                ocr_policy=ocr_policy,
                ocr_model=ocr_model,
                ocr_use_fp8=ocr_use_fp8,
                long_side_px=long_side_px,
                viewport_width=viewport_width if isinstance(viewport_width, int) else None,
                viewport_height=viewport_height if isinstance(viewport_height, int) else None,
                device_scale_factor=(
                    device_scale_factor if isinstance(device_scale_factor, int) else None
                ),
                color_scheme=color_scheme,
            ),
            owner=owner,
        )
        metadata = _build_replay_metadata(manifest)
        if metadata:
            self._record_custom_event(snapshot["id"], "replay_request", metadata)
        return snapshot

    def get_snapshot(self, job_id: str) -> JobSnapshot | None:
        try:
            return self._snapshot_payload(job_id)
        except KeyError:
            return None

    def _ensure_job_exists(self, job_id: str) -> None:
        if job_id in self._snapshots:
            return
        try:
            record = self.store.fetch_run(job_id)
        except Exception:
            record = None
        if record is None:
            raise KeyError(f"Job {job_id} not found")

    def add_tag(self, job_id: str, tag: str) -> None:
        """Append a tag to the in-memory + persisted run record."""
        for t in self.add_tags(job_id, [tag]):
            del t  # consume generator for side-effects

    def add_tags(self, job_id: str, tags: list[str]) -> list[str]:
        """Append a batch of tags. Returns the deduped + sorted final tag set."""
        self._ensure_job_exists(job_id)
        current = list(self._job_tags.get(job_id, []))
        changed = False
        for tag in tags:
            tag = str(tag).strip()
            if not tag or tag in current:
                continue
            current.append(tag)
            changed = True
        if not changed:
            return current
        self._job_tags[job_id] = current
        # Persist each new tag individually (store API is per-tag)
        for tag in tags:
            try:
                self.store.add_tag(job_id=job_id, tag=tag)
            except Exception:  # pragma: no cover - persistence is best-effort
                pass
        snapshot = self._snapshots.get(job_id)
        if snapshot is not None:
            snapshot["tags"] = list(current)
            self._broadcast(job_id)
        return current

    def remove_tag(self, job_id: str, tag: str) -> list[str]:
        """Remove a tag. Returns the new (deduped) tag set.

        No-op if the tag isn't present. Persists via store.set_tags.
        """
        self._ensure_job_exists(job_id)
        current = list(self._job_tags.get(job_id, []))
        if tag not in current:
            return current
        current = [t for t in current if t != tag]
        self._job_tags[job_id] = current
        try:
            self.store.set_tags(job_id=job_id, tags=current)
        except Exception:  # pragma: no cover
            pass
        snapshot = self._snapshots.get(job_id)
        if snapshot is not None:
            snapshot["tags"] = list(current)
            self._broadcast(job_id)
        return current

    def cancel_job(self, job_id: str, *, reason: str = "cancelled by user") -> bool:
        """Cancel an in-flight job. Returns True if a task was cancelled."""
        task = self._tasks.get(job_id)
        cancelled = False
        if task is not None and not task.done():
            task.cancel()
            cancelled = True
        snapshot = self._snapshots.get(job_id)
        if snapshot is not None:
            snapshot["error"] = reason
            self._set_state(job_id, JobState.CANCELLED)
        try:
            self.store.update_status(
                job_id=job_id,
                status=JobState.CANCELLED,
                finished_at=datetime.now(timezone.utc).replace(tzinfo=None),
            )
        except Exception:
            pass
        return cancelled

    def remove_job_state(self, job_id: str) -> bool:
        """Remove in-memory snapshot and event state for a deleted job."""
        existed = job_id in self._snapshots
        self._snapshots.pop(job_id, None)
        self._event_logs.pop(job_id, None)
        self._subscribers.pop(job_id, None)
        self._job_tags.pop(job_id, None)
        self._job_owners.pop(job_id, None)
        self._webhooks.pop(job_id, None)
        self._pending_webhooks.pop(job_id, None)
        return existed

    def batch_status(self, job_ids: list[str]) -> list[dict[str, Any]]:
        """Return a compact status record for each id (avoids N HTTP round-trips)."""
        out: list[dict[str, Any]] = []
        for jid in job_ids:
            snap = self.get_snapshot(jid) or {}
            out.append(
                {
                    "job_id": jid,
                    "state": str(snap.get("state", "UNKNOWN")),
                    "cache_hit": bool(snap.get("cache_hit")),
                    "url": str(snap.get("url") or ""),
                    "progress": snap.get("progress"),
                    "error": snap.get("error"),
                    "tags": list(snap.get("tags") or []),
                }
            )
        return out

    def list_jobs(
        self,
        *,
        state: str | None = None,
        profile_id: str | None = None,
        owner: str | None = None,
        enforce_owner: bool = False,
        url_contains: str | None = None,
        cache_hit: bool | None = None,
        tag: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        """List jobs from the store with optional filters.

        Returns (items, total_before_pagination). Each item is a flat dict
        ready to be wrapped in JobListItem.
        """
        try:
            rows, total = self.store.list_runs(
                state=state,
                profile_id=profile_id,
                owner=owner,
                enforce_owner=enforce_owner,
                url_contains=url_contains,
                cache_hit=cache_hit,
                tag=tag,
                limit=limit,
                offset=offset,
            )
        except TypeError:
            rows, total = self.store.list_runs(
                state=state,
                profile_id=profile_id,
                url_contains=url_contains,
                cache_hit=cache_hit,
                tag=tag,
                limit=limit,
                offset=offset,
            )
        items: list[dict[str, Any]] = []
        for r in rows:
            is_cache_hit = bool(getattr(r, "cache_hit", False))
            items.append(
                {
                    "id": r.id,
                    "url": r.url,
                    "state": r.status or "PENDING",
                    "created_at": r.started_at.isoformat() if r.started_at else None,
                    "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                    "cache_hit": is_cache_hit,
                    "cache_source_job_id": getattr(r, "cache_source_job_id", None),
                    "profile_id": r.profile_id,
                    "tags": list(r.tags or []),
                    "error": None,
                    "manifest_path": r.manifest_path,
                    "embedding_model": getattr(r, "embedding_model", None),
                    "embedding_dim": getattr(r, "embedding_dim", None),
                }
            )
        return items, total

    def get_structured_result(self, job_id: str) -> dict[str, Any]:
        """Return a parsed view of the job's Markdown for agent consumption.

        Splits the stitched Markdown into a heading + body hierarchy (sections)
        and merges DOM/OCR links into a single normalized list. Falls back to a
        minimal payload if the artifacts have been pruned.
        """
        raw_snap = self._snapshots.get(job_id)
        record = None
        if raw_snap is None:
            try:
                record = self.store.fetch_run(job_id)
            except Exception:
                record = None
            if record is None:
                raise KeyError(f"Job {job_id} not found")
            snap: dict[str, Any] = {
                "url": getattr(record, "url", ""),
                "state": getattr(record, "status", "UNKNOWN"),
                "cache_hit": bool(getattr(record, "cache_hit", False)),
                "profile_id": getattr(record, "profile_id", None),
            }
        else:
            snap = dict(raw_snap)
        manifest = snap.get("manifest") or {}
        markdown = ""
        try:
            markdown = self.store.read_markdown(job_id)
        except Exception:
            markdown = ""
        sections = _parse_markdown_sections(markdown)
        links_raw: list[dict[str, Any]] = []
        try:
            links_payload = self.store.read_links(job_id)
        except Exception:
            links_payload = None
        anchors: list[Any] = []
        if isinstance(links_payload, dict):
            anchors = list(links_payload.get("anchors") or [])
        elif isinstance(links_payload, list):
            anchors = list(links_payload)
        for entry in anchors:
            if not isinstance(entry, dict):
                continue
            links_raw.append(
                {
                    "href": entry.get("href") or "",
                    "text": entry.get("text"),
                    "title": entry.get("title"),
                    "source": entry.get("source") or "dom",
                    "delta": entry.get("delta"),
                }
            )
        if not links_raw:
            # Fallback: parse links from the markdown
            for href in _extract_markdown_links(markdown):
                links_raw.append({"href": href, "text": None, "title": None, "source": "ocr", "delta": None})
        state = snap.get("state") or "UNKNOWN"
        if isinstance(state, JobState):
            state = state.value
        return {
            "job_id": job_id,
            "url": snap.get("url") or (manifest.get("url") if isinstance(manifest, dict) else ""),
            "state": str(state),
            "word_count": len(markdown.split()) if markdown else 0,
            "char_count": len(markdown) if markdown else 0,
            "sections": sections,
            "links": links_raw[:200],  # cap so agent calls stay cheap
            "cache_hit": bool(snap.get("cache_hit")),
            "profile_id": snap.get("profile_id"),
        }

    def subscribe(self, job_id: str) -> asyncio.Queue[JobSnapshot]:
        if job_id not in self._snapshots:
            raise KeyError(f"Job {job_id} not found")
        queue: asyncio.Queue[JobSnapshot] = asyncio.Queue(maxsize=_SUBSCRIBER_QUEUE_MAXSIZE)
        _put_bounded(queue, self._snapshot_payload(job_id))
        self._subscribers.setdefault(job_id, []).append(queue)
        return queue

    def unsubscribe(self, job_id: str, queue: asyncio.Queue[JobSnapshot]) -> None:
        subscribers = self._subscribers.get(job_id)
        if not subscribers:
            return
        if queue in subscribers:
            subscribers.remove(queue)
        if not subscribers:
            self._subscribers.pop(job_id, None)

    def subscribe_events(
        self,
        job_id: str,
        *,
        since: datetime | None = None,
        min_sequence: int | None = None,
    ) -> tuple[list[dict[str, Any]], asyncio.Queue[dict[str, Any]]]:
        if job_id not in self._snapshots:
            raise KeyError(f"Job {job_id} not found")
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=_SUBSCRIBER_QUEUE_MAXSIZE)
        self._event_subscribers.setdefault(job_id, []).append(queue)
        backlog = self.get_events(job_id, since=since, min_sequence=min_sequence)
        return backlog, queue

    def unsubscribe_events(self, job_id: str, queue: asyncio.Queue[dict[str, Any]]) -> None:
        subscribers = self._event_subscribers.get(job_id)
        if not subscribers:
            return
        if queue in subscribers:
            subscribers.remove(queue)
        if not subscribers:
            self._event_subscribers.pop(job_id, None)

    def subscribe_global(self) -> asyncio.Queue[dict[str, Any]]:
        """Subscribe to bounded live state/event updates across all jobs."""
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=_SUBSCRIBER_QUEUE_MAXSIZE)
        self._global_subscribers.append(queue)
        return queue

    def unsubscribe_global(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        if queue in self._global_subscribers:
            self._global_subscribers.remove(queue)

    def start_watchdog(self) -> None:
        """Start the background watchdog task to monitor for stuck jobs."""
        if self._watchdog_task is None or self._watchdog_task.done():
            self._shutdown = False
            self._started_at = datetime.now(timezone.utc)
            self._watchdog_task = asyncio.create_task(self._watchdog_loop())
            LOGGER.info("Job watchdog started with %ds timeout", self._job_timeout_seconds)

    async def stop_watchdog(self) -> None:
        """Stop the watchdog task gracefully."""
        self._shutdown = True
        if self._watchdog_task and not self._watchdog_task.done():
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except asyncio.CancelledError:
                pass
            LOGGER.info("Job watchdog stopped")

    async def shutdown(self, *, timeout_s: float = 5.0) -> None:
        """Stop watchdog and cancel all in-flight job tasks with bounded wait."""
        await self.stop_watchdog()
        active_tasks = [task for task in self._tasks.values() if not task.done()]
        for job_id in list(self._tasks.keys()):
            self.cancel_job(job_id, reason="server shutdown")
        if active_tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*active_tasks, return_exceptions=True),
                    timeout=max(0.1, float(timeout_s)),
                )
            except asyncio.TimeoutError:
                LOGGER.warning(
                    "Timed out waiting for %d job tasks on shutdown", len(active_tasks)
                )
        self._tasks.clear()

    async def _cleanup_completed_jobs(self, now: datetime, retention_hours: int = 2) -> None:
        """Clean up in-memory data for completed jobs older than retention_hours.

        This prevents memory leaks by removing old snapshots, event logs,
        subscribers, and other data structures for jobs that completed
        more than retention_hours ago.
        """
        cutoff_time = now - timedelta(hours=retention_hours)
        jobs_to_clean = []

        for job_id, snapshot in list(self._snapshots.items()):
            # Only clean up completed/terminal jobs
            if snapshot["state"] not in (
                JobState.DONE,
                JobState.FAILED,
                JobState.CANCELLED,
                JobState.DONE.value,
                JobState.FAILED.value,
                JobState.CANCELLED.value,
            ):
                continue

            # Check completion time from database
            try:
                record = await asyncio.to_thread(self.store.fetch_run, job_id)
                if not record or not record.finished_at:
                    continue

                # Normalize finished_at to UTC-naive for comparison
                finished_at = record.finished_at
                finished_at_naive = (
                    finished_at.replace(tzinfo=None) if finished_at.tzinfo else finished_at
                )
                cutoff_naive = cutoff_time.replace(tzinfo=None)

                if finished_at_naive < cutoff_naive:
                    jobs_to_clean.append(job_id)
            except Exception as exc:  # pragma: no cover - defensive logging
                LOGGER.warning("Error checking completion time for job %s: %s", job_id, exc)

        # Clean up memory for old jobs
        for job_id in jobs_to_clean:
            self._snapshots.pop(job_id, None)
            self._event_logs.pop(job_id, None)
            self._event_sequences.pop(job_id, None)
            self._subscribers.pop(job_id, None)
            self._event_subscribers.pop(job_id, None)
            self._webhooks.pop(job_id, None)
            self._pending_webhooks.pop(job_id, None)
            self._cache_keys.pop(job_id, None)
            # Defensive cleanup of tasks (should already be cleaned up, but just in case)
            self._tasks.pop(job_id, None)

        if jobs_to_clean:
            LOGGER.info("Cleaned up memory for %d completed jobs", len(jobs_to_clean))

    async def _watchdog_loop(self) -> None:
        """Background task that monitors jobs and times out stuck ones.

        Note: Uses asyncio.to_thread() for database operations to avoid blocking
        the event loop during SQLite I/O.
        """
        last_cleanup = datetime.now(timezone.utc)
        while not self._shutdown:
            try:
                await asyncio.sleep(60)  # Check every minute
                now = datetime.now(timezone.utc)

                # Clean up completed jobs every 30 minutes
                if (now - last_cleanup).total_seconds() > 1800:  # 30 minutes
                    await self._cleanup_completed_jobs(now)
                    last_cleanup = now

                for job_id, snapshot in list(self._snapshots.items()):
                    # Skip already-completed jobs
                    if snapshot["state"] in (
                        JobState.DONE,
                        JobState.FAILED,
                        JobState.CANCELLED,
                        JobState.DONE.value,
                        JobState.FAILED.value,
                        JobState.CANCELLED.value,
                    ):
                        continue

                    # Get the run record to check start time (run in thread to avoid blocking)
                    record = await asyncio.to_thread(self.store.fetch_run, job_id)
                    if not record:
                        continue

                    # Calculate elapsed time (SQLite strips timezone, so normalize both to UTC-naive)
                    started_at = record.started_at
                    # Normalize both timestamps to naive UTC for consistent comparison
                    started_at_naive = (
                        started_at.replace(tzinfo=None) if started_at.tzinfo else started_at
                    )
                    now_naive = now.replace(tzinfo=None)
                    elapsed_seconds = (now_naive - started_at_naive).total_seconds()

                    if elapsed_seconds > self._job_timeout_seconds:
                        LOGGER.error(
                            "Job %s timed out after %.1fs in state %s",
                            job_id,
                            elapsed_seconds,
                            snapshot["state"],
                        )

                        # Mark job as failed
                        error_msg = f"Job timeout after {elapsed_seconds:.1f}s (max: {self._job_timeout_seconds}s)"
                        snapshot["error"] = error_msg
                        self._set_state(job_id, JobState.FAILED)

                        # Update database status (run in thread to avoid blocking)
                        # Make naive for SQLite (which strips timezone anyway)
                        await asyncio.to_thread(
                            self.store.update_status,
                            job_id=job_id,
                            status=JobState.FAILED,
                            finished_at=now.replace(tzinfo=None),
                        )

                        # Cancel the task if it's still running
                        task = self._tasks.get(job_id)
                        if task and not task.done():
                            task.cancel()
                            LOGGER.info("Cancelled task for timed-out job %s", job_id)

            except Exception as exc:  # pragma: no cover
                LOGGER.exception("Watchdog loop error: %s", exc)
                # Continue running despite errors

    def report_stage(
        self,
        job_id: str,
        stage: str | JobState,
        info: Mapping[str, Any] | None = None,
    ) -> None:
        """Update job stage and progress counters and broadcast to subscribers."""
        snapshot = self._snapshots.get(job_id)
        if snapshot is None:
            return
        current_state = snapshot.get("state")
        if current_state in (
            JobState.DONE,
            JobState.FAILED,
            JobState.CANCELLED,
            JobState.DONE.value,
            JobState.FAILED.value,
            JobState.CANCELLED.value,
        ):
            return
        stage_name = stage.value if isinstance(stage, JobState) else str(stage).upper()
        info_dict = dict(info) if isinstance(info, Mapping) else {}
        done = info_dict.get("done")
        total = info_dict.get("total")
        if isinstance(done, int) and isinstance(total, int) and total >= 0:
            snapshot["progress"] = {"done": max(0, min(done, total)), "total": total}
        if stage_name in JobState.__members__:
            snapshot["state"] = JobState[stage_name]
        elif stage_name == "PERSISTING":
            snapshot["state"] = JobState.STITCHING
        self._broadcast(job_id)

    async def _run_job(
        self,
        *,
        job_id: str,
        url: str,
        config: CaptureConfig | None = None,
        request: JobCreateRequest | None = None,
    ) -> None:
        storage = self.store
        started_at = datetime.now(timezone.utc)
        profile_id = getattr(config, "profile_id", None)
        owner = getattr(config, "owner", None)
        cache_key = self._cache_keys.get(job_id)
        saw_stage_callbacks = False

        def _stage_cb(stage_name: str, info: dict[str, Any] | None = None) -> None:
            nonlocal saw_stage_callbacks
            saw_stage_callbacks = True
            self.report_stage(job_id, stage_name, info)

        if config is not None:
            config.on_stage = _stage_cb

        try:
            try:
                await asyncio.to_thread(
                    storage.allocate_run,
                    job_id=job_id,
                    url=url,
                    started_at=started_at,
                    profile_id=profile_id,
                    owner=owner,
                    cache_key=cache_key,
                    tags=self._job_tags.get(job_id) or None,
                )
            except TypeError:
                await asyncio.to_thread(
                    storage.allocate_run,
                    job_id=job_id,
                    url=url,
                    started_at=started_at,
                    profile_id=profile_id,
                    cache_key=cache_key,
                )
            await asyncio.to_thread(
                storage.update_status, job_id=job_id, status=JobState.NAVIGATING
            )
            pending = self._pending_webhooks.pop(job_id, [])
            if pending:
                _persist_pending_webhooks(storage, pending, job_id)
            self._set_state(job_id, JobState.NAVIGATING)
            self._set_state(job_id, JobState.CAPTURING)
            runner_coro = self._runner(
                job_id=job_id,
                url=url,
                store=self.store,
                config=config,
                request=request,
            )
            if self._job_timeout_seconds > 0:
                capture_result, tile_artifacts = await asyncio.wait_for(
                    runner_coro, timeout=float(self._job_timeout_seconds)
                )
            else:
                capture_result, tile_artifacts = await runner_coro

            snapshot = self._snapshots.get(job_id)
            if snapshot is not None and snapshot.get("state") in (
                JobState.CANCELLED,
                JobState.CANCELLED.value,
                JobState.FAILED,
                JobState.FAILED.value,
            ):
                return

            total_tiles = int(getattr(capture_result.manifest, "tiles_total", 0) or 0)
            if not saw_stage_callbacks:
                self.report_stage(
                    job_id, JobState.TILING, {"done": 0, "total": total_tiles}
                )
                self.report_stage(
                    job_id, JobState.OCR_SUBMITTING, {"done": 0, "total": total_tiles}
                )
                for tile_idx in range(1, total_tiles + 1):
                    self.report_stage(
                        job_id,
                        JobState.OCR_WAITING,
                        {"done": tile_idx, "total": total_tiles},
                    )
                self.report_stage(
                    job_id,
                    JobState.STITCHING,
                    {"done": total_tiles, "total": total_tiles},
                )

            try:
                _refresh_manifest_cache_key(capture_result.manifest)
                await asyncio.to_thread(
                    storage.write_manifest,
                    job_id=job_id,
                    manifest=capture_result.manifest,
                )
            except Exception:
                pass
            run_record = self.store.fetch_run(job_id)
            manifest_path = str(run_record.manifest_path) if run_record else ""
            snapshot = self._snapshots[job_id]
            snapshot["manifest_path"] = manifest_path
            snapshot["progress"] = {
                "done": total_tiles,
                "total": total_tiles,
            }
            snapshot["manifest"] = asdict(capture_result.manifest)
            if capture_result.manifest.seam_markers:
                snapshot["seam_markers"] = capture_result.manifest.seam_markers
            snapshot["artifacts"] = tile_artifacts
            snapshot["cache_hit"] = bool(capture_result.manifest.cache_hit)
            self._broadcast(job_id)
            self._emit_ocr_event(job_id, capture_result.manifest)
            self._emit_dom_assist_event(job_id, capture_result.manifest)
            self._set_state(job_id, JobState.DONE)
            await asyncio.to_thread(
                storage.update_status,
                job_id=job_id,
                status=JobState.DONE,
                finished_at=datetime.now(timezone.utc),
            )
        except asyncio.TimeoutError:
            timeout_msg = f"Job timeout after {self._job_timeout_seconds}s"
            snapshot = self._snapshots.get(job_id)
            if snapshot is not None:
                snapshot["error"] = timeout_msg
            self._set_state(job_id, JobState.FAILED)
            try:
                await asyncio.to_thread(
                    storage.update_status,
                    job_id=job_id,
                    status=JobState.FAILED,
                    finished_at=datetime.now(timezone.utc),
                )
            except Exception:
                pass
        except asyncio.CancelledError:
            snapshot = self._snapshots.get(job_id)
            if snapshot is not None and snapshot.get("state") not in (
                JobState.CANCELLED,
                JobState.CANCELLED.value,
                JobState.FAILED,
                JobState.FAILED.value,
            ):
                snapshot["error"] = snapshot.get("error") or "cancelled"
                self._set_state(job_id, JobState.CANCELLED)
            try:
                await asyncio.to_thread(
                    storage.update_status,
                    job_id=job_id,
                    status=JobState.CANCELLED,
                    finished_at=datetime.now(timezone.utc),
                )
            except Exception:
                pass
            raise
        except Exception as exc:  # pragma: no cover - surfaced to API callers
            snapshot = self._snapshots.get(job_id)
            if snapshot is not None:
                snapshot["error"] = str(exc)
            self._set_state(job_id, JobState.FAILED)
            try:
                await asyncio.to_thread(
                    storage.update_status,
                    job_id=job_id,
                    status=JobState.FAILED,
                    finished_at=datetime.now(timezone.utc),
                )
            except Exception:
                pass
            raise
        finally:
            self._tasks.pop(job_id, None)
            self._cache_keys.pop(job_id, None)

    def _set_state(self, job_id: str, state: JobState) -> None:
        snapshot = self._snapshots.get(job_id)
        if snapshot is None:
            return
        snapshot["state"] = state
        self._broadcast(job_id)
        normalized_state = state.value if isinstance(state, JobState) else str(state)
        if normalized_state in (JobState.DONE.value, JobState.FAILED.value):
            metrics.record_job_completion(normalized_state)

    def _set_error(self, job_id: str, message: str | None) -> None:
        snapshot = self._snapshots.get(job_id)
        if snapshot is None:
            return
        snapshot["error"] = message
        self._broadcast(job_id)

    def get_events(
        self,
        job_id: str,
        since: datetime | None = None,
        *,
        min_sequence: int | None = None,
    ) -> List[dict[str, Any]]:
        if job_id not in self._snapshots:
            raise KeyError(f"Job {job_id} not found")
        events = self._event_logs.get(job_id, [])
        if since is None:
            if min_sequence is None:
                return [event.copy() for event in events]
            return [event.copy() for event in events if self._sequence_newer(event, min_sequence)]
        filtered: List[dict[str, Any]] = []
        for event in events:
            parsed_ts = self._parse_timestamp(event.get("timestamp"))
            if parsed_ts and parsed_ts >= since:
                if min_sequence is not None and not self._sequence_newer(event, min_sequence):
                    continue
                filtered.append(event.copy())
        return filtered

    def register_webhook(self, job_id: str, *, url: str, events: list[str] | None = None) -> None:
        if job_id not in self._snapshots:
            raise KeyError(f"Job {job_id} not found")
        valid_states = {member.value for member in JobState}
        normalized: list[str] = []
        for entry in events or [JobState.DONE.value, JobState.FAILED.value]:
            if entry not in valid_states:
                msg = f"Unsupported job state '{entry}'"
                raise ValueError(msg)
            normalized.append(entry)
        entry = {"url": url, "events": normalized}
        self._webhooks.setdefault(job_id, []).append(entry)
        try:
            record = self.store.register_webhook(job_id=job_id, url=url, events=normalized)
            entry["id"] = record.id
        except KeyError:
            self._pending_webhooks.setdefault(job_id, []).append(entry)

    def delete_webhook(
        self, job_id: str, *, webhook_id: int | None = None, url: str | None = None
    ) -> int:
        """Remove webhook registrations from persistence + in-memory caches."""

        try:
            deleted = self.store.delete_webhooks(job_id=job_id, webhook_id=webhook_id, url=url)
        except KeyError:
            # If the run has not been allocated yet we may still have in-memory registrations.
            if job_id not in self._snapshots:
                raise
            deleted = 0
        removed = self._remove_cached_webhooks(job_id, webhook_id=webhook_id, url=url)
        if deleted or removed:
            return max(deleted, removed)
        return 0

    def _remove_cached_webhooks(
        self,
        job_id: str,
        *,
        webhook_id: int | None = None,
        url: str | None = None,
    ) -> int:
        """Delete webhook entries from in-memory caches and pending queues."""

        removed = self._prune_webhook_entries(
            self._webhooks, job_id, webhook_id=webhook_id, url=url
        )
        pending_removed = self._prune_webhook_entries(
            self._pending_webhooks, job_id, webhook_id=webhook_id, url=url
        )
        return removed or pending_removed

    def _prune_webhook_entries(
        self,
        source: Dict[str, List[dict[str, Any]]],
        job_id: str,
        *,
        webhook_id: int | None = None,
        url: str | None = None,
    ) -> int:
        entries = source.get(job_id)
        if not entries:
            return 0
        remaining = [entry for entry in entries if not _webhook_matches(entry, webhook_id, url)]
        removed = len(entries) - len(remaining)
        if remaining:
            source[job_id] = remaining
        else:
            source.pop(job_id, None)
        return removed

    def _persist_pending_webhooks(self, job_id: str) -> None:
        pending = self._pending_webhooks.pop(job_id, [])
        if not pending:
            return
        _persist_pending_webhooks(self.store, pending, job_id)

    def _broadcast(self, job_id: str) -> None:
        payload = self._snapshot_payload(job_id)
        self._record_event(job_id, payload)
        for sub_key, queues in list(self._subscribers.items()):
            if sub_key == job_id or not isinstance(sub_key, str):
                for queue in list(queues):
                    _put_bounded(queue, payload.copy())
        self._maybe_trigger_webhooks(job_id, payload)

    def _snapshot_payload(self, job_id: str) -> JobSnapshot:
        snapshot = self._snapshots.get(job_id)
        try:
            record = self.store.fetch_run(job_id)
        except Exception:
            record = None
        if snapshot is None:
            if record is None:
                raise KeyError(f"Job {job_id} not found")
            try:
                manifest = self.store.read_manifest(job_id)
            except Exception:
                manifest = None
            try:
                artifacts = self.store.read_artifacts(job_id)
            except Exception:
                artifacts = []
            tiles_total = getattr(record, "tiles_total", None) or (
                manifest.get("tiles_total") if isinstance(manifest, Mapping) else 0
            ) or 0
            status_str = str(getattr(record, "status", None) or JobState.BROWSER_STARTING.value)
            done_tiles = int(tiles_total) if status_str == JobState.DONE.value else 0
            payload: JobSnapshot = {
                "id": record.id,
                "url": str(getattr(record, "url", "") or ""),
                "state": status_str,
                "progress": {"done": done_tiles, "total": int(tiles_total)},
                "manifest_path": str(getattr(record, "manifest_path", "") or ""),
                "cache_hit": bool(getattr(record, "cache_hit", False)),
                "cache_source_job_id": getattr(record, "cache_source_job_id", None),
                "profile_id": getattr(record, "profile_id", None),
                "owner": getattr(record, "owner", None),
                "tags": list(getattr(record, "tags", None) or []),
                "artifacts": artifacts,
                "error": None,
            }
            if isinstance(manifest, dict):
                payload["manifest"] = manifest
        else:
            payload = snapshot.copy()
        if record:
            if record.seam_marker_count is not None:
                payload["seam_marker_count"] = record.seam_marker_count
            else:
                payload.pop("seam_marker_count", None)
            if record.seam_hash_count is not None:
                payload["seam_hash_count"] = record.seam_hash_count
            else:
                payload.pop("seam_hash_count", None)
        state = payload.get("state")
        if isinstance(state, JobState):
            payload["state"] = state.value
        return payload

    def _record_event(self, job_id: str, payload: JobSnapshot) -> None:
        self._append_event_entry(job_id, {"event": "snapshot", "snapshot": payload})

    def _record_custom_event(self, job_id: str, event: str, data: Mapping[str, Any]) -> None:
        self._append_event_entry(job_id, {"event": event, "data": dict(data)})

    def _append_event_entry(self, job_id: str, entry: Mapping[str, Any]) -> None:
        sequence = self._event_sequences.get(job_id, 0)
        enriched = dict(entry)
        enriched.setdefault("job_id", job_id)
        enriched.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
        enriched["sequence"] = sequence
        self._event_sequences[job_id] = sequence + 1
        log = self._event_logs.setdefault(job_id, [])
        log.append(enriched)
        if len(log) > _EVENT_HISTORY_LIMIT:
            del log[: len(log) - _EVENT_HISTORY_LIMIT]
        for queue in list(self._event_subscribers.get(job_id, [])):
            _put_bounded(queue, enriched.copy())
        for queue in list(self._global_subscribers):
            _put_bounded(queue, enriched.copy())

    def _parse_timestamp(self, raw: Any) -> datetime | None:
        if not isinstance(raw, str):
            return None
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed

    def _sequence_newer(self, event: Mapping[str, Any], min_sequence: int) -> bool:
        try:
            seq = int(event.get("sequence", -1))
        except (TypeError, ValueError):
            return False
        return seq > min_sequence

    def _emit_ocr_event(self, job_id: str, manifest: CaptureManifest) -> None:
        summary = _summarize_ocr_batches(manifest)
        if not summary:
            return
        self._record_custom_event(job_id, "ocr_telemetry", summary)

    def _emit_dom_assist_event(self, job_id: str, manifest: CaptureManifest) -> None:
        assists = getattr(manifest, "dom_assists", None)
        if not assists:
            return
        summary = summarize_dom_assists(
            assists,
            tiles_total=getattr(manifest, "tiles_total", None),
        )
        if summary:
            self._record_custom_event(job_id, "dom_assist", summary)

    def _maybe_trigger_webhooks(self, job_id: str, payload: JobSnapshot) -> None:
        sender = self._webhook_sender
        if sender is None:
            return
        hooks = self._webhooks.get(job_id)
        if not hooks:
            return
        state = payload.get("state")
        if not isinstance(state, str):
            return
        for hook in hooks:
            allowed = hook.get("events") or []
            if state not in allowed:
                continue
            hook_url = hook["url"]
            event_payload = {
                "job_id": job_id,
                "state": state,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "snapshot": payload,
            }

            async def _dispatch(target_url: str = hook_url, body: dict[str, Any] = event_payload) -> None:
                await sender(target_url, body)

            asyncio.create_task(_dispatch())


async def execute_capture_job(
    *,
    job_id: str,
    url: str,
    store: Store | None = None,
    config: CaptureConfig | None = None,
    request: JobCreateRequest | None = None,
) -> tuple[CaptureResult, list[dict[str, object]]]:
    """Run the capture pipeline, persisting artifacts + manifest via ``Store``."""

    storage = store or build_store()

    capture_config = config or CaptureConfig(url=url)
    stage_cb = getattr(capture_config, "on_stage", None)
    try:
        capture_result = await capture_tiles(capture_config)
        markdown, ocr_ms, stitch_ms, ocr_links = await _run_ocr_pipeline(
            job_id=job_id,
            capture_result=capture_result,
            request=request,
            on_stage=stage_cb,
        )
        capture_result.manifest.ocr_ms = ocr_ms
        capture_result.manifest.stitch_ms = stitch_ms
        _refresh_manifest_cache_key(capture_result.manifest)
        metrics.observe_manifest_metrics(capture_result.manifest)
        append_warning_log(job_id=job_id, url=url, manifest=capture_result.manifest)
        if callable(stage_cb):
            stage_cb(
                "PERSISTING",
                {"done": len(capture_result.tiles), "total": len(capture_result.tiles)},
            )
        dom_snapshot = getattr(capture_result, "dom_snapshot", None)
        dom_path = None
        dom_links: Sequence[LinkRecord] = []
        base_url = getattr(capture_result.manifest, "url", None) or url
        if dom_snapshot:
            dom_path = storage.write_dom_snapshot(job_id=job_id, html=dom_snapshot)
        dom_overlay = getattr(capture_result, "dom_overlay", None)
        write_dom_overlay = getattr(storage, "write_dom_overlay", None)
        if dom_overlay is not None and callable(write_dom_overlay):
            try:
                write_dom_overlay(job_id=job_id, overlay=dom_overlay)
            except Exception as exc:  # pragma: no cover - defensive logging
                LOGGER.warning("Failed to write dom_overlay.json for %s: %s", job_id, exc)
        write_links = getattr(storage, "write_links", None)
        dom_source: Any = dom_path if dom_path is not None else dom_snapshot
        if dom_source is not None:
            try:
                dom_links = extract_links_from_dom(
                    dom_source,
                    base_url=base_url,
                )
            except Exception as exc:  # pragma: no cover - log and continue
                LOGGER.warning("Failed to extract DOM links for %s: %s", job_id, exc)
        if markdown and (dom_links or base_url):
            markdown = resolve_markdown_links(
                markdown,
                base_url=base_url,
                dom_links=dom_links,
            )
            ocr_links = extract_links_from_markdown(
                markdown,
                base_url=base_url,
                dom_links=dom_links,
            )
        blended_links: Sequence[LinkRecord] = []
        if dom_links or ocr_links:
            blended_links = blend_dom_with_ocr(dom_links=dom_links, ocr_links=ocr_links)
        if markdown and blended_links:
            markdown = append_links_appendix(markdown, blended_links)
        abandoned_tiles = getattr(capture_result, "abandoned_tiles", None)
        try:
            tile_artifacts = storage.write_tiles(
                job_id=job_id,
                tiles=capture_result.tiles,
                abandoned_tiles=abandoned_tiles,
            )
        except TypeError:
            tile_artifacts = storage.write_tiles(job_id=job_id, tiles=capture_result.tiles)
        if markdown and markdown.strip():
            capture_result.manifest.embedding_model = getattr(
                capture_result.manifest, "embedding_model", None
            ) or "hash-bucket-v1"
            capture_result.manifest.embedding_dim = (
                getattr(capture_result.manifest, "embedding_dim", None) or 1536
            )
            parsed_sections = _parse_markdown_sections(markdown)
            capture_result.manifest.sections_embedded = max(1, len(parsed_sections))
        storage.write_manifest(job_id=job_id, manifest=capture_result.manifest)
        if markdown:
            storage.write_markdown(job_id=job_id, content=markdown)
        insert_links = getattr(storage, "insert_links", None)
        if blended_links:
            serialized_links = serialize_links(blended_links)
            if callable(write_links):
                storage.write_links(job_id=job_id, links=serialized_links)
            if callable(insert_links) and not isinstance(storage, Store):
                try:
                    insert_links(job_id=job_id, links=serialized_links)
                except Exception as exc:  # pragma: no cover - defensive logging
                    LOGGER.warning("Failed to insert LinkRecord rows for %s: %s", job_id, exc)
    except Exception:
        raise

    return capture_result, tile_artifacts


async def _default_webhook_sender(url: str, payload: dict[str, Any]) -> None:
    """Best-effort webhook HTTP POST without signing."""

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(url, json=payload)
    except Exception as exc:  # pragma: no cover - logging only
        LOGGER.warning("Webhook delivery to %s failed: %s", url, exc)


def build_signed_webhook_sender(secret: str, *, version: str = "v1") -> WebhookSender:
    """Return a webhook sender that signs payloads using HMAC-SHA256."""

    secret_bytes = secret.encode("utf-8")

    async def _sender(url: str, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        signature = hmac.new(secret_bytes, body, hashlib.sha256).hexdigest()
        headers = {
            "Content-Type": "application/json",
            "X-MDWB-Signature": f"{version}={signature}",
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.post(url, content=body, headers=headers)
        except Exception as exc:  # pragma: no cover - logging only
            LOGGER.warning("Signed webhook delivery to %s failed: %s", url, exc)

    return _sender


async def _run_ocr_pipeline(
    *,
    job_id: str,
    capture_result: CaptureResult,
    request: JobCreateRequest | None = None,
    on_stage: Callable[[str, dict[str, Any] | None], None] | None = None,
) -> tuple[str, int | None, int | None, Sequence[LinkRecord]]:
    """Submit tiles to olmOCR and stitch the resulting Markdown."""

    tiles = capture_result.tiles
    if not tiles:
        return "", None, None, []

    total_tiles = len(tiles)
    if callable(on_stage):
        try:
            on_stage(JobState.OCR_SUBMITTING.value, {"done": 0, "total": total_tiles})
        except Exception:
            pass

    active_settings = global_settings
    effective_model = (
        getattr(request, "ocr_model", None)
        or getattr(request, "ocr_policy", None)
        or getattr(capture_result.manifest, "ocr_model", None)
        or active_settings.ocr.model
    )
    effective_fp8 = getattr(request, "ocr_use_fp8", None)
    if effective_fp8 is None:
        effective_fp8 = getattr(capture_result.manifest, "ocr_use_fp8", None)
    if effective_fp8 is None:
        effective_fp8 = active_settings.ocr.use_fp8
    effective_max_tiles = getattr(request, "ocr_max_tiles_in_flight", None) or getattr(
        capture_result.manifest, "ocr_max_tiles_in_flight", None
    )

    has_ocr_override = bool(
        getattr(request, "ocr_model", None)
        or getattr(request, "ocr_policy", None)
        or getattr(request, "ocr_use_fp8", None) is not None
        or getattr(request, "ocr_max_tiles_in_flight", None) is not None
        or getattr(capture_result.manifest, "requested_ocr_policy", None)
    )
    requests: list[OCRRequest] = [
        OCRRequest(
            tile_id=f"{job_id}-tile-{tile.index:04d}",
            tile_bytes=tile.png_bytes,
            model=effective_model if has_ocr_override else None,
        )
        for tile in tiles
    ]
    progress_emitted = False

    def _ocr_progress(done: int, total: int, meta: dict[str, Any] | None = None) -> None:
        nonlocal progress_emitted
        progress_emitted = True
        if callable(on_stage):
            try:
                on_stage(
                    JobState.OCR_WAITING.value,
                    {"done": done, "total": total, **(meta or {})},
                )
            except Exception:
                pass

    ocr_start = time.perf_counter()
    if has_ocr_override:
        ocr_cfg = replace(
            active_settings.ocr,
            model=effective_model,
            use_fp8=bool(effective_fp8),
            max_concurrency=(
                max(active_settings.ocr.min_concurrency, int(effective_max_tiles))
                if effective_max_tiles is not None
                else active_settings.ocr.max_concurrency
            ),
        )
        effective_settings = replace(active_settings, ocr=ocr_cfg)
        try:
            ocr_output = await submit_tiles(
                requests=requests,
                settings=effective_settings,
                on_progress=_ocr_progress,
            )
        except TypeError:
            try:
                ocr_output = await submit_tiles(requests=requests, settings=effective_settings)
            except TypeError:
                ocr_output = await submit_tiles(requests=requests)
    else:
        try:
            ocr_output = await submit_tiles(requests=requests, on_progress=_ocr_progress)
        except TypeError:
            ocr_output = await submit_tiles(requests=requests)
    if not progress_emitted and callable(on_stage):
        for idx in range(1, total_tiles + 1):
            try:
                on_stage(JobState.OCR_WAITING.value, {"done": idx, "total": total_tiles})
            except Exception:
                pass
    ocr_ms = int((time.perf_counter() - ocr_start) * 1000)
    _apply_ocr_metadata(capture_result.manifest, ocr_output)

    dom_headings = None
    dom_overlays = None
    dom_snapshot = capture_result.dom_snapshot
    if dom_snapshot:
        try:
            dom_headings = extract_headings_from_html(dom_snapshot)
        except Exception as exc:  # pragma: no cover - defensive logging
            LOGGER.warning("Failed to parse DOM headings for %s: %s", job_id, exc)
        try:
            dom_overlays = list(extract_dom_text_overlays(dom_snapshot))
        except Exception as exc:  # pragma: no cover - defensive logging
            LOGGER.warning("Failed to parse DOM overlays for %s: %s", job_id, exc)
    raw_dom_boxes = getattr(capture_result, "dom_overlay", None)
    if raw_dom_boxes:
        try:
            box_overlays = overlays_from_dom_boxes(raw_dom_boxes)
            if dom_overlays:
                dom_overlays = box_overlays + list(dom_overlays)
            else:
                dom_overlays = box_overlays
        except Exception as exc:  # pragma: no cover - defensive logging
            LOGGER.warning("Failed to parse DOM overlay boxes for %s: %s", job_id, exc)

    if callable(on_stage):
        try:
            on_stage(
                JobState.STITCHING.value,
                {"done": total_tiles, "total": total_tiles},
            )
        except Exception:
            pass
    stitch_start = time.perf_counter()
    stitch_result = stitch_markdown(
        ocr_output.markdown_chunks,
        tiles,
        dom_headings=dom_headings,
        dom_overlays=dom_overlays,
        job_id=job_id,
    )
    markdown = stitch_result.markdown
    dom_assists = stitch_result.dom_assists
    seam_events = stitch_result.seam_marker_events
    dedup_events = getattr(stitch_result, "dedup_events", None) or []
    if dom_assists:
        capture_result.manifest.dom_assists = [
            {
                "tile_index": entry.tile_index,
                "line": entry.line,
                "reason": entry.reason,
                "dom_text": entry.dom_text,
                "original_text": entry.original_text,
            }
            for entry in dom_assists
        ]
        summary = summarize_dom_assists(
            capture_result.manifest.dom_assists,
            tiles_total=getattr(capture_result.manifest, "tiles_total", None),
        )
        if summary:
            manifest_any = cast(Any, capture_result.manifest)
            manifest_any.dom_assist_summary = summary
    if seam_events:
        capture_result.manifest.seam_marker_events = [
            {
                "prev_tile_index": event.prev_tile_index,
                "curr_tile_index": event.curr_tile_index,
                "seam_hash": event.seam_hash,
                "prev_overlap_hash": event.prev_overlap_hash,
                "curr_overlap_hash": event.curr_overlap_hash,
            }
            for event in seam_events
        ]
    if dedup_events:
        manifest_any = cast(Any, capture_result.manifest)
        manifest_any.dedup_events = [
            event.to_dict() if hasattr(event, "to_dict") else asdict(event)
            for event in dedup_events
        ]
        manifest_any.dedup_summary = summarize_dedup_events(dedup_events)
    stitch_ms = int((time.perf_counter() - stitch_start) * 1000)

    # Optional semantic post-processing pass: LLM cleans the stitched Markdown.
    # Honors request-level semantic_post_enabled + max_chars; decouple provides endpoint/model/key.
    sem_cfg = resolve_semantic_post_settings(
        enabled_override=bool(getattr(request, "semantic_post_enabled", False)),
        max_chars_override=getattr(request, "semantic_post_max_chars", None),
    )
    semantic_settings = SemanticPostSettings(
        enabled=sem_cfg.enabled,
        endpoint=sem_cfg.endpoint,
        model=sem_cfg.model,
        api_key=sem_cfg.api_key,
        max_chars=sem_cfg.max_chars,
    )
    if semantic_settings.enabled:
        sem_start = time.perf_counter()
        sem_result = await apply_semantic_post(
            markdown=markdown,
            manifest=capture_result.manifest,
            job_id=job_id,
            settings=semantic_settings,
        )
        markdown = sem_result.markdown
        capture_result.manifest.semantic_post_summary = sem_result.summary
        capture_result.manifest.semantic_post_ms = int(
            (time.perf_counter() - sem_start) * 1000
        )

    base_url = getattr(capture_result.manifest, "url", None)
    ocr_links = extract_links_from_markdown(markdown, base_url=base_url)
    return markdown, ocr_ms, stitch_ms, ocr_links


def _apply_ocr_metadata(manifest: CaptureManifest, result: SubmitTilesResult) -> None:
    """Embed OCR telemetry + quota data into the capture manifest."""

    manifest.ocr_batches = [
        {
            "tile_ids": list(batch.tile_ids),
            "latency_ms": batch.latency_ms,
            "status_code": batch.status_code,
            "request_id": batch.request_id,
            "payload_bytes": batch.payload_bytes,
            "attempts": batch.attempts,
        }
        for batch in result.batches
    ]
    manifest.backend_id = result.backend.backend_id
    manifest.backend_mode = result.backend.backend_mode
    manifest.hardware_path = result.backend.hardware_path
    manifest.backend_reason_codes = list(result.backend.reason_codes)
    manifest.backend_reevaluate_after_s = result.backend.reevaluate_after_s
    manifest.fallback_chain = list(result.backend.fallback_chain)
    manifest.hardware_capabilities = result.host_capabilities
    manifest.ocr_local_service = result.local_service
    manifest.ocr_failover_events = [event.to_dict() for event in result.failover_events]
    if getattr(result, "served_model", None):
        manifest.served_ocr_model = result.served_model
    elif not getattr(manifest, "served_ocr_model", None):
        manifest.served_ocr_model = getattr(manifest, "ocr_model", None)
    if getattr(result, "prompt_template", None):
        manifest.ocr_prompt_template = result.prompt_template
    elif not getattr(manifest, "ocr_prompt_template", None):
        manifest.ocr_prompt_template = resolve_prompt_template(getattr(manifest, "ocr_model", None))
    manifest.ocr_quota = {
        "limit": result.quota.limit,
        "used": result.quota.used,
        "threshold_ratio": result.quota.threshold_ratio,
        "warning_triggered": result.quota.warning_triggered,
    }
    if result.autotune:
        manifest.ocr_autotune = result.autotune.to_dict()
    if result.quota.warning_triggered and result.quota.limit and result.quota.used is not None:
        manifest.warnings.append(
            CaptureWarningEntry(
                code="ocr-quota",
                message="Hosted OCR usage exceeded 70% of configured daily quota.",
                count=float(result.quota.used),
                threshold=float(result.quota.limit),
            )
        )


def _build_replay_metadata(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Summarize a replay request for event logs/webhooks."""

    summary: dict[str, Any] = {
        "source_job_id": manifest.get("job_id"),
        "source_url": manifest.get("url"),
        "source_profile_id": manifest.get("profile_id"),
    }
    environment = manifest.get("environment")
    if isinstance(environment, Mapping):
        summary["source_cft_version"] = environment.get("cft_version")
        summary["source_cft_label"] = environment.get("cft_label")
        summary["source_playwright_version"] = environment.get("playwright_version")
    try:
        digest = hashlib.sha1(json.dumps(manifest, sort_keys=True).encode("utf-8")).hexdigest()
    except Exception:  # pragma: no cover - defensive guard for unexpected payloads
        digest = None
    if digest:
        summary["manifest_sha1"] = digest[:12]
    return {key: value for key, value in summary.items() if value not in (None, "", [])}


def _effective_settings_for_config(
    settings: Settings,
    config: CaptureConfig | None,
) -> Settings:
    if config is None:
        return settings
    effective_model = config.ocr_model or config.ocr_policy or settings.ocr.model
    effective_fp8 = config.ocr_use_fp8 if config.ocr_use_fp8 is not None else settings.ocr.use_fp8
    effective_max_concurrency = (
        max(settings.ocr.min_concurrency, int(config.ocr_max_tiles_in_flight))
        if config.ocr_max_tiles_in_flight is not None
        else settings.ocr.max_concurrency
    )
    if (
        effective_model == settings.ocr.model
        and effective_fp8 == settings.ocr.use_fp8
        and effective_max_concurrency == settings.ocr.max_concurrency
    ):
        return settings
    return replace(
        settings,
        ocr=replace(
            settings.ocr,
            model=effective_model,
            use_fp8=bool(effective_fp8),
            max_concurrency=effective_max_concurrency,
        ),
    )


def _build_capture_config(
    request: JobCreateRequest,
    settings: Settings,
    *,
    owner: str | None = None,
) -> CaptureConfig:
    viewport_width = request.viewport_width or settings.browser.viewport_width
    viewport_height = request.viewport_height or settings.browser.viewport_height
    device_scale_factor = request.device_scale_factor or settings.browser.device_scale_factor
    color_scheme = (request.color_scheme or settings.browser.color_scheme or "light").lower()
    if color_scheme not in {"light", "dark"}:
        color_scheme = "light"
    ocr_policy = request.ocr_policy
    ocr_model = request.ocr_model or request.ocr_policy
    ocr_use_fp8 = request.ocr_use_fp8
    ocr_max_tiles_in_flight = request.ocr_max_tiles_in_flight
    long_side_px = request.long_side_px
    policy_candidate = ocr_policy or ocr_model
    if policy_candidate:
        try:
            profile = resolve_model_profile(policy_candidate, allow_disabled=False)
            ocr_policy = profile.policy_key
            if not request.ocr_model:
                ocr_model = profile.policy_key
            if long_side_px is None:
                long_side_px = profile.long_side_px
            if ocr_use_fp8 is None:
                ocr_use_fp8 = profile.fp8_preferred
            if ocr_max_tiles_in_flight is None:
                ocr_max_tiles_in_flight = profile.max_tiles_in_flight
        except Exception:
            pass
    if long_side_px is None:
        long_side_px = settings.browser.long_side_px
    return CaptureConfig(
        url=request.url,
        viewport_width=viewport_width,
        viewport_height=viewport_height,
        device_scale_factor=device_scale_factor,
        color_scheme=color_scheme,
        reduced_motion=True,
        profile_id=request.profile_id,
        owner=owner,
        long_side_px=long_side_px,
        ocr_policy=ocr_policy,
        ocr_model=ocr_model,
        ocr_use_fp8=ocr_use_fp8,
        ocr_max_tiles_in_flight=ocr_max_tiles_in_flight,
        semantic_post_enabled=bool(request.semantic_post_enabled),
        semantic_post_max_chars=request.semantic_post_max_chars,
    )


_OCR_PROMPT_VERSION = "v8_plain_text_accepted"


def _build_cache_seed(*, config: CaptureConfig, settings: Settings) -> str:
    """Build backend-agnostic cache seed for this capture request."""
    from pathlib import Path
    from app.blocklist import cached_blocklist
    from app.capture import _profile_storage_state_path, compute_effective_style_hash

    normalized_url = _normalize_url(config.url)
    effective_model = config.ocr_model or config.ocr_policy or settings.ocr.model
    effective_fp8 = config.ocr_use_fp8 if config.ocr_use_fp8 is not None else settings.ocr.use_fp8
    blocklist_version = ""
    blocklist_selectors: tuple[str, ...] = ()
    blocklist_path = getattr(settings.browser, "blocklist_path", None)
    if blocklist_path:
        try:
            bl_cfg = cached_blocklist(str(blocklist_path))
            blocklist_version = str(bl_cfg.version or "")
            blocklist_selectors = tuple(bl_cfg.selectors_for_url(normalized_url))
        except Exception:
            pass
    effective_style_hash = compute_effective_style_hash(
        settings_browser=settings.browser,
        config=config,
        blocklist_version=blocklist_version,
    )
    prompt_template = resolve_prompt_template(effective_model)
    payload: dict[str, Any] = {
        "url": normalized_url,
        "cft_version": config.cft_version or settings.browser.cft_version,
        "cft_label": config.cft_label or settings.browser.cft_label,
        "playwright_channel": getattr(settings.browser, "playwright_channel", "cft"),
        "browser_transport": config.browser_transport or settings.browser.browser_transport,
        "viewport": {
            "width": config.viewport_width,
            "height": config.viewport_height,
            "device_scale_factor": config.device_scale_factor,
            "color_scheme": config.color_scheme,
        },
        "viewport_overlap_px": settings.browser.viewport_overlap_px,
        "tile_overlap_px": settings.browser.tile_overlap_px,
        "scroll_settle_ms": getattr(settings.browser, "scroll_settle_ms", 350),
        "long_side_px": config.long_side_px or settings.browser.long_side_px,
        "screenshot_style_hash": effective_style_hash,
        "mask_selectors": list(settings.browser.screenshot_mask_selectors),
        "blocklist_version": blocklist_version,
        "blocklist_selectors": list(blocklist_selectors),
        "ocr_model": effective_model,
        "ocr_use_fp8": effective_fp8,
        "ocr_prompt_version": _OCR_PROMPT_VERSION,
        "ocr_prompt_template": prompt_template,
        "profile_id": config.profile_id or "",
    }
    if config.ocr_policy:
        payload["ocr_policy"] = config.ocr_policy
    sem_settings = getattr(settings, "semantic_post", None)
    sem_enabled = bool(
        config.semantic_post_enabled or getattr(sem_settings, "enabled", False)
    )
    if sem_enabled:
        sem_cfg = resolve_semantic_post_settings(
            enabled_override=True,
            max_chars_override=config.semantic_post_max_chars,
        )
        payload["semantic_post_enabled"] = True
        payload["semantic_post_max_chars"] = (
            config.semantic_post_max_chars
            if config.semantic_post_max_chars is not None
            else getattr(sem_settings, "max_chars", sem_cfg.max_chars)
        )
        payload["semantic_post_model"] = (
            getattr(sem_settings, "model", None) or sem_cfg.model
        )
        payload["semantic_post_endpoint"] = (
            getattr(sem_settings, "endpoint", None) or sem_cfg.endpoint
        )
    if config.profile_id:
        profiles_root = getattr(getattr(settings, "storage", None), "profiles_root", None)
        if profiles_root:
            try:
                state_path = _profile_storage_state_path(
                    Path(profiles_root),
                    config.profile_id,
                    owner=config.owner,
                )
                if state_path.exists():
                    payload["profile_state_sha256"] = hashlib.sha256(
                        state_path.read_bytes()
                    ).hexdigest()
            except Exception:
                pass
    if config.owner:
        payload["owner"] = config.owner
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def _build_runtime_cache_key(
    *,
    cache_seed: str,
    backend_id: str,
    backend_mode: str,
    hardware_path: str,
    fallback_chain: Sequence[str],
    served_ocr_model: str | None = None,
    ocr_model: str | None = None,
) -> str:
    payload: dict[str, Any] = {
        "cache_seed": cache_seed,
        "ocr_backend_id": backend_id,
        "ocr_backend_mode": backend_mode,
        "ocr_hardware_path": hardware_path,
        "ocr_fallback_chain": [entry for entry in fallback_chain if entry],
    }
    if (
        served_ocr_model
        and ocr_model
        and str(served_ocr_model).strip()
        and str(ocr_model).strip()
        and str(served_ocr_model).strip() != str(ocr_model).strip()
    ):
        payload["served_ocr_model"] = str(served_ocr_model).strip()
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def _build_cache_key(*, config: CaptureConfig, settings: Settings) -> str:
    cache_seed = config.cache_seed or _build_cache_seed(config=config, settings=settings)
    effective_settings = _effective_settings_for_config(settings, config)
    backend = resolve_ocr_backend(effective_settings, capabilities=get_host_capabilities())
    return _build_runtime_cache_key(
        cache_seed=cache_seed,
        backend_id=backend.backend_id,
        backend_mode=backend.backend_mode,
        hardware_path=backend.hardware_path,
        fallback_chain=backend.fallback_chain,
    )


def _refresh_manifest_cache_key(manifest: CaptureManifest) -> None:
    """Recompute cache key from runtime backend so failover outputs do not poison cache."""

    cache_seed = getattr(manifest, "cache_seed", None)
    backend_id = getattr(manifest, "backend_id", None)
    backend_mode = getattr(manifest, "backend_mode", None)
    hardware_path = getattr(manifest, "hardware_path", None)
    fallback_chain = list(getattr(manifest, "fallback_chain", []) or [])
    if not (cache_seed and backend_id and backend_mode and hardware_path):
        return
    if not fallback_chain:
        fallback_chain = [backend_id]
    served_ocr_model = getattr(manifest, "served_ocr_model", None)
    ocr_model = getattr(manifest, "ocr_model", None)
    manifest.cache_key = _build_runtime_cache_key(
        cache_seed=cache_seed,
        backend_id=backend_id,
        backend_mode=backend_mode,
        hardware_path=hardware_path,
        fallback_chain=fallback_chain,
        served_ocr_model=str(served_ocr_model) if isinstance(served_ocr_model, str) else None,
        ocr_model=str(ocr_model) if isinstance(ocr_model, str) else None,
    )


def _normalize_url(url: str) -> str:
    parsed = urlparse(url)
    scheme = (parsed.scheme or "http").lower()
    netloc = (parsed.netloc or "").lower()
    path = parsed.path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
    canonical_query = urlencode(sorted(query_pairs))
    normalized = parsed._replace(
        scheme=scheme,
        netloc=netloc,
        path=path,
        query=canonical_query,
        fragment="",
    )
    return urlunparse(normalized)


def _summarize_ocr_batches(manifest: CaptureManifest) -> dict[str, Any] | None:
    batches = getattr(manifest, "ocr_batches", None)
    if not batches:
        return None
    latencies = [
        batch.get("latency_ms")
        for batch in batches
        if isinstance(batch.get("latency_ms"), (int, float))
    ]
    payloads = [
        batch.get("payload_bytes")
        for batch in batches
        if isinstance(batch.get("payload_bytes"), (int, float))
    ]
    non_2xx = sum(
        1
        for batch in batches
        if isinstance(batch.get("status_code"), int) and batch["status_code"] >= 400
    )
    summary: dict[str, Any] = {
        "total_batches": len(batches),
        "non_2xx_batches": non_2xx,
        "backend_id": getattr(manifest, "backend_id", None),
        "backend_mode": getattr(manifest, "backend_mode", None),
        "hardware_path": getattr(manifest, "hardware_path", None),
        "backend_reason_codes": getattr(manifest, "backend_reason_codes", None),
        "backend_reevaluate_after_s": getattr(manifest, "backend_reevaluate_after_s", None),
    }
    hardware = getattr(manifest, "hardware_capabilities", None) or {}
    if isinstance(hardware, Mapping):
        summary["has_gpu"] = hardware.get("has_gpu")
        summary["gpu_count"] = hardware.get("gpu_count")
    if latencies:
        summary["avg_latency_ms"] = int(sum(latencies) / len(latencies))
        summary["max_latency_ms"] = int(max(latencies))
    if payloads:
        summary["total_payload_bytes"] = int(sum(payloads))
    last_request_id = next(
        (batch.get("request_id") for batch in reversed(batches) if batch.get("request_id")), None
    )
    if last_request_id:
        summary["last_request_id"] = last_request_id
    quota = getattr(manifest, "ocr_quota", None) or {}
    if quota:
        summary["quota_used"] = quota.get("used")
        summary["quota_limit"] = quota.get("limit")
        summary["quota_warning"] = bool(quota.get("warning_triggered"))
    autotune = getattr(manifest, "ocr_autotune", None) or {}
    if autotune:
        events = autotune.get("events") or []
        summary["autotune"] = {
            "initial_limit": autotune.get("initial_limit"),
            "final_limit": autotune.get("final_limit"),
            "peak_limit": autotune.get("peak_limit"),
            "event_count": len(events),
            "reevaluate_policy": autotune.get("reevaluate_policy"),
            "reevaluation_reason_code": autotune.get("reevaluation_reason_code"),
            "policy_suppression_count": autotune.get("policy_suppression_count"),
            "policy_cooldown_suppression_count": autotune.get(
                "policy_cooldown_suppression_count"
            ),
            "policy_flap_suppression_count": autotune.get("policy_flap_suppression_count"),
            "policy_switch_count_window": autotune.get("policy_switch_count_window"),
        }
        if events:
            summary["autotune"]["last_event"] = events[-1]
    local_service = getattr(manifest, "ocr_local_service", None) or {}
    if isinstance(local_service, Mapping) and local_service:
        summary["local_service"] = {
            "action": local_service.get("action"),
            "healthy": local_service.get("healthy"),
            "reason": local_service.get("reason"),
            "managed": local_service.get("managed"),
            "restart_count": local_service.get("restart_count"),
            "launch_attempts": local_service.get("launch_attempts"),
        }
    failover_events = getattr(manifest, "ocr_failover_events", None) or []
    if isinstance(failover_events, Sequence) and failover_events:
        summary["failover"] = {
            "event_count": len(failover_events),
            "backend_failures": sum(
                1
                for event in failover_events
                if isinstance(event, Mapping) and event.get("event") == "backend_failed"
            ),
            "backend_skips": sum(
                1
                for event in failover_events
                if isinstance(event, Mapping) and event.get("event") == "backend_skipped"
            ),
        }
        last_event = failover_events[-1]
        if isinstance(last_event, Mapping):
            summary["failover"]["last_reason_code"] = last_event.get("reason_code")
    return summary


def _persist_pending_webhooks(store: Store, pending: Sequence[dict[str, Any]], job_id: str) -> None:
    for entry in pending:
        try:
            record = store.register_webhook(job_id=job_id, url=entry["url"], events=entry["events"])
            entry["id"] = record.id
        except KeyError:  # pragma: no cover - should not happen after allocation
            LOGGER.warning("Skipping webhook persistence for %s; run missing", job_id)


def _webhook_matches(entry: dict[str, Any], webhook_id: int | None, url: str | None) -> bool:
    """Return True when the entry satisfies the selector passed by the caller.

    When both ``webhook_id`` and ``url`` are provided we require *both* to match,
    mirroring the SQL filters in :meth:`Store.delete_webhooks`. This prevents
    removing cached entries that the persistence layer rejected (keeping the
    in-memory view and database in sync).
    """

    entry_id = entry.get("id")
    entry_url = entry.get("url")
    if webhook_id is not None and url:
        return entry_id == webhook_id and entry_url == url
    if webhook_id is not None:
        return entry_id == webhook_id
    if url:
        return entry_url == url
    return False


# ---------------------------------------------------------------------------
# Structured-result helpers (for /jobs/{id}/result.json)
# ---------------------------------------------------------------------------

_HEADING_LINE_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_PROVENANCE_COMMENT_RE = re.compile(r"<!--\s*source:\s*tile_(\d+)[^>]*-->", re.IGNORECASE)
_LINK_RE = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(text: str) -> str:
    slug = _SLUG_RE.sub("-", text.lower()).strip("-")
    return slug or "section"


def _parse_markdown_sections(markdown: str) -> list[dict[str, Any]]:
    """Split a stitched Markdown into heading + body sections.

    Returns a list of {level, heading, body, anchor, tile_indices}. Body text
    is everything between this heading and the next; tile_indices records any
    ``<!-- source: tile_i ... -->`` provenance comments active for the section.
    """
    if not markdown:
        return []
    sections: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    active_tile: int | None = None
    for line in markdown.splitlines():
        prov_match = _PROVENANCE_COMMENT_RE.search(line)
        if prov_match:
            try:
                active_tile = int(prov_match.group(1))
            except ValueError:
                pass
            stripped_line = _PROVENANCE_COMMENT_RE.sub("", line).strip()
            if not stripped_line:
                continue
        match = _HEADING_LINE_RE.match(line)
        if match:
            if current is not None:
                sections.append(current)
            heading_text = match.group(2).strip()
            current = {
                "level": len(match.group(1)),
                "heading": heading_text,
                "body": "",
                "anchor": _slugify(heading_text),
                "tile_indices": [active_tile] if active_tile is not None else [],
            }
        else:
            if current is None:
                continue
            if line.strip() and active_tile is not None and active_tile not in current["tile_indices"]:
                current["tile_indices"].append(active_tile)
            if current["body"]:
                current["body"] += "\n" + line
            else:
                current["body"] = line
    if current is not None:
        sections.append(current)
    return sections


def _extract_markdown_links(markdown: str) -> list[str]:
    """Pull http(s) hrefs out of [text](href) Markdown links."""
    if not markdown:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for href in _LINK_RE.findall(markdown):
        if href.startswith(("http://", "https://")) and href not in seen:
            seen.add(href)
            out.append(href)
    return out
