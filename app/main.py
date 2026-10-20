"""Entry point for the FastAPI application."""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Mapping, cast

from fastapi import FastAPI, HTTPException, Query, Request, status
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from prometheus_client import start_http_server
from prometheus_fastapi_instrumentator import Instrumentator

from app import metrics
from app.auth import AUTH_CONTEXT, authenticate, claim_resource, current_owner, owns_resource
from app.rate_limit import consume_shared
from app.crawler import CrawlConfig, get_crawler, submit_and_wait_for_job
from app.dom_links import blend_dom_with_ocr, demo_dom_links, demo_ocr_links, serialize_links
from app.jobs import (
    EventHistoryGap,
    EventPersistenceError,
    GlobalEventHistoryGap,
    JobManager,
    JobSnapshot,
    JobState,
    _parse_markdown_sections,
    build_signed_webhook_sender,
    request_from_manifest,
)
from app.schemas import (
    BatchJobItem,
    BatchJobRequest,
    BatchJobResponse,
    CrawlRequest,
    CrawlResponse,
    CrawlStatusResponse,
    CrawlUrlResult,
    EmbeddingSearchRequest,
    EmbeddingTextRequest,
    EmbeddingTextResponse,
    EmbeddingSearchResponse,
    JobCreateRequest,
    JobDiffRequest,
    JobDiffResponse,
    JobDiffSection,
    JobListItem,
    JobListResponse,
    JobRerunResponse,
    JobSearchHit,
    JobSearchRequest,
    JobSearchResponse,
    JobSnapshotResponse,
    JobTagMultiRequest,
    ReplayRequest,
    SectionEmbeddingMatch,
    Slosummary,
    StructuredResult,
    JobTagRequest,
    JobSharePublicResponse,
    JobShareResponse,
    JobTocResponse,
    TocEntry,
    JobSummaryResponse,
    JobSearchTextRequest,
    EmbeddingBatchRequest,
    EmbeddingBatchItem,
    EmbeddingBatchResponse,
    JobCountsResponse,
    EmbedderStatsResponse,
    JobReplayRequest,
    JobReplayResponse,
    JobDeleteResponse,
    JobSectionsResponse,
    ExtractedLink,
    ExtractedTable,
    ExtractedCodeBlock,
    ExtractResponse,
    LinkLookupResponse,
    AdminStatsResponse,
    AdminPruneRequest,
    AdminPruneResponse,
    AdminCacheClearResponse,
    JobTagResponse,
    EmbeddingStoreRequest,
    EmbeddingStoreResponse,
    BatchStatusItem,
    BatchStatusRequest,
    BatchStatusResponse,
    JobArtifactsResponse,
    JobEventsJsonResponse,
    HeadingBodyResponse,
    FollowResponse,
    ExportMdResponse,
    BatchQueryRequest,
    BatchQueryResponse,
    CacheMetricsResponse,
    QueueMetricsResponse,
    JobLinksResponse,
    WebhookRegistrationRequest,
    WebhookSubscription,
    WebhookDeleteRequest,
)
from app.settings import settings
from app.store import RunPaths, build_store
from app.warning_log import summarize_dom_assists

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
    # Start the job watchdog to monitor for stuck jobs
    JOB_MANAGER.start_watchdog()
    yield
    # Gracefully stop the watchdog and cancel any in-flight tasks on shutdown
    if hasattr(JOB_MANAGER, "shutdown"):
        await JOB_MANAGER.shutdown()
    else:
        await JOB_MANAGER.stop_watchdog()
    active_store = _active_store()
    if hasattr(active_store, "aclose"):
        try:
            await active_store.aclose()
        except Exception:
            pass


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
_DEFAULT_STORE = store
JOB_MANAGER.store = store
_crawler = get_crawler(store=store)
_crawl_owners: dict[str, str | None] = {}
_EMBEDDER_SEMAPHORE = asyncio.Semaphore(8)


def _active_store():
    if store is not _DEFAULT_STORE:
        return store
    jm_store = getattr(JOB_MANAGER, "store", None)
    if jm_store is not None and jm_store is not _DEFAULT_STORE:
        return jm_store
    return store


async def _async_read_markdown(active_store: Any, job_id: str) -> str:
    if hasattr(active_store, "aread_markdown"):
        return await active_store.aread_markdown(job_id)
    return await asyncio.to_thread(active_store.read_markdown, job_id)


async def _async_read_manifest(active_store: Any, job_id: str) -> dict[str, Any]:
    if hasattr(active_store, "aread_manifest"):
        return await active_store.aread_manifest(job_id)
    return await asyncio.to_thread(active_store.read_manifest, job_id)


async def _async_read_links(active_store: Any, job_id: str) -> Any:
    if hasattr(active_store, "aread_links"):
        return await active_store.aread_links(job_id)
    return await asyncio.to_thread(active_store.read_links, job_id)


async def _async_fetch_run(active_store: Any, job_id: str) -> Any:
    if hasattr(active_store, "afetch_run"):
        return await active_store.afetch_run(job_id)
    return await asyncio.to_thread(active_store.fetch_run, job_id)


async def _async_get_snapshot(job_id: str) -> JobSnapshot | None:
    return await asyncio.to_thread(JOB_MANAGER.get_snapshot, job_id)


async def _async_get_structured(job_id: str) -> dict[str, Any]:
    return await asyncio.to_thread(JOB_MANAGER.get_structured_result, job_id)


async def _async_list_jobs(**kwargs: Any) -> tuple[list[dict[str, Any]], int]:
    def _call() -> tuple[list[dict[str, Any]], int]:
        try:
            return JOB_MANAGER.list_jobs(**kwargs)
        except TypeError:
            fallback_kwargs = dict(kwargs)
            fallback_kwargs.pop("owner", None)
            fallback_kwargs.pop("enforce_owner", None)
            return JOB_MANAGER.list_jobs(**fallback_kwargs)

    return await asyncio.to_thread(_call)


def _normalize_links_anchors(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, Mapping):
        anchors = payload.get("anchors") or payload.get("links") or []
    elif isinstance(payload, (list, tuple)):
        anchors = payload
    else:
        return []
    return [dict(a) for a in anchors if isinstance(a, Mapping)]

_SESSION_COOKIE = "mdwb_session"
_PUBLIC_PATHS = {"/", "/browser", "/health", "/health/live", "/health/ready", "/auth/config"}
_JOB_COLLECTIONS = {"batch", "crawl", "search", "stream", "demo"}


@app.exception_handler(EventPersistenceError)
async def event_journal_unavailable(request: Request, exc: EventPersistenceError) -> JSONResponse:
    return JSONResponse(status_code=503, content={"detail": {"code": "event-journal-unavailable"}})


@app.middleware("http")
async def access_control(request: Request, call_next):
    """Authenticate before route admission and confine all job surfaces to owners."""
    path = request.url.path
    public = (
        path in _PUBLIC_PATHS
        or path.startswith("/static/")
        or bool(re.fullmatch(r"/jobs/share/[^/]+", path))
    )
    if public or (path == "/auth/session" and request.method == "DELETE"):
        response = await call_next(request)
        if path.startswith("/jobs/share/"):
            response.headers["Cache-Control"] = "no-store"
        return response
    key = request.headers.get(settings.API_KEY_HEADER)
    authorization = request.headers.get("Authorization", "")
    if key is None and authorization.startswith("Bearer "):
        key = authorization[7:]
    cookie_auth = settings.REQUIRE_API_KEY and key is None and _SESSION_COOKIE in request.cookies
    if cookie_auth:
        key = request.cookies[_SESSION_COOKIE]
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = str(request.url.replace(path="", query="")).rstrip("/")
            if request.headers.get("Origin") != origin:
                return JSONResponse({"detail": "Same-origin request required"}, status_code=403)
    try:
        context = await asyncio.to_thread(authenticate, key, store, settings)
    except HTTPException as exc:
        return JSONResponse(
            {"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers
        )
    request.state.auth_context = context
    token = AUTH_CONTEXT.set(context)
    try:
        if context.owner_key is not None:
            if path.startswith("/admin/") and not context.is_admin:
                return JSONResponse({"detail": "Administrator key required"}, status_code=403)
            match = re.match(r"^/jobs/([^/]+)", path)
            resource = None
            if match and match.group(1) not in _JOB_COLLECTIONS and match.group(1) != "share":
                resource = f"job:{match.group(1)}"
            if path.startswith("/crawl/"):
                resource = f"crawl:{path.split('/')[2]}"
            if resource is not None and not await asyncio.to_thread(
                owns_resource,
                store,
                resource,
                context.owner_key,
            ):
                return JSONResponse({"detail": "Resource not found"}, status_code=404)
            # Demo routes expose fixtures, never a tenant's artifacts.
            allowed, quota = await asyncio.to_thread(
                consume_shared,
                store.engine,
                f"api_key:{context.api_key_id}",
                context.rate_limit or settings.RATE_LIMIT_PER_MINUTE,
            )
            headers = {
                "Cache-Control": "private, no-store",
                "X-RateLimit-Limit": str(quota["limit"]),
                "X-RateLimit-Remaining": str(quota["remaining"]),
                "X-RateLimit-Reset": str(quota["reset"]),
            }
            if not allowed:
                headers["Retry-After"] = str(quota["retry_after"])
                return JSONResponse(
                    {"detail": "Rate limit exceeded"}, status_code=429, headers=headers
                )
        else:
            headers = {}
        response = await call_next(request)
        response.headers.update(headers)
        return response
    finally:
        AUTH_CONTEXT.reset(token)


@app.get("/auth/config")
async def auth_config() -> dict[str, Any]:
    return {"required": settings.REQUIRE_API_KEY, "header": settings.API_KEY_HEADER}


@app.post("/auth/session", status_code=204)
async def browser_session(request: Request):
    # The admission middleware has verified the supplied header credential.
    if not settings.REQUIRE_API_KEY:
        raise HTTPException(status_code=400, detail="API key authentication is disabled")
    key = request.headers.get(settings.API_KEY_HEADER)
    if key is None:
        raise HTTPException(status_code=401, detail="Provide the configured API key header")
    response = PlainTextResponse("", status_code=204)
    response.set_cookie(
        _SESSION_COOKIE,
        key,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="strict",
        path="/",
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@app.delete("/auth/session", status_code=204)
async def browser_logout():
    response = PlainTextResponse("", status_code=204)
    response.delete_cookie(_SESSION_COOKIE, path="/")
    return response


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


def _link_anchors(payload: Any) -> list[dict[str, Any]]:
    """Consume the persisted flat links list and structured DOM payloads."""
    values = payload.get("anchors", []) if isinstance(payload, dict) else payload
    if not isinstance(values, list):
        return []
    return [entry for entry in values if isinstance(entry, dict)]



def get_request_owner(request: Request | None = None) -> str | None:
    """Authenticated journal owner. Callers keep the name; identity is current_owner()."""
    del request
    return current_owner()


def ensure_owner_access(request: Request, resource_owner: str | None) -> None:
    """404 when the authenticated owner does not own the resource."""
    del request
    owner = current_owner()
    if owner is None:
        return
    if resource_owner != owner:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resource not found")


def check_job_owner_access(request: Request, job_id: str) -> None:
    """404 unless the authenticated owner holds this job resource."""
    del request
    owner = current_owner()
    if owner is None or not job_id:
        return
    if not owns_resource(store, f"job:{job_id}", owner):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resource not found")


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


@app.get("/browser", response_class=HTMLResponse)
async def browser() -> str:
    """Serve the browser-like UI for navigating captured pages."""

    return (WEB_ROOT / "browser.html").read_text(encoding="utf-8")


@app.get("/health", tags=["health"])
async def healthcheck() -> dict[str, str]:
    """Return a simple status useful for smoke tests."""

    return {"status": "ok"}


@app.get("/health/beads", tags=["health"])
async def health_beads() -> dict:
    """Return the latest bead-health snapshot (from scripts/bead_health.py)."""
    import importlib
    import sys
    from pathlib import Path as _P

    # Ensure the project root is importable so ``import scripts`` works regardless
    # of the test harness's cwd (e.g. when tmp_path fixtures chdir).
    project_root = str(_P(__file__).resolve().parent.parent)
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    try:
        bead_health = importlib.import_module("scripts.bead_health")
    except Exception as exc:
        return {"status": "error", "detail": f"bead_health module unavailable: {exc}"}

    try:
        # Prefer ops/bead_health.jsonl (committed by ops tooling); fall back to a fresh snapshot.
        candidate = _P("ops/bead_health.jsonl")
        if candidate.exists():
            data = bead_health.read_latest(candidate)
            if data.get("status") != "no-data":
                return data
        return bead_health.snapshot()
    except Exception as exc:  # pragma: no cover - best-effort
        return {"status": "error", "detail": str(exc)}


@app.get("/embedders", tags=["embed"])
async def list_embedders_endpoint() -> dict[str, Any]:
    """List available embedders + which one is the default."""
    from app.embedders import list_embedders

    return {"embedders": list_embedders(), "default": "hash-bucket-v1"}


@app.get("/schema", tags=["observability"])
async def schema_discovery() -> dict[str, Any]:
    """Return a compact, agent-oriented schema discovery payload.

    Unlike the full FastAPI /openapi.json (which is for HTTP clients that
    understand Swagger), this endpoint is optimized for LLM agents:

    - Groups endpoints by their job lifecycle (capture / live / artifacts /
      embed / control / observability).
    - For every endpoint, lists its HTTP method, path, request/response
      schemas (field names + types), and a one-line intent.
    - Includes invariants an agent must respect (cache_key composition,
      supported OCR models, content limits).

    Replaces the need for an LLM to read the entire FastAPI source.
    """
    return {
        "version": "1.0",
        "service": "markdown-web-browser",
        "intent": "Render any URL to clean Markdown via tiled screenshots + olmOCR.",
        "invariants": {
            "cache_key": "url + CfT + viewport + DSF + OCR model + profile_id (content-addressed)",
            "ocr_models_default": "olmOCR-2-7B-1025-FP8 (hosted). Local: any OpenAI-compatible vLLM/SGLang server.",
            "determinism": "Chrome for Testing pinned, reduced motion, animations frozen.",
            "long_side_px": 1288,
            "viewport_overlap_px": 120,
            "embedding_dim": 1536,
            "rate_limit_429": "Honor Retry-After; back off 2-4 seconds.",
        },
        "endpoints": {
            "capture": {
                "POST /jobs": "Submit a single URL. Returns 202 + JobSnapshot.",
                "POST /jobs/batch": "Submit up to 200 URLs in one request. Returns 202 + BatchJobResponse with per-URL cache_hit and job_id.",
                "POST /jobs/crawl": "Kick off a depth-1 crawl that reuses the capture pipeline.",
                "POST /replay": "Replay a stored manifest by enqueueing a new capture.",
            },
            "live": {
                "GET /jobs": "List jobs with filters (state, profile_id, url_contains, cache_hit, tag, limit, offset).",
                "GET /jobs/{id}": "Fetch latest job snapshot. Returns JobSnapshotResponse.",
                "GET /jobs/{id}/stream": "SSE: live state + manifest progress events.",
                "GET /jobs/{id}/events": "NDJSON: structured event log; reconnect via ?after=sequence. A missing history range returns 409; ?since=ISO filters timestamps.",
                "GET /crawl/{id}": "Live crawl status with per-URL outcomes.",
            },
            "artifacts": {
                "GET /jobs/{id}/links.json": "DOM-harvested anchors/forms/headings.",
                "GET /jobs/{id}/manifest.json": "Full CfT + timings + OCR telemetry + autotune + warnings.",
                "GET /jobs/{id}/result.md": "Final Markdown output (raw text/markdown).",
                "GET /jobs/{id}/result.json": "Structured sections (heading + body) + normalized links. Use this when you want a TOC without re-parsing Markdown.",
                "GET /jobs/{id}/artifact/highlight?tile=...&y0=...&y1=...": "HTML viewer for a tile region referenced by a provenance comment.",
                "GET /jobs/{id}/artifact/{path}": "Read any artifact (tiles, links.json, dom_snapshot.html, manifest.json).",
            },
            "embed": {
                "POST /jobs/{id}/embeddings/search": "Search sqlite-vec section embeddings. Body: {vector: [..], top_k: 5}.",
            },
            "control": {
                "POST /jobs/{id}/webhooks": "Register a webhook callback. Body: {url, events: ['DONE','FAILED']}.",
                "GET /jobs/{id}/webhooks": "List active webhook subscriptions.",
                "DELETE /jobs/{id}/webhooks": "Remove a webhook (by id or url).",
            },
            "observability": {
                "GET /health": 'Health check (returns {"status": "ok"}).',
                "GET /metrics": "Prometheus scrape endpoint (text).",
                "GET /metrics/slo": "JSON: latest capture/OCR SLO rollup with per-category p50/p95 + budget breaches.",
                "GET /schema": "This endpoint. Optimized for LLM agents.",
                "GET /openapi.json": "Standard FastAPI OpenAPI 3 schema (for code-gen clients).",
            },
        },
        "cli": {
            "mdwb fetch <url> [--watch] [--reuse-cache] [--semantic-post]": "Submit + optionally stream a capture.",
            "mdwb crawl <seed_url> [--watch] [--max-pages] [--domain-allowlist]": "Depth-1 crawl.",
            "mdwb show <job_id> [--ocr-metrics]": "Dump latest snapshot (table or JSON).",
            "mdwb stream <job_id>": "Tail SSE feed.",
            "mdwb events <job_id>": "Tail NDJSON event log.",
            "mdwb watch <job_id>": "Live progress overlay.",
            "mdwb diag <job_id>": "CfT/Playwright/timings triage.",
            "mdwb dom links --job-id <id>": "Render stored links.json.",
            "mdwb replay manifest <manifest.json>": "Resubmit a stored manifest.",
            "mdwb jobs ocr-metrics <job_id>": "OCR batch telemetry.",
            "mdwb jobs embeddings search <job_id>": "sqlite-vec section search.",
            "mdwb jobs bundle <job_id> --out file.tar.zst": "Tar+compress job artifacts.",
            "mdwb jobs artifacts manifest <job_id>": "List artifact paths.",
            "mdwb jobs agents bead-summary <plan.md>": "Convert checklist to bead summaries.",
            "mdwb warnings --count N": "Tail ops/warnings.jsonl.",
            "mdwb slo --json": "Latest capture/OCR SLO rollup.",
            "mdwb discover --json": "Static catalog of every endpoint + command (use this for offline discovery).",
            "mdwb demo stream|snapshot|events": "Exercise demo endpoints (no live pipeline).",
            "mdwb resume status --root PATH": "Inspect resume state.",
        },
        "agent_tips": [
            "Always pass reuse_cache=true on retry; the cache_key is content-addressed and stable.",
            "Prefer POST /jobs/batch for >2 URLs to amortize connection overhead.",
            "Prefer GET /jobs/{id}/result.json over /result.md when you need a TOC or outbound link list.",
            "GET /schema gives a 5-KB intent map; GET /openapi.json gives the full type schema (~50 KB).",
            "Webhooks (DONE/FAILED) beat polling for long captures.",
            "Embeddings are 1536-dim float32; send raw vectors in the search body, not text.",
            "For deep research, use mdwb crawl --max-pages 10 + --domain-allowlist to scope.",
        ],
    }


def _compute_live_slo_summary() -> dict[str, Any]:
    """Compute a capture/OCR SLO rollup from the latest manifest index.

    Falls back to an empty rollup when no manifests have been recorded yet, so
    the endpoint is always safe to call (operators use this for dashboards and
    pager health checks).
    """
    import json as _json
    from scripts.compute_slo import (
        compute_slo_summary,
        load_budgets,
    )

    summary: dict[str, Any] = {
        "status": "no-data",
        "categories": {},
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    candidates = [
        Path("benchmarks/production/latest_manifest_index.json"),
        Path("benchmarks/production/weekly_summary.json"),
    ]
    entries: list[dict[str, Any]] = []
    for candidate in candidates:
        if not candidate.exists():
            continue
        try:
            payload = _json.loads(candidate.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(payload, list):
            entries = payload
            break
        if isinstance(payload, dict):
            for key in ("entries", "manifests", "items"):
                if isinstance(payload.get(key), list):
                    entries = payload[key]
                    break
            else:
                entries = [payload]
            break
    if entries:
        budgets = load_budgets(None)
        rollup = compute_slo_summary(entries, budget_map=budgets)
        summary = {
            "status": "ok",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "categories": rollup,
            "entry_count": len(entries),
        }
    return summary


@app.get("/metrics/slo", tags=["observability"])
async def metrics_slo() -> dict[str, Any]:
    """Return the latest capture/OCR SLO rollup as JSON.

    Reads the most recent manifest index (production smoke or weekly summary)
    and runs ``compute_slo_summary`` in-process so dashboards and pager checks
    can hit a single stable endpoint instead of running the CLI separately.
    """

    return _compute_live_slo_summary()


@app.get("/jobs/demo")
async def demo_job_snapshot() -> dict:
    """Return a deterministic demo job snapshot."""

    return _demo_snapshot()


async def _call_create_job(
    job_request: JobCreateRequest,
    *,
    tags: list[str] | None = None,
    owner: str | None = None,
) -> JobSnapshot:
    kwargs: dict[str, Any] = {}
    if tags is not None:
        kwargs["tags"] = tags
    if owner is not None:
        kwargs["owner"] = owner
    try:
        return await JOB_MANAGER.create_job(job_request, **kwargs)
    except TypeError:
        kwargs.pop("owner", None)
        try:
            return await JOB_MANAGER.create_job(job_request, **kwargs)
        except TypeError:
            return await JOB_MANAGER.create_job(job_request)


@app.post("/jobs", response_model=JobSnapshotResponse, status_code=status.HTTP_202_ACCEPTED)
async def create_job(request: JobCreateRequest) -> JobSnapshotResponse:
    try:
        snapshot = await JOB_MANAGER.create_job(request)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _snapshot_to_response(snapshot)


@app.get("/jobs", response_model=JobListResponse)
async def list_jobs(
    http_request: Request,
    state: str | None = None,
    profile_id: str | None = None,
    url_contains: str | None = None,
    cache_hit: bool | None = None,
    tag: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> JobListResponse:
    """List jobs with filters for agent inspection.

    Filters are AND-combined. Tags are stored on RunRecord.metadata; jobs
    submitted via /jobs/batch carry the request's tags automatically so an
    agent can scope a follow-up query (e.g. all jobs in dataset:2026-q1).
    """
    owner = get_request_owner(http_request)
    items, total = await _async_list_jobs(
        state=state,
        profile_id=profile_id,
        owner=owner,
        enforce_owner=True,
        url_contains=url_contains,
        cache_hit=cache_hit,
        tag=tag,
        limit=limit,
        offset=offset,
    )
    applied = {
        k: v
        for k, v in {
            "state": state,
            "profile_id": profile_id,
            "url_contains": url_contains,
            "cache_hit": str(cache_hit) if cache_hit is not None else None,
            "tag": tag,
            "limit": str(limit),
            "offset": str(offset),
        }.items()
        if v is not None
    }
    return JobListResponse(
        items=[JobListItem(**i) for i in items],
        total=total,
        filtered=len(items),
        filters=applied,
    )


@app.post("/jobs/batch", response_model=BatchJobResponse, status_code=status.HTTP_202_ACCEPTED)
async def batch_create_jobs(request: BatchJobRequest, http_request: Request) -> BatchJobResponse:
    """Submit many URLs in one round-trip; each becomes its own job.

    Agents use this for "process this list of pages" workflows so they don't
    pay N×RTT overhead. The response is a list of per-URL outcomes mirroring
    the single-URL POST /jobs contract (id, state, cache_hit, etc.).
    """
    owner = get_request_owner(http_request)
    items: list[BatchJobItem] = []
    cache_hits = 0
    queued = 0
    failed = 0
    for url in request.urls:
        try:
            job_request = JobCreateRequest(
                url=url,
                profile_id=request.profile_id,
                reuse_cache=request.reuse_cache,
                ocr_policy=request.ocr_policy,
                ocr=request.ocr,
                viewport=request.viewport,
            )
            snapshot = await _call_create_job(job_request, tags=request.tags, owner=owner)
            cache_hit_flag = bool(snapshot.get("cache_hit"))
            if cache_hit_flag:
                cache_hits += 1
            else:
                queued += 1
            items.append(
                BatchJobItem(
                    url=url,
                    job_id=str(snapshot.get("job_id") or snapshot.get("id")),
                    state=str(snapshot.get("state", "PENDING")),
                    cache_hit=cache_hit_flag,
                )
            )
        except Exception as exc:  # pragma: no cover - defensive
            failed += 1
            items.append(BatchJobItem(url=url, error=str(exc)))
    return BatchJobResponse(
        submitted=len(request.urls),
        cache_hits=cache_hits,
        queued=queued,
        failed=failed,
        items=items,
    )


@app.post("/jobs/crawl", response_model=CrawlResponse, status_code=status.HTTP_202_ACCEPTED)
async def start_crawl(request: CrawlRequest, http_request: Request) -> CrawlResponse:
    """Kick off a multi-URL crawl that reuses the capture pipeline."""
    owner = get_request_owner(http_request)
    config = CrawlConfig(
        seed_url=str(request.url),
        max_pages=request.max_pages,
        max_depth=request.max_depth,
        domain_allowlist=list(request.domain_allowlist),
        respect_robots_txt=request.respect_robots_txt,
        crawl_delay_ms=request.crawl_delay_ms,
    )

    async def _capture(url: str) -> str:
        return await submit_and_wait_for_job(
            JOB_MANAGER,
            url=url,
            profile_id=request.profile_id,
            ocr_policy=request.ocr_policy,
            reuse_cache=request.reuse_cache,
            owner=owner,
        )

    crawl_id = await _crawler.start_crawl(config, capture_fn=_capture)
    claim_resource(store, f"crawl:{crawl_id}")
    import asyncio

    await asyncio.sleep(0)
    snapshot = _crawler.get_crawl_status(crawl_id) or {}
    return CrawlResponse(
        crawl_id=crawl_id,
        seed_url=config.seed_url,
        status=snapshot.get("status", "running"),
        started_at=snapshot.get("started_at", ""),
        child_job_ids=[r.get("job_id") for r in snapshot.get("results", []) if r.get("job_id")],
        queued_urls=list(snapshot.get("queued_urls") or [config.seed_url]),
    )


@app.get("/crawl/{crawl_id}", response_model=CrawlStatusResponse)
async def get_crawl_status(crawl_id: str, http_request: Request) -> CrawlStatusResponse:
    snapshot = _crawler.get_crawl_status(crawl_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="Crawl not found")
    if crawl_id in _crawl_owners:
        ensure_owner_access(http_request, _crawl_owners.get(crawl_id))
    return CrawlStatusResponse(
        crawl_id=snapshot["crawl_id"],
        seed_url=snapshot["seed_url"],
        status=snapshot["status"],
        started_at=snapshot["started_at"],
        finished_at=snapshot["finished_at"],
        max_pages=snapshot["max_pages"],
        max_depth=snapshot["max_depth"],
        visited=snapshot["visited"],
        completed=snapshot["completed"],
        failed=snapshot["failed"],
        pending=snapshot["pending"],
        queued_urls=snapshot.get("queued_urls", []),
        results=[CrawlUrlResult(**r) for r in snapshot.get("results", [])],
    )


@app.post(
    "/jobs/{job_id}/rerun", response_model=JobRerunResponse, status_code=status.HTTP_202_ACCEPTED
)
async def rerun_job(
    job_id: str,
    http_request: Request,
    reuse_cache: bool = False,
) -> JobRerunResponse:
    """Re-capture the same URL with the same profile/OCR/tags.

    A new job_id is created; the original is unchanged. Tags persist so a
    follow-up GET /jobs?tag=X query can find both. Use this instead of
    re-posting the URL when the agent only knows the existing job_id.
    """
    owner = get_request_owner(http_request)
    snapshot = await _async_get_snapshot(job_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="Job not found")
    url = str(snapshot.get("url") or "")
    if not url:
        raise HTTPException(status_code=400, detail="Original job has no URL; cannot rerun")
    profile_id = snapshot.get("profile_id")
    tags = list(snapshot.get("tags") or [])
    manifest = snapshot.get("manifest") if isinstance(snapshot.get("manifest"), Mapping) else {}
    try:
        new_request = request_from_manifest({**manifest, "url": url, "profile_id": profile_id})
        new_request.reuse_cache = reuse_cache
        new_snapshot = await JOB_MANAGER.create_job(new_request, tags=tags)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    new_id = str(new_snapshot.get("job_id") or new_snapshot.get("id") or "")
    return JobRerunResponse(
        original_job_id=job_id,
        new_job_id=new_id,
        url=url,
        reuse_cache=reuse_cache,
    )


@app.post("/jobs/{job_id}/diff", response_model=JobDiffResponse)
async def diff_jobs(job_id: str, request: JobDiffRequest, http_request: Request) -> JobDiffResponse:
    """Compare two captures section-by-section.

    Both jobs are parsed into the same heading hierarchy, then a heading-level
    diff is produced. Use this for "did this page change since yesterday?"
    agent workflows.
    """
    await asyncio.to_thread(check_job_owner_access, http_request, request.other_job_id)
    snap_a = await _async_get_snapshot(job_id)
    if snap_a is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    snap_b = await _async_get_snapshot(request.other_job_id)
    if snap_b is None:
        raise HTTPException(status_code=404, detail=f"Job {request.other_job_id} not found")
    active_store = _active_store()
    md_a = ""
    md_b = ""
    try:
        md_a = await _async_read_markdown(active_store, job_id)
    except Exception:
        md_a = ""
    try:
        md_b = await _async_read_markdown(active_store, request.other_job_id)
    except Exception:
        md_b = ""
    sections_a = {s["anchor"]: s for s in _parse_markdown_sections(md_a)}
    sections_b = {s["anchor"]: s for s in _parse_markdown_sections(md_b)}
    diffs: list[JobDiffSection] = []
    for anchor_key in set(sections_a) | set(sections_b):
        a = sections_a.get(anchor_key)
        b = sections_b.get(anchor_key)
        if a is None:
            assert b is not None  # anchor_key belongs to the union of the two maps
            diffs.append(
                JobDiffSection(
                    heading=b["heading"],
                    anchor=anchor_key,
                    state="added",
                    a_chars=0,
                    b_chars=len(b["body"]),
                    a_excerpt="",
                    b_excerpt=b["body"][: request.max_chars_per_section],
                )
            )
        elif b is None:
            diffs.append(
                JobDiffSection(
                    heading=a["heading"],
                    anchor=anchor_key,
                    state="removed",
                    a_chars=len(a["body"]),
                    b_chars=0,
                    a_excerpt=a["body"][: request.max_chars_per_section],
                    b_excerpt="",
                )
            )
        else:
            state = "changed" if a["body"] != b["body"] else "unchanged"
            diffs.append(
                JobDiffSection(
                    heading=a["heading"],
                    anchor=anchor_key,
                    state=state,
                    a_chars=len(a["body"]),
                    b_chars=len(b["body"]),
                    a_excerpt=a["body"][: request.max_chars_per_section],
                    b_excerpt=b["body"][: request.max_chars_per_section],
                )
            )
    links_a: set[str] = set()
    links_b: set[str] = set()
    if request.include_links:
        active_store = _active_store()
        try:
            payload = await _async_read_links(active_store, job_id)
            anchors = _normalize_links_anchors(payload)
            links_a = {str(a.get("href")) for a in anchors if a.get("href")}
        except Exception:
            pass
        try:
            payload = await _async_read_links(active_store, request.other_job_id)
            anchors = _normalize_links_anchors(payload)
            links_b = {str(a.get("href")) for a in anchors if a.get("href")}
        except Exception:
            pass
    return JobDiffResponse(
        a_job_id=job_id,
        b_job_id=request.other_job_id,
        a_word_count=len(md_a.split()) if md_a else 0,
        b_word_count=len(md_b.split()) if md_b else 0,
        a_url=str(snap_a.get("url") or ""),
        b_url=str(snap_b.get("url") or ""),
        sections=diffs,
        links_a_only=sorted(links_a - links_b),
        links_b_only=sorted(links_b - links_a),
        links_common=sorted(links_a & links_b),
    )


@app.post("/jobs/search", response_model=JobSearchResponse)
async def search_jobs(
    request: JobSearchRequest,
    http_request: Request,
) -> JobSearchResponse:
    """Full-text search across all stored job Markdown.

    Cheap O(n) over disk; the agent wins because it doesn't need to load any
    model or service. Quoted phrases match literally; otherwise terms are
    AND-combined (case-insensitive substring).
    """
    import re as _re

    raw = request.query.strip()
    quoted = _re.findall(r"\"([^\"]+)\"", raw)
    rest = _re.sub(r"\"[^\"]+\"", " ", raw).split()
    terms = [t.lower() for t in rest if t]
    quoted = [q.lower() for q in quoted]
    if not terms and not quoted:
        return JobSearchResponse(query=raw, matches=[], total_scanned=0, returned=0)

    owner = get_request_owner(http_request)
    # Pre-filter: only scan runs matching filters. Use the store listing.
    rows, _ = await _async_list_jobs(
        state=request.state,
        owner=owner,
        enforce_owner=True,
        url_contains=request.url_contains,
        tag=request.tag,
        limit=500,  # hard cap so a misconfigured query doesn't walk the whole DB
    )
    active_store = _active_store()
    matches: list[JobSearchHit] = []
    total_scanned = 0
    for row in rows:
        rid = row["id"]
        total_scanned += 1
        try:
            md = await _async_read_markdown(active_store, rid)
        except Exception:
            continue
        if not md:
            continue
        md_lower = md.lower()
        # ALL quoted phrases must match; ALL terms must match
        if quoted and not all(q in md_lower for q in quoted):
            continue
        if terms and not all(t in md_lower for t in terms):
            continue
        # Find first matching line for snippet
        line_number = 0
        matched_line = ""
        score = 0.0
        for ln, line in enumerate(md.splitlines(), start=1):
            line_lower = line.lower()
            line_hits = sum(1 for t in terms + quoted if t in line_lower)
            if line_hits == 0:
                continue
            score = max(score, line_hits / max(1, len(line.split())))
            if not matched_line:
                matched_line = line
                line_number = ln
        if score == 0:
            score = float(len(terms) + len(quoted))  # phrase-only match fallback
            for ln, line in enumerate(md.splitlines(), start=1):
                if any(q in line.lower() for q in quoted):
                    matched_line = line
                    line_number = ln
                    break
        matches.append(
            JobSearchHit(
                job_id=rid,
                url=row.get("url", ""),
                state=row.get("state", "UNKNOWN"),
                matched_line=matched_line[:300],
                line_number=line_number,
                score=score,
            )
        )
    # Sort by score desc, then job_id asc for stable order
    matches.sort(key=lambda h: (-h.score, h.job_id))
    return JobSearchResponse(
        query=raw,
        matches=matches[: request.limit],
        total_scanned=total_scanned,
        returned=min(len(matches), request.limit),
    )


@app.post("/embeddings/text", response_model=EmbeddingTextResponse)
async def embed_text(request: EmbeddingTextRequest) -> EmbeddingTextResponse:
    """Compute a vector for ``request.text`` using the named embedder.

    Supported embedders (see :func:`app.embedders.list_embedders`):
    - ``hash-bucket-v1`` (default): deterministic 1536-dim projection, no model weights
    - ``openai-compatible``: proxies to any OpenAI-compatible /v1/embeddings endpoint
      (reads ``OPENAI_BASE_URL`` / ``OPENAI_API_KEY`` / ``OPENAI_EMBEDDING_MODEL``)
    - ``sentence-transformers``: local model, requires the optional ``local-ocr`` extras
    """
    from app.embedders import get_embedder, list_embedders

    known = list_embedders()
    if request.model not in known:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown embedder {request.model!r}; known: {known}",
        )
    try:
        embedder = get_embedder(request.model)
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"embedder {request.model!r} unavailable: {exc}",
        ) from exc
    try:
        async with _EMBEDDER_SEMAPHORE:
            vector = await asyncio.to_thread(embedder.embed, request.text)
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"embedder {request.model!r} failed: {exc}",
        ) from exc
    return EmbeddingTextResponse(
        model=request.model,
        dim=len(vector),
        vector=vector,
        text_chars=len(request.text),
    )


@app.get("/metrics/slo.json", response_model=dict)
async def metrics_slo_json() -> dict:
    """JSON variant of /metrics/slo (identical content, .json suffix for clarity).

    Some agents prefer explicit .json extensions; this route exists purely
    for that convention.
    """

    return _compute_live_slo_summary()


@app.get("/schema.json", response_model=dict)
async def schema_json() -> dict:
    """Machine-readable version of /schema.

    Identical payload; the .json suffix matches the convention many
    code-gen clients use to discover typed specs.
    """

    return await schema_discovery()


@app.get("/jobs/stream")
async def jobs_global_stream(request: Request, after: str | None = None) -> StreamingResponse:
    """Stream this owner's job events with the per-job sequence contract."""
    after = request.headers.get("last-event-id") or after
    await JOB_MANAGER.restore_global_history()
    try:
        backlog, queue, cursor = JOB_MANAGER.subscribe_global_events(after=after)
    except GlobalEventHistoryGap as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    async def event_generator() -> AsyncIterator[str]:
        last_cursor = cursor
        try:
            # Establish the tail before live delivery, even for an empty owner.
            # Replays commit their own cursors below, without skipping backlog.
            if not backlog:
                yield f'id: {cursor}\ndata: {{"event":"subscribed"}}\n\n'
            for entry in backlog:
                last_cursor = entry["cursor"]
                yield f"id: {last_cursor}\ndata: {_serialize_log_entry(entry)}\n\n"
            while not await request.is_disconnected():
                try:
                    JOB_MANAGER.check_event_journal()
                except EventPersistenceError:
                    yield 'event: error\ndata: {"code":"event-journal-unavailable"}\n\n'
                    break
                try:
                    entry = await asyncio.wait_for(queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    try:
                        await JOB_MANAGER.restore_global_history()
                        missed = JOB_MANAGER.get_global_events(after=last_cursor)
                    except GlobalEventHistoryGap as exc:
                        yield f"event: gap\ndata: {json.dumps(exc.detail)}\n\n"
                        break
                    except EventPersistenceError:
                        yield 'event: error\ndata: {"code":"event-journal-unavailable"}\n\n'
                        break
                    for event in missed:
                        last_cursor = event["cursor"]
                        yield f"id: {last_cursor}\ndata: {_serialize_log_entry(event)}\n\n"
                    if not missed:
                        yield ": heartbeat\n\n"
                    continue
                sequence = int(entry["cursor"].split(":")[1])
                previous = int(last_cursor.split(":")[1])
                if sequence <= previous:
                    continue
                if sequence > previous + 1:
                    try:
                        missed = JOB_MANAGER.get_global_events(after=last_cursor)
                    except GlobalEventHistoryGap as exc:
                        yield f"event: gap\ndata: {json.dumps(exc.detail)}\n\n"
                        break
                else:
                    missed = [entry]
                for event in missed:
                    last_cursor = event["cursor"]
                    yield f"id: {last_cursor}\ndata: {_serialize_log_entry(event)}\n\n"
        finally:
            JOB_MANAGER.unsubscribe_all(queue)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.get("/jobs/{job_id}", response_model=JobSnapshotResponse)
async def fetch_job(job_id: str) -> JobSnapshotResponse:
    await JOB_MANAGER.restore_job_history(job_id)
    try:
        snapshot = await _async_get_snapshot(job_id)
    except KeyError as exc:  # pragma: no cover - runtime only
        raise HTTPException(status_code=404, detail="Job not found") from exc
    if snapshot is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return _snapshot_to_response(snapshot)


@app.post("/replay", response_model=JobSnapshotResponse, status_code=status.HTTP_202_ACCEPTED)
async def replay_job(request: ReplayRequest, http_request: Request) -> JobSnapshotResponse:
    """Replay a stored manifest by enqueueing a new capture with the same URL/profile."""
    owner = get_request_owner(http_request)
    try:
        if owner is not None:
            try:
                snapshot = await JOB_MANAGER.replay_job(request.manifest, owner=owner)
            except TypeError:
                snapshot = await JOB_MANAGER.replay_job(request.manifest)
        else:
            snapshot = await JOB_MANAGER.replay_job(request.manifest)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _snapshot_to_response(snapshot)


@app.get("/jobs/{job_id}/stream")
async def job_stream(job_id: str, request: Request, after: int | None = None) -> StreamingResponse:
    header_cursor = request.headers.get("last-event-id")
    if header_cursor is not None:
        if len(header_cursor) > 20 or not re.fullmatch(r"-1|[0-9]+", header_cursor):
            raise HTTPException(status_code=400, detail="Invalid job event cursor")
        after = int(header_cursor)
    if after is not None and after < -1:
        raise HTTPException(status_code=400, detail="Event cursor must be at least -1")
    if job_id == "demo":
        queue = asyncio.Queue()
        backlog = [{"event": "snapshot", "snapshot": _demo_snapshot()}]
    else:
        await JOB_MANAGER.restore_job_history(job_id)
        try:
            backlog, queue = JOB_MANAGER.subscribe_events(job_id, min_sequence=after)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Job not found") from exc
        except EventHistoryGap as exc:
            raise HTTPException(status_code=409, detail=exc.detail) from exc
        if not backlog and after is None:
            # A pre-journal run has a current persisted result, but no replayable
            # history. Show that result without manufacturing a sequence/ID.
            backlog = [{"event": "snapshot", "snapshot": JOB_MANAGER.get_snapshot(job_id)}]

    async def event_generator() -> AsyncIterator[str]:
        try:
            async for entry in _job_event_feed(job_id, request, backlog, queue, after):
                if entry["event"] == "heartbeat":
                    yield ": heartbeat\n\n"
                    continue
                if entry["event"] in {"gap", "error"}:
                    yield f"event: {entry['event']}\ndata: {_serialize_log_entry(entry)}\n\n"
                    break
                snapshot = entry.get("snapshot", {})
                for event_name, payload in _snapshot_events(snapshot):
                    yield _sse_frame(event_name, payload)
                if entry["event"] != "snapshot":
                    payload = json.dumps(entry.get("data", {}))
                    yield _sse_frame(entry["event"], payload)
                if snapshot.get("state") == "DONE" and job_id != "demo":
                    try:
                        markdown = await asyncio.to_thread(JOB_MANAGER.store.read_markdown, job_id)
                    except (KeyError, FileNotFoundError):
                        markdown = ""
                    if markdown:
                        yield _sse_frame("raw", markdown)
                # Commit the ID only AFTER the complete HTML/raw group. A
                # disconnect halfway through safely replays the whole group.
                sequence = _extract_sequence(entry)
                if sequence is not None:
                    yield f"event: job-event\nid: {sequence}\ndata: {_serialize_log_entry(entry)}\n\n"
        finally:
            if job_id != "demo":
                JOB_MANAGER.unsubscribe_events(job_id, queue)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


async def _job_event_feed(
    job_id: str,
    request: Request,
    backlog: list[dict[str, Any]],
    queue: asyncio.Queue[dict[str, Any]],
    after: int | None,
) -> AsyncIterator[dict[str, Any]]:
    """Replay retained events and repair bounded queue drops for both transports."""
    last_sequence = _extract_sequence(backlog[-1]) if backlog else after
    for entry in backlog:
        yield entry
    heartbeat = 0
    while not await request.is_disconnected():
        try:
            JOB_MANAGER.check_event_journal()
        except EventPersistenceError:
            yield {"event": "error", "code": "event-journal-unavailable"}
            return
        try:
            entry = await asyncio.wait_for(queue.get(), timeout=1)
        except asyncio.TimeoutError:
            try:
                await JOB_MANAGER.restore_job_history(job_id)
                missed = JOB_MANAGER.get_events(job_id, min_sequence=last_sequence)
            except EventHistoryGap as exc:
                yield {"event": "gap", **exc.detail}
                return
            except EventPersistenceError:
                yield {"event": "error", "code": "event-journal-unavailable"}
                return
            except KeyError:
                yield {"event": "error", "code": "job-not-found"}
                return
            if missed:
                for event in missed:
                    last_sequence = _extract_sequence(event)
                    yield event
                heartbeat = 0
                continue
            heartbeat += 1
            metrics.increment_sse_heartbeat()
            yield {
                "event": "heartbeat",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "data": {"count": heartbeat},
            }
            continue
        heartbeat = 0
        sequence = _extract_sequence(entry)
        if sequence is not None and last_sequence is not None:
            if sequence <= last_sequence:
                continue
            if sequence > last_sequence + 1:
                try:
                    missed = JOB_MANAGER.get_events(job_id, min_sequence=last_sequence)
                except EventHistoryGap as exc:
                    yield {"event": "gap", **exc.detail}
                    return
                for event in missed:
                    last_sequence = _extract_sequence(event)
                    yield event
                continue
        if sequence is not None:
            last_sequence = sequence
        yield entry


@app.get("/jobs/{job_id}/events")
async def job_events(
    job_id: str, request: Request, since: str | None = None, after: int | None = None
) -> StreamingResponse:
    parsed_since = _parse_since(since)
    if after is not None and after < -1:
        raise HTTPException(status_code=400, detail="Event cursor must be at least -1")
    await JOB_MANAGER.restore_job_history(job_id)
    try:
        backlog, queue = JOB_MANAGER.subscribe_events(
            job_id, since=parsed_since, min_sequence=after
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Job not found") from exc
    except EventHistoryGap as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from exc

    async def event_generator() -> AsyncIterator[str]:

        try:
            async for entry in _job_event_feed(job_id, request, backlog, queue, after):
                yield _serialize_log_entry(entry) + "\n"

        finally:
            JOB_MANAGER.unsubscribe_events(job_id, queue)

    return StreamingResponse(event_generator(), media_type="application/x-ndjson")


@app.get("/jobs/{job_id}/links.json")
async def job_links(job_id: str) -> list[dict[str, object]]:
    """Return stored links for a job, falling back to demo data when requested."""

    if job_id == "demo":
        blended = blend_dom_with_ocr(dom_links=demo_dom_links(), ocr_links=demo_ocr_links())
        return serialize_links(blended)
    active_store = _active_store()
    try:
        payload = await _async_read_links(active_store, job_id)
        return _normalize_links_anchors(payload)
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Corrupt links.json: {exc}",
        ) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@app.get("/jobs/{job_id}/manifest.json")
async def job_manifest(job_id: str) -> JSONResponse:
    active_store = _active_store()
    try:
        manifest = await _async_read_manifest(active_store, job_id)
    except (json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"Corrupt manifest.json: {exc}") from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Job not found") from exc
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Manifest not available yet") from None
    return JSONResponse(manifest)


@app.get("/jobs/{job_id}/slo", response_model=Slosummary)
async def job_slo(job_id: str) -> Slosummary:
    """Per-job SLO summary for a single capture.

    Reads the manifest from the store and returns p50/p95 capture/OCR/total
    timings, plus budget breach count if a budget file is configured.
    """
    active_store = _active_store()
    try:
        manifest = await _async_read_manifest(active_store, job_id)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=f"Manifest not found: {exc}") from exc
    if not isinstance(manifest, dict):
        raise HTTPException(status_code=404, detail="Manifest is empty")
    timings = manifest.get("timings") or {}
    p50_total = timings.get("total_ms")
    p95_total = p50_total  # single sample
    p50_cap = timings.get("capture_ms")
    p95_cap = p50_cap
    p50_ocr = timings.get("ocr_ms")
    p95_ocr = p50_ocr
    budget_ms: int | None = None
    breaches = 0
    try:
        from scripts.compute_slo import load_budgets

        budgets = load_budgets(None)
        # Use a category key fallback: profile_id or "default"
        category = (manifest.get("environment") or {}).get("profile_id") or "default"
        budget_ms = budgets.get(category) or budgets.get("default")
    except Exception:
        budget_ms = None
    if budget_ms and p95_total is not None and int(p95_total) > int(budget_ms):
        breaches = 1
    return Slosummary(
        p50_total_ms=p50_total,
        p95_total_ms=p95_total,
        p50_capture_ms=p50_cap,
        p95_capture_ms=p95_cap,
        p50_ocr_ms=p50_ocr,
        p95_ocr_ms=p95_ocr,
        budget_ms=budget_ms,
        budget_breaches=breaches,
        status="within_budget" if breaches == 0 else "breach",
        count=1,
    )


@app.post("/jobs/{job_id}/tag", response_model=JobTagResponse)
async def job_add_tag(job_id: str, request: JobTagRequest) -> JobTagResponse:
    """Append a tag to a job so it shows up in `GET /jobs?tag=...` queries."""
    try:
        await asyncio.to_thread(JOB_MANAGER.add_tag, job_id, request.tag)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Job not found") from exc
    snap = await _async_get_snapshot(job_id) or {}
    return JobTagResponse(job_id=job_id, tags=list(snap.get("tags") or []))


@app.post("/jobs/{job_id}/tags", response_model=JobTagResponse)
async def job_add_tags(job_id: str, request: JobTagMultiRequest) -> JobTagResponse:
    """Append multiple tags to a job in one call.

    Idempotent: existing tags are not duplicated. Returns the final tag set so
    agents don't have to follow up with GET /jobs/{id}/tag.
    """
    try:
        tags = await asyncio.to_thread(JOB_MANAGER.add_tags, job_id, list(request.tags))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Job not found") from exc
    return JobTagResponse(job_id=job_id, tags=tags)


@app.delete("/jobs/{job_id}/tag/{tag}", response_model=JobTagResponse)
async def job_remove_tag(job_id: str, tag: str) -> JobTagResponse:
    """Remove a single tag by value.

    Path-encoded so ``/`` in tag names is escaped as ``%2F`` (rare but supported).
    """
    try:
        tags = await asyncio.to_thread(JOB_MANAGER.remove_tag, job_id, tag)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Job not found") from exc
    return JobTagResponse(job_id=job_id, tags=tags)


@app.post("/jobs/{job_id}/cancel", response_model=JobSnapshotResponse)
async def job_cancel(job_id: str, reason: str = "cancelled by user") -> JobSnapshotResponse:
    """Cancel an in-flight job. Sets state to CANCELLED and broadcasts."""
    await asyncio.to_thread(JOB_MANAGER.cancel_job, job_id, reason=reason)
    snapshot = await _async_get_snapshot(job_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return _snapshot_to_response(snapshot)


@app.post("/jobs/{job_id}/share", response_model=JobShareResponse)
async def share_job(
    job_id: str,
    ttl_seconds: int = 86_400,
) -> JobShareResponse:
    """Mint a signed share token (HMAC-SHA256, WEBHOOK_SECRET-bound)."""
    import time
    import hmac
    import hashlib
    import base64
    import json as _json
    from datetime import datetime, timezone

    if await _async_get_snapshot(job_id) is None:
        raise HTTPException(status_code=404, detail="Job not found")
    secret = (settings.webhook_secret or "mdwb-dev-webhook").encode("utf-8")
    if settings.REQUIRE_API_KEY and (
        settings.webhook_secret == "mdwb-dev-webhook" or len(secret) < 32
    ):
        raise HTTPException(
            status_code=503, detail="Configure a private WEBHOOK_SECRET before sharing"
        )
    if JOB_MANAGER.get_snapshot(job_id) is None:
        raise HTTPException(status_code=404, detail="Job not found")
    expires_at = int(time.time()) + max(60, ttl_seconds)
    payload = {"job_id": job_id, "exp": expires_at}
    body = _json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    sig = hmac.new(secret, body, hashlib.sha256).digest()
    token = base64.urlsafe_b64encode(body + b"." + sig).decode("ascii").rstrip("=")
    return JobShareResponse(
        job_id=job_id,
        token=token,
        expires_at=datetime.fromtimestamp(expires_at, tz=timezone.utc).isoformat(),
        share_url=f"/jobs/share/{token}",
    )


def _redact_public_manifest(manifest: Any) -> dict[str, Any] | None:
    if not isinstance(manifest, Mapping):
        return None
    redacted: dict[str, Any] = {}
    sensitive_keys = {
        "profile_id",
        "owner",
        "api_key",
        "webhook_secret",
        "cache_seed",
        "cache_key",
        "manifest_path",
        "storage_state_path",
        "cache_path",
        "artifact_root",
    }
    storage_cfg = getattr(settings, "storage", None)
    cache_root_str = str(
        getattr(storage_cfg, "cache_root", getattr(settings, "cache_root", "/tmp"))
    )
    profiles_root_str = str(
        getattr(storage_cfg, "profiles_root", getattr(settings, "profiles_root", "/tmp"))
    )

    def _clean_val(val: Any) -> Any:
        if isinstance(val, Mapping):
            return _redact_public_manifest(val)
        if isinstance(val, list):
            return [_clean_val(v) for v in val if not (isinstance(v, str) and "mdwb_" in v)]
        return val

    for key, value in manifest.items():
        if str(key).lower() in sensitive_keys:
            continue
        if isinstance(value, str):
            if "mdwb_" in value:
                continue
            if value.startswith(("/tmp/", "/var/", "/home/", "/data/", cache_root_str, profiles_root_str)):
                continue
        redacted[str(key)] = _clean_val(value)
    return redacted


@app.get("/jobs/share/{token}", response_model=JobSharePublicResponse)
async def public_share_view(token: str) -> JobSharePublicResponse:
    """Resolve a share token; return redacted public snapshot (no API key needed)."""
    import hmac
    import hashlib
    import base64
    import json as _json
    import time as _time

    secret = (settings.webhook_secret or "mdwb-dev-webhook").encode("utf-8")
    if settings.REQUIRE_API_KEY and (
        settings.webhook_secret == "mdwb-dev-webhook" or len(secret) < 32
    ):
        raise HTTPException(status_code=404, detail="Invalid share token")
    pad = "=" * ((4 - len(token) % 4) % 4)
    try:
        decoded = base64.urlsafe_b64decode(token + pad)
    except Exception:
        raise HTTPException(status_code=404, detail="Invalid share token") from None
    if len(decoded) < 34 or decoded[-33:-32] != b".":
        raise HTTPException(status_code=404, detail="Invalid share token")
    body, sig = decoded[:-33], decoded[-32:]
    expected = hmac.new(secret, body, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        raise HTTPException(status_code=404, detail="Invalid share token")
    try:
        payload = _json.loads(body)
    except Exception:
        raise HTTPException(status_code=404, detail="Invalid share token") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("exp"), int):
        raise HTTPException(status_code=404, detail="Invalid share token")
    if payload["exp"] < _time.time():
        raise HTTPException(status_code=404, detail="Share token expired")
    target_job = payload.get("job_id")
    if not isinstance(target_job, str):
        raise HTTPException(status_code=404, detail="Invalid share token")
    snap = await _async_get_snapshot(target_job)
    if snap is None:
        raise HTTPException(status_code=404, detail="Job not found")
    raw_manifest = snap.get("manifest") or {}
    public_fields = {
        "tiles_total",
        "capture_ms",
        "ocr_ms",
        "stitch_ms",
        "cft_label",
        "cft_version",
        "playwright_version",
        "browser_transport",
        "screenshot_style_hash",
        "long_side_px",
        "selected_ocr_model",
    }
    manifest = {key: value for key, value in raw_manifest.items() if key in public_fields}
    from urllib.parse import urlsplit, urlunsplit

    source_url = urlsplit(str(snap.get("url") or ""))
    public_url = urlunsplit(
        (source_url.scheme, source_url.netloc.rsplit("@", 1)[-1], source_url.path, "", "")
    )
    return JobSharePublicResponse(
        job_id=target_job,
        url=public_url,
        state=str(snap.get("state", "UNKNOWN")),
        cache_hit=bool(snap.get("cache_hit")),
        created_at=str(snap.get("started_at") or "") or None,
        finished_at=str(snap.get("finished_at") or "") or None,
        manifest=manifest,
        share_expires_at=str(payload.get("exp") or ""),
    )


@app.post("/jobs/{job_id}/embeddings/store", response_model=EmbeddingStoreResponse)
async def store_embeddings(job_id: str, request: EmbeddingStoreRequest) -> EmbeddingStoreResponse:
    """Re-compute and persist section embeddings (any model in /embedders)."""
    from app.embedders import get_embedder, list_embedders

    if request.model not in list_embedders():
        raise HTTPException(
            status_code=400,
            detail=f"Unknown embedder {request.model!r}; known: {list_embedders()}",
        )
    try:
        embedder = get_embedder(request.model)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"embedder unavailable: {exc}") from exc

    active_store = _active_store()
    section_specs: list[dict[str, Any]] = []
    if request.sections:
        for i, raw_text in enumerate(request.sections):
            if str(raw_text).strip():
                section_specs.append(
                    {
                        "section_id": f"{job_id}-{i}",
                        "tile_start": i,
                        "tile_end": i + 1,
                        "text": str(raw_text).strip(),
                    }
                )
    else:
        try:
            structured = await _async_get_structured(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Job not found") from exc
        except Exception:
            structured = {"sections": []}
        seen_ids: dict[str, int] = {}
        for i, s in enumerate(structured.get("sections", []) or []):
            text = ((s.get("heading") or "") + " " + (s.get("body") or "")).strip()
            if not text:
                continue
            base_id = str(s.get("anchor") or f"{job_id}-{i}").strip() or f"{job_id}-{i}"
            count = seen_ids.get(base_id, 0)
            seen_ids[base_id] = count + 1
            sec_id = base_id if count == 0 else f"{base_id}-{count + 1}"
            tiles = s.get("tile_indices") or []
            if isinstance(tiles, list) and tiles:
                t_start = int(min(tiles))
                t_end = max(t_start, int(max(tiles)))
            else:
                t_start = i
                t_end = i + 1
            section_specs.append(
                {
                    "section_id": sec_id,
                    "tile_start": t_start,
                    "tile_end": t_end,
                    "text": text,
                }
            )
        if not section_specs:
            try:
                raw_md = await _async_read_markdown(active_store, job_id)
            except Exception:
                raw_md = ""
            if raw_md and raw_md.strip():
                section_specs.append(
                    {
                        "section_id": "document",
                        "tile_start": 0,
                        "tile_end": 0,
                        "text": raw_md.strip(),
                    }
                )

    if not section_specs:
        return EmbeddingStoreResponse(
            job_id=job_id, model=request.model, stored=0, replaced=0, dim=embedder.dim
        )

    if request.replace:
        try:
            if hasattr(active_store, "adelete_section_embeddings"):
                await active_store.adelete_section_embeddings(
                    job_id=job_id,
                    model=request.model,
                )
            else:
                await asyncio.to_thread(
                    active_store.delete_section_embeddings,
                    job_id=job_id,
                    model=request.model,
                )
        except Exception:
            pass
        replaced = len(section_specs)
    else:
        replaced = 0

    from app.embeddings import SectionEmbedding

    def _compute_sections() -> list[SectionEmbedding]:
        computed: list[SectionEmbedding] = []
        for spec in section_specs:
            vec = embedder.embed(spec["text"])
            computed.append(
                SectionEmbedding(
                    section_id=spec["section_id"],
                    tile_start=spec["tile_start"],
                    tile_end=spec["tile_end"],
                    vector=vec,
                    model=request.model,
                    dim=len(vec),
                )
            )
        return computed

    try:
        async with _EMBEDDER_SEMAPHORE:
            section_objs = await asyncio.to_thread(_compute_sections)
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail=f"embedder {request.model} failed: {exc}"
        ) from exc

    try:
        if hasattr(active_store, "aupsert_embeddings"):
            await active_store.aupsert_embeddings(
                run_id=job_id,
                sections=section_objs,
                model=request.model,
            )
        else:
            await asyncio.to_thread(
                active_store.upsert_embeddings,
                run_id=job_id,
                sections=section_objs,
            )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover
        raise HTTPException(status_code=500, detail=f"failed to persist embeddings: {exc}") from exc

    return EmbeddingStoreResponse(
        job_id=job_id,
        model=request.model,
        stored=len(section_objs),
        replaced=replaced,
        dim=embedder.dim,
    )


@app.get("/jobs/{job_id}/raw")
async def job_raw_events(job_id: str, min_sequence: int | None = None) -> dict:
    """JSON array of the entire event log (single-shot, vs. the streaming NDJSON at /events)."""
    await JOB_MANAGER.restore_job_history(job_id)
    try:
        events = await asyncio.to_thread(
            JOB_MANAGER.get_events, job_id, since=None, min_sequence=min_sequence
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Job not found") from exc
    return {"job_id": job_id, "count": len(events), "events": events}


@app.get("/jobs/{job_id}/headings/{heading:path}", response_model=HeadingBodyResponse)
async def job_heading(job_id: str, heading: str) -> HeadingBodyResponse:
    """Get the body + sub-sections for a specific heading.

    Resolves the heading by exact text match (case-insensitive); 404 if not found.
    Agents use this to drill into "give me the body of the Pricing section".
    """
    structured: dict[str, Any]
    try:
        structured = await _async_get_structured(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Job not found") from exc
    except Exception:
        structured = {"sections": [], "url": ""}
    sections = structured.get("sections", []) or []
    target_lower = heading.lower()
    match_idx = None
    for i, s in enumerate(sections):
        if str(s.get("heading", "")).lower() == target_lower:
            match_idx = i
            break
    if match_idx is None:
        raise HTTPException(status_code=404, detail=f"Heading {heading!r} not found")
    matched = sections[match_idx]
    body = matched.get("body", "") or ""
    matched_level = matched.get("level", 1)
    subsections: list = []
    for s in sections[match_idx + 1 :]:
        if s.get("level", 1) <= matched_level:
            break
        subsections.append(
            TocEntry(
                level=s.get("level", 1),
                heading=s.get("heading", ""),
                anchor=s.get("anchor"),
                body_chars=len(s.get("body") or ""),
            )
        )
    # Gather links under the matched heading
    outbound: list = []
    active_store = _active_store()
    try:
        links_payload = await _async_read_links(active_store, job_id)
    except Exception:
        links_payload = None
    for a in _normalize_links_anchors(links_payload):
        if a.get("href"):
            outbound.append(a["href"])
    return HeadingBodyResponse(
        job_id=job_id,
        url=structured.get("url", ""),
        heading=matched.get("heading", ""),
        anchor=matched.get("anchor"),
        body=body,
        body_chars=len(body),
        subsections=subsections,
        outbound_links=outbound[:200],
    )


@app.get("/jobs/{job_id}/follow", response_model=FollowResponse)
async def job_follow(
    job_id: str,
    anchor: str,
) -> FollowResponse:
    """Resolve an in-page anchor (e.g. #section-1) to the link it points at.

    Walks the Markdown looking for the corresponding ``[text](url#anchor)`` link.
    """
    active_store = _active_store()
    try:
        md = (await _async_read_markdown(active_store, job_id)) or ""
    except KeyError as exc:
        if await _async_get_snapshot(job_id) is None:
            raise HTTPException(status_code=404, detail="Job not found") from exc
        md = ""
    except Exception:
        md = ""
    import re as _re

    candidates: list = []
    resolved_href: str | None = None
    resolved_text: str | None = None
    if md:
        # Find links whose URL ends with the anchor
        for m in _re.finditer(r"\[([^\]]+)\]\(([^)]+)\)", md):
            text, target = m.group(1), m.group(2)
            if target.endswith(f"#{anchor}"):
                if resolved_href is None:
                    resolved_href = target
                    resolved_text = text
                else:
                    candidates.append(target)
            elif f"#{anchor}" in target:
                candidates.append(target)
    return FollowResponse(
        job_id=job_id,
        anchor=anchor,
        resolved_href=resolved_href,
        resolved_text=resolved_text,
        candidates=candidates,
    )


@app.get("/jobs/{job_id}/export.md", response_model=ExportMdResponse)
async def job_export_md(
    job_id: str,
    max_chars: int | None = None,
    strip_provenance: bool = True,
) -> ExportMdResponse:
    """Single-shot JSON export of the (optionally cleaned) Markdown.

    Stream-chunked variant lives at GET /jobs/{id}/export.md.stream (future work).
    """
    active_store = _active_store()
    try:
        md = (await _async_read_markdown(active_store, job_id)) or ""
    except Exception:
        md = ""
    if not md:
        raise HTTPException(status_code=404, detail="Markdown not available")
    if strip_provenance:
        import re as _re

        md = _re.sub(r"<!--.*?-->\n?", "", md, flags=_re.DOTALL).strip()
    truncated = False
    if max_chars is not None and len(md) > max_chars:
        md = md[:max_chars]
        truncated = True
    return ExportMdResponse(
        job_id=job_id,
        char_count=len(md),
        truncated=truncated,
        markdown=md,
    )


@app.post("/jobs/{job_id}/queries", response_model=BatchQueryResponse)
async def batch_queries(job_id: str, request: BatchQueryRequest) -> BatchQueryResponse:
    """Answer multiple queries in one HTTP call.

    Each query in ``request.queries`` is one of:
    ``sections``, ``links``, ``headings``, ``summary``, ``toc``.
    """
    structured: dict[str, Any]
    results: dict = {}
    qset = {q.lower() for q in request.queries}
    active_store = _active_store()
    if "sections" in qset or "headings" in qset or "toc" in qset:
        try:
            structured = await _async_get_structured(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Job not found") from exc
        except Exception:
            structured = {"sections": [], "url": ""}
        secs = structured.get("sections", []) or []
        if "sections" in qset or "headings" in qset:
            results["sections"] = [
                {
                    "level": s.get("level", 1),
                    "heading": s.get("heading"),
                    "anchor": s.get("anchor"),
                    "body_chars": len(s.get("body") or ""),
                }
                for s in secs
            ]
        if "toc" in qset:
            try:
                links_payload = await _async_read_links(active_store, job_id)
            except Exception:
                links_payload = None
            anchors = _normalize_links_anchors(links_payload)
            outbound = sorted(
                {str(a.get("href")) for a in anchors if a.get("href")}
            )
            results["toc"] = {
                "sections": results.get("sections", [
                    {"level": s.get("level", 1), "heading": s.get("heading"), "anchor": s.get("anchor"), "body_chars": len(s.get("body") or "")}
                    for s in secs
                ]),
                "outbound_links": outbound[:200],
            }
    if "links" in qset:
        try:
            links_payload = await _async_read_links(active_store, job_id)
        except KeyError as exc:
            if await _async_get_snapshot(job_id) is None:
                raise HTTPException(status_code=404, detail="Job not found") from exc
            links_payload = None
        except Exception:
            links_payload = None
        anchors = _normalize_links_anchors(links_payload)
        results["links"] = [
            {"href": a.get("href"), "text": a.get("text"), "source": a.get("source")}
            for a in anchors
        ][:200]
    if "summary" in qset:
        try:
            structured = await _async_get_structured(job_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Job not found") from exc
        except Exception:
            structured = {"sections": [], "url": ""}
        sections = structured.get("sections", []) or []
        headings = [s.get("heading") for s in sections if s.get("heading")]
        url = structured.get("url", "")
        parts = [f"Captured page at {url}."]
        if headings:
            parts.append("Sections: " + ", ".join(headings[:3]) + ".")
        results["summary"] = {
            "url": url,
            "headings": headings,
            "summary": " ".join(parts) if parts else "(no summary)",
            "section_count": len(sections),
        }
    return BatchQueryResponse(job_id=job_id, results=results)


@app.get("/metrics/cache", response_model=CacheMetricsResponse)
async def metrics_cache() -> CacheMetricsResponse:
    """Report cache hit rate, entry count, TTL config.

    Best-effort: if the store doesn't expose a cache, returns the report
    with ``enabled=False`` and zeroes.
    """
    entries = 0
    hits = 0
    misses = 0
    enabled = False
    ttl: int | None = None
    active_store = _active_store()
    try:
        if hasattr(active_store, "acache_stats"):
            stats = await active_store.acache_stats()
            entries = int(stats.get("entries", 0))
            hits = int(stats.get("hits", 0))
            misses = int(stats.get("misses", 0))
            enabled = bool(stats.get("enabled", True))
            ttl = stats.get("ttl_seconds")
        elif hasattr(active_store, "cache_stats"):
            stats = await asyncio.to_thread(active_store.cache_stats)
            entries = int(stats.get("entries", 0))
            hits = int(stats.get("hits", 0))
            misses = int(stats.get("misses", 0))
            enabled = bool(stats.get("enabled", True))
            ttl = stats.get("ttl_seconds")
    except Exception:
        pass
    total = hits + misses
    hit_rate = (hits / total) if total else 0.0
    return CacheMetricsResponse(
        cache_name="capture",
        entries=entries,
        hit_rate=hit_rate,
        hits=hits,
        misses=misses,
        ttl_seconds=ttl,
        enabled=enabled,
    )


@app.get("/metrics/queue", response_model=QueueMetricsResponse)
async def metrics_queue() -> QueueMetricsResponse:
    """Per-state queue depth + watchdog uptime + last run timestamp."""
    from datetime import datetime, timezone

    by_state: dict = {}
    in_flight = 0
    try:
        for job_id, task in (JOB_MANAGER._tasks or {}).items():
            if task and not task.done():
                in_flight += 1
                snap = await _async_get_snapshot(job_id) or {}
                state = str(snap.get("state", "UNKNOWN"))
                by_state[state] = by_state.get(state, 0) + 1
    except Exception:
        pass
    watchdog_running = bool(
        JOB_MANAGER._watchdog_task is not None and not JOB_MANAGER._watchdog_task.done()
    )
    # Approximate watchdog uptime via the task's start time
    uptime: float | None = None
    try:
        if JOB_MANAGER._watchdog_task is not None:
            uptime = (datetime.now(timezone.utc) - JOB_MANAGER._started_at).total_seconds()
    except Exception:
        pass
    last_run = None
    try:
        rows, _ = await _async_list_jobs(limit=1)
        if rows:
            last_run = str(rows[0].get("created_at"))
    except Exception:
        pass
    total = 0
    try:
        _, total = await _async_list_jobs(limit=100_000)
    except Exception:
        pass
    return QueueMetricsResponse(
        by_state=by_state,
        watchdog_running=watchdog_running,
        watchdog_uptime_seconds=uptime,
        last_run_at=last_run,
        in_flight=in_flight,
        total_completed=total,
    )


@app.post("/admin/cache/invalidate")
async def admin_cache_invalidate(
    request: Request,
    url: str | None = None,
    cache_key: str | None = None,
) -> dict:
    """Drop a specific URL or cache_key from the capture cache (vs. clear-all).

    Body: ``{"url": "https://example.com"}`` (or use the ``url`` query param).
    """
    from datetime import datetime, timezone

    target = (url or cache_key or "").strip()
    if not target:
        try:
            body = await request.json()
            if isinstance(body, Mapping):
                target = str(body.get("url") or body.get("cache_key") or "").strip()
        except Exception:
            target = ""
    if not target:
        raise HTTPException(status_code=400, detail="url is required")
    removed_count = 0
    active_store = _active_store()
    try:
        if hasattr(active_store, "ainvalidate_cache_key"):
            res = await active_store.ainvalidate_cache_key(target)
            removed_count = int(res) if isinstance(res, int) else (1 if res else 0)
        elif hasattr(active_store, "invalidate_cache_key"):
            res = await asyncio.to_thread(active_store.invalidate_cache_key, target)
            removed_count = int(res) if isinstance(res, int) else (1 if res else 0)
    except Exception:
        pass
    return {
        "url": target,
        "removed": removed_count > 0,
        "invalidated_entries": removed_count,
        "invalidated_at": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/jobs/{job_id}/sections", response_model=JobSectionsResponse)
async def job_sections(job_id: str) -> JobSectionsResponse:
    """Flat list of sections for fast TOC + jump-to-anchor navigation.

    Cheaper than /jobs/{id}/toc because it doesn't include outbound links.
    Agents can pair this with /jobs/{id}/links/{query} to drill down.
    """
    structured: dict[str, Any]
    try:
        structured = await _async_get_structured(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Job not found") from exc
    except Exception:
        structured = {"sections": [], "url": ""}
    sections = structured.get("sections", []) or []
    return JobSectionsResponse(
        job_id=job_id,
        url=structured.get("url", ""),
        sections=[
            TocEntry(
                level=s.get("level", 1),
                heading=s.get("heading", ""),
                anchor=s.get("anchor"),
                body_chars=len(s.get("body") or ""),
            )
            for s in sections
        ],
        total_chars=sum(len(s.get("body") or "") for s in sections),
    )


@app.get("/jobs/{job_id}/extract", response_model=ExtractResponse)
async def job_extract(job_id: str) -> ExtractResponse:
    """Extract structured data: links, tables, code blocks.

    Aggregates from multiple sources so an agent can answer "what tables are
    on this page" / "what external links does it have" / "what code is shown"
    with one call.
    """
    import re as _re

    active_store = _active_store()
    try:
        links_payload = await _async_read_links(active_store, job_id)
    except Exception:
        links_payload = None
    anchors = _normalize_links_anchors(links_payload)
    links = [
        ExtractedLink(
            href=a.get("href") or "",
            text=a.get("text"),
            title=a.get("title"),
            source=a.get("source"),
        )
        for a in anchors
        if a.get("href")
    ]

    try:
        md = (await _async_read_markdown(active_store, job_id)) or ""
    except Exception:
        md = ""
    tables: list = []
    code_blocks: list = []
    if md:
        in_table = False
        current_table_lines: list = []
        current_table_start: int | None = None
        for line_no, line in enumerate(md.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("|") and stripped.endswith("|") and len(stripped) > 2:
                if not in_table:
                    in_table = True
                    current_table_start = line_no
                    current_table_lines = []
                current_table_lines.append(stripped)
            else:
                if in_table:
                    in_table = False
                    if len(current_table_lines) >= 2:
                        header = [c.strip() for c in current_table_lines[0].strip("|").split("|")]
                        rows = [
                            [c.strip() for c in row.strip("|").split("|")]
                            for row in current_table_lines[2:]
                        ]
                        tables.append(
                            ExtractedTable(
                                headers=header,
                                rows=rows,
                                source_line=current_table_start,
                            )
                        )
                    current_table_lines = []
        if in_table and current_table_lines:
            header = [c.strip() for c in current_table_lines[0].strip("|").split("|")]
            tables.append(ExtractedTable(headers=header, rows=[], source_line=current_table_start))

        for m in _re.finditer(r"```(\w*)\n(.*?)```", md, flags=_re.DOTALL):
            lang, content = m.group(1) or None, m.group(2)
            code_blocks.append(
                ExtractedCodeBlock(
                    language=lang,
                    content=content[:2000],
                    source_line=md[: m.start()].count("\n") + 1,
                )
            )

    try:
        structured = await _async_get_structured(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Job not found") from exc
    except Exception:
        structured = {"url": ""}

    return ExtractResponse(
        job_id=job_id,
        url=structured.get("url", ""),
        links=links[:200],
        tables=tables[:50],
        code_blocks=code_blocks[:50],
    )


@app.get("/jobs/{job_id}/links/{query:path}", response_model=LinkLookupResponse)
async def job_links_lookup(job_id: str, query: str) -> LinkLookupResponse:
    """Fuzzy link lookup by text or anchor substring.

    Useful for agents that need to resolve "what URL is behind this heading?"
    without re-parsing the full links.json.
    """
    active_store = _active_store()
    try:
        payload = await _async_read_links(active_store, job_id)
    except KeyError as exc:
        if await _async_get_snapshot(job_id) is None:
            raise HTTPException(status_code=404, detail="Job not found") from exc
        payload = None
    except Exception:
        payload = None
    anchors = _normalize_links_anchors(payload)
    q = query.lower()
    matches: list = []
    for a in anchors:
        text = (a.get("text") or "").lower()
        href = a.get("href") or ""
        if q in text or q in href.lower():
            matches.append(
                ExtractedLink(
                    href=href,
                    text=a.get("text"),
                    title=a.get("title"),
                    source=a.get("source"),
                )
            )
        if len(matches) >= 50:
            break
    return LinkLookupResponse(job_id=job_id, query=query, matches=matches)


@app.get("/admin/stats", response_model=AdminStatsResponse)
async def admin_stats() -> AdminStatsResponse:
    """Per-day counts (last 90 days) + per-state + per-embedder.

    Walks up to 50k jobs in memory. Intended for ops dashboards.
    """
    from datetime import datetime, timedelta, timezone

    rows, _ = await _async_list_jobs(limit=50_000)
    by_state: dict = {}
    by_day: dict = {}
    today = datetime.now(timezone.utc).date()
    cutoff = today - timedelta(days=90)
    cache_hits = 0
    embedder_counts: dict = {}
    for r in rows:
        st = r.get("state") or "UNKNOWN"
        by_state[st] = by_state.get(st, 0) + 1
        if r.get("cache_hit"):
            cache_hits += 1
        em = r.get("embedding_model")
        if em:
            embedder_counts[em] = embedder_counts.get(em, 0) + 1
        ts = r.get("created_at")
        if not ts:
            continue
        try:
            d = datetime.fromisoformat(ts.replace("Z", "+00:00")).date()
        except Exception:
            continue
        if d < cutoff:
            continue
        by_day[d.isoformat()] = by_day.get(d.isoformat(), 0) + 1
    return AdminStatsResponse(
        total=len(rows),
        by_state=by_state,
        by_day=by_day,
        embedder_default="hash-bucket-v1",
        embedder_counts=embedder_counts,
        cache_hits=cache_hits,
        generated_at=datetime.now(timezone.utc).isoformat(),
    )


@app.post("/admin/jobs/prune", response_model=AdminPruneResponse)
async def admin_jobs_prune(request: AdminPruneRequest) -> AdminPruneResponse:
    """Bulk-delete jobs older than N days (DONE/FAILED states only)."""
    from datetime import datetime, timedelta, timezone

    cutoff_ts = datetime.now(timezone.utc) - timedelta(days=request.older_than_days)
    cutoff_iso = cutoff_ts.isoformat()

    rows, _ = await _async_list_jobs(limit=50_000, state=request.state)
    candidates: list[str] = []
    for r in rows:
        if r.get("state") not in ("DONE", "FAILED", "CANCELLED"):
            continue
        ts = r.get("finished_at") or r.get("created_at")
        if not ts:
            continue
        try:
            d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if d >= cutoff_ts:
            continue
        candidates.append(str(r["id"]))

    active_store = _active_store()
    candidate_set = set(candidates)
    shared_protected: list[str] = []
    if hasattr(active_store, "acache_path_references"):
        for jid in candidates:
            try:
                refs = await active_store.acache_path_references(jid)
                if any(ref not in candidate_set for ref in refs):
                    shared_protected.append(jid)
            except Exception:
                pass
    elif hasattr(active_store, "cache_path_references"):
        for jid in candidates:
            try:
                refs = await asyncio.to_thread(active_store.cache_path_references, jid)
                if any(ref not in candidate_set for ref in refs):
                    shared_protected.append(jid)
            except Exception:
                pass

    deleted = 0
    if not request.dry_run:
        for jid in candidates:
            try:
                resp = await job_delete(jid)
                if resp.deleted:
                    deleted += 1
            except Exception:
                pass

    return AdminPruneResponse(
        candidates=len(candidates),
        deleted=deleted,
        dry_run=request.dry_run,
        cutoff=cutoff_iso,
        candidate_job_ids=candidates,
        shared_protected_job_ids=shared_protected,
    )


@app.post("/admin/cache/clear", response_model=AdminCacheClearResponse)
async def admin_cache_clear() -> AdminCacheClearResponse:
    """Clear the capture cache metadata (non-destructive)."""
    from datetime import datetime, timezone

    cleared = 0
    active_store = _active_store()
    try:
        if hasattr(active_store, "aclear_cache"):
            res = await active_store.aclear_cache()
            cleared = int(res) if isinstance(res, int) else (1 if res else 0)
        elif hasattr(active_store, "clear_cache"):
            res = await asyncio.to_thread(active_store.clear_cache)
            cleared = int(res) if isinstance(res, int) else (1 if res else 0)
    except Exception:
        pass
    return AdminCacheClearResponse(
        cache_name="capture",
        cleared_entries=cleared,
        cleared_at=datetime.now(timezone.utc).isoformat(),
    )


@app.get("/jobs/{job_id}/toc", response_model=JobTocResponse)
async def job_toc(job_id: str, max_links: int = 50) -> JobTocResponse:
    """Text-only table of contents: headings + a bounded outbound link list.

    Designed for agents that want a quick overview of a captured page without
    fetching the full Markdown. Sections are derived from ``/jobs/{id}/result.json``;
    outbound links from ``/jobs/{id}/links.json``.
    """
    structured: dict[str, Any]
    try:
        structured = await _async_get_structured(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Job not found") from exc
    except Exception:
        structured = {"sections": [], "url": ""}
    active_store = _active_store()
    try:
        links_payload = await _async_read_links(active_store, job_id)
    except Exception:
        links_payload = None
    anchors = _normalize_links_anchors(links_payload)
    outbound = sorted(
        {
            str(a.get("href"))
            for a in anchors
            if str(a.get("href") or "").startswith(("http://", "https://"))
        }
    )
    return JobTocResponse(
        job_id=job_id,
        url=structured.get("url", ""),
        sections=[
            TocEntry(
                level=s.get("level", 1),
                heading=s.get("heading", ""),
                anchor=s.get("anchor"),
                body_chars=len(s.get("body") or ""),
            )
            for s in structured.get("sections", [])
        ],
        outbound_links=outbound[:max_links],
    )


@app.get("/jobs/{job_id}/summary", response_model=JobSummaryResponse)
async def job_summary(job_id: str) -> JobSummaryResponse:
    """Compact natural-language summary of a captured page."""
    from datetime import datetime, timezone

    structured: dict[str, Any]
    try:
        structured = await _async_get_structured(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Job not found") from exc
    except Exception:
        structured = {"sections": [], "url": ""}
    sections = structured.get("sections", []) or []
    active_store = _active_store()
    try:
        manifest = (await _async_read_manifest(active_store, job_id)) or {}
    except Exception:
        manifest = {}
    try:
        links_payload = await _async_read_links(active_store, job_id)
    except Exception:
        links_payload = None
    link_count = len(_normalize_links_anchors(links_payload))
    headings = [s.get("heading", "") for s in sections if s.get("heading")]
    summary_parts = []
    url = structured.get("url") or (manifest.get("url") if isinstance(manifest, dict) else "")
    if url:
        summary_parts.append(f"Captured page at {url}.")
    if headings:
        first_three = headings[:3]
        summary_parts.append("Sections: " + ", ".join(first_three) + ".")
    if link_count:
        summary_parts.append(f"Found {link_count} outbound link{'s' if link_count != 1 else ''}.")
    if not summary_parts:
        summary_parts.append("(no summary available)")
    summary = " ".join(summary_parts)
    return JobSummaryResponse(
        job_id=job_id,
        url=url or "",
        summary=summary,
        section_count=len(sections),
        char_count=sum(len(s.get("body") or "") for s in sections),
        outbound_link_count=link_count,
        embedding_model=(manifest.get("embedding_model") if isinstance(manifest, dict) else None),
        generated_at=datetime.now(timezone.utc).isoformat(),
    )


@app.post("/jobs/search/text", response_model=JobSearchResponse)
async def search_jobs_text(
    request: JobSearchTextRequest,
    http_request: Request,
) -> JobSearchResponse:
    """Alias of POST /jobs/search kept for text-search UX clarity."""
    from app.schemas import JobSearchRequest

    inner = JobSearchRequest(**request.model_dump())
    return await search_jobs(inner, http_request=http_request)


@app.post("/embeddings/text/batch", response_model=EmbeddingBatchResponse)
async def embed_text_batch(request: EmbeddingBatchRequest) -> EmbeddingBatchResponse:
    """Compute embeddings for N texts in one call."""
    from app.embedders import get_embedder, list_embedders

    if request.model not in list_embedders():
        raise HTTPException(
            status_code=400,
            detail=f"Unknown embedder {request.model!r}; known: {list_embedders()}",
        )
    try:
        embedder = get_embedder(request.model)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"embedder unavailable: {exc}") from exc

    def _compute_batch() -> list[EmbeddingBatchItem]:
        items: list[EmbeddingBatchItem] = []
        for text in request.texts:
            v = embedder.embed(text)
            items.append(EmbeddingBatchItem(text=text, dim=len(v), vector=v))
        return items

    try:
        async with _EMBEDDER_SEMAPHORE:
            vectors = await asyncio.to_thread(_compute_batch)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"embedder {request.model} failed: {exc}") from exc
    return EmbeddingBatchResponse(
        model=request.model,
        dim=embedder.dim,
        count=len(vectors),
        vectors=vectors,
    )


@app.get("/metrics/job-counts", response_model=JobCountsResponse)
async def metrics_job_counts() -> JobCountsResponse:
    """Per-state + per-day counts for dashboards / alerts."""
    from datetime import datetime, timedelta, timezone

    rows, total = await _async_list_jobs(limit=10_000)
    by_state: dict = {}
    for r in rows:
        st = r.get("state") or "UNKNOWN"
        by_state[st] = by_state.get(st, 0) + 1

    by_day: dict = {}
    today = datetime.now(timezone.utc).date()
    cutoff = today - timedelta(days=30)
    for r in rows:
        ts = r.get("created_at")
        if not ts:
            continue
        try:
            d = datetime.fromisoformat(ts.replace("Z", "+00:00")).date()
        except Exception:
            continue
        if d < cutoff:
            continue
        by_day[d.isoformat()] = by_day.get(d.isoformat(), 0) + 1

    return JobCountsResponse(
        total=total,
        by_state=by_state,
        by_issue={},
        by_day=by_day,
        generated_at=datetime.now(timezone.utc).isoformat(),
    )


@app.get("/metrics/embedders", response_model=EmbedderStatsResponse)
async def metrics_embedders() -> EmbedderStatsResponse:
    """Counts per embedder based on what captures have persisted."""
    from app.embedders import list_embedders

    rows, _ = await _async_list_jobs(limit=10_000)
    counts: dict = {}
    for r in rows:
        m = r.get("embedding_model")
        if m:
            counts[m] = counts.get(m, 0) + 1
    return EmbedderStatsResponse(
        available=list_embedders(),
        default="hash-bucket-v1",
        counts=counts,
    )


@app.post("/jobs/{job_id}/replay", response_model=JobReplayResponse, status_code=status.HTTP_202_ACCEPTED)
async def job_replay(
    job_id: str,
    request: JobReplayRequest,
    http_request: Request,
) -> JobReplayResponse:
    """Re-emit the same job as a fresh run with the same URL + profile + tags."""
    owner = get_request_owner(http_request)
    snap = await _async_get_snapshot(job_id)
    if snap is None:
        raise HTTPException(status_code=404, detail="Job not found")
    url = str(snap.get("url") or "")
    if not url:
        raise HTTPException(status_code=400, detail="Original job has no URL; cannot replay")
    profile_id = request.profile_id or snap.get("profile_id")
    tags = list(snap.get("tags") or [])
    raw_manifest = snap.get("manifest") if isinstance(snap.get("manifest"), Mapping) else {}
    try:
        new_request = request_from_manifest({**raw_manifest, "url": url})
        new_request.profile_id = profile_id
        new_request.reuse_cache = request.reuse_cache
        if request.ocr_policy is not None:
            new_request.ocr_policy = request.ocr_policy
        new_snapshot = await JOB_MANAGER.create_job(new_request, tags=tags)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JobReplayResponse(
        original_job_id=job_id,
        new_job_id=str(new_snapshot.get("job_id") or new_snapshot.get("id") or ""),
        url=url,
    )


@app.delete("/jobs/{job_id}", response_model=JobDeleteResponse)
async def job_delete(job_id: str, purge_artifacts: bool = False) -> JobDeleteResponse:
    """Remove a run; filesystem purging requires an explicit opt-in."""
    import shutil

    task = JOB_MANAGER._tasks.get(job_id)
    if task is not None and not task.done():
        raise HTTPException(status_code=409, detail="Cancel the active job before removing its run")

    paths: list = []
    bytes_freed = 0
    active_store = _active_store()
    try:
        record = await _async_fetch_run(active_store, job_id)
    except Exception:
        record = None
    if record is not None and purge_artifacts:
        try:
            artifact_root = RunPaths.from_record(record).root.resolve()
            cache_root = active_store.config.cache_root.resolve()
            if not artifact_root.is_relative_to(cache_root) or artifact_root == cache_root:
                raise HTTPException(status_code=409, detail="Artifact root is outside the cache")
            if active_store.path_reference_count(record.cache_path) > 1:
                raise HTTPException(status_code=409, detail="Artifacts are shared with another run")
            if artifact_root.exists():
                for p in artifact_root.rglob("*"):
                    if p.is_file():
                        try:
                            bytes_freed += p.stat().st_size
                        except OSError:
                            pass
                shutil.rmtree(artifact_root)
                paths.append(artifact_root)
        except HTTPException:
            raise
        except OSError as exc:
            raise HTTPException(
                status_code=500, detail="Artifact purge failed; run metadata retained"
            ) from exc
    try:
        removed = bool(await asyncio.to_thread(active_store.delete_run, job_id))
    except Exception:
        removed = False
    if removed:
        if hasattr(JOB_MANAGER, "remove_job_state"):
            try:
                JOB_MANAGER.remove_job_state(job_id)
            except Exception:
                pass
        JOB_MANAGER._snapshots.pop(job_id, None)
        JOB_MANAGER._job_tags.pop(job_id, None)
        JOB_MANAGER._job_owners.pop(job_id, None)
    return JobDeleteResponse(
        job_id=job_id,
        deleted=removed,
        artifacts_removed=len(paths),
        bytes_freed=bytes_freed,
    )


@app.get("/health/live", tags=["health"])
async def health_live() -> dict:
    """Liveness probe: 200 as long as the process is alive."""
    return {"status": "ok"}


@app.get("/health/ready", tags=["health"])
async def health_ready() -> dict:
    """Readiness probe: 200 only if the JobManager watchdog is running."""
    if JOB_MANAGER._watchdog_task is None or JOB_MANAGER._watchdog_task.done():
        raise HTTPException(status_code=503, detail="watchdog not running")
    return {"status": "ok", "watchdog": "running"}


@app.get("/jobs/{job_id}/slo.json", response_model=Slosummary)
async def job_slo_json(job_id: str) -> Slosummary:
    """Explicit .json alias for /jobs/{id}/slo (identical content)."""
    return await job_slo(job_id)


@app.get("/jobs/{job_id}/events.json", response_model=JobEventsJsonResponse)
async def job_events_json(
    job_id: str, since: str | None = None, after: int | None = None
) -> JobEventsJsonResponse:
    """Single-shot JSON dump of the event log (vs. the streaming NDJSON at /events)."""
    parsed = _parse_since(since)
    if after is not None and after < -1:
        raise HTTPException(status_code=400, detail="Event cursor must be at least -1")
    await JOB_MANAGER.restore_job_history(job_id)
    try:
        events = JOB_MANAGER.get_events(job_id, since=parsed, min_sequence=after)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Job not found") from exc
    except EventHistoryGap as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from exc
    return JobEventsJsonResponse(job_id=job_id, count=len(events), events=events)


@app.get("/jobs/{job_id}/links", response_model=JobLinksResponse)
async def job_links_structured(job_id: str) -> JobLinksResponse:
    """Per-source breakdown of links (dom / ocr / both / unknown).

    Agents use this to filter "trusted" links (dom only) vs "all" links.
    """
    active_store = _active_store()
    try:
        payload = await _async_read_links(active_store, job_id)
    except KeyError as exc:
        if await _async_get_snapshot(job_id) is None:
            raise HTTPException(status_code=404, detail="Job not found") from exc
        payload = None
    except Exception:
        payload = None
    anchors = _normalize_links_anchors(payload)
    by_source: dict[str, list] = {"dom": [], "ocr": [], "both": [], "other": []}
    if not anchors:
        return JobLinksResponse(
            job_id=job_id,
            by_source={},
            counts={k: 0 for k in by_source},
            total=0,
            anchors=[],
        )
    for entry in anchors:
        if not isinstance(entry, dict):
            continue
        raw_src = str(entry.get("source") or "").strip().lower()
        if raw_src == "dom":
            src = "dom"
        elif raw_src == "ocr":
            src = "ocr"
        elif raw_src in {"both", "dom+ocr", "hybrid"}:
            src = "both"
        else:
            src = "other"
        by_source[src].append(
            {
                "href": entry.get("href") or "",
                "text": entry.get("text"),
                "delta": entry.get("delta"),
            }
        )
    return JobLinksResponse(
        job_id=job_id,
        by_source={k: v for k, v in by_source.items() if v},
        counts={k: len(v) for k, v in by_source.items()},
        total=len(anchors),
        anchors=anchors,
    )


@app.get("/jobs/{job_id}/artifacts", response_model=JobArtifactsResponse)
async def job_artifacts(
    job_id: str,
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> JobArtifactsResponse:
    """Enumerate every artifact file produced for a capture.

    Resolves ``RunPaths`` from ``Store`` as authority, verifies SHA256 digests,
    paginates inventory, and emits relative URLs without leaking host paths.
    """
    record = store.fetch_run(job_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Job not found")
    root = RunPaths.from_record(record).root.resolve()
    if not root.is_relative_to(store.config.cache_root.resolve()):
        raise HTTPException(status_code=409, detail="Artifact root is outside the cache")
    if not root.is_dir():
        raise HTTPException(status_code=404, detail="Artifact root is unavailable")
    artifact_root = str(root)
    files: list[dict] = []
    if artifact_root:
        from pathlib import Path as _P

        root = _P(artifact_root)
        for p in sorted(root.rglob("*")) if root.exists() else []:
            if p.is_file() and p.resolve().is_relative_to(root):
                files.append(
                    {
                        "path": str(p.relative_to(root)),
                        "absolute": str(p),
                        "size": p.stat().st_size,
                        "kind": "tile" if "tile" in p.parts else p.suffix.lstrip(".") or "file",
                    }
                )
    return JobArtifactsResponse.model_validate(
        {
            "job_id": job_id,
            "artifact_root": artifact_root,
            "file_count": len(files),
            "total_bytes": sum(f["size"] for f in files),
            "files": files,
            "synthetic": {
                "structured_result": f"/jobs/{job_id}/result.json",
                "per_job_slo": f"/jobs/{job_id}/slo",
                "raw_markdown": f"/jobs/{job_id}/result.md?raw=true",
            },
        }
    )


@app.post("/jobs/batch/status", response_model=BatchStatusResponse)
async def jobs_batch_status(
    request: BatchStatusRequest,
    http_request: Request,
) -> BatchStatusResponse:
    """Return compact status for many jobs in one call.

    Body: ``{"job_ids": ["abc", "def", ...]}``. Useful for dashboards that
    want to refresh N tiles without N HTTP round-trips.
    """
    if not request.job_ids:
        raise HTTPException(status_code=400, detail="job_ids must be a non-empty list")
    for jid in request.job_ids:
        await asyncio.to_thread(check_job_owner_access, http_request, str(jid))
    statuses = await asyncio.to_thread(
        JOB_MANAGER.batch_status, [str(i) for i in request.job_ids]
    )
    return BatchStatusResponse(
        count=len(statuses),
        statuses=[BatchStatusItem(**s) for s in statuses],
    )


@app.get("/jobs/{job_id}/result.json", response_model=StructuredResult)
async def job_result_json(job_id: str) -> StructuredResult:
    """Machine-friendly, parsed result for /jobs/{id}.

    Returns the stitched Markdown split into a heading + body hierarchy plus
    the normalized outbound links. Agents can answer "give me the headings"
    or "give me the outbound links" without re-parsing the raw Markdown.
    """
    snapshot = await _async_get_snapshot(job_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="Job not found")
    structured = await _async_get_structured(job_id)
    return StructuredResult(**structured)


@app.get("/jobs/{job_id}/result.md")
async def job_markdown(job_id: str, raw: bool = False) -> PlainTextResponse:
    """Return the stitched Markdown.

    With ``?raw=true`` we strip provenance comments
    (``<!-- source: tile_i, y=..., sha256=..., scale=... -->``) so the agent
    gets a clean input. The default keeps provenance for audit/UI.
    """
    active_store = _active_store()
    try:
        markdown = await _async_read_markdown(active_store, job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Job not found") from exc
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Markdown not available yet") from None
    if raw:
        import re as _re

        markdown = _re.sub(r"<!--[\s\S]*?-->\n?", "", markdown, flags=_re.DOTALL).strip()
    return PlainTextResponse(markdown, media_type="text/markdown")


@app.get("/jobs/{job_id}/result.md/raw", response_class=PlainTextResponse)
async def job_markdown_raw(job_id: str) -> PlainTextResponse:
    """Convenience alias: GET /jobs/{id}/result.md/raw = result.md?raw=true."""
    return await job_markdown(job_id, raw=True)


@app.get("/jobs/{job_id}/artifact/highlight", response_class=HTMLResponse)
async def job_artifact_highlight(
    job_id: str, tile: str, y0: int = 0, y1: int | None = None
) -> HTMLResponse:
    active_store = _active_store()
    try:
        if hasattr(active_store, "aresolve_artifact"):
            await active_store.aresolve_artifact(job_id, tile)
        else:
            await asyncio.to_thread(active_store.resolve_artifact, job_id, tile)
    except (KeyError, FileNotFoundError) as exc:
        raise HTTPException(status_code=404, detail="Artifact not found") from exc
    start = max(0, y0)
    end = max(start + 1, y1 if y1 is not None else start + 1)
    content = _render_highlight_page(job_id=job_id, tile=tile, y0=start, y1=end)
    return HTMLResponse(content)


@app.get("/jobs/{job_id}/artifact/{artifact_path:path}")
async def job_artifact(job_id: str, artifact_path: str) -> FileResponse:
    active_store = _active_store()
    try:
        if hasattr(active_store, "aresolve_artifact"):
            target = await active_store.aresolve_artifact(job_id, artifact_path)
        else:
            target = await asyncio.to_thread(active_store.resolve_artifact, job_id, artifact_path)
    except (KeyError, FileNotFoundError) as exc:
        raise HTTPException(status_code=404, detail="Artifact not found") from exc
    return FileResponse(
        target,
        headers={
            "Content-Security-Policy": "sandbox; default-src 'none'",
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.post("/jobs/{job_id}/embeddings/search", response_model=EmbeddingSearchResponse)
async def embeddings_search(
    job_id: str, payload: EmbeddingSearchRequest
) -> EmbeddingSearchResponse:
    """Search section embeddings for a capture run using cosine similarity."""
    active_store = _active_store()
    query_vector = payload.vector
    target_model = payload.model
    if query_vector is None:
        if not payload.query or not payload.query.strip():
            raise HTTPException(
                status_code=400,
                detail="Provide either 'vector' or 'query' for embedding search",
            )
        from app.embedders import get_embedder, list_embedders

        if not target_model:
            try:
                rec = await _async_fetch_run(active_store, job_id)
                if rec is not None and getattr(rec, "embedding_model", None):
                    target_model = str(rec.embedding_model)
            except Exception:
                pass
        target_model = target_model or "hash-bucket-v1"
        if target_model not in list_embedders():
            raise HTTPException(
                status_code=400,
                detail=f"Unknown embedder {target_model!r}; known: {list_embedders()}",
            )
        try:
            embedder = get_embedder(target_model)
            async with _EMBEDDER_SEMAPHORE:
                query_vector = await asyncio.to_thread(embedder.embed, payload.query)
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail=f"embedder {target_model!r} failed: {exc}",
            ) from exc

    def _run_search():
        if target_model is not None:
            try:
                return active_store.search_section_embeddings(
                    job_id=job_id,
                    vector=query_vector,
                    top_k=payload.top_k,
                    model=target_model,
                )
            except TypeError:
                pass
        return active_store.search_section_embeddings(
            job_id=job_id,
            vector=query_vector,
            top_k=payload.top_k,
        )

    try:
        if (
            hasattr(active_store, "asearch_section_embeddings")
            and "search_section_embeddings" not in active_store.__dict__
        ):
            total, matches = await active_store.asearch_section_embeddings(
                job_id=job_id,
                vector=query_vector,
                top_k=payload.top_k,
                model=target_model,
            )
        else:
            total, matches = await asyncio.to_thread(_run_search)
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
        await asyncio.to_thread(
            JOB_MANAGER.register_webhook, job_id, url=payload.url, events=payload.events
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Job not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"job_id": job_id, "registered": True}


@app.get("/jobs/{job_id}/webhooks", response_model=list[WebhookSubscription])
async def list_webhooks(job_id: str) -> list[WebhookSubscription]:
    active_store = _active_store()
    try:
        if hasattr(active_store, "alist_webhooks"):
            records = await active_store.alist_webhooks(job_id)
        else:
            records = await asyncio.to_thread(active_store.list_webhooks, job_id)
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
        deleted = await asyncio.to_thread(
            JOB_MANAGER.delete_webhook, job_id, webhook_id=payload.id, url=payload.url
        )
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
            dom_summary = None
            dom_assists = manifest.get("dom_assists")
            if isinstance(dom_assists, list) and dom_assists:
                tiles_total = manifest.get("tiles_total")
                tiles_total_int = tiles_total if isinstance(tiles_total, int) else None
                dom_summary = summarize_dom_assists(dom_assists, tiles_total=tiles_total_int) or {
                    "count": len(dom_assists)
                }
            if not dom_summary:
                raw_summary = manifest.get("dom_assist_summary")
                if isinstance(raw_summary, Mapping):
                    dom_summary = dict(raw_summary)
                elif raw_summary:
                    dom_summary = raw_summary
            if dom_summary:
                events.append(("dom_assist", json.dumps(dom_summary)))
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
        events.append(("log", f'<li class="text-red-500">{html.escape(str(error))}</li>'))
    return events


def _serialize_log_entry(entry: dict[str, Any]) -> str:
    payload = entry.copy()
    payload.setdefault("event", "snapshot")
    return json.dumps(payload)


def _sse_frame(event: str, payload: str) -> str:
    # Bare lines are SSE protocol fields. Keep provider error text inside data.
    normalized = payload.replace("\r\n", "\n").replace("\r", "\n")
    lines = "\n".join(f"data: {line}" for line in normalized.split("\n"))
    return f"event: {event}\n{lines}\n\n"


def _extract_sequence(entry: Mapping[str, Any]) -> int | None:
    try:
        raw = entry.get("sequence") if isinstance(entry, Mapping) else None
    except AttributeError:
        return None
    if raw is None:
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
