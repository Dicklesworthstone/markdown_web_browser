# Architecture & Operational Best Practices (Markdown Web Browser)
_Last updated: 2025-11_

## Objectives
- Deterministically convert arbitrary URLs into high-fidelity Markdown for agent ingestion.
- Preserve provenance and reproducibility across browser, tiler, and OCR steps.
- Keep the pipeline observable and debuggable under real-world load.

## Golden Invariants
- **Screenshot-first** (not print/PDF), with **viewport-sweep chunking** for tall pages.
- **Model-aligned scaling**: every tile’s longest side **≤ 1288 px** for olmOCR‑2. 
- **Reproducible browser**: run **Chrome for Testing** (CfT), pin revision, log CfT label + exact build in `manifest.json`. 
- **Async, bounded I/O**: HTTP/2 multiplexing to OCR, token‑bucket rate limit, retries with jitter. 

## High-Level Dataflow

```

URL → Playwright (CfT, viewport=1280×2000, dpr=2, reduced-motion)
→ Viewport sweep (chunk screenshots, settle, no full_page on tall)
→ pyvips tiler (overlap≈120px, downscale to ≤1288 long side)
→ OCR client (HTTP/2, token-bucket, retries) → Markdown segments
→ Stitcher (overlap de-dup, DOM-guided heading normalization,
guarded hyphenation, table seam rules)
→ out.md (+ provenance comments)
→ links.json (anchors/forms/headings/landmarks/meta)
→ manifest.json (browser/versions/policy/timings)

```

### Browser Capture
- **CfT + Playwright**: Prefer CfT for reproducible builds. Record `cft_channel`, `cft_build`, `playwright_version`. 
- **Stability**:
  - `deviceScaleFactor=2`, `colorScheme="light"`, `emulate_media(reduced_motion="reduce")`. 
  - If you also use Playwright Test in smoke suites, its `toHaveScreenshot(animations: 'disabled')` helps freeze visuals; pipeline capture itself should rely on CSS/JS motion suppression, not test-only APIs. 
- **Tall pages**:
  - Use **viewport sweeps** with settle checks; avoid `full_page=True` on very tall pages to sidestep Chrome raster limits that cause truncation/OOM. Start chunking around **~12k px** total height. 
- **Overlay minimization**:
  - Combine a request‑level adblock engine (EasyList/EasyPrivacy + cookie filters) with CSS selector hides for sticky/fixed elements. (See `docs/blocklist.md`.) 

### Tiling & Imaging
- **pyvips** for crop/resize/encode — fastest/lowest memory at large sizes; PNG lossless for OCR (non‑interlaced). 
- **Geometry**: overlap ≈120 px; pre‑downscale each slice so longest side ≤1288 px; record cssY offset, DPR, hashes.
- **Hashes**: internal cache key via `xxh3_128`; provenance via SHA‑256 stored in manifest.

### OCR I/O
- **HTTP/2** `httpx.AsyncClient(http2=True)` to multiplex many small tile requests to the same host. 
- **Rate limit** via token‑bucket per host; retry 408/429/5xx with exponential backoff + jitter; 4xx (non‑429) are terminal per tile. 
- **Model policy** in `docs/models.yaml` controls long‑side px, parallelism, FP8 preference, and prompt template. Default: `olmOCR-2-7B-1025-FP8`. 

### Stitching
- Overlap de-dup using RapidFuzz on trailing/leading line windows; fall back to difflib only when needed. 
- DOM-guided heading normalization uses the captured DOM outline so H-levels match the source document (we still emit `<!-- normalized-heading: … -->` comments for provenance).
- Guard hyphenation fix outside code/math fences; table seam logic only collapses duplicate headers when overlapping tiles share the same SSIM/overlap hash.
- Provenance comments include tile metrics (`y`, `height`, `sha256`, `path`) plus a `/jobs/{id}/artifact/highlight?tile=…` URL so reviewers can jump straight to the source pixels.

### DOM Harvest
- Capture anchors/forms/headings/meta plus **ARIA landmarks** (role=main/nav/etc.), language and direction. Agents use this to prioritize content over chrome. (Use MDN’s IntersectionObserver and landmarks semantics to decide what to fetch next in infinite/virtualized lists.) 

### Caching & Re-runs
- Conditional HEAD/GET with `If-None-Match`/`If-Modified-Since` before recapture. If **304**, reuse last run. 

### Observability
- Expose `/metrics` via **prometheus-fastapi-instrumentator**; export p50/p95 per stage, tiles/sec, 429/5xx rates. Add OTel FastAPI tracing for end‑to‑end spans. 
- Prefer streaming diagnostics over polling: `/jobs/{id}/stream` drives the status bar + HTMX tabs, while `/jobs/{id}/events` provides a cursor-aware NDJSON feed (UI Events tab + `scripts/mdwb_cli.py watch`) with heartbeat entries so dashboards catch stalls immediately.
- The frontend uses the HTMX SSE extension (`hx-ext="sse" sse-connect="…"`) so status cards, warning pills, and Markdown panels update declaratively without bespoke `EventSource` plumbing; reconnect/error handling comes from the extension.

## Deliverables per run
- `artifact/tiles/tile_XXXX.png`
- `out.md` with `<!-- source: tile_i, y=..., height=..., sha256=..., scale=..., path=..., highlight=/jobs/... -->`
- `links.json` (anchors/forms/headings/meta/landmarks)
- `manifest.json` (CfT label+build, DPR, viewport, policies, timings, hashes)
- `section_embeddings` (sqlite-vec vectors) exposed via `/jobs/{id}/embeddings/search`

## Agent starter helpers
- `scripts/agents/summarize_article.py` submits (or reuses) a capture job, waits for completion, downloads `result.md`, and emits the first few sentences of plain text. It reuses the shared CLI settings + HTTP clients, so the standard `.env` values keep working.
- `scripts/agents/generate_todos.py` performs the same capture/poll cycle but looks for checkboxes, bullet lists, and “Next Steps”/“Action Items” headings to emit actionable tasks (text or `--json`).

Both scripts live under `scripts/agents/`, are Typer CLIs, and share helpers (`scripts/agents/shared.py`) that agents can import directly when composing new automations.

## Persistence & Bundles (Plan §§2, 10, 19.4, 19.6)

- **Content-addressed layout** — `RunPaths` buckets captures by cache key so each
  run lives under `{CACHE_ROOT}/{host}/{path_slug}/cache/{key_prefix}/{cache_key}/{yyyy-mm-dd_HHMMSS}/`
  with deterministic `manifest.json`, `out.md`, `links.json`, and `artifact/` children.
  The prefix (`cache_key[:2]`) keeps directory fan-out manageable, and the cache key
  itself lets tooling jump straight from `_build_cache_key()` → filesystem path without
  scraping SQLite first.
- **SQLite metadata (`RUNS_DB_PATH`)** — `RunRecord`/`LinkRecord` tables capture CfT
  label/build, screenshot style hash, OCR policy, concurrency window, timing metrics,
  plus capture breadcrumbs (shrink/retry counts, overlap ratios, validation failure counts)
  so dashboards can surface seam health without scraping manifests. The `section_embeddings`
  virtual table (sqlite-vec) stores
  1536-dim vectors keyed by `(run_id, section_id, tile_start, tile_end)` so agents can
  jump directly to relevant Markdown spans via `Store.search_section_embeddings()` and
  the `/jobs/{id}/embeddings/search` API.
- **Tar bundles** — `Store.build_bundle` streams each run directory through a
  `tarfile` + `zstandard` writer, producing `bundle.tar.zst` next to the artifacts.
  Use this for incident attachments or dataset exports (Plan §19.6).

## Gotchas (and our mitigations)
- **Raster limits on tall pages** → viewport sweeps. 
- **Animated UIs** → reduced-motion + CSS disables; test-time `animations:'disabled'` for smoketests. 
- **Consent overlays** → adblock engine + CSS hide + “retry sweep if height shrinks” heuristic. 

## API Surface (Plan §§2, 4, 9)

- `POST /jobs` — enqueue a capture job; returns an initial `JobSnapshotResponse` with state `BROWSER_STARTING`.
- `GET /jobs/{id}` — latest snapshot (state/progress/manifest path) for orchestration and polling clients.
- `GET /jobs/{id}/stream` — SSE feed emitting `state`, `progress`, `manifest`, `warnings`, and `artifacts` events; used by the HTMX UI and agent tooling.
- `/jobs/{id}/events` — NDJSON stream (with `?since=<iso>`) that replays backlog, streams new sequenced entries, and emits heartbeats every 5 s so CLIs/UIs can detect disconnects.
- Artifact endpoints: `/jobs/{id}/manifest.json`, `/jobs/{id}/links.json`, `/jobs/{id}/result.md`, and `/jobs/{id}/artifact/{path}` expose persisted outputs for CLI/automation and demos.
- `/jobs/{id}/embeddings/search` — cosine similarity search on sqlite-vec section embeddings (used by the Embeddings tab + agents).
- `/warnings` (internal) — warning/blocklist incidents append to `ops/warnings.jsonl`; the `mdwb warnings tail` CLI command reads this log so ops can review capture anomalies without crawling manifests.
