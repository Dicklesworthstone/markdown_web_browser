# Changelog

All notable changes to [Markdown Web Browser](https://github.com/Dicklesworthstone/markdown_web_browser) are documented here.

This project has no formal release tags or GitHub Releases. The changelog covers all 138 commits on the `main` branch (2025-11-07 through 2026-03-11), organized by capability rather than raw chronological order. Each entry links to the relevant commit on GitHub.

---

## Core Capture Pipeline

The foundation of the project: a deterministic, screenshot-first pipeline that turns any URL into provenance-tracked Markdown.

### Initial Implementation (2025-11-07)

- Playwright-based deterministic viewport sweep (Chromium CfT, 1280x2000, DPR 2, reduced motion) with `pyvips` tiler slicing sweeps into <=1288px tiles with ~120px overlap; each tile carries offsets, DPR, and SHA256 hashes ([`7e571b9`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/7e571b9dc59acdfd4662fd9c53afff324f1da9a1))
- OCR client submitting tiles to hosted or local olmOCR with retries and concurrency control ([`7e571b9`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/7e571b9dc59acdfd4662fd9c53afff324f1da9a1))
- Stitcher merging tile Markdown, aligning headings with DOM outline, trimming overlaps via SSIM + fuzzy text comparisons, and injecting provenance comments linking back to exact pixel coordinates ([`7e571b9`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/7e571b9dc59acdfd4662fd9c53afff324f1da9a1))
- Content-addressed `Store` with SQLite + sqlite-vec metadata for embeddings search ([`7e571b9`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/7e571b9dc59acdfd4662fd9c53afff324f1da9a1))
- Pydantic request/response schemas, `python-decouple` settings (`app/settings.py`) with `.env` support, structured logging via structlog ([`7e571b9`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/7e571b9dc59acdfd4662fd9c53afff324f1da9a1))
- Complete capture pipeline with persistence layer ([`e640cc5`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/e640cc54b4bea084cab8450ea85ffc3674e8c1af))

### Seam Markers and Stitching Improvements (2025-11-08)

- Seam markers (`<!-- seam-marker ... -->`) at tile boundaries with enriched provenance (`viewport_y`, `overlap_px`) and highlight links (`/jobs/{id}/artifact/highlight?tile=X&y0=Y&y1=Z`) for visual tile-boundary review ([`2a406ba`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/2a406bad0620f9294b729e3012399066d9d675f0))
- Smart table header deduplication using overlap-aware similarity scoring ([`2a406ba`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/2a406bad0620f9294b729e3012399066d9d675f0))
- Seam watermark instrumentation and FlowLogger v2 integration ([`25eefd7`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/25eefd766ca5b0809d13aecf4103fa8e0eed41a8))
- Seam marker event tracking and persistence ([`af1b968`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/af1b968a4401d661ffb1b7ba9a3746de3fa48f6e), [`3106500`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/31065006278df275abcb43fe7f568d5aface2827))

### Intelligent Tile Overlap Deduplication (2025-11-09)

- Four-tier deduplication engine in `app/dedup.py` ([`a63e26c`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/a63e26cb9c2a8b98dac427a9d86428a16f6ceb76)):
  1. **Pixel verification** (O(1)) -- fast rejection via existing SHA256 hashes
  2. **Exact boundary matching** (O(n)) -- longest exact sequence at tile boundaries (~70% of real-world cases)
  3. **Sequence similarity** (O(n*m)) -- difflib SequenceMatcher with 90% threshold for OCR variation
  4. **Fuzzy line-by-line fallback** -- 85% threshold for worst-case OCR drift
- Safety limits: never removes >3x estimated overlap; configurable via `DeduplicationSettings` with telemetry in manifests; 22 tests covering all tiers ([`a63e26c`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/a63e26cb9c2a8b98dac427a9d86428a16f6ceb76))
- Sequence matching algorithm fix with comprehensive test coverage ([`7251503`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/725150340db54450a7486b6580d921280963e784))

### Content-Addressed Cache Reuse (2025-11-08)

- Deterministic cache key from `url + CfT + viewport + DSF + OCR model + profile`; identical requests return immediately with `cache_hit=true` and reuse existing artifacts ([`67bad76`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/67bad76e138a3c58407553af6de0a4fae08124aa))
- `POST /replay` endpoint for re-running captures from stored manifests ([`67bad76`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/67bad76e138a3c58407553af6de0a4fae08124aa))
- Cache-keyed storage paths ([`5a32136`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/5a321363c56870b9ae763d368556242f28e298d3))
- Cache key bumped to v8 after OCR prompt changes to invalidate stale results ([`777d082`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/777d082040c2ee8927fbcd135ea1d02b0ac50e57))

### Critical PNG Encoding Fix (2025-11-09)

- Replaced `image.write_to_buffer(".png")` with `image.pngsave_buffer()` to eliminate intermittent "VipsForeignSaveSpngTarget" errors across libvips 8.10-8.16+; unblocked the entire capture-tile-OCR-stitch pipeline for production ([`eeed9cb`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/eeed9cbcf36cce824ed3357c48937c0b266796dc))

---

## OCR Engine

Multi-provider OCR with adaptive concurrency, prompt engineering, and hybrid DOM recovery.

### OCR Client and OpenAI-Compatible API (2025-11-07 -- 2025-11-09)

- Initial olmOCR integration with retries and concurrency ([`7e571b9`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/7e571b9dc59acdfd4662fd9c53afff324f1da9a1))
- Migrated OCR client to OpenAI-compatible chat completions API format; auto-detects `/chat/completions` endpoints; supports GPT-4 Vision, Azure OpenAI, DeepInfra, and any OpenAI-compatible provider ([`7578541`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/75785417f89bb001338cad4a196004fc135e7a05))
- Batch processing with configurable concurrency, request telemetry (latency, status codes, request IDs), and quota tracking ([`0b391fe`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/0b391fe6a65b9a6e2fd96dc84a040f0475c9eea1))

### OCR Prompt Engineering (2025-11-09)

- Comprehensive OCR instruction prompt yielding 186% output increase (222 to 634+ lines on finviz.com) by mandating complete extraction of all UI elements, metadata, tabular data, lists, and text within graphical elements; GitHub Flavored Markdown formatting requirements; proper table syntax ([`417dc6c`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/417dc6c1f3eeee061a8133522087296a02c89727), [`d07f3b5`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/d07f3b52aee7bbc943eeb767b68d9e2bfe96b4b4))
- Switched to official olmOCR prompt from allenai/olmocr for simpler, more reliable extraction; increased `max_tokens` to 8000; set `temperature=0.1` for deterministic output ([`777d082`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/777d082040c2ee8927fbcd135ea1d02b0ac50e57))

### OCR Concurrency Autotune (2025-11-08)

- Adaptive concurrency controller: starts at `OCR_MIN_CONCURRENCY`, scales toward `OCR_MAX_CONCURRENCY` on healthy latency (<3.5s), throttles on 408/429/5xx or latency >7s ([`2a406ba`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/2a406bad0620f9294b729e3012399066d9d675f0))
- `AutotuneState` tracking initial/peak/final limits and adjustment events; manifest `ocr_autotune` section records scaling decisions ([`2a406ba`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/2a406bad0620f9294b729e3012399066d9d675f0))
- Race condition fix in `_AdaptiveLimiter.slot()` -- added lock protection around `_pending_reduction` in finally block ([`777d082`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/777d082040c2ee8927fbcd135ea1d02b0ac50e57))

### DOM-Assisted Hybrid Text Recovery (2025-11-08)

- Intelligent OCR confidence detection via multiple heuristics: replacement character detection, mixed numeric/punctuation bursts, hyphen-break patterns at line boundaries, low alpha-numeric ratio ([`2a406ba`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/2a406bad0620f9294b729e3012399066d9d675f0))
- DOM text overlay patches low-confidence OCR regions with DOM-derived text; `DOMAssist` dataclass tracks tile index, line number, reason, and replacements ([`2a406ba`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/2a406bad0620f9294b729e3012399066d9d675f0))
- Manifest `dom_assists` array; CLI `diag`/`watch` commands display DOM assist summaries with reasons and counts ([`2a406ba`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/2a406bad0620f9294b729e3012399066d9d675f0))
- Prometheus metrics for DOM assist density ([`d3ca56f`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/d3ca56fab9b95460b213f2057d58f22fc1ade5eb), [`4b83eda`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/4b83eda0d63e67a5b1765a13f330ef235e2bcf17))

### GLM (ZhipuAI) OCR Provider (2026-02-11)

- GLM OCR provider with both OpenAI-compatible chat API and native MaaS layout parsing API ([`94a1a70`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/94a1a70e165e31a1f6afd0f160806fd9296ac9d8))
  - `normalize_glm_file_reference()`, `build_glm_maas_payload()`, `build_glm_openai_chat_payload()`
  - `extract_glm_openai_markdown()`, `extract_glm_maas_markdown()` with multi-level fallback
- GLM provider documentation and expanded OCR test coverage ([`9df8f3a`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/9df8f3af9c56d32d04482c14d72a2e6669e6162b))
- OCR backend contract (`OcrBackend` protocol), hardware detection (`app/hardware.py`), and autopilot policy (`app/ocr_policy.py`) for automatic backend selection ([`50b66fa`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/50b66fadaa5571fb24f7b95b5e1f2d7e4720eaba))
- GLM MaaS/local GPU/CPU OCR adapter automation ([`eed5280`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/eed5280f2c984ec594dcb495096a50d42162b3d6))
- Local GLM OCR service lifecycle manager ([`5e312e4`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/5e312e44ce0e9572f58d6e302a904484755909d9))
- GLM OCR runtime provenance, anti-flap policy preventing rapid backend switching, and autopilot test suites ([`41d7b43`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/41d7b43393cad3aa138f2d56bcc12467157e75e5))
- Fix OpenAI OCR content-list extraction regression ([`6f79f68`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/6f79f68a08b05c81fd4cffa2b94de019e88e09d5))

---

## Bot Detection Evasion

Stealth techniques enabling reliable capture of sites protected by Cloudflare, PerimeterX, DataDome, and similar anti-bot systems.

### Multi-Layer Stealth (2025-11-09)

- 60+ lines of stealth JavaScript for navigator.webdriver, plugins, permissions API, hardware fingerprint masking ([`66f1128`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/66f11288fd97b97d0d03f4d06acb21eae041013f))
- Changed page load strategy from `networkidle` to `domcontentloaded` to avoid Cloudflare challenge hangs (3-5x faster page loads) ([`66f1128`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/66f11288fd97b97d0d03f4d06acb21eae041013f))
- String interpolation security fix in scroll evaluation ([`66f1128`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/66f11288fd97b97d0d03f4d06acb21eae041013f))
- Visual proof of Cloudflare bypass with finviz.com screenshots in docs ([`77b2e54`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/77b2e54941e2240cbb7043aef6dc4babbc7e9ce5))
- Simplified installation by removing Xvfb dependency ([`1939bf3`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/1939bf382e31db75ddea1140a5be5ffc987a7fb4))

### Browser Profiles (2025-11-08)

- Persistent browser profiles under `CACHE_ROOT/profiles/<id>/storage_state.json` for reusing login/storage state across captures ([`67bad76`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/67bad76e138a3c58407553af6de0a4fae08124aa))
- Profile IDs recorded in manifests, `RunRecord`, and exposed via API/SSE/CLI; profile-aware context creation with automatic slug sanitization ([`67bad76`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/67bad76e138a3c58407553af6de0a4fae08124aa))

---

## Browser UI

An interactive, Chrome-inspired web interface for browsing the web as clean Markdown.

### Full Browser Experience (2025-11-09)

- Complete browser interface at `/browser` with address bar (auto-detects URLs vs search terms), back/forward navigation history, refresh button, and dual rendered/raw Markdown views ([`a63e26c`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/a63e26cb9c2a8b98dac427a9d86428a16f6ceb76))
- Vanilla JavaScript (no framework); Stripe-quality CSS design system with dark mode; marked.js for Markdown rendering; Prism.js for syntax highlighting ([`a63e26c`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/a63e26cb9c2a8b98dac427a9d86428a16f6ceb76))
- Smart caching (1-hour TTL); real-time progress bars during tile processing ([`a63e26c`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/a63e26cb9c2a8b98dac427a9d86428a16f6ceb76))
- Keyboard shortcuts: `Alt+Left/Right` (navigate), `Ctrl+R` (refresh), `Ctrl+U` (toggle view), `Ctrl+L` (focus address bar) ([`a63e26c`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/a63e26cb9c2a8b98dac427a9d86428a16f6ceb76))
- Enhanced Prism syntax highlighting for markdown in Browser UI (bold, italic, lists) ([`777d082`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/777d082040c2ee8927fbcd135ea1d02b0ac50e57))
- Improved syntax highlighting reliability and optimized screenshot file size ([`a9d7c96`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/a9d7c96731c757fbd23f51e1c9b211b4a20ed396))
- Fixed browser CSS gradient rendering ([`12823cb`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/12823cb736bd6d65fe30b6646cfa99f731c83ddb))

---

## Job Dashboard and Event Streaming

The HTMX/Alpine.js web dashboard and SSE/NDJSON real-time event infrastructure.

### Dashboard Foundation (2025-11-07)

- Web UI foundation with SSE streaming and configuration framework ([`0998cdd`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/0998cdd2868e48682f97fa17de6c02916312bd9f))
- FastAPI `/jobs` endpoint with background `JobManager` for async job processing ([`7e571b9`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/7e571b9dc59acdfd4662fd9c53afff324f1da9a1))
- Job orchestration with real API endpoints and enhanced warning system ([`21930f8`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/21930f88c5ddcefc1cb6d659d7be572856b6127d))
- SSE/NDJSON event streaming (`/jobs/{id}/stream`, `/jobs/{id}/events`) ([`1019c41`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/1019c413ba5fdd381d5341042663fc278211d66d), [`481a085`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/481a085f45f2e01064723b13ff840730f439cf03))
- Webhook infrastructure for job lifecycle callbacks ([`badc66e`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/badc66e7a0afa5c6cf2aab7ce05e1df38541650e))
- Warning telemetry, web UI event handling, and OCR tooling ([`5cf0d88`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/5cf0d8820334f40b46f2381214bea7176df155ad))

### HTMX SSE Extension (2025-11-08)

- Replaced custom EventSource JavaScript with declarative HTMX SSE extension (`hx-ext="sse"`, `sse-connect`); JavaScript bridge for `htmx:sseMessage` events ([`2a406ba`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/2a406bad0620f9294b729e3012399066d9d675f0))
- DOM assist events render inline summaries in the Events tab ([`2a406ba`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/2a406bad0620f9294b729e3012399066d9d675f0))
- Health badge and reconnection cues integrated with HTMX lifecycle events ([`2a406ba`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/2a406bad0620f9294b729e3012399066d9d675f0))

### Enhanced Links Tab (2025-11-08)

- `LinkRecord` extended with `rel`, `target`, `kind`, `domain` metadata; domain-grouped display with `(relative)` and `(fragment)` buckets ([`2a406ba`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/2a406bad0620f9294b729e3012399066d9d675f0))
- Coverage badges (DOM vs OCR source with delta warnings), attribute badges (`target="_blank"`, `rel`) ([`2a406ba`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/2a406bad0620f9294b729e3012399066d9d675f0))
- Inline actions: "Open in new job" (triggers immediate capture), "Copy Markdown" (formatted anchor to clipboard), "Mark crawled" (localStorage persistence) ([`2a406ba`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/2a406bad0620f9294b729e3012399066d9d675f0))

---

## CLI (`scripts/mdwb_cli.py`)

The Typer-based command-line interface for programmatic interaction with the capture pipeline.

### Core Commands (2025-11-07)

- `fetch`, `show`, `stream`, `watch`, `events`, `diag`, `warnings`, `dom links`, `resume status`, `demo` subcommands ([`346a1aa`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/346a1aa9aaee25803fd95c862ce5255fccf66459), [`9972f7a`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/9972f7ae0ef3bfab412698d2f68246666c3d9c8b))
- `mdwb diag <job-id>` for detailed diagnostics: CfT/Playwright metadata, capture/OCR/stitch timings, warning summaries, blocklist hits ([`0b391fe`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/0b391fe6a65b9a6e2fd96dc84a040f0475c9eea1))
- `jobs embeddings search` for vector similarity search (inline or file-based vectors, configurable top-k, JSON/table output) ([`0b391fe`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/0b391fe6a65b9a6e2fd96dc84a040f0475c9eea1))
- `jobs ocr-metrics` with batch telemetry display ([`0b391fe`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/0b391fe6a65b9a6e2fd96dc84a040f0475c9eea1))
- `jobs replay manifest` for resubmitting stored manifests via `/replay` ([`67bad76`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/67bad76e138a3c58407553af6de0a4fae08124aa))
- `jobs agents bead-summary` for converting markdown checklists into bead-ready summaries ([`0b391fe`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/0b391fe6a65b9a6e2fd96dc84a040f0475c9eea1))
- `jobs bundle` for downloading tar archives of job artifacts ([`0b391fe`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/0b391fe6a65b9a6e2fd96dc84a040f0475c9eea1))

### Resume Functionality (2025-11-07)

- `fetch --resume` with `done_flags/` tracking for incremental batch processing; skip already-processed URLs automatically ([`9ad7612`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/9ad76125d47e17603d3198700799ad66e567b7f6))
- `resume status` with `--pending`, `--json`, progress percentage and ETA calculations ([`8f0ade2`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/8f0ade287588722c1e5ff71227f09b4f6b64fe18), [`60bf48f`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/60bf48fb3856ae0220d8ec478474f2c25f6d76f5))
- Resume orphan flag detection ([`4ce9e59`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/4ce9e59b5ad5d3462ec0c854cca047cd02980453))

### TOON Format Support (2026-01-25)

- Any `--json`-capable CLI command now also accepts `--format toon` for TOON output (falls back to JSON if `tru` unavailable) ([`d758646`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/d758646703c7ba76fb411b9ddfb6978782537183))

### --stats Flag and Output Format Env Vars (2026-01-28)

- `--stats` flag on `resume_status`, `demo_snapshot`, and `demo_links` showing TOON-vs-JSON savings ([`5d61852`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/5d61852f9e585086dca5a6645a90bc117a7905f0))
- Output format resolution chain: `--format` > `--json` > `MWB_OUTPUT_FORMAT` > `TOON_DEFAULT_FORMAT` > default ([`5d61852`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/5d61852f9e585086dca5a6645a90bc117a7905f0))

### CLI Bug Fixes

- CLI watch hooks in raw mode ([`38e3ed8`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/38e3ed82dc816e2a80d285585e94193da816ea80))
- CLI stream job terminal state handling ([`3106500`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/31065006278df275abcb43fe7f568d5aface2827))
- Format mdwb_cli.py to pass ruff format check ([`40df5f5`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/40df5f5f271a3f0848d23a784120fc84171ddafa))

---

## DOM and Links Pipeline

Extraction of structured link data from DOM snapshots and OCR output.

### DOM Snapshot and Links Extraction (2025-11-07)

- DOM snapshot capture and automated links extraction pipeline; `links.json` with anchors/forms/headings/meta from DOM ([`26675cb`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/26675cb14158317f47caaa2fbe0d9d968a6c4057))
- DOM link extraction (`app/dom_links.py`): blend DOM-extracted links with OCR-derived links; mark deltas between DOM-only and OCR-only sources for comparison ([`0b391fe`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/0b391fe6a65b9a6e2fd96dc84a040f0475c9eea1))
- `mdwb dom links --job-id <id>` renders stored links (anchors/forms/headings/meta) ([`0b391fe`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/0b391fe6a65b9a6e2fd96dc84a040f0475c9eea1))

---

## Agent Automation

Scripts and utilities for downstream LLM workflows consuming captured Markdown.

### Starter Scripts (2025-11-07)

- `scripts/agents/summarize_article.py` -- submit URL or reuse existing job, extract summary ([`9ad7612`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/9ad76125d47e17603d3198700799ad66e567b7f6), [`fc43319`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/fc43319947141939359d5c76cf5a0e1c710e299c))
- `scripts/agents/generate_todos.py` -- extract action items and TODOs from captured content ([`9ad7612`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/9ad76125d47e17603d3198700799ad66e567b7f6), [`fc43319`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/fc43319947141939359d5c76cf5a0e1c710e299c))
- Shared utilities with retry logic, exponential backoff, error handling, validation ([`fc43319`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/fc43319947141939359d5c76cf5a0e1c710e299c))
- All scripts are Typer CLIs accepting `--url`, `--job-id`, `--api-base`, `--json`, `--out` flags ([`fc43319`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/fc43319947141939359d5c76cf5a0e1c710e299c))

### Semantic Post-Processing (2025-11-09)

- Optional LLM-based Markdown refinement module (`app/semantic_post.py`): async HTTP client, configurable endpoint/model/timeout, telemetry (latency, token usage, delta chars), graceful fallback to original Markdown ([`9340fb9`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/9340fb951767e6cfcb2ec3c886016923dce72a06))

---

## Web Crawler

Depth-1 web crawling for link expansion and site capture.

### Crawler Implementation (2025-11-09)

- `app/crawler.py` (431 lines): `CrawlOrchestrator`, `RobotsChecker`, `CrawlConfig`, `CrawlState` ([`49e6466`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/49e6466ab8dba940dcfa4b290a0faa8fc7307fcc))
- Async implementation with robots.txt compliance, domain allowlisting, configurable crawl delay (default 1000ms), depth tracking, `capture_fn` callback integration ([`49e6466`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/49e6466ab8dba940dcfa4b290a0faa8fc7307fcc))

---

## Server and Production Infrastructure

Server runtime, auth, rate limiting, deployment, and Kubernetes support.

### FastAPI Server (2025-11-07)

- FastAPI application with REST API endpoints, background `JobManager`, SSE/NDJSON event feeds ([`7e571b9`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/7e571b9dc59acdfd4662fd9c53afff324f1da9a1))
- Blocklist system for domains and URL patterns ([`bee427a`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/bee427a98ea9145b805d481cfcdd14bc87dd8c6b))

### Granian ASGI Server (2025-11-08)

- `scripts/run_server.py` with `SERVER_IMPL` environment toggle (uvicorn default, Granian optional); `RunRecord` persists `server_runtime` in SQLite; manifests record `environment.server_runtime` ([`2a406ba`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/2a406bad0620f9294b729e3012399066d9d675f0))

### Production Auth, Rate Limiting, and Job Queue (2025-11-09)

- API key middleware for authentication, rate limiting, background job queue ([`687054e`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/687054e6867d63f84a56bfced7b13982927c9901))
- Hardened production auth with job watchdog for stuck jobs ([`883a96f`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/883a96f2ab9fc5443fe7014fef495aace9f71705))

### Database Performance (2025-11-09)

- Connection pooling via singleton `get_store()` pattern (eliminates per-request engine creation) ([`5ede8f9`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/5ede8f93504d24faaa76da354163a75bb0c8ec48))
- Throttled timestamp updates reducing write contention ([`5ede8f9`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/5ede8f93504d24faaa76da354163a75bb0c8ec48))
- Thread safety for `Store` singleton and datetime timezone comparison fix ([`3c3a619`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/3c3a619c655baa7ff81817cd12fd1c93d50d559f))
- Thread safety for rate limiter; division by zero prevention ([`c745d27`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/c745d2780fcf3e00b359ff9b15f80bb7f7d5c046))
- SQL correctness fixes, dead code removal ([`ec565a1`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/ec565a13141d8b0ac495984ec79cc174b9d84b87))

### Kubernetes and Deployment (2025-11-09)

- Kubernetes manifests and hardened installer with safety checks ([`dde5c06`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/dde5c06caf46ea24e8ad28331e485060513bce44))
- Production deployment documentation (DEPLOYMENT.md) covering Docker, Docker Compose, Kubernetes, scaling guidance (small/medium/large), SSL/TLS, monitoring, and security best practices ([`eeed9cb`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/eeed9cbcf36cce824ed3357c48937c0b266796dc))
- Complete cache implementation with API docs and local OCR infrastructure ([`2932a98`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/2932a989332b42443c77edbe5d3f78a534b12336))

---

## Security

Input validation, XSS prevention, injection hardening.

### Fixes (2025-11-09)

- Comprehensive input validation and additional XSS protections ([`2889e88`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/2889e8806abe10ed93503f6acc6c0f75cdb84f51))
- Prevent shell injection in CLI hooks ([`12823cb`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/12823cb736bd6d65fe30b6646cfa99f731c83ddb))
- Prevent XSS in seam marker rendering ([`3ef4854`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/3ef485402f9b35f0435fb7a97c22cbebf0dcc7e7))
- SQL injection prevention in `app/store.py` via column name regex validation and type whitelisting ([`a63e26c`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/a63e26cb9c2a8b98dac427a9d86428a16f6ceb76))
- URL validation error handling and iterator safety ([`23cf577`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/23cf577a9834217278a4ae2201833599d0fbe190))

---

## Installer

The one-command `install.sh` for automated setup.

### Initial Installer (2025-11-09)

- All-in-one `install.sh` (460 lines): multi-OS support (Debian/Ubuntu, RHEL/CentOS, Arch Linux, macOS), auto-installs uv/libvips/Playwright, creates `.env` from template, runs validation tests, generates launcher script (`./mdwb`) ([`9340fb9`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/9340fb951767e6cfcb2ec3c886016923dce72a06))
- Options: `--yes`/`-y` (non-interactive), `--dir=PATH`, `--no-deps`, `--no-browsers`, `--ocr-key=KEY` ([`9340fb9`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/9340fb951767e6cfcb2ec3c886016923dce72a06))
- Critical fix: install Chrome for Testing (`--channel=cft`) instead of regular Chromium ([`687054e`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/687054e6867d63f84a56bfced7b13982927c9901))

### --easy-mode Alias (2026-02-22)

- `--easy-mode` as alias for `--yes`, enabling the ACFS update orchestrator to call all managed tools uniformly; fixes [#9](https://github.com/Dicklesworthstone/markdown_web_browser/issues/9) ([`9603167`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/960316733b5ec73a2acd75c18c4d04f5a384d0fd))
- Fix hardcoded `anthropics` GitHub org URL; fixes [#10](https://github.com/Dicklesworthstone/markdown_web_browser/issues/10) ([`6914f84`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/6914f84a28e7e0f1d14035790c69b26cd58104ea))

### Playwright 1.57+ Compatibility (2026-02-25)

- Resolve three `install.sh` bugs: uv PATH (uv 0.10+ uses `~/.local/bin`), removed `--channel=cft` flag (incompatible with Playwright 1.57+), added `__main__` guard to `scripts/mdwb_cli.py` so launcher actually runs; closes [#12](https://github.com/Dicklesworthstone/markdown_web_browser/issues/12) ([`e6b098e`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/e6b098e35cf0a3ecf0b81611e5d5327eb015cc55))
- Replace "Chrome for Testing" / "CfT" terminology in installer messages with "Playwright Chromium" for clarity ([`3e8d71e`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/3e8d71e786d87bdd37aeeb6d46d04c2e3a37f48b))

### Documentation Fixes

- Correct install script URLs in README; fixes [#6](https://github.com/Dicklesworthstone/markdown_web_browser/issues/6) ([`aec0491`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/aec04918f35dfd9fe1af457e64061bd60dc4a10a))

---

## Observability and SLO Tracking

Prometheus metrics, SLO computation, smoke tests, and operational tooling.

### Prometheus Metrics (2025-11-07 -- 2025-11-08)

- Prometheus metrics for capture/OCR/stitch durations, warning/blocklist counts, job completions, SSE heartbeats via `prometheus-fastapi-instrumentator` ([`77cedfd`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/77cedfd2df4ae621ad14f627105ee632fea92d12))
- `scripts/check_metrics.py` for Prometheus endpoint validation with `--api-base`, `--exporter-url`, `--json`, `--check-weekly` options ([`0b391fe`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/0b391fe6a65b9a6e2fd96dc84a040f0475c9eea1))
- Prometheus metrics for DOM assist density ([`d3ca56f`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/d3ca56fab9b95460b213f2057d58f22fc1ade5eb))

### SLO Computation (2025-11-08)

- SLO tracking, DOM assist summaries, and seam marker persistence ([`af1b968`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/af1b968a4401d661ffb1b7ba9a3746de3fa48f6e))
- SLO computation pipeline consuming manifest indexes and benchmark budgets; JSON report and optional Prometheus textfile metrics output (`scripts/compute_slo.py`) ([`f64876b`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/f64876b125c47a59cc01670ba9a2c5d3a3b513ac))
- SLO validation in metrics checks ([`4ce9e59`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/4ce9e59b5ad5d3462ec0c854cca047cd02980453))

### Smoke Test Runner (2025-11-07 -- 2025-11-08)

- Smoke test automation framework (`scripts/run_smoke.py`, `scripts/smoke_runner.py`) with SLO computation ([`77cedfd`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/77cedfd2df4ae621ad14f627105ee632fea92d12))
- `scripts/show_latest_smoke.py` for quick pointers to latest smoke outputs, including `--weekly` view with seam marker percentiles and capture/OCR SLO status ([`0b391fe`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/0b391fe6a65b9a6e2fd96dc84a040f0475c9eea1))
- `scripts/update_smoke_pointers.py` for refreshing smoke dashboards with `--root`, `--weekly-source`, `--no-compute-slo`, `--budget-file` options ([`0b391fe`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/0b391fe6a65b9a6e2fd96dc84a040f0475c9eea1))
- Comprehensive project gap analysis and enhanced smoke test SLO tracking ([`5939591`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/5939591d44639407cfe3ec9950bfeaf76c83997c))

### Warning Telemetry (2025-11-07)

- `ops/warnings.jsonl` structured warning log for capture/blocklist incidents ([`5cf0d88`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/5cf0d8820334f40b46f2381214bea7176df155ad))
- `mdwb warnings --count N` to tail warning log via CLI ([`77cedfd`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/77cedfd2df4ae621ad14f627105ee632fea92d12))
- Empty pointer validation and seam marker telemetry documentation ([`d0caa24`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/d0caa2490cb7c8241db98497763663f4e8e78ae7))

---

## Testing Infrastructure

Test suites, frameworks, and quality gates.

### Test Suites (2025-11-07 -- 2025-11-09)

- Three-tiered integration testing: unit, integration, e2e ([`5637023`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/56370230e3dfc3f8dd8eb830026642350b0684cc))
- Test modules: `test_e2e_cli.py`, `test_mdwb_cli_events.py`, `test_check_metrics.py`, `test_store_manifest.py`, `test_manifest_contract.py`, `test_agent_scripts.py`, `test_dom_links.py`, `test_mdwb_cli_diag.py`, `test_mdwb_cli_embeddings.py`, `test_mdwb_cli_ocr.py`, `test_api_replay.py`, `test_capture_sweeps.py` ([`fc43319`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/fc43319947141939359d5c76cf5a0e1c710e299c), [`0b391fe`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/0b391fe6a65b9a6e2fd96dc84a040f0475c9eea1), [`67bad76`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/67bad76e138a3c58407553af6de0a4fae08124aa))
- Pytest summary reporting tool generating structured JSON summaries with JUnit XML reports ([`fc43319`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/fc43319947141939359d5c76cf5a0e1c710e299c))
- Playwright smoke tests (`playwright/smoke_capture.spec.ts`) with shared config ([`67bad76`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/67bad76e138a3c58407553af6de0a4fae08124aa))
- Validation test suite for installer: basic Playwright capture, viewport sweep, pyvips-independent browser tests ([`9340fb9`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/9340fb951767e6cfcb2ec3c886016923dce72a06))

### Quality Gates (`scripts/run_checks.sh`)

- Wraps `ruff check`, `ty check`, pytest, and Playwright test sequence ([`67bad76`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/67bad76e138a3c58407553af6de0a4fae08124aa))
- `MDWB_CHECK_METRICS=1` to include Prometheus health check ([`0b391fe`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/0b391fe6a65b9a6e2fd96dc84a040f0475c9eea1))
- `MDWB_RUN_E2E=1`, `MDWB_RUN_E2E_RICH=1`, `MDWB_RUN_E2E_GENERATED=1` toggles for different E2E tiers ([`fc43319`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/fc43319947141939359d5c76cf5a0e1c710e299c))
- `SKIP_LIBVIPS_CHECK=1` for minimal containers without libvips ([`0b391fe`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/0b391fe6a65b9a6e2fd96dc84a040f0475c9eea1))

### Test Fixes (2026-01-24)

- Resolve `ty` type checker and `ruff` lint errors ([`66b437c`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/66b437cb94310aa739250f3353f3b26e222f37f0))
- Fix CI test failures for capture sweeps and stitch ([`5d5a8af`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/5d5a8afa3cdbcad347e0dcf9685f98fd8224511b))
- Update browser capture and pyvips tests ([`f9b088d`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/f9b088d20a1f2eb95377c84501607388e3f267cf))

---

## CI/CD

GitHub Actions workflows, Dependabot, and release automation.

### Comprehensive CI Pipelines (2026-01-17)

- **CI workflow** (`ci.yml`): ruff check/format, `ty` type checker, pytest, Playwright tests, Docker build; concurrency control and uv dependency caching ([`c8fa588`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/c8fa5881d289f573d48bbed687a31584cb79db03))
- **Release workflow** (`release.yml`): multi-platform Docker images (amd64/arm64) to GHCR on version tags; auto-generated changelog ([`c8fa588`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/c8fa5881d289f573d48bbed687a31584cb79db03))
- **Dependabot** config for Python, GH Actions, and Docker with grouped minor/patch updates ([`c8fa588`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/c8fa5881d289f573d48bbed687a31584cb79db03))
- Improved nightly smoke workflow with uv caching, 60-min timeout, artifact retention, failure log upload ([`c8fa588`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/c8fa5881d289f573d48bbed687a31584cb79db03))

### ACFS Notification Workflows (2026-01-27)

- CI workflow to notify ACFS lesson registry on installer changes ([`77dcd30`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/77dcd301bd8a4d3cec9333614e4b0c7eefb89b0a))
- CI workflow for ACFS lesson registry sync ([`931edf6`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/931edf6cc1f83db4b5eb45a4e416fcd71249b7f8))

### Dependency Updates

- Bump GH Actions versions across all workflows ([`0cec4ff`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/0cec4ff7226de91cf27f6c71cb38fd7ae247d600))
- Bump the actions group with 9 updates ([`91eadaf`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/91eadaffb9b2a052c9e32a92b37bbb6633db5578))
- Bump the actions group with 4 updates ([`3b925f2`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/3b925f2db319dda611a55d7cff27974bc729c615), [`eeb2afd`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/eeb2afdea8235e72c9e7351af6714ec9c189cdf0))
- Bump the actions group with 3 updates ([`7eea2b4`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/7eea2b4f21fc7edea835c1bbb63a3bb2e01e6dab))
- Bump Python Docker base from 3.13-slim to 3.14-slim ([`894f80e`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/894f80eae1639fdecf03fd8c5c098933328191ab))
- Update Python dependencies to latest stable versions ([`842e6e2`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/842e6e29dd016d7beeb1af2b5640e33ccc84727d))
- Add missing required env vars to nightly smoke workflow ([`06e9d5d`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/06e9d5dc8b6903026c14e895d27da3ca277bf6e0))

---

## Playwright Configuration

Shared browser configuration for deterministic captures across the project.

### Config File (2025-11-08)

- `playwright.config.mjs` with environment-based configuration: shared viewport 1280x2000, deviceScaleFactor=2, colorScheme="light", reduced motion, caret hiding for stable screenshots ([`67bad76`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/67bad76e138a3c58407553af6de0a4fae08124aa))
- CDP/BiDi transport selection via `PLAYWRIGHT_TRANSPORT`; screenshot masking selectors via `PLAYWRIGHT_SCREENSHOT_MASKS`; channel fallback (`PLAYWRIGHT_CHANNEL`) ([`67bad76`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/67bad76e138a3c58407553af6de0a4fae08124aa))

---

## Licensing and Project Metadata

### License (2025-11-09 -- 2026-02-21)

- Add MIT License ([`be23e85`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/be23e85617f7f60aef81f6d315ef12f996a02d6b))
- Restore MIT LICENSE file ([`51c3a59`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/51c3a59ba9a56a5e279554dc18b9df3fc2e42f4d))
- Update license to MIT with OpenAI/Anthropic Rider ([`7b25d36`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/7b25d36620e7b72c260cad70c1df7947a29167fe))

### Branding

- GitHub social preview image (1280x640) ([`d7773ec`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/d7773ec9719c21aeda1b97872886b51f2311fb5c))

---

## Documentation

### README and Guides

- Initial README with architecture overview, CLI cheatsheet, configuration docs ([`7e571b9`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/7e571b9dc59acdfd4662fd9c53afff324f1da9a1))
- Updated README example to use Hacker News with actual screenshot ([`41ca91a`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/41ca91a27a32078f17bfc68a45d8821abf494e8d))
- Visual proof of Cloudflare bypass with finviz.com screenshots ([`77b2e54`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/77b2e54941e2240cbb7043aef6dc4babbc7e9ce5))
- Comprehensive gallery documentation (`docs/gallery.md`) ([`b501e66`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/b501e663c361d0c96b00ef2720c7f4dcef0feb4a))
- Production deployment documentation (DEPLOYMENT.md) ([`eeed9cb`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/eeed9cbcf36cce824ed3357c48937c0b266796dc))
- Replaced outdated planning documents with comprehensive project status audit ([`3703e6b`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/3703e6bd21fbfbbc62bb1a563a04eb83bcf0ace6))
- Correct install script URLs in README; fixes [#6](https://github.com/Dicklesworthstone/markdown_web_browser/issues/6) ([`aec0491`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/aec04918f35dfd9fe1af457e64061bd60dc4a10a))

### Operations Docs

- Operations documentation updates (`docs/ops.md`, `docs/config.md`) ([`dd75abe`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/dd75abeac52736e1b8c377a9ea0b16f552e1f700), [`653fc17`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/653fc1742bfe3b7f42e089d763c9d7c08a929ddd))
- Deduplication strategy documentation with production readiness assessment ([`891cdec`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/891cdec04581ec77dbceb5119ab8eafd6258166a), [`bb0c253`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/bb0c2537bdf1af9669edea61a35ce876cb56daa0))
- AGENTS.md updates ([`bf8cfc8`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/bf8cfc83aaab518d13b8a13c0fce8f9e72dad632), [`b71f926`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/b71f9268b31383390e35a1a0820551bbbd5fba2e), [`f2e432e`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/f2e432e68c6c3ef64cff779a32853fe09086eed6))

---

## How to Read This Changelog

- **Commit links**: Every backtick-quoted hash links to `https://github.com/Dicklesworthstone/markdown_web_browser/commit/<full-sha>`.
- **Issue links**: `#N` references link to `https://github.com/Dicklesworthstone/markdown_web_browser/issues/N`.
- **No tags or releases exist** in this repository as of 2026-03-21. All history is on the `main` branch.
- **Module paths**: `app/` is the FastAPI application, `scripts/` contains the CLI and ops tooling, `web/` has the frontend, `tests/` has the test suites.
