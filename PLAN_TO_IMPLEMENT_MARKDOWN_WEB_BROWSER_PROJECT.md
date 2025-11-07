# PLAN_TO_IMPLEMENT_MARKDOWN_WEB_BROWSER_PROJECT.md

> Goal: Build a "markdown web browser" that renders any URL in a headless Chromium, captures the fully laid-out page as images, sends those images to an OCR-to-Markdown model (olmOCR via remote API or local), and shows the resulting Markdown (raw and rendered). Optimized for agent consumption, reproducibility, longevity, and caching.

---

## 0. Philosophy & Key Decisions

- **Screenshot-first, PDF-never by default.** Capture the exact pixels that end users see (no print CSS, no pagination artifacts). PDF capture stays opt-in for archival workflows only.
- **Tiled capture at model-native resolution.** olmOCR’s model card is clearest when the longest side is ~1288 px. We tile long pages into overlapping images at this size, then down/up-scale as needed.
- **Deterministic, inspectable pipeline.** Every run emits `artifact/` (PNGs), `out.md`, `manifest.json` (UA, viewport, scroll policy, capture code versions), and `links.json` (DOM-extracted anchors/forms).
- **Agent-friendly extras.** Beyond Markdown we export a structured JSON index of links, headings, tables, and artifact paths so agents without vision can act immediately.
- **Local-first dev, remote-first inference.** MVP leans on the public olmOCR inference server; we progressively add official local GPU inference for privacy and throughput.
- **2025Q4 upgrade focus.** We now maintain a living “incremental upgrade” addendum (Section 19) that captures the highest-ROI improvements such as Chrome for Testing pinning, pyvips tiling, HTMX SSE, sqlite-vec indexing, and olmOCR-2 FP8 defaults.

---

## 1. Target Stack

### 1.1 Backend
- **Language/Runtime:** Python 3.13
- **Env/Packaging:** `uv` + `pyproject.toml` (strict hashes, lockfile) with `--python 3.13`
- **Web framework:** FastAPI (ASGI)
- **ASGI servers:** Uvicorn by default; Granian optional for higher-throughput deployments (see Section 19.8.1)
- **Browser automation:** Playwright (Chromium channel pinned to Chrome for Testing) with persistent profiles per user
- **Image processing:** Pillow for edge formats + libvips (`pyvips`) for fast resizes/tiling/PNG encode tuning
- **Job orchestration:** asyncio task graph (MVP) → optional move to `Arq`/`RQ` later
- **Cache/Store:** Content-addressed filesystem layout, Git LFS optional; metadata in SQLite/SQLModel + sqlite-vec for semantic retrieval (Section 19.6)
- **Auth/Keys:** `.env` via `python-decouple` (olmOCR API key), optional OAuth for authenticated browsing profiles later

### 1.2 Frontend
- Static HTML served by FastAPI
- Alpine.js for state + HTMX for declarative swaps/streaming
- Tailwind CSS for layout
- Lucide SVG icons (vanilla SVGs to keep stack light)
- Client Markdown render via `markdown-it` (server prerender via `md4c`/`mistune` optional)
- highlight.js for code blocks
- HTMX SSE extension once Section 19.7.1 lands

### 1.3 OCR/VLM
- Primary: Remote olmOCR (`olmOCR-2-7B-1025`), now defaulting to FP8 variant for throughput
- Optional: Local inference using the official toolkit on vLLM/SGLang, exposed via the same HTTP client interface
- Model policies encoded in `models.yaml` (long side target, prompt template, FP8 preference, concurrency caps)

---

## 2. High-Level Architecture

```
┌─────────────┐  URL  ┌──────────────┐  tiles/png  ┌───────────────┐  md pieces  ┌──────────────┐
│  Web UI     ├──────►│  API: /jobs  ├────────────►│  Tile Builder │────────────►│  OCR Client  │
└─────┬───────┘       └──────┬───────┘             └───────┬───────┘            └──────┬──────┘
      │        SSE/ws        │ capture events                │ parallel submits          │ merge
      │                      ▼                               ▼                          ▼
      │                 ┌────────┐                    ┌───────────────┐          ┌────────────┐
      │                 │ Playwr │  DOM snapshot      │ Cache/Content │◄─────────┤  Stitcher  │
      │                 │  head  │────links.json─────►│ Store (FS+DB) │          └────┬───────┘
      │                 └────────┘                    └───────────────┘               │
      │                                                                         out.md │
      ▼                                                                                ▼
┌──────────────┐                                                               ┌──────────────┐
│ Progress bar │◄──────────────────────────────────────────────────────────────┤  CLI/API     │
└──────────────┘   SSE/JSONLines updates, events for agents                    └──────────────┘
```

Key flows:
1. User enters a URL and `POST /jobs` creates a job while SSE immediately streams state updates.
2. Playwright (CfT build) renders the page, executes the scroll policy, hides distractions, and captures deterministic viewport sweeps instead of brittle `full_page` shots.
3. Tile Builder slices/rescales to 1288-long-side tiles with ≈120 px overlap, recording offsets, SHA256, CfT version, and screenshot-style hashes.
4. OCR Client submits tiles concurrently with back-pressure to remote FP8 endpoints or local toolkit adapters, using retries and policy-driven concurrency caps.
5. Stitcher merges Markdown segments, normalizes headings via DOM cues, fixes hyphenation, and appends provenance comments plus the DOM-derived Links Appendix.
6. Viewer/CLI surfaces rendered vs raw Markdown, artifacts, links JSON, manifest, embeddings search, and live events for agents.

Key signals:
- Capture + OCR progress via SSE (`/jobs/{id}/stream`) and JSONLines feed (`/jobs/{id}/events`).
- Content store keeps tiles, manifests, embeddings, link tables, provenance metadata.

---

## 3. Detailed Pipeline

- **Bootstrap:** Launch headless Chromium pinned to a Chrome-for-Testing build and record both `cft_version` and Playwright version in `manifest.json`.
- **Transport selection:** Default to CDP for CfT Chromium; fall back to **WebDriver BiDi** whenever CDP parity lags (e.g., cross-browser replay, non-Chromium agents, or CfT regressions). Record the active transport in `manifest.browser_transport` and include it in benchmark/run reports.
- **Context:** `viewport=1280×2000`, `deviceScaleFactor=2`, `colorScheme="light"`, `locale="en-US"`, reduced motion, screenshot `animations="disabled"`, and init scripts masking `navigator.webdriver`. Prefer Playwright’s built-in screenshot options (masking, CSS scaling) over bespoke CSS.
- **Version guard:** Require Playwright ≥1.50 so we inherit the built-in animation disabling, `expect(page).toHaveScreenshot()` stability, and locator `hasNot*` filters that keep the viewport sweep deterministic. Capture the exact version in `manifest.playwright_version`.
- **Screenshot config:** Set global defaults in `playwright.config.(ts|mjs)` for `use: { viewport, deviceScaleFactor: 2, colorScheme: "light", screenshot: { animations: "disabled", caret: "hide", maskColor: "#fff", timeout: 15000 } }`. Prefer locator/page `toHaveScreenshot` assertions with CSS mask lists instead of one-off sleep/scroll hacks.
- **Navigation:** `page.goto(url, wait_until="networkidle")` with a firm timeout and cleanup of long-lived sockets.
- **Scroll policy:** Deterministic viewport sweep that scrolls ≈1000 px per step, waits 300–350 ms, caps at 200 steps, and stops once scrollHeight stabilizes twice; uses `IntersectionObserver` sentinels plus SPA height-shrink retries (Section 19.2.4).
- **Stability pass:** Inject selector blocklist CSS via Playwright’s screenshot `style` option to hide cookie banners/sticky headers, pause/mute media, and enforce `prefers-reduced-motion: reduce`.
- **Playwright screenshot APIs:** Lean on `expect(page).toHaveScreenshot()` / `expect(locator).toHaveScreenshot()` for smoke tests and capture assertions so animation freezing, masking, and CSS scaling stay centralized in upstream Playwright code.
- **DOM harvest:** Capture `anchors`, `forms`, `headings`, and `meta` data (title, canonical, og:url, lang, timestamps) into `links.json` so the Links Appendix and agents can mirror the live DOM.

### 3.2 Tiling & Resizing
- Prefer viewport-sized sweeps over `full_page=True`; if a monolithic bitmap is needed, slice it into vertical strips (~1400 px) with **≈120 px overlap**.
- Downscale/upscale so the longest side is ≤1288 px (policy-controlled), preserving aspect ratio; record original offsets, scale factors, DPI, CfT version, screenshot-style hash, and per-tile `sha256` for reproducibility.
- Use pyvips for slicing/resizing/PNG encode (`Q=9`, palette off, non-interlaced) and fall back to Pillow only for exotic formats; optionally run background `oxipng` to shrink cached artifacts without delaying OCR.

### 3.3 OCR Submission
- **Concurrency:** Start with 4–8 in-flight requests and auto-tune toward `min(8, cpu_count)` based on recent p95 latency and 5xx rate; expose "throttle to N" events in the UI.
- **Retries:** Exponential backoff with jitter, 2–3 attempts for 5xx/timeouts; partial Markdown pages stream as soon as each tile succeeds.
- **API contract:** Configurable base URL, API key header, model name—responses contain Markdown text per tile plus optional token usage metadata.
- **Local fast path:** When `OCR_LOCAL_URL` is set, point the same client at vLLM/SGLang toolkit servers (Section 19.1.2) without changing HTTP semantics.
- **Cost/latency budget:** Track per-tile estimates from previous runs and surface them in the manifest/status bar for better ETA and spend projections.

### 3.4 Markdown Stitching
- Concatenate tile Markdown with deterministic boundary markers `\n\n<!-- tile:{i} offset:{y} -->\n\n`, upgrading to `source:` comments per Section 19.4.3.
- Trim duplicate fragments in overlaps by fuzzy matching the last N lines of tile *i* with the first N lines of tile *i+1*, assisted by SSIM on the ≈120 px overlap strips.
- Normalize heading levels using the DOM heading outline so `DOM: h2 -> OCR: ####` becomes a canonical H2 while preserving the original text in an HTML comment for provenance.
- Merge tables that span tiles only when header rows repeat and overlap SSIM stays high; otherwise keep them separate to avoid over-merging.
- Apply conservative hyphenation fixes (`word-\nbreaks` → `wordbreaks`) when the next line begins with lowercase characters.
- Append a **Links Appendix** grouping anchors by domain with `[text](href)` entries, forms summary, and `meta` block sourced from `links.json`.

### 3.5 Caching & Reproducibility
- **Directory layout:** `cache/{host}/{path_slug}/{yyyy-mm-dd_HHMMSS}/` with `artifact/full.png` (optional), `artifact/tiles/tile_{index:04d}.png`, `out.md`, `links.json`, `manifest.json`.
- **Database:** `runs.db` (SQLite/SQLModel/sqlite-vec) stores `runs(id, url, started_at, finished_at, status, cache_path, sha256_full, tiles, ocr_provider, model)` plus `links(run_id, href, text, rel, type)` and section embedding vectors.
- **Git/LFS (optional):** Commit Markdown outputs, store heavy artifacts via LFS so CI can diff Markdown over time.
- **Content addressing:** Key caches by `(normalized_url, cft_version, viewport, deviceScaleFactor, model_name, model_rev)` to prevent mismatches when browsers or models change.
- _2025-11-08 — PinkCreek (bd-ug0) added `scripts/check_env.py` so CI/smoke jobs can fail fast when `.env` is incomplete (API_BASE_URL, CfT pin, olmOCR endpoints, concurrency caps)._

---

## 4. API Surface

### 4.1 Core Endpoints
> _Status 2025-11-08 — BlackPond (bd-3px) implemented the `/jobs` REST + SSE contract: job creation, polling, `/jobs/{id}/stream`, artifact routes, and `/jobs/{id}/embeddings/search` are now live against the FastAPI app/stores._
- `POST /jobs` → `{id}`
  - Body: `{url, profile_id?, capture_mode:"screenshot"|"pdf"|"auto", ocr:{provider, model}, viewport?, scroll?, reduce_motion?, local_ocr?:bool}`
- `GET /jobs/{id}` → job snapshot
- `GET /jobs/{id}/stream` → SSE stream (HTMX-ready)
- `GET /jobs/{id}/events` → JSONLines feed for agents/CLI tailing
- `GET /jobs/{id}/result.md` → final Markdown
- `GET /jobs/{id}/links.json` → anchors/forms/headings/meta
- `GET /jobs/{id}/artifact/{name}` → images/PDF
- `POST /jobs/{id}/webhooks` → register callback URLs (defaults to DONE/FAILED notifications)
- `POST /replay` → re-run with same manifest but different OCR/tiling policy

_2025-11-08 — FuchsiaPond (bd: markdown_web_browser-t82) implemented real `POST /jobs` + `GET /jobs/{id}` routes via the new JobManager, so capture requests now persist manifests/tiles in `Store` and expose snapshots for the UI. SSE + events remain on the roadmap._
_2025-11-08 — JobManager now drives `/jobs/{id}/stream`, so the HTMX SSE endpoint emits live snapshot JSON (state, progress, manifest path) instead of the demo feed; `/jobs/{id}/events` now serves newline-delimited snapshots for CLI/agent consumption. The UI “Run Capture” button posts to `/jobs` and auto-attaches the stream, and `scripts/mdwb_cli.py` exposes real `stream`/`events` commands for job monitoring._

### 4.2 Job States
1. `BROWSER_STARTING`
2. `NAVIGATING`
3. `SCROLLING`
4. `CAPTURING`
5. `TILING`
6. `OCR_SUBMITTING`
7. `OCR_WAITING`
8. `STITCHING`
9. `DONE` | `FAILED`

Each state carries timestamps, counters (tiles done/total), errors, CfT version, and concurrency throttle notes.

---

## 5. Frontend UX

- **Top bar:** Back/Forward, URL field, search, Go, profile chooser, capture mode, OCR policy dropdown (`olmOCR-2-7B-1025-FP8` default), local/remote toggle.
- **Main pane tabs:** Rendered Markdown | Raw Markdown | Artifacts | Links | Manifest | Embeddings (new) | Events tail.
  - Rendered Markdown: sanitized HTML preview with DOM-guided heading normalization; suspicious spans show DOM patch hints.
  - Raw Markdown: `<pre>`/Monaco with copy/export buttons.
  - Artifacts: tile gallery + highlight toggles (tile id, y-offset, SHA, scale).
  - Links: domain-grouped table with quality badges (DOM vs OCR coverage, target rel attributes).
  - Manifest: JSON viewer with CfT/Playwright versions, screenshot style hash, long-side px, model revision, concurrency autopilot data.
  - Embeddings: SQLite-vec powered section search + "jump to tile" action.
  - Events: SSE log mirrored in HTMX.
- **Bottom status bar:** Live state, tile progress, concurrency autopilot notes, warnings (canvas-heavy tile, overlay blocklist, FP8 preference), SSE health indicator.
- **Keyboard & accessibility:** Global shortcuts (`g` to focus URL, `cmd/ctrl+enter` to run, `[`/`]` to swap tiles) plus ARIA landmarks around tabs ensure agents or screen readers can follow streaming updates; status bar exposes polite live regions for SSE text.
- **Microcopy & cues:** Each card shows whether content came from OCR vs DOM patching (badge: “DOM assist”), tile cards include provenance hover showing `tile_i`, `offset`, `sha256`, and CfT version; manifest tab highlights mismatches (e.g., CfT drift) in amber to prompt re-run.
- **Link actions:** Table rows have inline buttons for “Open in new job”, “Copy anchor Markdown”, and “Mark as crawled”. When the DOM vs OCR delta exceeds a threshold the row gets a warning icon that links to artifact preview anchored to the tile range.

_2025-11-08 — PurpleDog (bd-rje) wired the HTMX/Alpine shell to the live `/jobs` API: the toolbar now POSTs `/jobs`, stores the returned job id, and points the SSE/links panels at `/jobs/{id}` endpoints with demo fallbacks only when a job id isn’t supplied._

_2025-11-08 — SSE bridge now driven by `web/app.js` EventSource client + `/jobs/demo/stream` manifest events so UI can render live state/progress/runtime/log placeholders before the real `/jobs/{id}/stream` exists._

_2025-11-08 — Demo stream extended with rendered/raw Markdown, artifacts list, and links table events so all tabs can exercise the data contract ahead of bd-3px._

_2025-11-08 — Started bd-ogf scaffolding: `app/dom_links.py` placeholder for DOM harvest + hybrid overlay plus `/jobs/demo/links.json` sample endpoint to unblock Links tab + agent testing._

_2025-11-08 — UI shell can now target arbitrary `/jobs/{id}/stream` endpoints via Job ID selector + EventSource reconnect logic; Links tab also offers a manual refresh that hits `/jobs/{id}/links.json` for testing ahead of real data._

_2025-11-08 — Hardened bd-rje scaffolding: FastAPI now serves `/` + `/static` via absolute paths, artifact list rendering avoids `innerHTML`, and the Run button clearly states that job submission is still stubbed._

_2025-11-08 — `app/dom_links.py` now defines LinkRecord merge/serialization helpers; demo SSE + `/jobs/demo/links.json` pull from this shared logic so backend/UI stay in sync for Link delta contracts._

_2025-11-08 — Added BeautifulSoup-powered DOM snapshot parsing so `extract_links_from_dom()` can return real anchors/forms once capture snapshots land._

---

## 6. Error Handling & Resilience

- Network idle that never resolves → fallback to `domcontentloaded` plus extra wait; record detour in manifest.
- Canvas/WebGL-first content → emit warning banner + attach raw tile thumbnail next to Markdown block.
- Anti-automation overlays → blocklist injection + UI toggle for per-domain overrides.
_2025-11-08 — BrownStone (bd-dm9) introduced JSON-backed selector blocklist + capture warnings; manifests now log `blocklist_hits` + warning codes for SSE/UI surfacing. PinkCreek added `scripts/mdwb_cli.py demo` helpers so the CLI can render these warnings/links using the shared `.env` config._
- Scroll shrink / poor overlap → capture now emits `scroll-shrink` and `overlap-low` warnings whenever viewport sweeps retry due to shrinking SPAs or overlap match ratios fall below the configured threshold (defaults: 1 shrink event, 0.65 ratio). _2025-11-08 — BrownStone (bd-dm9)._
_2025-11-08 — WhiteSnow (bd-dm9) added sweep stats + `validation_failures` to CaptureManifest and the ops warning log so retries and duplicate seams show up even when no DOM warnings fire._
- Server overload → adaptive OCR concurrency, queue visibility, remote/local failover.
- Partial results → stream partial Markdown as tiles finish; mark sections as incomplete with provenance comments.
- Full-page retries → viewport sweep restarts when shrink detected; record both sweeps.
- Lazy-load deadlocks → Instrument the scroll policy to detect when `IntersectionObserver` stops firing despite new requests; escalate to a “force flush” mode that triggers manual `scrollTo` bursts and logs `scroll_height_stuck` in the manifest for later triage.
- OCR flake classification → Tag retries as `timeout`, `5xx`, `throttle`, or `quality_guard`; persist counters per job so ops can spot systemic issues (e.g., remote endpoint throttling) without combing logs.
- Artifact corruption → Validate tile SHA256 before submit and after OCR response; auto-delete/re-shoot corrupted tiles while keeping the previous attempt zipped for debugging.
- _2025-11-08 — BrownStone (bd-dm9) now logs warning/blocklist incidents to `ops/warnings.jsonl` automatically (configurable via `WARNING_LOG_PATH`)._
- _2025-11-08 — BlueCreek (bd-bo2) is wiring the existing `JobManager` event log into `/jobs/{id}/events`, adding heartbeats, and keeping the NDJSON feed tailing future updates so CLI/agents can rely on a continuous history._

---

## 7. Security & Privacy

- Remote OCR bright banner + manifest flag.
- Key storage server-side only; never leak to client.
- Artifacts stored locally with optional auto-purge TTL per workspace.
- Chromium runs with hardened flags; per-user persistent profiles supported but opt-in.
- TLS baseline: terminate HTTPS at the FastAPI/ASGI edge with automatic certificate rotation; include CfT version, commit SHA, and job UUID in structured logs for forensic stitching.
- Data retention policy: artifacts default to 30-day retention with per-workspace override; manifest records `purge_at` so downstream agents know when cache entries expire.
- Red/blue profile separation: authenticated browsing profiles live under user-specific directories with OS-level sandboxing; jobs referencing profiles inherit that isolation and record it in `manifest.profile_id`.

---

## 8. Performance Budget

- Tile resolution ≤ 1288 longest side (policy-driven).
- Concurrent OCR requests start at 6, auto-tune toward `min(8, cpu_count)` based on p95 and 5xx rate.
- **Latency targets (p95):**
  - Short page (<5 tiles): ≤8 s end-to-end
  - Medium article (5–15 tiles): ≤25 s
  - Long dashboard (>15 tiles): ≤45 s
  Alert when capture_ms + ocr_ms breaches these budgets twice in 30 min.
- Cache hits show prior Markdown instantly; invalidated by content hash change or CfT/model mismatch.
- HTTP/2 enabled in httpx, gzip Markdown responses, persistent connections.
- Capture telemetry: record `capture_ms`, `tiling_ms`, `ocr_ms`, `stitch_ms`, `links_ms` per job and publish a weekly export so you can spot regressions tied to CfT or model upgrades.
- Throughput budgeting: each queue worker advertises `tiles/sec` based on last 15 minutes; dispatcher uses that to decide when to spawn additional workers or redirect to local inference.
- Artifact size guardrails: track PNG mean/95th percentile sizes per domain; warn when compression drifts, which often signals overlay injections or CSS loops inflating DOM height.

---

## 9. CLI & Agent Interfaces

### 9.1 CLI
```
mdwb fetch https://example.com \
  --out out.md \
  --tiles 1288 --overlap 120 \
  --concurrency 6 \
  --ocr.server $OLMOCR_URL --ocr.key $OLMOCR_KEY --ocr.model olmOCR-2-7B-1025-FP8
```

_2025-11-08 — PurpleDog (bd-dwf) added the initial Typer/Rich CLI scaffold (`scripts/mdwb_cli.py`) with demo `snapshot/links/stream/watch/events/warnings` commands powered by the `/jobs/demo/*` endpoints (including `--json`/`--raw` output + manifest warning/blocklist rendering), `mdwb dom links` for offline DOM snapshot parsing, and the first real `/jobs` commands (`mdwb fetch/show/stream/watch`) wired to the JobManager SSE feed / polling API (fetch supports `--watch`) while `mdwb watch` falls back to polling until `/jobs/{id}/events` lands._
_2025-11-08 — BlueCreek (bd-e1x) fixed the `mdwb images` workflow so it no longer references `files` before assignment; the command now validates directories up front, short-circuits when empty, and reuses the computed manifest for preconvert/resume._

_2025-11-08 — RunPaths now tracks `dom_snapshot_path` and the capture pipeline writes each job’s DOM HTML plus extracted `links.json` to disk (`Store.write_dom_snapshot` + `write_links`), with `GET /jobs/{job_id}/links.json` + `mdwb dom links --job-id …` surfacing the data._

_2025-11-08 — BrownStone (bd-dm9) added `mdwb warnings tail` to read the new warning/blocklist JSONL log (`WARNING_LOG_PATH`)._

_2025-11-08 — BrownStone (bd-dm9) expanded the manifest + `/jobs` schema so blocklist hits and structured capture warnings flow through snapshots/SSE; demo endpoints + UI now render the warning pills, paving the way for real `/jobs` events once 3px is wired._

### 9.2 Agent JSON Contract
```
POST /jobs { url, options }
→ { id }
GET  /jobs/{id}/status
→ { state, progress:{done,total}, links_uri, markdown_uri, artifacts:[...], embeddings:{db, section_ids} }
```

### 9.3 Event Stream
_2025-11-08 — OrangeMountain (bd-3px) wired a persistent event log so `/jobs/{id}/events?since=<iso>` serves NDJSON snapshots and `/jobs/{id}/webhooks` registers signed callbacks (header `X-MDWB-Signature`). `scripts/mdwb_cli.py events` now tails the JSON feed for automation._
- SSE: `event:state`, `event:tile`, `event:warning`
- JSONLines: newline-delimited objects mirroring SSE for CLI `--follow`

### 9.4 Agent Hooks & Webhooks
- `/jobs/{id}/webhook` registration lets external agents receive signed POST callbacks on state transitions (same payload as SSE events) so automation pipelines do not need to poll.
- `/jobs/events/subscribe` returns an API key-scoped cursor for bulk consumption; agents can request `filter=warnings` to only receive anomaly events (canvas, overlay, retry spree).
- CLI supports `mdwb watch <id> --on tile 'echo "tile done"'` for quick local automation, mirroring the webhook payloads.

---

## 10. Repository Layout

```
markdown-web-browser/
  pyproject.toml
  README.md
  .env.example
  PLAN_TO_IMPLEMENT_MARKDOWN_WEB_BROWSER_PROJECT.md
  app/
    main.py               # FastAPI app (routes, SSE, static)
    jobs.py               # job model/state machine
    capture.py            # Playwright session, scroll policy, screenshot
    tiler.py              # tile slicing, resizing, hashing, pyvips helpers
    ocr_client.py         # remote/local OCR adapters + policy lookup
    stitch.py             # stitching heuristics, SSIM overlap merge
    store.py              # cache paths, sqlite/sqlite-vec models
    embeddings.py         # section embeddings, sqlite-vec helpers
    schemas.py            # Pydantic DTOs
    settings.py           # config
  web/
    index.html            # Alpine/HTMX/Tailwind UI
    tailwind.css
    app.js                # Alpine stores, SSE + JSONLines handling
    icons/                # Lucide SVGs
  tests/
    test_e2e_small.py
    test_tiling.py
    test_stitch.py
    test_scroll_policy.py
    test_embeddings.py
    test_manifest_contract.py
    fixtures/
  scripts/
    dev_run.sh
    replay_job.sh          # convenience wrapper for /replay
    olmocr_cli.py          # copied from /data/projects/olmocr, orchestrates hosted olmOCR runs
  ops/
    dashboards.json       # Grafana/Metabase tiles for latency + throughput
    alerts.md             # runbook for paging on capture/ocr failures
  docs/
    architecture.md
    blocklist.md          # selector blocklist governance
    models.yaml           # OCR policy table referenced in Section 1.3
    olmocr_cli.md         # adapted from AUTOMATED_OLMOCR_CLI_TOOL_DOCUMENTATION.md (remote CLI usage)
```

Authoritative references:
- `PLAN_TO_IMPLEMENT_MARKDOWN_WEB_BROWSER_PROJECT.md` — canonical architecture + ops playbooks.
- `docs/architecture.md`, `docs/blocklist.md`, `docs/models.yaml`, `docs/config.md` — supporting specs.
- `docs/olmocr_cli.md` — CLI how-to (copied from `/data/projects/olmocr/AUTOMATED_OLMOCR_CLI_TOOL_DOCUMENTATION.md`).

---

## 11. `pyproject.toml`

```toml
[project]
name = "markdown-web-browser"
version = "0.1.0"
description = "Render any URL to Markdown via tiled screenshots + olmOCR"
requires-python = ">=3.13"
dependencies = [
  "fastapi>=0.115",
  "uvicorn[standard]>=0.30",
  "granian>=2.1.2",
  "playwright>=1.48",
  "pillow>=11.0",
  "pyvips>=2.2",
  "python-decouple>=3.8",
  "pydantic>=2.8",
  "jinja2>=3.1",
  "httpx>=0.27",
  "sqlmodel>=0.0.22",
  "sqlite-vec>=0.1.1",
  "mistune>=3.0",
  "prometheus-client>=0.20",
  "structlog>=24.1",
]

[project.optional-dependencies]
local-ocr = [
  "vllm>=0.6.0",
  "sglang>=0.3.0",
  "olmocr>=0.4.0",
]

observability = [
  "opentelemetry-sdk>=1.28",
  "opentelemetry-exporter-otlp>=1.28",
]

[tool.uv]
dev-dependencies = [
  "pytest>=8.3",
  "pytest-asyncio>=0.24",
  "ruff>=0.6",
  "oxipng>=9.0",
]

[tool.ruff]
line-length = 100

[tool.pytest.ini_options]
asyncio_mode = "auto"

Observability extras wire FastAPI + Uvicorn/Granian metrics into Prometheus via `prometheus-client` and optionally export richer traces to OTLP backends; `structlog` keeps JSON logs consistent with the manifest metadata fields.
```

---

## 12. Minimal `.env.example`

_Status 2025-11-08 — BrownStone (bd: markdown_web_browser-37t) added structured settings + manifest schema, synced `.env.example`, and is finishing docs/config + manifest notes before handing ownership back to PurplePond._

```
OLMOCR_SERVER=https://ai2endpoints.cirrascale.ai/api
OLMOCR_API_KEY=sk-***
OLMOCR_MODEL=olmOCR-2-7B-1025-FP8
OCR_LOCAL_URL=http://localhost:8001
OCR_USE_FP8=true
CACHE_ROOT=.cache
CFT_VERSION=chrome-130.0.6723.69
PLAYWRIGHT_CHANNEL=cft
PROMETHEUS_PORT=9000
HTMX_SSE_HEARTBEAT_MS=4000

# Optional overrides for local inference + concurrency autopilot
OCR_MAX_CONCURRENCY=8
OCR_MIN_CONCURRENCY=2

Document every new env var inside `docs/config.md` so operators know how CfT pinning, SSE heartbeat, and concurrency limits interplay; manifests should echo the effective values for auditability.
```

---

## 13. Milestones

### M0 — Headless Capture + OCR (CLI only)
- Playwright capture, tiling, parallel OCR submit, stitch to `out.md`
- Logs five major states

### M1 — Web UI + SSE
- Alpine/HTMX UI, status bar, raw vs rendered Markdown tabs, artifacts & links panels
- HTMX SSE extension + `/jobs/{id}/events`

### M2 — Profiles, Auth, Robustness
- Persistent Chromium profiles; overlay blocklist; smarter scroll/lazy-load detection
- DOM-guided heading leveling + provenance comments

### M3 — Local Inference & Batch Mode
- Switchable local olmOCR via vLLM/SGLang
- Bulk URL ingestion; throughput metrics dashboard

### M4 — Agent Toolkit & Retrieval
- Clean JSON contract, link/action queueing, sqlite-vec search, simple rules engine for next-URL crawling

---

## 14. Test Plan

_2025-11-08 — PinkCreek (bd-ug0) owning ops/test instrumentation: codifying ruff/ty/Playwright automation + nightly smoke + weekly latency scripts before wiring dashboards and CLI docs._

- **Golden pages:** static docs, sticky headers, SPAs with virtualized lists, huge tables, canvas charts.
- **Assertions:** headings preserved, no duplicate sections at seams, DOM vs OCR link delta < 10%, tables recognized, provenance comments present.
- **Perf:** p50/p95 timings, retries ≤2 per tile, memory footprint below threshold on 10k px pages.
- **New guards:** CfT pinning test, viewport sweep regression (full-page omission repro), table split fuzzer, scroll stabilization harness.
- **Generative E2E guardrail:** every major feature must have at least one GenIA-E2ETest (or comparable LLM-generated) scenario that exercises scrolling, tiling, OCR, stitching, and manifest logging end-to-end. Keep fixtures for these tests under `tests/test_e2e_generated.py` and fail CI when the Markdown diff exceeds 2%.

---

## 15. Roadmap Enhancements

- Layout hints: inject invisible DOM watermarks to help tiling seam detection.
- Semantic post-processing: optional LLM pass to fix broken lists/tables.
- Citations/back-links: embed `<!-- source:tile_i offset_y -->` comments for provenance (strengthened via Section 19.4.3).
- Crawl mode: depth-1 link expansion with domain allowlist and concurrency caps.
- PDF capture improvements deferred (Section 19.12).

---

## 16. Rationale: Why Screenshot-First Wins

- **Fidelity:** exact pixels as seen by users; print CSS often hides nav/asides or reflows content.
- **Dynamic content:** canvas charts, lazy-loaded images, client-rendered components are captured accurately.
- **Predictable OCR scale:** we control resolution/tiling; no multi-page print pagination artifacts.
- **Simplicity:** one representation (PNG tiles) across all sites; fewer one-off workarounds.
- **Agent context:** DOM-derived Links Appendix + sqlite-vec search compensates for OCR’s lack of clickable anchors.

---

## 17. Open Questions (MVP validation)

- Optimal overlap/window for seam dedup across diverse typography when SSIM heuristics are in play.
- Whether hyphenation fixes harm code blocks/inline math; need guardrails/tests.
- Best concurrency vs rate-limit curve for remote FP8 endpoint vs local toolkit.
- How much DOM text patching (Section 19.5) we can do before hallucinating mismatches.

---

## 18. Quickstart (Developer)

1. `uv venv --python 3.13 && uv sync`
2. `playwright install chromium`
3. `cp .env.example .env` and fill in API keys
4. `uv run python -m app.main` → open `http://localhost:8000`

---

## 19. Incremental Upgrade Plan (2025 Q4)

Below is a focused, pragmatic list of near-term upgrades. They map to the sections above so you can implement them without re-architecting.

### 19.1 OCR & Inference Upgrades (hosted olmOCR focus)
- **Single provider clarity.** `olmOCR-2-7B-1025-FP8` via the hosted API is the only supported model for now. Keep the UI dropdown but mark other entries “future” to avoid confusion.
- **Batching & concurrency.** Size OCR batches so each request stays <25 MB and target 6–8 in-flight requests (tunable per latency telemetry). Track per-request latency, HTTP status, and token usage in the manifest so you can correlate spikes with page complexity.
- **Quota & retry budgets.** Implement token-rate alarms: warn when daily usage hits 70 % of quota, and cap retries at 2 with exponential backoff (3s, 9s). Log the remote request ID in the job manifest to speed up vendor support.
- **Fallback playbook.** When the hosted API throttles or errors persist >5 minutes, surface a UI banner (“OCR degraded”) and queue jobs rather than silently failing. Document switch-over steps (pause new jobs, notify vendor, resume once healthy) in the ops runbook.

### 19.2 Browser Capture Reliability & Determinism
> _Status 2025-11-08 — FuchsiaPond (bd: markdown_web_browser-t82) wired CfT pinning + viewport sweep instrumentation, including SPA shrink detection with configurable retry limits recorded in manifests._
- **Chrome for Testing binaries.** Switch Playwright to CfT channel, log `cft_version` in `manifest.json`, and expose a health check/test that ensures the pinned version is installed.
- **CfT label + build.** When Google publishes label tracks (e.g., `Stable`, `Stable-1`, `Stable-2`), record both the label and exact build number for every run. Use labels for day-to-day pinning (easy rollbacks) and fall back to explicit builds when diagnosing regressions; keep both in the manifest and ops dashboards.
- **Viewport sweep instead of `full_page=True`.** Implement deterministic viewport-sized tiling with overlap stitching to avoid missing elements based on scroll position.
- **CDP vs BiDi fallback.** Default to CDP for CfT Chromium but support WebDriver BiDi when CDP features lag or when cross-browser capture is required. Surface the chosen transport in manifests/tests so issues can be bisected quickly.
- **Playwright ≥1.50 + normalized rendering.** Require Playwright 1.50 or newer for built-in animation freezes (`animations:"disabled"`), `expect(page).toHaveScreenshot()` assertions, and locator `hasNot*` filters. Always set reduced motion, disable animations, fix `deviceScaleFactor=2`, enforce `colorScheme="light"`, and inject blocklist CSS via screenshot `style`. Record `playwright_version` and `screenshot_style_hash` in manifests.
- **Scroll policy revamp.** Use scrollHeight + network-idle stabilization with IntersectionObserver sentinels; retry if SPA shrinks height mid-run.

### 19.3 Tiling, Image IO, and Compression
> _Status 2025-11-08 — FuchsiaPond (bd: markdown_web_browser-t82) implemented pyvips-based tiler enforcing 1288 px longest side + 120 px overlap, plus top/bottom overlap hashes for future SSIM seam trimming._
- Enforce 1288 px longest side (downscale if necessary) and track original DPI/scale.
- Migrate heavy operations to pyvips, retaining Pillow only for niche formats.
- Tune PNG encode (Q=9, palette off) and optionally run background `oxipng` to trim caches.
- Compute SSIM in overlap strips (≈120 px) to auto-trim duplicate lines.

### 19.4 Stitching: Safer Merges & Provenance
- DOM-guided heading leveling that stores the original line in an HTML comment right above normalized content.
- Table merge heuristics keyed on repeated header rows + high SSIM; otherwise keep blocks separate.
- Upgrade provenance comments to `<!-- source: tile_i, y=1234, sha256=..., scale=2.0 -->` and add `/jobs/{id}/artifact/... ?highlight=tile_i,y0,y1` viewer helpers.

### 19.5 Hybrid Text Recovery (opt-in)
- Detect low-confidence OCR regions (symbol rate, low alpha ratio, hyphen density) and patch them with DOM text overlays scoped to the offending block (hero headings, captions, icon fonts).

### 19.6 Caching, Indexing, Retrieval Quality

> **Progress — 2025-11-08 (BlackPond):** Persistence layer + embeddings search endpoints are live. `app/store.py`, `app/embeddings.py`, and `app/main.py` now expose sqlite-vec backed upsert/search helpers plus `/jobs/{id}/embeddings/search`, with docs/config updated to match.
> _Status 2025-11-08 — FuchsiaPond (bd: markdown_web_browser-t82) integrated the capture pipeline with `Store`, so manifests + tiles are written to cache directories and RunRecord metadata updates automatically once captures finish._
- Add sqlite-vec section embeddings keyed by `(run_id, section_id, tile_range)` for instant "jump to section" queries.
- Content-address caches by `(normalized_url, cft_version, viewport, deviceScaleFactor, model_name, model_rev)`.
- Offer tar.zst bundle downloads of `artifact/`, `out.md`, `links.json`, `manifest.json` using Zstandard.

### 19.7 API & UI Ergonomics
- Integrate the official HTMX SSE extension so markup stays declarative (`hx-ext="sse" hx-sse="connect:/jobs/{id}/stream swap:progress"`).
- Add `/jobs/{id}/events` JSONLines feed for agents/CLI `--follow` workflows.
- Enhance the Links tab with per-domain grouping, target rel column, and "DOM vs OCR delta" badge.

### 19.8 Performance & Concurrency
- Offer Granian as an alternative ASGI server, document when to use it, and expose toggles in ops docs.
- Implement concurrency autotune: start at `min(8, cpu_count)` OCR in-flight requests, adjust based on p95 + 5xx rate, and emit "throttle to N" events in UI.
- Enable HTTP/2 in httpx, reuse connections, gzip Markdown streams.

### 19.9 Robustness & Anti-Flakiness
- Maintain versioned CSS/selector blocklists (JSON) with per-domain overrides; inject via screenshot style or `page.addStyleTag`.
  - Detect canvas/video-heavy tiles and show warnings plus raw tile thumbnails near Markdown blocks.
  - Retry viewport sweeps when shrinkage detected; record both sweeps in manifest.
  _2025-11-08 — BrownStone (bd-dm9) wired `config/blocklist.json` + `app.blocklist` loader into capture. Manifest now records blocklist version, per-selector hit counts, and warning codes (`canvas_heavy`, `video_overlay`, `sticky_chrome`)._

### 19.10 Test Plan Additions

_2025-11-08 — PinkCreek (bd-ug0) threading the new guards into automated runners (ruff/ty, viewport sweep regression, smoke capture) plus nightly/weekly job wiring._
- CfT pinning test (manifest vs installed binary).
- Full-page omission regression test using known repro page from Playwright issue tracker.
- Table split fuzzer for SSIM + header repetition logic.
- Scroll stabilization harness that mocks pages with height bursts.

### 19.11 Doc & Config Tweaks
- `pyproject.toml`: add `[project.optional-dependencies.local-ocr]`, ensure `granian` present.
- `manifest.json`: include CfT version, Playwright version, screenshot style hash, model revision, long-side px.
- `.env.example`: add `OCR_LOCAL_URL` and `OCR_USE_FP8`.

### 19.12 Deferrals (Nice-to-have)
- PDF capture improvements remain optional (use `Page.printToPDF` only when explicitly requested).
- Semantic post-edit stays behind a flag; olmOCR-2 already improves math/tables—avoid over-editing by default.

### 19.13 Why These Upgrades Matter
- Strict alignment with olmOCR-2 guidance (1288 px, FP8) yields instant quality/perf boosts.
- CfT pinning + viewport sweeps remove flaky captures and make runs reproducible.
- pyvips and sqlite-vec drive measurable throughput and retrieval wins without new infra.
- HTMX SSE + `/events` keep the frontend tiny and agents happy.

### 19.14 Hosted olmOCR CLI integration

_2025-11-08 — PinkCreek (bd-ug0) replaced the placeholder with the Typer/Rich CLI plus API_BASE_URL/MDWB_API_KEY plumbing; next up is wiring nightly smoke + weekly latency harnesses._
- **Scripts + docs.** Copy `scripts/olmocr_cli.py` and `docs/olmocr_cli.md` from `/data/projects/olmocr/` (the Typer CLI + documentation) into this repo. Treat them as the blessed way to run batch jobs or debug hosted OCR latency outside the web UI.
- **Config surface.** Keep the CLI defaulting to the hosted API URL; expose knobs for `--server-url`, `--workers`, and `--tensor-parallel-size` but document that TP>1 is only relevant if we later reintroduce local inference.
- **Ops usage.** Point on-call guides to `olmocr_cli.py run --workspace …` for reproducing customer issues quickly, since it already filters noisy logs and streams ETA.
- **Maintenance.** When we upgrade dependency pins (PyTorch, FlashInfer, etc.) in the CLI’s source repo, mirror the updates here so the instructions never drift.

References: [1]–[12] (Hugging Face model card, olmOCR paper, Allen AI release notes, Chrome for Testing blog, Playwright issue, Playwright release notes, Scrapfly scroll guide, pyvips docs, sqlite-vec repo, htmx SSE extension, Granian repo, PyPI entry).

---

## 20. Operational Playbooks & Metrics

_2025-11-08 — PinkCreek (bd-ug0) designing ops automation: smoke/latency job runners, Prometheus/Grafana updates, and release checklists for Sections 20.1-20.4._

### 20.1 Capture & OCR SLOs
- **SLO definition:** 99% of jobs finish within 2× the rolling 7-day p95 for that URL category (news, docs, app). Track both capture latency and OCR latency separately.
- **Error budgets:** Dedicate 25% of weekly engineering time to burn-down whenever error budget drops below 90%; prioritize CfT drift, overlay blocklists, or OCR timeout spikes.
- **Budget attribution:** Manifest fields (`capture_ms`, `tiling_ms`, `ocr_ms`, `stitch_ms`) roll up into BigQuery/duckdb so you can attribute regressions to either Playwright upgrades or model changes.

### 20.2 Monitoring & Alerting
- **Metrics:** expose `/metrics` with Prometheus counters and histograms (tiles_processed_total, ocr_retry_total, capture_duration_seconds_bucket). Include labels for model, CfT version, and concurrency tier.
- **Dashboards:** `ops/dashboards.json` ships Grafana panels for scroll stabilization success rate, overlay blocklist hits, SSE heartbeat latency, and DOM vs OCR link delta distributions.
- **Alert rules:**
  - `HighCaptureLatency`: p95 capture_duration > 120s for 15 minutes.
  - `OCRThrottleSpike`: `ocr_retry_total{reason="throttle"}` rate > 5/min.
  - `SSEDrop`: SSE heartbeat gap > 12s (monitor via HTMX ping endpoint).

### 20.3 Release & Regression Process
- **Chrome for Testing pinning:** upgrade CfT monthly; run the viewport sweep regression test plus the full-page omission golden test before merging. Record the CfT tag in `manifest.json` and release notes.
- **Playwright upgrades:** follow the release notes (Section 19 references) and rerun scroll stabilization harness + blocklist snapshots.
- **CfT labels vs builds:** document when labels shift (e.g., Stable → Stable-1) and ensure manifests + dashboards carry both label and build. Roll forward/backward by adjusting labels first; only pin explicit builds when debugging.
- **Model updates:** when bumping olmOCR or policy YAML, regenerate `models.yaml` docs, rerun table split fuzzers, and stage a baseline job set (top 20 URLs) to compare DOM vs OCR link deltas.
- **Canary:** run nightly canaries against 5 representative URLs with both remote FP8 and local toolkit to ensure parity; fail the canary job if Markdown diff > 2% or embeddings drift > 0.1 cosine.
- **Screenshot stabilization checklist:** before tagging a release, verify (a) `[aria-busy=true]` nodes clear, (b) volatile widgets (clocks, tickers, ads) are masked via Playwright config, (c) animation disabling + caret hiding is enabled, and (d) CSS blocklist entries were reviewed/updated (`docs/blocklist.md`). Document completion in release notes.
- **CLI verification:** For capture/OCR fixes, run `scripts/olmocr_cli.py run` against at least two URLs in the production smoke set to ensure the hosted API + CLI workflow stay healthy. Log request IDs in the release notes for traceability.

### 20.4 Incident Response Ladder
1. **Acknowledge:** SSE/UI banner plus PagerDuty page when capture or OCR SLO alert fires.
2. **Triage:** check ops dashboard to identify CfT, blocklist, or OCR backend as culprit; reference manifest samples via `mdwb diag <job id>`.
3. **Mitigate:** toggle concurrency autopilot limits (`OCR_MAX_CONCURRENCY`) or flip to local toolkit if remote is degraded; patch blocklist JSON via admin UI if overlays broke capture.
4. **Postmortem:** write a lightweight retro (Google Doc or `docs/incidents/INC-yyyymmdd.md`) capturing root cause, timelines, manifest snippets, and follow-up tasks (tests, blocklist entries, doc updates).

---

## 21. Appendix: Reference Configs & Snippets

_2025-11-08 — PinkCreek (bd-ug0) will refresh Sections 21.4-21.5 once the ops pipelines + CLI sync land so manifests/CLI examples stay in lockstep._

### 21.1 HTMX SSE Fragment (frontend)
```html
<section
  id="job-status"
  hx-ext="sse"
  hx-sse="connect:/jobs/{{ job_id }}/stream swap:innerHTML"
  class="status-panel">
  <div class="state" aria-live="polite">Waiting for events…</div>
  <progress value="0" max="{{ manifest.tiles_total }}" aria-live="polite"></progress>
</section>
```

### 21.2 sqlite-vec Schema Helper
```sql
CREATE VIRTUAL TABLE IF NOT EXISTS section_embeddings USING vec0(
  run_id TEXT,
  section_id TEXT,
  tile_start INTEGER,
  tile_end INTEGER,
  embedding FLOAT[1536]
);

INSERT INTO section_embeddings
SELECT :run_id, section_id, tile_start, tile_end, vec_from_blob(:embedding)
FROM parsed_markdown_sections;
```

### 21.3 `models.yaml` Sample
```yaml
olmocr-2-7b-1025-fp8:
  provider: ai2
  prompt_template: olmocr_v4
  long_side_px: 1288
  fp8_preferred: true
  max_tiles_in_flight: 8
  stitch:
    table_bias: true
    max_overlap_trim_lines: 6
florence-2-base:
  provider: microsoft
  prompt_template: florence_tabular
  long_side_px: 1536
  fp8_preferred: false
  max_tiles_in_flight: 4
```

### 21.4 Manifest Excerpt
```json
{
  "job_id": "2025-11-07T12-22-18Z-e1a9",
  "url": "https://example.com/article",
  "cft_version": "chrome-130.0.6723.69",
  "playwright_version": "1.48.2",
  "device_scale_factor": 2,
  "long_side_px": 1288,
  "model": "olmOCR-2-7B-1025-FP8",
  "ocr_policy": "olmocr-2-7b-1025-fp8",
  "scroll_policy": {
    "settle_ms": 350,
    "max_steps": 200,
    "intersection_observer": true
  },
  "screenshot_style_hash": "a9134b9",
  "tiles_total": 18,
  "capture_ms": 18234,
  "ocr_ms": 27654,
  "stitch_ms": 1042
}
```

### 21.5 CLI Automation Snippet
```bash
mdwb fetch https://example.com --out out.md --model olmOCR-2-7B-1025-FP8 \
  --events | jq --unbuffered 'select(.event=="warning")'
```

_2025-11-08 — PinkCreek wired `scripts/mdwb_cli.py` into the shared `.env` config so demo commands (snapshot/links/stream) automatically use `API_BASE_URL` + `MDWB_API_KEY`; override with `--api-base` when pointing at staging._

Use these snippets as scaffolding for docs, onboarding, and regression verification—keep them in sync with real code as part of the release checklist (Section 20.3).

---

## 22. Production Smoke Set & Latency Tracking

_2025-11-08 — PinkCreek (bd-ug0) spinning up nightly smoke + weekly latency automation and wiring manifests/log storage per this section._
- `scripts/run_smoke.py` orchestrates nightly captures per `benchmarks/production_set.json` (writing `manifest_index.json` under `benchmarks/production/<date>/` and refreshing `weekly_summary.json`). See `docs/ops.md` for the runbook + verification checklist.
- Use `--dry-run` to exercise the smoke pipeline without hitting `/jobs`; pair with `--seed` (defaults to `0`) to keep synthetic manifests deterministic so dashboard diffs stay stable. The script also mirrors the latest run into `benchmarks/production/latest_{manifest_index,summary}.json|md` so dashboards can read a stable path.

Focus on a curated set of real customer-style URLs instead of synthetic benchmarks. Maintain `benchmarks/production_set.json` listing each URL, category, and target latency.

| Category | Example URLs | p95 budget |
| --- | --- | --- |
| Docs/articles | 5 long-form articles/docs | ≤25 s |
| Dashboards/apps | 5 authenticated-style dashboards (consent overlays, tables) | ≤45 s |
| Lightweight pages | 3 short marketing/help pages | ≤8 s |

### Nightly smoke run
- Capture each URL once per night using the standard pipeline.
- Record `capture_ms`, `ocr_ms`, `stitch_ms`, tile count, CfT label + build, browser transport, OCR request IDs, and diff stats in `benchmarks/production/<date>/<slug>/manifest.json`.
- Compare Markdown vs. last successful run; flag diffs >2 % or link deltas >10 % for review. Store tiles/DOM snapshots alongside manifests for debugging.

### Weekly latency report
- Aggregate nightly data into `benchmarks/production/weekly_summary.json` with p50/p95 per category and list any runs exceeding budgets.
- Use the summary to decide if a release is safe; if all categories meet budgets, ship. If any URL exceeds budget twice in a week, fix before release.

### Agent readiness checklist
1. Nightly production smoke green for 48 hours (no SLA breaches).
2. Weekly summary shows all categories within latency budgets.
3. Generative E2E guardrail tests (Section 14) green.
4. Hosted OCR quota usage <80 % (Section 19.1 telemetry).

Only when all four items are true should you deploy capture/OCR changes.

---

## 23. Launch & Demo Strategy

- **Example gallery:** Maintain `docs/gallery/` with side-by-side “screenshot vs Markdown” for 6–8 representative sites (article, dashboard, consent-heavy flow). Update it whenever you tweak capture/stitching so newcomers instantly see the output quality.
- **Hosted sandbox:** Stand up a rate-limited demo (could be the existing FastAPI UI with auth/rate limits) where users can submit a URL and receive Markdown + artifacts. Even if it queues requests, the hands-on experience drives adoption and social sharing.
- **Agent starter scripts:** Include a `scripts/agents/` folder (or docs section) with ready-to-run examples—e.g., “Summarize a news article via CLI + hosted LLM,” “Generate TODOs from a dashboard”—so agent builders can integrate quickly.
- **Markdown dataset drop:** Periodically publish a small “Markdown Web Browser Corpus” (e.g., 25 popular sites converted) with manifests and links JSON under a permissive license. This gives researchers and agent hackers immediate fodder and shows confidence in the pipeline.
- **Messaging checklist:** When announcing releases, lead with simple proof points (“Any webpage → auditable Markdown with tiling + provenance in <30 s”). Link to the gallery/demo/dataset so people can try it without cloning.
