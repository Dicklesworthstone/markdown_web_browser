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
from app.crawler import CrawlConfig, get_crawler, submit_and_wait_for_job
from app.dom_links import blend_dom_with_ocr, demo_dom_links, demo_ocr_links, serialize_links
from app.jobs import JobManager, JobSnapshot, JobState, _parse_markdown_sections, build_signed_webhook_sender
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
    JobTagResponse,
    EmbeddingStoreRequest,
    EmbeddingStoreResponse,
    BatchStatusItem,
    BatchStatusRequest,
    BatchStatusResponse,
    JobArtifactsResponse,
    JobEventsJsonResponse,
    JobLinksResponse,
    WebhookRegistrationRequest,
    WebhookSubscription,
    WebhookDeleteRequest,
)
from app.settings import settings
from app.store import build_store
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
    # Gracefully stop the watchdog on shutdown
    await JOB_MANAGER.stop_watchdog()


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
_crawler = get_crawler(store=store)


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
                "GET /jobs/{id}/events": "NDJSON: structured event log (cursor-based via ?since=ISO).",
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
                "GET /health": "Health check (returns {\"status\": \"ok\"}).",
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

    summary: dict[str, Any] = {"status": "no-data", "categories": {}, "generated_at": datetime.now(timezone.utc).isoformat()}
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


@app.post("/jobs", response_model=JobSnapshotResponse, status_code=status.HTTP_202_ACCEPTED)
async def create_job(request: JobCreateRequest) -> JobSnapshotResponse:
    snapshot = await JOB_MANAGER.create_job(request)
    return _snapshot_to_response(snapshot)


@app.get("/jobs", response_model=JobListResponse)
async def list_jobs(
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
    items, total = JOB_MANAGER.list_jobs(
        state=state,
        profile_id=profile_id,
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
async def batch_create_jobs(request: BatchJobRequest) -> BatchJobResponse:
    """Submit many URLs in one round-trip; each becomes its own job.

    Agents use this for "process this list of pages" workflows so they don't
    pay N×RTT overhead. The response is a list of per-URL outcomes mirroring
    the single-URL POST /jobs contract (id, state, cache_hit, etc.).
    """
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
            )
            snapshot = await JOB_MANAGER.create_job(job_request, tags=request.tags)
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
async def start_crawl(request: CrawlRequest) -> CrawlResponse:
    """Kick off a multi-URL crawl that reuses the capture pipeline."""
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
        )

    crawl_id = await _crawler.start_crawl(config, capture_fn=_capture)
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
async def get_crawl_status(crawl_id: str) -> CrawlStatusResponse:
    snapshot = _crawler.get_crawl_status(crawl_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="Crawl not found")
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



@app.post("/jobs/{job_id}/rerun", response_model=JobRerunResponse, status_code=status.HTTP_202_ACCEPTED)
async def rerun_job(
    job_id: str,
    reuse_cache: bool = False,
) -> JobRerunResponse:
    """Re-capture the same URL with the same profile/OCR/tags.

    A new job_id is created; the original is unchanged. Tags persist so a
    follow-up GET /jobs?tag=X query can find both. Use this instead of
    re-posting the URL when the agent only knows the existing job_id.
    """
    snapshot = JOB_MANAGER.get_snapshot(job_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="Job not found")
    url = str(snapshot.get("url") or "")
    if not url:
        raise HTTPException(status_code=400, detail="Original job has no URL; cannot rerun")
    profile_id = snapshot.get("profile_id")
    tags = list(snapshot.get("tags") or [])
    new_request = JobCreateRequest(url=url, profile_id=profile_id, reuse_cache=reuse_cache)
    new_snapshot = await JOB_MANAGER.create_job(new_request, tags=tags)
    new_id = str(new_snapshot.get("job_id") or new_snapshot.get("id") or "")
    return JobRerunResponse(
        original_job_id=job_id,
        new_job_id=new_id,
        url=url,
        reuse_cache=reuse_cache,
    )


@app.post("/jobs/{job_id}/diff", response_model=JobDiffResponse)
async def diff_jobs(job_id: str, request: JobDiffRequest) -> JobDiffResponse:
    """Compare two captures section-by-section.

    Both jobs are parsed into the same heading hierarchy, then a heading-level
    diff is produced. Use this for "did this page change since yesterday?"
    agent workflows.
    """
    snap_a = JOB_MANAGER.get_snapshot(job_id)
    if snap_a is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    snap_b = JOB_MANAGER.get_snapshot(request.other_job_id)
    if snap_b is None:
        raise HTTPException(status_code=404, detail=f"Job {request.other_job_id} not found")
    md_a = ""
    md_b = ""
    try:
        md_a = store.read_markdown(job_id)
    except Exception:
        md_a = ""
    try:
        md_b = store.read_markdown(request.other_job_id)
    except Exception:
        md_b = ""
    sections_a = {s["anchor"]: s for s in _parse_markdown_sections(md_a)}
    sections_b = {s["anchor"]: s for s in _parse_markdown_sections(md_b)}
    diffs: list[JobDiffSection] = []
    for anchor_key in set(sections_a) | set(sections_b):
        a = sections_a.get(anchor_key)
        b = sections_b.get(anchor_key)
        if a is None:
            diffs.append(
                JobDiffSection(
                    heading=b["heading"], anchor=anchor_key, state="added",
                    a_chars=0, b_chars=len(b["body"]), a_excerpt="",
                    b_excerpt=b["body"][: request.max_chars_per_section],
                )
            )
        elif b is None:
            diffs.append(
                JobDiffSection(
                    heading=a["heading"], anchor=anchor_key, state="removed",
                    a_chars=len(a["body"]), b_chars=0,
                    a_excerpt=a["body"][: request.max_chars_per_section], b_excerpt="",
                )
            )
        else:
            state = "changed" if a["body"] != b["body"] else "unchanged"
            diffs.append(
                JobDiffSection(
                    heading=a["heading"], anchor=anchor_key, state=state,
                    a_chars=len(a["body"]), b_chars=len(b["body"]),
                    a_excerpt=a["body"][: request.max_chars_per_section],
                    b_excerpt=b["body"][: request.max_chars_per_section],
                )
            )
    links_a: set[str] = set()
    links_b: set[str] = set()
    if request.include_links:
        try:
            payload = store.read_links(job_id)
            anchors = (payload or {}).get("anchors") or []
            links_a = {a.get("href") for a in anchors if a.get("href")}
        except Exception:
            pass
        try:
            payload = store.read_links(request.other_job_id)
            anchors = (payload or {}).get("anchors") or []
            links_b = {a.get("href") for a in anchors if a.get("href")}
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
async def search_jobs(request: JobSearchRequest) -> JobSearchResponse:
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

    # Pre-filter: only scan runs matching filters. Use the store listing.
    rows, _ = JOB_MANAGER.list_jobs(
        state=request.state,
        url_contains=request.url_contains,
        tag=request.tag,
        limit=500,  # hard cap so a misconfigured query doesn't walk the whole DB
    )
    matches: list[JobSearchHit] = []
    total_scanned = 0
    for row in rows:
        rid = row["id"]
        total_scanned += 1
        try:
            md = store.read_markdown(rid)
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
    vector = embedder.embed(request.text)
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
                    if (
                        sequence is not None
                        and last_sequence is not None
                        and sequence < last_sequence
                    ):
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


@app.get("/jobs/{job_id}/slo", response_model=Slosummary)
async def job_slo(job_id: str) -> Slosummary:
    """Per-job SLO summary for a single capture.

    Reads the manifest from the store and returns p50/p95 capture/OCR/total
    timings, plus budget breach count if a budget file is configured.
    """
    try:
        manifest = store.read_manifest(job_id)
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
    JOB_MANAGER.add_tag(job_id, request.tag)
    snap = JOB_MANAGER.get_snapshot(job_id) or {}
    return JobTagResponse(job_id=job_id, tags=list(snap.get("tags") or []))


@app.post("/jobs/{job_id}/tags", response_model=JobTagResponse)
async def job_add_tags(job_id: str, request: JobTagMultiRequest) -> JobTagResponse:
    """Append multiple tags to a job in one call.

    Idempotent: existing tags are not duplicated. Returns the final tag set so
    agents don't have to follow up with GET /jobs/{id}/tag.
    """
    tags = JOB_MANAGER.add_tags(job_id, list(request.tags))
    return JobTagResponse(job_id=job_id, tags=tags)


@app.delete("/jobs/{job_id}/tag/{tag}", response_model=JobTagResponse)
async def job_remove_tag(job_id: str, tag: str) -> JobTagResponse:
    """Remove a single tag by value.

    Path-encoded so ``/`` in tag names is escaped as ``%2F`` (rare but supported).
    """
    tags = JOB_MANAGER.remove_tag(job_id, tag)
    return JobTagResponse(job_id=job_id, tags=tags)


@app.post("/jobs/{job_id}/cancel", response_model=JobSnapshotResponse)
async def job_cancel(job_id: str, reason: str = "cancelled by user") -> JobSnapshotResponse:
    """Cancel an in-flight job. Sets state to CANCELLED and broadcasts."""
    JOB_MANAGER.cancel_job(job_id, reason=reason)
    snapshot = JOB_MANAGER.get_snapshot(job_id)
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

    secret = (settings.webhook_secret or "mdwb-dev-webhook").encode("utf-8")
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


@app.get("/jobs/share/{token}", response_model=JobSharePublicResponse)
async def public_share_view(token: str) -> JobSharePublicResponse:
    """Resolve a share token; return redacted public snapshot (no API key needed)."""
    import hmac
    import hashlib
    import base64
    import json as _json
    import time as _time

    secret = (settings.webhook_secret or "mdwb-dev-webhook").encode("utf-8")
    pad = "=" * ((4 - len(token) % 4) % 4)
    try:
        decoded = base64.urlsafe_b64decode(token + pad)
    except Exception:
        raise HTTPException(status_code=404, detail="Invalid share token") from None
    if b"." not in decoded:
        raise HTTPException(status_code=404, detail="Invalid share token")
    body, sig = decoded.rsplit(b".", 1)
    expected = hmac.new(secret, body, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        raise HTTPException(status_code=404, detail="Invalid share token")
    try:
        payload = _json.loads(body)
    except Exception:
        raise HTTPException(status_code=404, detail="Invalid share token") from None
    if int(payload.get("exp", 0)) < _time.time():
        raise HTTPException(status_code=404, detail="Share token expired")
    target_job = payload.get("job_id")
    if not isinstance(target_job, str):
        raise HTTPException(status_code=404, detail="Invalid share token")
    snap = JOB_MANAGER.get_snapshot(target_job)
    if snap is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return JobSharePublicResponse(
        job_id=target_job,
        url=str(snap.get("url") or ""),
        state=str(snap.get("state", "UNKNOWN")),
        cache_hit=bool(snap.get("cache_hit")),
        profile_id=snap.get("profile_id"),
        tags=list(snap.get("tags") or []),
        created_at=str(snap.get("started_at") or "") or None,
        finished_at=str(snap.get("finished_at") or "") or None,
        manifest=snap.get("manifest"),
        share_expires_at=str(payload.get("exp") or ""),
    )


@app.post("/jobs/{job_id}/embeddings/store", response_model=EmbeddingStoreResponse)
async def store_embeddings(
    job_id: str, request: EmbeddingStoreRequest
) -> EmbeddingStoreResponse:
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

    sections_text: list[str] = []
    if request.sections:
        sections_text = list(request.sections)
    else:
        try:
            structured = JOB_MANAGER.get_structured_result(job_id)
        except Exception:
            structured = {"sections": []}
        for s in structured.get("sections", []) or []:
            text = (s.get("heading") or "") + " " + (s.get("body") or "")
            if text.strip():
                sections_text.append(text.strip())

    if not sections_text:
        return EmbeddingStoreResponse(
            job_id=job_id, model=request.model, stored=0, replaced=0, dim=embedder.dim
        )

    if request.replace:
        try:
            store.delete_section_embeddings(job_id=job_id, model=request.model)
        except Exception:
            pass
        replaced = len(sections_text)
    else:
        replaced = 0

    from app.embeddings import SectionEmbedding

    embeddings = []
    for i, text in enumerate(sections_text):
        try:
            vec = embedder.embed(text)
        except Exception as exc:
            raise HTTPException(
                status_code=503, detail=f"embedder {request.model} failed: {exc}"
            ) from exc
        embeddings.append(
            {
                "section_id": f"{job_id}-{i}",
                "tile_start": i,
                "tile_end": i + 1,
                "vector": vec,
            }
        )
    try:
        store.upsert_embeddings(
            run_id=job_id,
            sections=[SectionEmbedding(**e) for e in embeddings],
        )
    except Exception as exc:  # pragma: no cover
        raise HTTPException(
            status_code=500, detail=f"failed to persist embeddings: {exc}"
        ) from exc

    return EmbeddingStoreResponse(
        job_id=job_id,
        model=request.model,
        stored=len(embeddings),
        replaced=replaced,
        dim=embedder.dim,
    )


@app.get("/jobs/{job_id}/raw")
async def job_raw_events(job_id: str) -> dict:
    """JSON array of the entire event log (single-shot, vs. the streaming NDJSON at /events)."""
    events = JOB_MANAGER.get_events(job_id, since=None, min_sequence=0)
    return {"job_id": job_id, "count": len(events), "events": events}




@app.get("/jobs/{job_id}/slo.json", response_model=Slosummary)
async def job_slo_json(job_id: str) -> Slosummary:
    """Explicit .json alias for /jobs/{id}/slo (identical content)."""
    return await job_slo(job_id)


@app.get("/jobs/{job_id}/events.json", response_model=JobEventsJsonResponse)
async def job_events_json(job_id: str, since: str | None = None) -> JobEventsJsonResponse:
    """Single-shot JSON dump of the event log (vs. the streaming NDJSON at /events)."""
    parsed = _parse_since(since)
    events = JOB_MANAGER.get_events(job_id, since=parsed, min_sequence=0)
    return {
        "job_id": job_id,
        "count": len(events),
        "events": events,
    }


@app.get("/jobs/{job_id}/links", response_model=JobLinksResponse)
async def job_links_structured(job_id: str) -> JobLinksResponse:
    """Per-source breakdown of links (dom / ocr / both / unknown).

    Agents use this to filter "trusted" links (dom only) vs "all" links.
    """
    try:
        payload = store.read_links(job_id)
    except Exception:
        payload = None
    if not isinstance(payload, dict):
        return {"job_id": job_id, "by_source": {}, "total": 0, "anchors": []}
    anchors = payload.get("anchors") or []
    by_source: dict[str, list] = {"dom": [], "ocr": [], "both": [], "other": []}
    for entry in anchors:
        if not isinstance(entry, dict):
            continue
        src = str(entry.get("source") or "other")
        if src not in by_source:
            src = "other"
        by_source[src].append(
            {
                "href": entry.get("href") or "",
                "text": entry.get("text"),
                "delta": entry.get("delta"),
            }
        )
    return {
        "job_id": job_id,
        "by_source": {k: v for k, v in by_source.items() if v},
        "counts": {k: len(v) for k, v in by_source.items()},
        "total": len(anchors),
        "anchors": anchors,
    }


@app.get("/jobs/{job_id}/artifacts", response_model=JobArtifactsResponse)
async def job_artifacts(job_id: str) -> JobArtifactsResponse:
    """Enumerate every artifact file produced for a capture.

    Lists ``manifest.json``, ``out.md``, ``links.json``, ``dom_snapshot.html``,
    ``result.json``, ``slo`` (synthetic), and any tile images. Exists so an
    agent can decide what to download without guessing.
    """
    try:
        manifest = store.read_manifest(job_id)
    except Exception:
        manifest = None
    if not isinstance(manifest, dict):
        raise HTTPException(status_code=404, detail="Manifest not available")
    artifact_root = manifest.get("artifact_root") or manifest.get("cache_path") or ""
    files: list[dict] = []
    if artifact_root:
        from pathlib import Path as _P
        root = _P(artifact_root)
        for p in sorted(root.rglob("*")) if root.exists() else []:
            if p.is_file():
                files.append(
                    {
                        "path": str(p.relative_to(root)),
                        "absolute": str(p),
                        "size": p.stat().st_size,
                        "kind": "tile" if "tile" in p.parts else p.suffix.lstrip(".") or "file",
                    }
                )
    return {
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


@app.post("/jobs/batch/status", response_model=BatchStatusResponse)
async def jobs_batch_status(request: BatchStatusRequest) -> BatchStatusResponse:
    """Return compact status for many jobs in one call.

    Body: ``{"job_ids": ["abc", "def", ...]}``. Useful for dashboards that
    want to refresh N tiles without N HTTP round-trips.
    """
    if not request.job_ids:
        raise HTTPException(status_code=400, detail="job_ids must be a non-empty list")
    statuses = JOB_MANAGER.batch_status([str(i) for i in request.job_ids])
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
    snapshot = JOB_MANAGER.get_snapshot(job_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="Job not found")
    structured = JOB_MANAGER.get_structured_result(job_id)
    return StructuredResult(**structured)


@app.get("/jobs/{job_id}/result.md")
async def job_markdown(job_id: str, raw: bool = False) -> PlainTextResponse:
    """Return the stitched Markdown.

    With ``?raw=true`` we strip provenance comments
    (``<!-- source: tile_i, y=..., sha256=..., scale=... -->``) so the agent
    gets a clean input. The default keeps provenance for audit/UI.
    """
    try:
        markdown = store.read_markdown(job_id)
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
async def embeddings_search(
    job_id: str, payload: EmbeddingSearchRequest
) -> EmbeddingSearchResponse:
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
        events.append(("log", f'<li class="text-red-500">{error}</li>'))
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
