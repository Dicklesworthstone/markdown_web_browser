"""Entry point for the FastAPI application."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import AsyncGenerator

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from app.dom_links import blend_dom_with_ocr, demo_dom_links, demo_ocr_links, serialize_links
from app.schemas import (
    EmbeddingSearchRequest,
    EmbeddingSearchResponse,
    SectionEmbeddingMatch,
)
from app.store import build_store

BASE_DIR = Path(__file__).resolve().parent.parent
WEB_ROOT = BASE_DIR / "web"

app = FastAPI(title="Markdown Web Browser")
app.mount("/static", StaticFiles(directory=WEB_ROOT), name="static")
store = build_store()


def _demo_manifest_payload() -> dict:
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


@app.get("/jobs/demo/stream")
async def demo_job_stream(request: Request) -> StreamingResponse:
    """Emit a deterministic SSE stream so the frontend can exercise UI wiring."""

    async def event_generator() -> AsyncGenerator[str, None]:
        manifest_payload = _demo_manifest_payload()
        updates = [
            ("state", "<span class=\"badge badge--info\">CAPTURING</span>"),
            ("progress", "4 / 12 tiles"),
            ("runtime", "CfT Stable-1 · Playwright 1.55.0"),
            (
                "rendered",
                "<article><h3>Demo Article</h3><p>The Markdown preview updates live as OCR tiles finish.</p></article>",
            ),
            (
                "raw",
                "# Demo Article\n\nThis Markdown block mirrors OCR output. Tile provenance comments will appear inline.\n",
            ),
            (
                "manifest",
                json.dumps(manifest_payload),
            ),
            (
                "links",
                json.dumps(
                    serialize_links(
                        blend_dom_with_ocr(
                            dom_links=demo_dom_links(),
                            ocr_links=demo_ocr_links(),
                        )
                    )
                ),
            ),
            (
                "artifacts",
                json.dumps(
                    [
                        {"id": "tile_00", "offset": "y=0", "sha": "a1b2c3"},
                        {"id": "tile_01", "offset": "y=1100", "sha": "d4e5f6"},
                    ]
                ),
            ),
            (
                "log",
                "<li>00:00:01 — Started viewport sweep (1280×2000, overlap 120px)</li>",
            ),
            (
                "log",
                "<li>00:00:02 — Tile t0 sha256 a1b2 captured · SSIM warm-up</li>",
            ),
            (
                "log",
                "<li>00:00:03 — OCR queued 6 tiles · remote policy olmocr-2-7b-1025-fp8</li>",
            ),
            ("state", "<span class=\"badge badge--warn\">OCR_WAITING</span>"),
            ("progress", "9 / 12 tiles"),
        ]

        for event_name, payload in updates:
            yield f"event: {event_name}\ndata: {payload}\n\n"
            await asyncio.sleep(0.75)
            if await request.is_disconnected():
                return

        heartbeat = 0
        while True:
            heartbeat += 1
            yield "event: log\n"
            yield f"data: <li>Heartbeat {heartbeat}: awaiting final stitch…</li>\n\n"
            await asyncio.sleep(4)
            if await request.is_disconnected():
                return

    return StreamingResponse(event_generator(), media_type="text/event-stream")

@app.get("/jobs/demo/links.json")
async def demo_links() -> list[dict[str, str]]:
    """Return sample links JSON to unblock UI + agents while backend matures."""

    blended = blend_dom_with_ocr(dom_links=demo_dom_links(), ocr_links=demo_ocr_links())
    return serialize_links(blended)
