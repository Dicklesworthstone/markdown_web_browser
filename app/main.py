"""Entry point for the FastAPI application."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Mapping, cast

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from prometheus_client import start_http_server
from prometheus_fastapi_instrumentator import Instrumentator

from app import metrics
from app.dom_links import blend_dom_with_ocr, demo_dom_links, demo_ocr_links, serialize_links
from app.jobs import JobManager, JobSnapshot, JobState, build_signed_webhook_sender
from app.schemas import (
    EmbeddingSearchRequest,
    EmbeddingSearchResponse,
    JobCreateRequest,
    JobSnapshotResponse,
    SectionEmbeddingMatch,
    WebhookRegistrationRequest,
    WebhookSubscription,
    WebhookDeleteRequest,
)
from app.settings import settings
from app.store import build_store

BASE_DIR = Path(__file__).resolve().parent.parent
WEB_ROOT = BASE_DIR / "web"

LOGGER = logging.getLogger(__name__)
_PROMETHEUS_EXPORTER_STARTED = False


async def _start_prometheus_exporter() -> None:
    """Expose Prometheus metrics on the configured auxiliary port."""

    global _PROMETHEUS_EXPORTER_STARTED
    if _PROMETHEUS_EXPORTER_STARTED:
        return
    port = settings.telemetry.prometheus_port
    if port <= 0:
        return
    try:
        start_http_server(port)
    except OSError as exc:  # pragma: no cover - system dependent
        LOGGER.warning("Prometheus exporter failed to bind on port %s: %s", port, exc)
        return
    _PROMETHEUS_EXPORTER_STARTED = True
    LOGGER.info("Prometheus exporter listening on port %s", port)


@asynccontextmanager
async def _lifespan(_: FastAPI):
    await _start_prometheus_exporter()
    yield


app = FastAPI(title="Markdown Web Browser", lifespan=_lifespan)
app.mount("/static", StaticFiles(directory=WEB_ROOT), name="static")
instrumentator = Instrumentator(should_instrument_requests_inprogress=True)
instrumentator.instrument(app)
try:
    instrumentator.expose(app, include_in_schema=False, should_gzip=True)
except ValueError:  # pragma: no cover - already registered
    LOGGER.debug("Prometheus /metrics endpoint already exposed")

JOB_MANAGER = JobManager(webhook_sender=build_signed_webhook_sender(settings.webhook_secret))
store = build_store()


def _demo_manifest_payload() -> dict:
    warnings = [
        {
            "code": "canvas-heavy",
            "message": "High canvas count may hide chart labels.",
            "count": 6,
            "threshold": 3,
        },
        {
            "code": "video-heavy",
            "message": "Multiple video elements detected; expect motion blur.",
            "count": 3,
            "threshold": 2,
        },
    ]
    return {
        "job_id": "demo",
        "cft_version": "chrome-130.0.6723.69",
        "cft_label": "Stable-1",
        "playwright_version": "1.55.0",
        "device_scale_factor": 2,
        "long_side_px": 1288,
        "tiles_total": 12,
        "capture_ms": 11234,
        "ocr_ms": 20987,
        "stitch_ms": 1289,
        "blocklist_version": "2025-11-07",
        "blocklist_hits": {
            "#onetrust-consent-sdk": 2,
            "[data-testid='cookie-banner']": 1,
        },
        "warnings": warnings,
    }


def _demo_snapshot() -> dict:
    snapshot = {
        "id": "demo",
        "url": "https://example.com/article",
        "state": "CAPTURING",
        "progress": {"done": 4, "total": 12},
        "manifest": _demo_manifest_payload(),
    }
    snapshot["links"] = serialize_links(
        blend_dom_with_ocr(dom_links=demo_dom_links(), ocr_links=demo_ocr_links())
    )
    return snapshot


def _snapshot_to_response(snapshot: JobSnapshot) -> JobSnapshotResponse:
    state = snapshot.get("state")
    if isinstance(state, JobState):
        state_value = state.value
    else:
        state_value = str(state)
    manifest = snapshot.get("manifest")
    return JobSnapshotResponse(
        id=snapshot["id"],
        state=state_value,
        url=snapshot["url"],
        progress=snapshot.get("progress"),
        manifest_path=snapshot.get("manifest_path"),
        manifest=manifest,
        error=snapshot.get("error"),
    )


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    """Serve the current web UI shell."""

    return (WEB_ROOT / "index.html").read_text(encoding="utf-8")


@app.get("/health", tags=["health"])
async def healthcheck() -> dict[str, str]:
    """Return a simple status useful for smoke tests."""

    return {"status": "ok"}


@app.get("/jobs/demo")
async def demo_job_snapshot() -> dict:
    """Return a deterministic demo job snapshot."""

    return _demo_snapshot()


@app.post("/jobs", response_model=JobSnapshotResponse, status_code=status.HTTP_202_ACCEPTED)
async def create_job(request: JobCreateRequest) -> JobSnapshotResponse:
    snapshot = await JOB_MANAGER.create_job(request)
    return _snapshot_to_response(snapshot)


@app.get("/jobs/{job_id}", response_model=JobSnapshotResponse)
async def fetch_job(job_id: str) -> JobSnapshotResponse:
    try:
        snapshot = JOB_MANAGER.get_snapshot(job_id)
    except KeyError as exc:  # pragma: no cover - runtime only
        raise HTTPException(status_code=404, detail="Job not found") from exc
    return _snapshot_to_response(snapshot)


@app.get("/jobs/{job_id}/stream")
async def job_stream(job_id: str, request: Request) -> StreamingResponse:
    try:
        queue = JOB_MANAGER.subscribe(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Job not found") from exc

    async def event_generator() -> AsyncIterator[str]:
        heartbeat = 0
        try:
            while True:
                try:
                    snapshot = await asyncio.wait_for(queue.get(), timeout=5)
                except asyncio.TimeoutError:
                    heartbeat += 1
                    metrics.increment_sse_heartbeat()
                    yield f"event: log\ndata: <li>Heartbeat {heartbeat}: waiting for updates…</li>\n\n"
                    if await request.is_disconnected():
                        break
                    continue
                for event_name, payload in _snapshot_events(snapshot):
                    yield f"event: {event_name}\ndata: {payload}\n\n"
                if await request.is_disconnected():
                    break
        finally:
            JOB_MANAGER.unsubscribe(job_id, queue)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.get("/jobs/{job_id}/events")
async def job_events(job_id: str, request: Request, since: str | None = None) -> StreamingResponse:
    parsed_since = _parse_since(since)
    try:
        backlog, queue = JOB_MANAGER.subscribe_events(job_id, since=parsed_since)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Job not found") from exc

    async def event_generator() -> AsyncIterator[str]:
        heartbeat = 0
        last_sequence = _extract_sequence(backlog[-1]) if backlog else None
        try:
            for entry in backlog:
                yield _serialize_log_entry(entry) + "\n"
            while True:
                try:
                    event_entry = await asyncio.wait_for(queue.get(), timeout=5)
                    heartbeat = 0
                    sequence = _extract_sequence(event_entry)
                    if sequence is not None and last_sequence is not None and sequence <= last_sequence:
                        continue
                    if sequence is not None:
                        last_sequence = sequence
                    yield _serialize_log_entry(event_entry) + "\n"
                except asyncio.TimeoutError:
                    heartbeat += 1
                    heartbeat_entry = {
                        "event": "heartbeat",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "data": {"count": heartbeat},
                    }
                    yield json.dumps(heartbeat_entry) + "\n"
                if await request.is_disconnected():
                    break
        finally:
            JOB_MANAGER.unsubscribe_events(job_id, queue)

    return StreamingResponse(event_generator(), media_type="application/x-ndjson")


@app.get("/jobs/{job_id}/links.json")
async def job_links(job_id: str) -> list[dict[str, str]]:
    """Return stored links for a job, falling back to demo data when requested."""

    if job_id == "demo":
        blended = blend_dom_with_ocr(dom_links=demo_dom_links(), ocr_links=demo_ocr_links())
        return serialize_links(blended)
    try:
        return store.read_links(job_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@app.get("/jobs/{job_id}/manifest.json")
async def job_manifest(job_id: str) -> JSONResponse:
    try:
        manifest = store.read_manifest(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Job not found") from exc
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Manifest not available yet") from None
    return JSONResponse(manifest)

@app.get("/jobs/{job_id}/result.md")
async def job_markdown(job_id: str) -> PlainTextResponse:
    try:
        markdown = store.read_markdown(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Job not found") from exc
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Markdown not available yet") from None
    return PlainTextResponse(markdown, media_type="text/markdown")


@app.get("/jobs/{job_id}/artifact/{artifact_path:path}")
async def job_artifact(job_id: str, artifact_path: str) -> FileResponse:
    try:
        target = store.resolve_artifact(job_id, artifact_path)
    except (KeyError, FileNotFoundError) as exc:
        raise HTTPException(status_code=404, detail="Artifact not found") from exc
    return FileResponse(target)


@app.post("/jobs/{job_id}/embeddings/search", response_model=EmbeddingSearchResponse)
async def embeddings_search(job_id: str, payload: EmbeddingSearchRequest) -> EmbeddingSearchResponse:
    """Search section embeddings for a capture run using cosine similarity."""

    try:
        total, matches = await asyncio.to_thread(
            store.search_section_embeddings,
            job_id=job_id,
            vector=payload.vector,
            top_k=payload.top_k,
        )
    except KeyError as exc:  # pragma: no cover - run not found
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return EmbeddingSearchResponse(
        total_sections=total,
        matches=[
            SectionEmbeddingMatch(
                section_id=match.section_id,
                tile_start=match.tile_start,
                tile_end=match.tile_end,
                similarity=match.similarity,
                distance=match.distance,
            )
            for match in matches
        ],
    )


@app.post("/jobs/{job_id}/webhooks", status_code=status.HTTP_202_ACCEPTED)
async def register_webhook(job_id: str, payload: WebhookRegistrationRequest) -> dict[str, Any]:
    try:
        JOB_MANAGER.register_webhook(job_id, url=payload.url, events=payload.events)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Job not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"job_id": job_id, "registered": True}


@app.get("/jobs/{job_id}/webhooks", response_model=list[WebhookSubscription])
async def list_webhooks(job_id: str) -> list[WebhookSubscription]:
    try:
        records = store.list_webhooks(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Job not found") from exc
    return [
        WebhookSubscription(url=record.url, events=record.events, created_at=record.created_at)
        for record in records
    ]


@app.delete("/jobs/{job_id}/webhooks")
async def delete_webhook(job_id: str, payload: WebhookDeleteRequest) -> dict[str, Any]:
    if payload.id is None and not payload.url:
        raise HTTPException(status_code=400, detail="Provide an id or url to delete a webhook")
    try:
        deleted = JOB_MANAGER.delete_webhook(job_id, webhook_id=payload.id, url=payload.url)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Job not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if deleted == 0:
        raise HTTPException(status_code=404, detail="Webhook not found")
    return {"job_id": job_id, "deleted": deleted}


def _snapshot_events(snapshot: JobSnapshot) -> list[tuple[str, str]]:
    events: list[tuple[str, str]] = []
    state = snapshot.get("state")
    if state:
        state_value = state if isinstance(state, str) else str(state)
        events.append(("state", state_value))
    progress = snapshot.get("progress")
    if isinstance(progress, dict):
        done = progress.get("done", 0)
        total = progress.get("total", 0)
        events.append(("progress", f"{done} / {total} tiles"))
    manifest = snapshot.get("manifest")
    if manifest:
        events.append(("manifest", json.dumps(manifest)))
        if isinstance(manifest, dict):
            warnings = manifest.get("warnings")
            if warnings:
                events.append(("warnings", json.dumps(warnings)))
            blocklist_hits = manifest.get("blocklist_hits")
            if blocklist_hits:
                events.append(("blocklist", json.dumps(blocklist_hits)))
            sweep_stats = manifest.get("sweep_stats")
            overlap_ratio = manifest.get("overlap_match_ratio")
            if sweep_stats or overlap_ratio is not None:
                events.append(
                    (
                        "sweep",
                        json.dumps(
                            {
                                "sweep_stats": sweep_stats,
                                "overlap_match_ratio": overlap_ratio,
                            }
                        ),
                    )
                )
            validation_failures = manifest.get("validation_failures")
            if validation_failures:
                events.append(("validation", json.dumps(validation_failures)))
            environment = manifest.get("environment")
            if isinstance(environment, dict):
                env_data = cast(dict[str, Any], environment)
                cft_label = str(env_data.get("cft_label") or env_data.get("cft_version") or "CfT")
                playwright_version = str(env_data.get("playwright_version") or "?")
                events.append(("runtime", f"{cft_label} · Playwright {playwright_version}"))
    artifacts = snapshot.get("artifacts")
    if artifacts:
        events.append(("artifacts", json.dumps(artifacts)))
    error = snapshot.get("error")
    if error:
        events.append(("log", f"<li class=\"text-red-500\">{error}</li>"))
    return events


def _serialize_log_entry(entry: dict[str, Any]) -> str:
    payload = entry.copy()
    payload.setdefault("event", "snapshot")
    return json.dumps(payload)


def _extract_sequence(entry: Mapping[str, Any]) -> int | None:
    try:
        raw = entry.get("sequence") if isinstance(entry, Mapping) else None
    except AttributeError:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _parse_since(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid since timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed
