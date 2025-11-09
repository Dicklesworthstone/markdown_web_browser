"""Entry point for the FastAPI application."""

from __future__ import annotations

import asyncio
import html
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
    ReplayRequest,
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
        profile_id=snapshot.get("profile_id"),
        cache_hit=snapshot.get("cache_hit"),
    )


def _render_highlight_page(*, job_id: str, tile: str, y0: int, y1: int) -> str:
    image_url = f"/jobs/{job_id}/artifact/{tile}"
    safe_image_url = html.escape(image_url, quote=True)
    safe_tile = html.escape(tile)
    highlight_height = max(1, y1 - y0)
    return f"""
<!DOCTYPE html>
<html lang=\"en\">
  <head>
    <meta charset=\"utf-8\" />
    <title>Tile highlight — {safe_tile}</title>
    <style>
      body {{
        font-family: system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
        margin: 1.5rem;
        background: #0f1115;
        color: #f2f4f8;
      }}
      .viewer {{
        max-width: 960px;
        margin: 0 auto;
      }}
      .tile-wrapper {{
        position: relative;
        display: inline-block;
        border: 1px solid #333a45;
        background: #1b1f27;
      }}
      #tile-image {{
        display: block;
        max-width: 100%;
      }}
      #highlight-box {{
        position: absolute;
        left: 0;
        right: 0;
        border: 2px solid rgba(255, 193, 7, 0.9);
        background: rgba(255, 193, 7, 0.25);
        pointer-events: none;
      }}
      .meta {{
        margin-top: 1rem;
        font-size: 0.9rem;
      }}
      .meta code {{
        background: #272c36;
        padding: 0.15rem 0.35rem;
        border-radius: 0.25rem;
      }}
    </style>
  </head>
  <body>
    <main class=\"viewer\">
      <div class=\"tile-wrapper\">
        <img id=\"tile-image\" src=\"{safe_image_url}\" alt=\"Tile image {safe_tile}\" />
        <div id=\"highlight-box\" data-y0=\"{y0}\" data-height=\"{highlight_height}\"></div>
      </div>
      <section class=\"meta\">
        <p><strong>Tile:</strong> <code>{safe_tile}</code></p>
        <p><strong>Highlight:</strong> y={y0} → y={y1}</p>
      </section>
    </main>
    <script>
      (function() {{
        const img = document.getElementById('tile-image');
        const box = document.getElementById('highlight-box');
        const y0 = Number(box.dataset.y0) || 0;
        const height = Number(box.dataset.height) || 1;
        const update = () => {{
          if (!img.naturalHeight) {{
            return;
          }}
          const scale = img.clientHeight / img.naturalHeight;
          box.style.top = `${{y0 * scale}}px`;
          box.style.height = `${{height * scale}}px`;
        }};
        if (img.complete) {{
          update();
        }} else {{
          img.addEventListener('load', update, {{ once: true }});
        }}
        window.addEventListener('resize', update);
      }})();
    </script>
  </body>
</html>
"""


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


@app.post("/replay", response_model=JobSnapshotResponse, status_code=status.HTTP_202_ACCEPTED)
async def replay_job(request: ReplayRequest) -> JobSnapshotResponse:
    """Replay a stored manifest by enqueueing a new capture with the same URL/profile."""

    try:
        snapshot = await JOB_MANAGER.replay_job(request.manifest)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
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
                    if sequence is not None and last_sequence is not None and sequence < last_sequence:
                        continue
                    if sequence is not None:
                        last_sequence = sequence
                    yield _serialize_log_entry(event_entry) + "\n"
                except asyncio.TimeoutError:
                    heartbeat += 1
                    metrics.increment_sse_heartbeat()
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
async def job_links(job_id: str) -> list[dict[str, object]]:
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


@app.get("/jobs/{job_id}/artifact/highlight", response_class=HTMLResponse)
async def job_artifact_highlight(job_id: str, tile: str, y0: int = 0, y1: int | None = None) -> HTMLResponse:
    try:
        store.resolve_artifact(job_id, tile)
    except (KeyError, FileNotFoundError) as exc:
        raise HTTPException(status_code=404, detail="Artifact not found") from exc
    start = max(0, y0)
    end = max(start + 1, y1 if y1 is not None else start + 1)
    content = _render_highlight_page(job_id=job_id, tile=tile, y0=start, y1=end)
    return HTMLResponse(content)


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
    profile_id = snapshot.get("profile_id")
    if profile_id:
        events.append(("profile", str(profile_id)))
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
            dom_assists = manifest.get("dom_assists")
            if isinstance(dom_assists, list) and dom_assists:
                events.append(("dom_assist", json.dumps({"count": len(dom_assists)})))
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
