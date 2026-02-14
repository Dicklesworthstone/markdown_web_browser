# AGENTS.md — markdown_web_browser

> Guidelines for AI coding agents working in this Python codebase.

---

## RULE 0 - THE FUNDAMENTAL OVERRIDE PREROGATIVE

If I tell you to do something, even if it goes against what follows below, YOU MUST LISTEN TO ME. I AM IN CHARGE, NOT YOU.

---

## RULE NUMBER 1: NO FILE DELETION

**YOU ARE NEVER ALLOWED TO DELETE A FILE WITHOUT EXPRESS PERMISSION.** Even a new file that you yourself created, such as a test code file. You have a horrible track record of deleting critically important files or otherwise throwing away tons of expensive work. As a result, you have permanently lost any and all rights to determine that a file or folder should be deleted.

**YOU MUST ALWAYS ASK AND RECEIVE CLEAR, WRITTEN PERMISSION BEFORE EVER DELETING A FILE OR FOLDER OF ANY KIND.**

---

## Irreversible Git & Filesystem Actions — DO NOT EVER BREAK GLASS

1. **Absolutely forbidden commands:** `git reset --hard`, `git clean -fd`, `rm -rf`, or any command that can delete or overwrite code/data must never be run unless the user explicitly provides the exact command and states, in the same message, that they understand and want the irreversible consequences.
2. **No guessing:** If there is any uncertainty about what a command might delete or overwrite, stop immediately and ask the user for specific approval. "I think it's safe" is never acceptable.
3. **Safer alternatives first:** When cleanup or rollbacks are needed, request permission to use non-destructive options (`git status`, `git diff`, `git stash`, copying to backups) before ever considering a destructive command.
4. **Mandatory explicit plan:** Even after explicit user authorization, restate the command verbatim, list exactly what will be affected, and wait for a confirmation that your understanding is correct. Only then may you execute it—if anything remains ambiguous, refuse and escalate.
5. **Document the confirmation:** When running any approved destructive command, record (in the session notes / final response) the exact user text that authorized it, the command actually run, and the execution time. If that record is absent, the operation did not happen.

---

## Git Branch: ONLY Use `main`, NEVER `master`

**The default branch is `main`. The `master` branch exists only for legacy URL compatibility.**

- **All work happens on `main`** — commits, PRs, feature branches all merge to `main`
- **Never reference `master` in code or docs** — if you see `master` anywhere, it's a bug that needs fixing
- **The `master` branch must stay synchronized with `main`** — after pushing to `main`, also push to `master`:
  ```bash
  git push origin main:master
  ```

**If you see `master` referenced anywhere:**
1. Update it to `main`
2. Ensure `master` is synchronized: `git push origin main:master`

---

## Toolchain: Python & uv

We use **uv** for everything in this project (env management, installs, running tools). **Never** call `pip`, `poetry`, `conda`, or ad-hoc `python -m venv`.

- **Runtime:** Python 3.13 only — no backward-compatibility shims
- **Packaging:** `pyproject.toml` exclusively; never introduce `requirements*.txt` or duplicate config files
- **Build system:** setuptools >= 70.0
- **Unsafe code:** N/A (Python)

### Key Dependencies

| Package | Purpose |
|---------|---------|
| `fastapi` + `uvicorn` + `granian` | Web framework and ASGI servers |
| `playwright` | Browser automation (Chrome for Testing channel, pinned) |
| `pyvips` | Image tiling, resizing, PNG encode (primary image library) |
| `pillow` | Edge image format fallback only |
| `python-decouple` | Configuration via `.env` (exclusive config loader) |
| `pydantic` | Data validation and schemas |
| `httpx[http2]` | HTTP client for OCR and external APIs |
| `sqlmodel` + `sqlite-vec` | Database ORM + vector embeddings |
| `zstandard` | Compression |
| `mistune` | Markdown parsing |
| `prometheus-client` + `prometheus-fastapi-instrumentator` | Metrics and instrumentation |
| `structlog` | Structured logging |
| `typer` + `rich` | CLI framework and console output |
| `beautifulsoup4` | HTML parsing (DOM link extraction) |
| `numpy` | Numerical operations |
| `arq` + `redis` | Background job queue |
| `jinja2` | HTML templating |
| `psutil` | System/hardware introspection |

### Optional Dependency Groups

| Group | Packages | Purpose |
|-------|----------|---------|
| `local-ocr` | `vllm`, `sglang`, `olmocr` | Local OCR inference via vLLM/SGLang servers |
| `observability` | `opentelemetry-sdk`, `opentelemetry-exporter-otlp` | OpenTelemetry tracing |
| `dev` | `pyoxipng`, `pytest`, `pytest-asyncio`, `ruff` | Testing and linting |

---

## Code Editing Discipline

### No Script-Based Changes

**NEVER** run a script that processes/changes code files in this repo. Brittle regex-based transformations create far more problems than they solve.

- **Always make code changes manually**, even when there are many instances
- For many simple changes: use parallel subagents
- For subtle/complex changes: do them methodically yourself

### No File Proliferation

If you want to change something or add a feature, **revise existing code files in place**.

**NEVER** create variations like:
- `capture_v2.py`
- `main_improved.py`
- `ocr_client_enhanced.py`

New files are reserved for **genuinely new functionality** that makes zero sense to include in any existing file. The bar for creating new files is **incredibly high**.

---

## Backwards Compatibility

We do not care about backwards compatibility—we're in early development with no users. We want to do things the **RIGHT** way with **NO TECH DEBT**.

- Never create "compatibility shims"
- Never create wrapper functions for deprecated APIs
- Just fix the code directly

---

## Compiler Checks (CRITICAL)

**After any substantive code changes, you MUST verify no errors were introduced:**

```bash
# Check for linting errors and auto-fix (ruff replaces flake8/isort/black)
ruff check --fix --unsafe-fixes

# Check for type errors
uvx ty check

# Run tests
uv run pytest tests/ -x
```

If you see errors, **carefully understand and resolve each issue**. Read sufficient context to fix them the RIGHT way.

After capture-facing changes, also run:
```bash
uv run playwright test tests/smoke_capture.spec.ts
```
This confirms Playwright 1.50+ screenshot APIs still freeze animations under the pinned CfT label/build and whichever transport (CDP/BiDi) was exercised.

---

## Testing

### Testing Policy

Tests live in the `tests/` directory with `conftest.py` for shared fixtures. Tests must cover:
- Happy path
- Edge cases (empty input, max values, boundary conditions)
- Error conditions

Integration and E2E tests live in `scripts/` (e.g., `test_e2e_comprehensive.py`, `test_integration_full.py`).

### Unit Tests

```bash
# Run all tests
uv run pytest tests/

# Run with output
uv run pytest tests/ -s --tb=short

# Run a specific test file
uv run pytest tests/test_ocr_client.py
uv run pytest tests/test_stitch.py
uv run pytest tests/test_deduplication.py

# Run with all markers
uv run pytest tests/ -x --timeout=60
```

### Test Categories

| Directory / File | Focus Areas |
|------------------|-------------|
| `tests/test_ocr_client.py` | OCR client contracts, failover, request formatting, response parsing |
| `tests/test_ocr_policy.py` | OCR autopilot policy, model selection, capabilities |
| `tests/test_stitch.py` | Tile stitching, overlap SSIM, provenance comments |
| `tests/test_deduplication.py` | Content dedup strategies, hash-based and semantic |
| `tests/test_capture_*.py` | Browser capture, viewport sweeps, screenshot stability |
| `tests/test_store_manifest.py` | Manifest storage, contract validation |
| `tests/test_job_manager.py` | Job lifecycle, queue management, error handling |
| `tests/test_mdwb_cli_*.py` | CLI subcommands: fetch, show, events, webhooks, diagnostics, artifacts, OCR, resume, replay, warnings, embeddings |
| `tests/test_metrics.py` | Prometheus metrics, SLO computation |
| `tests/test_hardware.py` | Hardware detection and resource limits |
| `tests/test_local_ocr.py` | Local OCR adapter contracts |
| `scripts/test_e2e_comprehensive.py` | Full end-to-end pipeline (70+ assertions) |
| `scripts/test_integration_*.py` | Cross-module integration tests |

### Test Fixtures

Shared fixtures live in `tests/fixtures/` and `tests/conftest.py`. Use `pytest-asyncio` with `asyncio_mode = "auto"` for async test functions.

---

## Third-Party Library Usage

If you aren't 100% sure how to use a third-party library, **SEARCH ONLINE** to find the latest documentation and current best practices.

---

## markdown_web_browser — This Project

**This is the project you're working on.** markdown_web_browser renders any URL to clean Markdown via tiled screenshots and olmOCR, with a web UI, REST API, background job queue, and CLI.

### What It Does

Captures web pages as tiled viewport screenshots via Playwright (Chrome for Testing), processes them through olmOCR for text extraction, stitches tiles with overlap validation, deduplicates content, and produces clean Markdown output. Includes a browser-like web UI (HTMX + SSE), REST API, background workers (arq + Redis), semantic search (sqlite-vec embeddings), and a comprehensive CLI.

### Architecture

```
URL → Playwright Capture → Viewport Sweep (scroll + IntersectionObserver)
                               │
                    Tile (1288px longest-side, ~120px overlap)
                               │
                    pyvips encode (PNG Q=9, palette off)
                               │
                    olmOCR (hosted or local vLLM/SGLang)
                               │
                    Stitch + SSIM overlap validation
                               │
                    Dedup (hash + semantic)
                               │
                    Markdown output + DOM links appendix
                               │
                    Store (SQLModel + sqlite-vec) + Manifest
```

### Project Structure

```
markdown_web_browser/
├── pyproject.toml                 # Package config (uv/setuptools)
├── .env.example                   # Environment variable reference
├── app/                           # Main application package
│   ├── main.py                    # FastAPI app, routes, SSE endpoints
│   ├── capture.py                 # Playwright browser capture + viewport sweeps
│   ├── tiler.py                   # Tile slicing (1288px, overlap)
│   ├── stitch.py                  # Tile stitching + SSIM overlap validation
│   ├── ocr_client.py              # olmOCR HTTP client (hosted + local)
│   ├── ocr_policy.py              # OCR model selection + autopilot policy
│   ├── local_ocr.py               # Local vLLM/SGLang OCR adapter
│   ├── dedup.py                   # Content deduplication (hash + semantic)
│   ├── store.py                   # SQLModel DB + manifest storage
│   ├── jobs.py                    # arq background job manager
│   ├── queue.py                   # Redis job queue
│   ├── cache.py                   # Response caching
│   ├── schemas.py                 # Pydantic request/response models
│   ├── settings.py                # python-decouple config loader
│   ├── crawler.py                 # Multi-page crawl orchestrator
│   ├── dom_links.py               # DOM link extraction (BeautifulSoup)
│   ├── embeddings.py              # sqlite-vec embedding pipeline
│   ├── semantic_post.py           # Semantic post-processing
│   ├── blocklist.py               # URL/domain blocklist
│   ├── auth.py                    # API key authentication
│   ├── rate_limit.py              # Rate limiting
│   ├── metrics.py                 # Prometheus counters/histograms
│   ├── hardware.py                # System hardware detection
│   ├── capture_warnings.py        # Capture stability warnings
│   └── warning_log.py             # Warning aggregation/logging
├── web/                           # Browser UI (HTMX + Tailwind + SSE)
│   ├── index.html                 # Landing page
│   ├── browser.html               # Browser UI template
│   ├── browser.js / browser.css   # UI logic and styles
│   ├── app.js                     # Application JavaScript
│   ├── htmx.min.js / htmx-ext-sse.js  # HTMX library + SSE extension
│   └── tailwind.css               # Tailwind CSS
├── scripts/                       # CLI, E2E tests, operational scripts
│   ├── mdwb_cli.py                # Main CLI (typer-based)
│   ├── olmocr_cli.py              # olmOCR CLI helper
│   ├── run_server.py              # Server launcher
│   ├── run_worker.py              # Background worker launcher
│   ├── run_smoke.py               # Smoke test runner
│   ├── test_e2e_comprehensive.py  # Comprehensive E2E tests
│   ├── test_integration_*.py      # Integration test suites
│   └── ...                        # Analysis, metrics, deployment scripts
├── tests/                         # Unit and integration tests (pytest)
├── docs/                          # Documentation
│   ├── architecture.md            # System architecture
│   ├── config.md                  # Configuration reference
│   ├── models.yaml                # OCR model declarations
│   ├── blocklist.md               # Blocklist documentation
│   ├── api.md                     # API reference
│   ├── ops.md                     # Operations playbook
│   └── ...                        # Additional docs
├── config/                        # Runtime configuration
│   └── blocklist.json             # URL blocklist data
├── k8s/                           # Kubernetes manifests
├── playwright/                    # Playwright configuration
├── playwright.config.mjs          # Playwright test config
├── Dockerfile                     # Container build
├── docker-compose.yml             # Multi-service compose
├── deploy.sh                      # Deployment script
└── install.sh                     # Installation script
```

### Key Files by Module

| Module | Key Files | Purpose |
|--------|-----------|---------|
| Capture | `app/capture.py` | Playwright viewport sweep capture with stabilization (scrollHeight + `IntersectionObserver`), retry, animation freeze |
| Tiling | `app/tiler.py` | 1288px longest-side tile slicing with ~120px overlap, DPI/scale metadata |
| Stitching | `app/stitch.py` | Tile reassembly with SSIM overlap validation, provenance comments (`<!-- source: tile_i, y=..., sha256=..., scale=... -->`) |
| OCR | `app/ocr_client.py`, `app/ocr_policy.py`, `app/local_ocr.py` | olmOCR HTTP client (hosted default: `olmOCR-2-7B-1025-FP8`), autopilot model selection, local vLLM/SGLang adapter |
| Storage | `app/store.py`, `app/schemas.py` | SQLModel async sessions, manifest storage with capture metadata checklist |
| Jobs | `app/jobs.py`, `app/queue.py` | arq + Redis background workers, job lifecycle management |
| Web UI | `web/browser.html`, `web/browser.js` | HTMX + SSE browser-like interface |
| CLI | `scripts/mdwb_cli.py` | Typer-based CLI: fetch, show, events, webhooks, diagnostics, artifacts, OCR, resume, replay |
| Dedup | `app/dedup.py`, `app/embeddings.py` | Hash-based + semantic dedup via sqlite-vec embeddings |

### Configuration & Secrets

- `.env` already exists and must **never** be overwritten
- Load config exclusively via `python-decouple`:

```python
from decouple import Config as DecoupleConfig, RepositoryEnv

decouple_config = DecoupleConfig(RepositoryEnv(".env"))
API_BASE_URL = decouple_config("API_BASE_URL", default="http://localhost:8007")
```

- Mirror any new env var in `.env.example` and document in `docs/config.md`
- Manifest entries must echo effective values (CfT version, screenshot style hash, concurrency caps, etc.)
- **Never** call `os.getenv`, `dotenv.load_dotenv`, or read `.env` manually

### Database & Persistence (SQLModel + SQLAlchemy Async)

**Do:**
- Create engines with `create_async_engine()` and sessions via `async_sessionmaker(...)`; wrap usage inside `async with` so sessions close automatically
- Await every async DB call: `await session.execute(...)`, `await session.scalars(...)`, `await session.commit()`, `await engine.dispose()`, etc.
- Keep **one** `AsyncSession` per request/task and avoid sharing across concurrent coroutines
- Use `selectinload`/`joinedload` or `await obj.awaitable_attrs.<rel>` to avoid sync lazy loads
- Wrap sync helpers with `await session.run_sync(...)` as needed

**Don't:**
- Reuse a single session concurrently
- Trigger implicit lazy loads inside async code
- Mix sync engines/drivers (e.g., psycopg2) with async sessions — use asyncpg
- "Double-await" helper results (e.g., `result.scalars().all()` is sync after the initial await)
- Block the event loop with CPU/batch work — push it into `run_sync()` or background workers

### Capture, OCR, and Tiling Expectations

- **Chrome for Testing + deterministic context**: `viewport=1280x2000`, `deviceScaleFactor=2`, `colorScheme="light"`, reduced motion, animations disabled. Always log CfT + Playwright versions in the manifest
- **Viewport sweeps only**: never fall back to `full_page=True`. Scroll via the stabilization routine (scrollHeight + `IntersectionObserver`) and retry if SPA height shrinks
- **Tiling**: keep ~120px overlap, enforce 1288px longest side, store offsets/DPI/scale/hash metadata, and compute SSIM over overlaps before stitching
- **pyvips pipeline**: slicing/resizing/PNG encode (`Q=9`, palette off, non-interlaced) must use `pyvips`; Pillow is for edge cases only
- **OCR policies**: all models must be declared in `docs/models.yaml` with keys like `long_side_px`, `prompt_template`, `fp8_preferred`, `max_tiles_in_flight`. Default remote model is `olmOCR-2-7B-1025-FP8`; local adapters point to vLLM/SGLang servers when `OCR_LOCAL_URL` is set
- **Provenance**: every stitched block gets a `<!-- source: tile_i, y=..., sha256=..., scale=... -->` comment. Links Appendix comes from `links.json` (DOM harvest) and must note DOM vs OCR deltas
- **Manifest discipline**: include CfT version, Playwright version, screenshot style hash, long-side px, concurrency thresholds, and timing metrics (`capture_ms`, `ocr_ms`, etc.) so Ops dashboards stay accurate
- **Capture metadata checklist**: every bug report, REAL/MacroBench bundle, or ops escalation must state (a) CfT label + build, (b) `browser_transport` (CDP vs BiDi), (c) Playwright version (>=1.50), (d) screenshot style hash, (e) OCR model/policy key, and (f) whether viewport sweep retries triggered. Missing data = re-run
- **Screenshot config**: keep `playwright.config.(ts|mjs)` in sync with defaults (`use.screenshot.animations="disabled"`, `caret="hide"`, shared mask selectors). Prefer `expect(page|locator).toHaveScreenshot()` with mask lists over bespoke CSS tweaks

### Production Smoke Coverage & Latency Reporting

- **Nightly smoke run**: reserve `benchmarks/production/**`, run the curated URL set (via `uv run scripts/olmocr_cli.py run --workspace ... --pdf ...` or equivalent), and stash manifests + tiles under `benchmarks/production/<date>/<slug>`. Log `capture_ms`, `ocr_ms`, `stitch_ms`, tile count, CfT label/build, transport, and OCR request IDs for every URL
- **Weekly latency report**: generate `benchmarks/production/weekly_summary.json` capturing p50/p95 per category and highlight any budget violations
- **Regression handling**: if a URL exceeds its latency or diff budget twice in a week, update the relevant Mail thread with links to tiles, manifest, DOM snapshot, and OCR request IDs so others can reproduce; pause shipments touching capture/OCR until it is green again
- **Readiness check**: before handoff, confirm (a) nightly smoke green for 48 hours, (b) weekly summary within budgets, (c) generative E2E tests green, (d) hosted OCR usage <80%. Mention these in your status update

### Stabilization Checklist (Before Marking Capture Fixes Done)

Before marking a capture fix "done," ensure:
1. `[aria-busy]` cleared
2. Volatile widgets are masked in Playwright config
3. Blocklist entries updated
4. Viewport sweep retries noted
5. Document completion in your handoff message

### Reference Docs

| Document | Purpose |
|----------|---------|
| `PLAN_TO_IMPLEMENT_MARKDOWN_WEB_BROWSER_PROJECT.md` | Canonical architecture, capture/OCR policies, Ops playbooks (sections 0-21) |
| `docs/architecture.md` | System architecture |
| `docs/config.md` | Configuration reference |
| `docs/models.yaml` | OCR model declarations |
| `docs/blocklist.md` | Blocklist documentation |
| `docs/api.md` | API reference |
| `docs/ops.md` | Operations playbook |
| `docs/olmocr_cli.md` | olmOCR CLI usage |

If the instructions in this file ever appear to conflict, defer to this `AGENTS.md`, then the Plan document, then the most recent user directive.

---

## MCP Agent Mail — Multi-Agent Coordination

A mail-like layer that lets coding agents coordinate asynchronously via MCP tools and resources. Provides identities, inbox/outbox, searchable threads, and advisory file reservations with human-auditable artifacts in Git.

### Why It's Useful

- **Prevents conflicts:** Explicit file reservations (leases) for files/globs
- **Token-efficient:** Messages stored in per-project archive, not in context
- **Quick reads:** `resource://inbox/...`, `resource://thread/...`

### Same Repository Workflow

1. **Register identity:**
   ```
   ensure_project(project_key=<abs-path>)
   register_agent(project_key, program, model)
   ```

2. **Reserve files before editing:**
   ```
   file_reservation_paths(project_key, agent_name, ["app/**"], ttl_seconds=3600, exclusive=true)
   ```

3. **Communicate with threads:**
   ```
   send_message(..., thread_id="FEAT-123")
   fetch_inbox(project_key, agent_name)
   acknowledge_message(project_key, agent_name, message_id)
   ```

4. **Quick reads:**
   ```
   resource://inbox/{Agent}?project=<abs-path>&limit=20
   resource://thread/{id}?project=<abs-path>&include_bodies=true
   ```

5. Set `AGENT_NAME` in your env so hooks can block commits when exclusive reservations exist.

### Cross-Repo Coordination

- **Shared project key**: for tightly-coupled repos, reuse the same `project_key` but reserve distinct globs (e.g., `frontend/**`, `backend/**`)
- **Separate project keys**: otherwise, register separately and use `macro_contact_handshake` / `request_contact` to exchange permissions before messaging

### Macros vs Granular Tools

- **Prefer macros for speed:** `macro_start_session`, `macro_prepare_thread`, `macro_file_reservation_cycle`, `macro_contact_handshake`
- **Use granular tools for control:** `register_agent`, `file_reservation_paths`, `send_message`, `fetch_inbox`, `acknowledge_message`, `release_file_reservations`

### Common Pitfalls

- `"from_agent not registered"`: Always `register_agent` in the correct `project_key` first
- `"FILE_RESERVATION_CONFLICT"`: Adjust patterns, wait for expiry, or use non-exclusive reservation
- **Auth errors:** If JWT+JWKS enabled, include bearer token with matching `kid`; static bearer only works if JWT is disabled

---

## Beads (br) — Dependency-Aware Issue Tracking

Beads provides a lightweight, dependency-aware issue database and CLI (`br` - beads_rust) for selecting "ready work," setting priorities, and tracking status. It complements MCP Agent Mail's messaging and file reservations.

**Important:** `br` is non-invasive—it NEVER runs git commands automatically. You must manually commit changes after `br sync --flush-only`.

### Conventions

- **Single source of truth:** Beads for task status/priority/dependencies; Agent Mail for conversation and audit
- **Shared identifiers:** Use Beads issue ID (e.g., `br-123`) as Mail `thread_id` and prefix subjects with `[br-123]`
- **Reservations:** When starting a task, call `file_reservation_paths()` with the issue ID in `reason`

### Typical Agent Flow

1. **Pick ready work (Beads):**
   ```bash
   br ready --json  # Choose highest priority, no blockers
   ```

2. **Reserve edit surface (Mail):**
   ```
   file_reservation_paths(project_key, agent_name, ["app/**"], ttl_seconds=3600, exclusive=true, reason="br-123")
   ```

3. **Announce start (Mail):**
   ```
   send_message(..., thread_id="br-123", subject="[br-123] Start: <title>", ack_required=true)
   ```

4. **Work and update:** Reply in-thread with progress

5. **Complete and release:**
   ```bash
   br close 123 --reason "Completed"
   br sync --flush-only  # Export to JSONL (no git operations)
   ```
   ```
   release_file_reservations(project_key, agent_name, paths=["app/**"])
   ```
   Final Mail reply: `[br-123] Completed` with summary

### Event Mirroring & Pitfalls

- If `br update --status blocked`, send a high-importance Mail note in the matching thread with details
- If Mail shows "ACK overdue" for a decision, label the Beads issue (e.g., `needs-ack`) or adjust its priority
- Do **not** open/track tasks solely inside Mail; always keep Beads in sync
- Always include the Beads ID in Mail thread IDs to avoid drift between systems

### Mapping Cheat Sheet

| Concept | Value |
|---------|-------|
| Mail `thread_id` | `br-###` |
| Mail subject | `[br-###] ...` |
| File reservation `reason` | `br-###` |
| Commit messages | Include `br-###` for traceability |

---

## bv — Graph-Aware Triage Engine

bv is a graph-aware triage engine for Beads projects (`.beads/beads.jsonl`). It computes PageRank, betweenness, critical path, cycles, HITS, eigenvector, and k-core metrics deterministically.

**Scope boundary:** bv handles *what to work on* (triage, priority, planning). For agent-to-agent coordination (messaging, work claiming, file reservations), use MCP Agent Mail.

**CRITICAL: Use ONLY `--robot-*` flags. Bare `bv` launches an interactive TUI that blocks your session.**

### The Workflow: Start With Triage

**`bv --robot-triage` is your single entry point.** It returns:
- `quick_ref`: at-a-glance counts + top 3 picks
- `recommendations`: ranked actionable items with scores, reasons, unblock info
- `quick_wins`: low-effort high-impact items
- `blockers_to_clear`: items that unblock the most downstream work
- `project_health`: status/type/priority distributions, graph metrics
- `commands`: copy-paste shell commands for next steps

```bash
bv --robot-triage        # THE MEGA-COMMAND: start here
bv --robot-next          # Minimal: just the single top pick + claim command
```

### Command Reference

**Planning:**
| Command | Returns |
|---------|---------|
| `--robot-plan` | Parallel execution tracks with `unblocks` lists |
| `--robot-priority` | Priority misalignment detection with confidence |

**Graph Analysis:**
| Command | Returns |
|---------|---------|
| `--robot-insights` | Full metrics: PageRank, betweenness, HITS, eigenvector, critical path, cycles, k-core, articulation points, slack |
| `--robot-label-health` | Per-label health: `health_level`, `velocity_score`, `staleness`, `blocked_count` |
| `--robot-label-flow` | Cross-label dependency: `flow_matrix`, `dependencies`, `bottleneck_labels` |
| `--robot-label-attention [--attention-limit=N]` | Attention-ranked labels |

**History & Change Tracking:**
| Command | Returns |
|---------|---------|
| `--robot-history` | Bead-to-commit correlations |
| `--robot-diff --diff-since <ref>` | Changes since ref: new/closed/modified issues, cycles |

**Other:**
| Command | Returns |
|---------|---------|
| `--robot-burndown <sprint>` | Sprint burndown, scope changes, at-risk items |
| `--robot-forecast <id\|all>` | ETA predictions with dependency-aware scheduling |
| `--robot-alerts` | Stale issues, blocking cascades, priority mismatches |
| `--robot-suggest` | Hygiene: duplicates, missing deps, label suggestions |
| `--robot-graph [--graph-format=json\|dot\|mermaid]` | Dependency graph export |
| `--export-graph <file.html>` | Interactive HTML visualization |

### Scoping & Filtering

```bash
bv --robot-plan --label backend              # Scope to label's subgraph
bv --robot-insights --as-of HEAD~30          # Historical point-in-time
bv --recipe actionable --robot-plan          # Pre-filter: ready to work
bv --recipe high-impact --robot-triage       # Pre-filter: top PageRank
bv --robot-triage --robot-triage-by-track    # Group by parallel work streams
bv --robot-triage --robot-triage-by-label    # Group by domain
```

### Understanding Robot Output

**All robot JSON includes:**
- `data_hash` — Fingerprint of source beads.jsonl
- `status` — Per-metric state: `computed|approx|timeout|skipped` + elapsed ms
- `as_of` / `as_of_commit` — Present when using `--as-of`

**Two-phase analysis:**
- **Phase 1 (instant):** degree, topo sort, density
- **Phase 2 (async, 500ms timeout):** PageRank, betweenness, HITS, eigenvector, cycles

### jq Quick Reference

```bash
bv --robot-triage | jq '.quick_ref'                        # At-a-glance summary
bv --robot-triage | jq '.recommendations[0]'               # Top recommendation
bv --robot-plan | jq '.plan.summary.highest_impact'        # Best unblock target
bv --robot-insights | jq '.status'                         # Check metric readiness
bv --robot-insights | jq '.Cycles'                         # Circular deps (must fix!)
```

---

## UBS — Ultimate Bug Scanner

**Golden Rule:** `ubs <changed-files>` before every commit. Exit 0 = safe. Exit >0 = fix & re-run.

### Commands

```bash
ubs file.py file2.py                        # Specific files (< 1s) — USE THIS
ubs $(git diff --name-only --cached)        # Staged files — before commit
ubs --only=python src/                      # Language filter (3-5x faster)
ubs --ci --fail-on-warning .                # CI mode — before PR
ubs .                                       # Whole project
```

### Output Format

```
  Category (N errors)
    file.py:42:5 - Issue description
    Suggested fix
Exit code: 1
```

Parse: `file:line:col` -> location | fix suggestion | Exit 0/1 -> pass/fail

### Fix Workflow

1. Read finding -> category + fix suggestion
2. Navigate `file:line:col` -> view context
3. Verify real issue (not false positive)
4. Fix root cause (not symptom)
5. Re-run `ubs <file>` -> exit 0
6. Commit

### Bug Severity

- **Critical (always fix):** Command injection, unquoted variables, eval with user input, SQL injection
- **Important (production):** Missing error handling, unset variables, unsafe pipes
- **Contextual (judgment):** TODO/FIXME, echo debugging

---

## RCH — Remote Compilation Helper

RCH offloads `cargo build`, `cargo test`, `cargo clippy`, and other compilation commands to a fleet of 8 remote Contabo VPS workers instead of building locally. This prevents compilation storms from overwhelming csd when many agents run simultaneously.

**RCH is installed at `~/.local/bin/rch` and is hooked into Claude Code's PreToolUse automatically.** Most of the time you don't need to do anything if you are Claude Code — builds are intercepted and offloaded transparently.

To manually offload a build:
```bash
rch exec -- cargo build --release
rch exec -- cargo test
rch exec -- cargo clippy
```

Quick commands:
```bash
rch doctor                    # Health check
rch workers probe --all       # Test connectivity to all 8 workers
rch status                    # Overview of current state
rch queue                     # See active/waiting builds
```

If rch or its workers are unavailable, it fails open — builds run locally as normal.

**Note for Codex/GPT-5.2:** Codex does not have the automatic PreToolUse hook, but you can (and should) still manually offload compute-intensive compilation commands using `rch exec -- <command>`. This avoids local resource contention when multiple agents are building simultaneously.

---

## ast-grep vs ripgrep

**Use `ast-grep` when structure matters.** It parses code and matches AST nodes, ignoring comments/strings, and can **safely rewrite** code.

- Refactors/codemods: rename APIs, change import forms
- Policy checks: enforce patterns across a repo
- Editor/automation: LSP mode, `--json` output

**Use `ripgrep` when text is enough.** Fastest way to grep literals/regex.

- Recon: find strings, TODOs, log lines, config values
- Pre-filter: narrow candidate files before ast-grep

### Rule of Thumb

- Need correctness or **applying changes** -> `ast-grep`
- Need raw speed or **hunting text** -> `rg`
- Often combine: `rg` to shortlist files, then `ast-grep` to match/modify

### Python Examples

```bash
# Find structured code (ignores comments)
ast-grep run -l Python -p 'def $NAME($$$ARGS): $$$BODY'

# Find all bare except clauses
ast-grep run -l Python -p 'except: $$$'

# Quick textual hunt
rg -n 'os.getenv' -t py

# Combine speed + precision
rg -l -t py 'os\.getenv' | xargs ast-grep run -l Python -p 'os.getenv($$$)' --json
```

### Advanced ast-grep Rules

#### Ban `await` inside `Promise.all([...])` (TypeScript, auto-fix)

```yaml
# rules/no-await-in-promise-all.yml
id: no-await-in-promise-all
language: typescript
rule:
  pattern: await $A
  inside:
    pattern: Promise.all($_)
    stopBy:
      not: { any: [{kind: array}, {kind: arguments}] }
fix: $A
```

#### Imports without a file extension (TypeScript, flag all)

```yaml
# rules/find-import-file-without-ext.yml
id: find-import-file
language: typescript
rule:
  regex: "/[^.]+[^/]$"
  kind: string_fragment
  any:
    - inside: { stopBy: end, kind: import_statement }
    - inside:
        stopBy: end
        kind: call_expression
        has: { field: function, regex: "^import$" }
```

#### Find usages of a specifically imported symbol (TypeScript)

```yaml
# rules/find-import-usage.yml
id: find-import-usage
language: typescript
rule:
  kind: identifier
  pattern: $MOD
  inside:
    stopBy: end
    kind: program
    has:
      kind: import_statement
      has:
        stopBy: end
        kind: import_specifier
        pattern: $MOD
```

#### Prefer `||=` over `a = a || b` (TypeScript, tight codemod)

```bash
ast-grep -p '$A = $A || $B' -r '$A ||= $B' -l ts
```

#### Disallow `console` except `console.error` inside `catch` (TypeScript, policy)

```yaml
# rules/no-console-except-error.yml
id: no-console-except-error
language: typescript
rule:
  any:
    - pattern: console.error($$$)
      not: { inside: { kind: catch_clause, stopBy: end } }
    - pattern: console.$METHOD($$$)
constraints:
  METHOD: { regex: "log|debug|warn" }
```

#### React/TSX: replace `cond && <JSX/>` with ternary (auto-fix)

```yaml
# rules/no-and-short-circuit-in-jsx.yml
id: no-and-short-circuit-in-jsx
language: tsx
rule:
  kind: jsx_expression
  has: { pattern: $A && $B }
  not: { inside: { kind: jsx_attribute } }
fix: "{$A ? $B : null}"
```

#### TSX/SVG: hyphenated attributes to camelCase (auto-fix with transform)

```yaml
# rules/svg-attr-to-camel.yml
id: rewrite-svg-attribute
language: tsx
rule:
  pattern: $PROP
  regex: ([a-z]+)-([a-z])
  kind: property_identifier
  inside: { kind: jsx_attribute }
transform:
  NEW_PROP: { convert: { source: $PROP, toCase: camelCase } }
fix: $NEW_PROP
```

#### HTML/Vue: `:visible` to `:open` on specific tags (scoped rewrite)

```yaml
# rules/antd-visible-to-open.yml
id: upgrade-ant-design-vue
language: html
utils:
  inside-tag:
    inside:
      kind: element
      stopBy: { kind: element }
      has:
        stopBy: { kind: tag_name }
        kind: tag_name
        pattern: $TAG_NAME
rule:
  kind: attribute_name
  regex: :visible
  matches: inside-tag
constraints:
  TAG_NAME: { regex: a-modal|a-tooltip }
fix: :open
```

#### C/C++: fix format-string vulnerabilities (auto-insert `"%s"`)

```yaml
# rules/cpp-fmt-string.yml
id: fix-format-security-error
language: cpp
rule: { pattern: $PRINTF($S, $VAR) }
constraints:
  PRINTF: { regex: "^sprintf|fprintf$" }
  VAR:
    not:
      any:
        - { kind: string_literal }
        - { kind: concatenated_string }
fix: $PRINTF($S, "%s", $VAR)
```

#### YAML configs: flag host/port and emit a custom message (linting)

```yaml
# rules/detect-host-port.yml
id: detect-host-port
language: yaml
message: You are using $HOST on Port $PORT, please change it to 8000
severity: error
rule:
  any:
    - pattern: "port: $PORT"
    - pattern: "host: $HOST"
```

#### Multi-step codemod (XState v4 to v5) with `utils` and `transform`

```yaml
# rules/xstate-migration.yml
id: migrate-import-name
utils:
  FROM_XS: { kind: import_statement, has: { kind: string, regex: xstate } }
  XS_EXPORT:
    kind: identifier
    inside: { has: { matches: FROM_XS }, stopBy: end }
rule: { regex: ^Machine|interpret$, pattern: $IMPT, matches: XS_EXPORT }
transform:
  STEP1: { replace: { by: create$1, replace: (Machine), source: $IMPT } }
  FINAL: { replace: { by: createActor, replace: interpret, source: $STEP1 } }
fix: $FINAL
---
id: migrate-to-provide
rule: { pattern: $MACHINE.withConfig }
fix: $MACHINE.provide
---
id: migrate-to-actors
rule:
  kind: property_identifier
  regex: ^services$
  inside: { pattern: $M.withConfig($$$ARGS), stopBy: end }
fix: actors
```

#### When one-node rewrites are not enough: use Rewriters

```yaml
# rules/barrel-to-single.yml
id: barrel-to-single
language: javascript
rule: { pattern: "import {$$$IDENTS} from './module'" }
rewriters:
- id: rewrite-identifier
  rule: { kind: identifier, pattern: $IDENT }
  transform: { LIB: { convert: { source: $IDENT, toCase: lowerCase } } }
  fix: "import $IDENT from './module/$LIB'"
transform:
  IMPORTS:
    rewrite:
      rewriters: [rewrite-identifier]
      source: $$$IDENTS
      joinBy: "\n"
fix: $IMPORTS
```

### Practical Heuristics (Structure vs. Text)

- **Structure-sensitive changes** or **bulk refactors** -> `ast-grep` rules (often YAML) with `inside/has/not`, `constraints`, `transform`, and sometimes `rewriters`
- **Fast reconnaissance** or **non-code assets** -> `ripgrep`; pipe file lists into `ast-grep` when precision or rewriting begins

---

## Morph Warp Grep — AI-Powered Code Search

**Use `mcp__morph-mcp__warp_grep` for exploratory "how does X work?" questions.** An AI agent expands your query, greps the codebase, reads relevant files, and returns precise line ranges with full context.

**Use `ripgrep` for targeted searches.** When you know exactly what you're looking for.

**Use `ast-grep` for structural patterns.** When you need AST precision for matching/rewriting.

### When to Use What

| Scenario | Tool | Why |
|----------|------|-----|
| "How is OCR tiling implemented?" | `warp_grep` | Exploratory; don't know where to start |
| "Where is the viewport sweep handler?" | `warp_grep` | Need to understand architecture |
| "Find all uses of `pyvips`" | `ripgrep` | Targeted literal search |
| "Find files with `os.getenv`" | `ripgrep` | Simple pattern |
| "Replace all `os.getenv` with decouple" | `ast-grep` | Structural refactor |

### warp_grep Usage

```
mcp__morph-mcp__warp_grep(
  repoPath: "/dp/markdown_web_browser",
  query: "How does the capture system handle viewport sweeps?"
)
```

Returns structured results with file paths, line ranges, and extracted code snippets.

### Anti-Patterns

- **Don't** use `warp_grep` to find a specific function name -> use `ripgrep`
- **Don't** use `ripgrep` to understand "how does X work" -> wastes time with manual reads
- **Don't** use `ripgrep` for codemods -> risks collateral edits

---

## cass — Cross-Agent Session Search

`cass` indexes prior agent conversations (Claude Code, Codex, Cursor, Gemini, ChatGPT, etc.) so we can reuse solved problems.

**Rules:** Never run bare `cass` (TUI). Always use `--robot` or `--json`.

### Examples

```bash
cass health
cass search "OCR tiling" --robot --limit 5
cass view /path/to/session.jsonl -n 42 --json
cass expand /path/to/session.jsonl -n 42 -C 3 --json
cass capabilities --json
cass robot-docs guide
```

### Tips

- Use `--fields minimal` for lean output
- Filter by agent with `--agent`
- Use `--days N` to limit to recent history

stdout is data-only, stderr is diagnostics; exit code 0 means success.

Treat cass as a way to avoid re-solving problems other agents already handled.

<!-- bv-agent-instructions-v1 -->

---

## Beads Workflow Integration

This project uses [beads_rust](https://github.com/Dicklesworthstone/beads_rust) (`br`) for issue tracking. Issues are stored in `.beads/` and tracked in git.

**Important:** `br` is non-invasive—it NEVER executes git commands. After `br sync --flush-only`, you must manually run `git add .beads/ && git commit`.

### Essential Commands

```bash
# View issues (launches TUI - avoid in automated sessions)
bv

# CLI commands for agents (use these instead)
br ready              # Show issues ready to work (no blockers)
br list --status=open # All open issues
br show <id>          # Full issue details with dependencies
br create --title="..." --type=task --priority=2
br update <id> --status=in_progress
br close <id> --reason "Completed"
br close <id1> <id2>  # Close multiple issues at once
br sync --flush-only  # Export to JSONL (NO git operations)
```

### Workflow Pattern

1. **Start**: Run `br ready` to find actionable work
2. **Claim**: Use `br update <id> --status=in_progress`
3. **Work**: Implement the task
4. **Complete**: Use `br close <id>`
5. **Sync**: Run `br sync --flush-only` then manually commit

### Key Concepts

- **Dependencies**: Issues can block other issues. `br ready` shows only unblocked work.
- **Priority**: P0=critical, P1=high, P2=medium, P3=low, P4=backlog (use numbers, not words)
- **Types**: task, bug, feature, epic, question, docs
- **Blocking**: `br dep add <issue> <depends-on>` to add dependencies

### Session Protocol

**Before ending any session, run this checklist:**

```bash
git status              # Check what changed
git add <files>         # Stage code changes
br sync --flush-only    # Export beads to JSONL
git add .beads/         # Stage beads changes
git commit -m "..."     # Commit everything together
git push                # Push to remote
```

### Best Practices

- Check `br ready` at session start to find available work
- Update status as you work (in_progress -> closed)
- Create new issues with `br create` when you discover tasks
- Use descriptive titles and set appropriate priority/type
- Always `br sync --flush-only && git add .beads/` before ending session

<!-- end-bv-agent-instructions -->

## Landing the Plane (Session Completion)

**When ending a work session**, you MUST complete ALL steps below.

**MANDATORY WORKFLOW:**

1. **File issues for remaining work** - Create issues for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **Sync beads** - `br sync --flush-only` to export to JSONL
5. **Hand off** - Provide context for next session

---

## Note for Codex/GPT-5.2

You constantly bother me and stop working with concerned questions that look similar to this:

```
Unexpected changes (need guidance)

- Working tree still shows edits I did not make in pyproject.toml, app/capture.py, app/ocr.py, tests/smoke_capture.spec.ts. Please advise whether to keep/commit/revert these before any further work. I did not touch them.

Next steps (pick one)

1. Decide how to handle the unrelated modified files above so we can resume cleanly.
```

NEVER EVER DO THAT AGAIN. The answer is literally ALWAYS the same: those are changes created by the potentially dozen of other agents working on the project at the same time. This is not only a common occurrence, it happens multiple times PER MINUTE. The way to deal with it is simple: you NEVER, under ANY CIRCUMSTANCE, stash, revert, overwrite, or otherwise disturb in ANY way the work of other agents. Just treat those changes identically to changes that you yourself made. Just fool yourself into thinking YOU made the changes and simply don't recall it for some reason.

---

## Note on Built-in TODO Functionality

Also, if I ask you to explicitly use your built-in TODO functionality, don't complain about this and say you need to use beads. You can use built-in TODOs if I tell you specifically to do so. Always comply with such orders.
