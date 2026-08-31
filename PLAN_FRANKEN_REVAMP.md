# PLAN_FRANKEN_REVAMP.md — Markdown Web Browser on the Franken Stack

> **Goal:** Turn `markdown_web_browser` (mdwb) into a vastly more compelling,
> faster, more agent-intuitive product by re-platforming its hot paths on
> Rust crates the user already maintains (`asupersync`, `frankensqlite`,
> `frankensearch`, `frankentui`, `franken_markdown`, `franken_ocr` aka
> `focr`). Keep Python where Python is right (FastAPI surface, CLI glue,
> prompt engineering); let Rust do what Rust is best at (structured
> concurrency, durable storage, vector search, OCR decoding, terminal UIs,
> native markdown rendering).
>
> **Audience:** A fresh coding agent who has never seen the project must be
> able to pick up any bead and execute it from the description alone.

---

## 0. Why this plan exists (the problem statement)

`markdown_web_browser` is a FastAPI + Playwright + pyvips + SQLModel + sqlite-vec
service that turns URLs into auditable Markdown. It is well-architected and
already works, but three structural facts blunt its agent-ergonomics:

1. **Hot loops are sync-on-async.** Playwright drives everything on a
   single asyncio loop; tile slicing happens in-process; OCR submission is
   throttled by a hand-rolled concurrency autopilot
   (`app/ocr_policy.py:1-297`, `app/ocr_client.py:1-1585`).
   Cancellation is best-effort; a hung OCR request blocks the watchdog
   in `app/jobs.py:1-1189`.
2. **Storage is single-writer SQLite + sqlite-vec.** `app/store.py:1-955`
   wraps a single `runs.db`; concurrent captures serialize on the writer
   and embeddings are stored as opaque BLOBs in a vec0 virtual table.
   `app/embeddings.py:1-239` re-implements vector search in Python.
3. **The "agent surface" is a polyglot of typer sub-commands** plus two
   starter scripts (`scripts/agents/summarize_article.py`,
   `scripts/agents/generate_todos.py`) that already duplicate logic.
   There is no canonical agent JSON contract — every command invents its
   own shape.

The Franken ecosystem directly addresses all three:

| Franken crate | What it brings | Why it matters here |
| --- | --- | --- |
| `asupersync` 0.4.9 | Structured-concurrency runtime (`Cx`, `Scope`, obligations), cancel-correct channels, native SQLite pool, gRPC, HTTP/2, browser-core wasm exports | Replace ad-hoc `asyncio` lifecycle with `&Cx` flows; turn OCR submission into a `Region` of `Cx::spawn` tasks; offer a wasm-free `asupersync-tokio-compat` shim so FastAPI stays on uvicorn if needed |
| `frankensqlite` (Rust) | Pure-Rust SQLite rewrite with MVCC concurrent writers, FFI-stable, drop-in `rusqlite` API | Replace `app/store.py`'s single-writer DB; let JobManager fan out N writers per capture; keep SQLModel for typed schemas |
| `frankensearch` 0.2.3 | Hybrid lexical + semantic search; two-tier (lexical + vector) RRF fusion; Reranker; ASUP-backed daemon | Replace `app/embeddings.py`'s sqlite-vec +1536-dim search with a proper two-tier index that is indexable from CLI + agent via `--json` envelopes |
| `frankentui` (ftui) | Pure-Rust terminal UI engine with rendering pipeline, layout, themes | Build a local agent CLI that inspects runs in a TUI: tile grid, seam telemetry, OCR queues, search panel |
| `franken_markdown` (fmd) | Pure-Rust Markdown → HTML / PDF with Knuth-Plass justification, deterministic hyphenation, font subsetting | Render job Markdown into the Browser UI without round-tripping through Python; bundle the rendering into `out.md` artifacts and `bundle.tar.zst` |
| `franken_ocr` (focr) | Pure-Rust CPU-only OCR binary; agent robot NDJSON interface; int8 SIMD kernels; model auto-detect | Drop the `app/local_ocr.py:1-649` Python vLLM/SGLang glue; `focr ocr-batch` already gives us a single-process, low-RAM throughput path that the existing `app/ocr_client.py:1-1585` struggles to match |

This plan turns each of those facts into a concrete work stream with
migratable boundaries, observable wins, and revert paths.

---

## 1. New product shape (the "what we're building")

The existing product surface stays. The changes are additive.

### 1.1 New artifacts (every job now ships these)

| Artifact | Path | Purpose |
| --- | --- | --- |
| `out.md` | unchanged | Final stitched Markdown |
| `out.html` | **NEW** | `fmd`-rendered HTML (deterministic, font-subsetted) for in-browser preview and PDF export |
| `out.pdf` | **NEW** (opt-in) | `fmd` PDF with Knuth-Plass justification + discretionary hyphenation |
| `links.json` | unchanged | DOM-extracted anchors/forms/headings |
| `tiles.manifest.jsonl` | **NEW** | Per-tile provenance + hashes in JSONL so a downstream tool can stream them without parsing the heavy `manifest.json` |
| `index.fdb` | **NEW** | FrankenSQLite database holding `runs`, `tiles`, `links`, `events`, `embeddings`; replaces the per-job `runs.db` row |
| `search.idx` | **NEW** | Frankensearch index for this run; enables `mdwb search <job-id> <query>` |
| `bundle.tar.zst` | unchanged | Adds `out.html`, `index.fdb`, `search.idx`, `tiles.manifest.jsonl` |

### 1.2 New CLI surface (additive only; nothing existing removed)

```
mdwb fetch <url>                       (unchanged)
mdwb search <job-id> "<query>"          # NEW — frankensearch over sections
mdwb index <job-id> [--rebuild]         # NEW — (re)build search.idx + index.fdb
mdwb doctor                             # NEW — single-binary self-check (focr selftest, fmd doctor, fs doctor)
mdwb tui                                # NEW — frankentui TUI for live + historical jobs
mdwb export <job-id> [--html|--pdf|--all]   # NEW — render out.html / out.pdf via fmd
mdwb replay <manifest.json> --ocr-policy=focr      # NEW — pick focr for local replay
mdwb benchmark --backend=focr --urls=... # NEW — agent-friendly benchmark envelope
mdwb agent <verb> <args...>             # NEW — single canonical agent entry point
                                          verbs: capture, search, summarize,
                                          todos, extract, cite, watch
```

### 1.3 New agent contract (the "single JSON envelope")

Every `mdwb agent <verb>` returns the same envelope, irrespective of which
backing crate produced the data:

```jsonc
{
  "schema": "mdwb.agent.v1",
  "verb": "capture",
  "ok": true,
  "started_at": "2026-08-29T18:30:00Z",
  "finished_at": "2026-08-29T18:30:42Z",
  "elapsed_ms": 42000,
  "data": { /* verb-specific payload */ },
  "diagnostics": {
    "backend": "focr",          // or "olmocr-hosted"
    "cft_version": "131.0.6778.85",
    "playwright_version": "1.50.1",
    "focr_simd": "avx2",
    "focr_model_license": "Baidu Unlimited-OCR - Copyright (c) 2026 Baidu, MIT License",
    "frankensqlite_version": "0.4.7",
    "frankensearch_version": "0.2.3",
    "fmd_version": "0.6.1"
  },
  "warnings": [],
  "next_actions": [
    "mdwb agent search <job-id> \"earnings\" --top-k=5",
    "mdwb export <job-id> --html"
  ]
}
```

This replaces the per-command JSON shape currently spread across Typer
sub-commands (`scripts/mdwb_cli.py:1-2788`). Old commands keep working;
new verbs emit envelopes.

### 1.4 New runtime shape (the "Franken-runtime")

```
            ┌──────────────────────────────────────────────┐
            │   mdwb-tui (frankentui)                      │
            │   ────────────────────                       │
            │   Tabbed TUI: Tiles | Search | Events | OCR │
            └────────────────────┬─────────────────────────┘
                                 │ MCP / IPC over Unix socket
                                 ▼
┌──────────────────────────────────────────────────────────────┐
│   mdwb-runtime  (Rust, asupersync-native)                    │
│                                                              │
│   ├── capture-service     ← orchestrates Playwright over CDP │
│   ├── tiling-service      ← pyvips via libvips; bundled .so  │
│   ├── ocr-pool            ← focr-batch subprocess + hosted   │
│   ├── stitch-service      ← franken_markdown → out.md/.html  │
│   ├── search-service      ← frankensearch daemon client      │
│   ├── store-service       ← frankensqlite (MVCC) + FDB files │
│   └── events-bus          ← structured-concurrency cancel    │
│           cross all services, mandatory `&Cx` everywhere     │
└────────────────────┬─────────────────────────────────────────┘
                     │ HTTP/1.1 + JSON (FastAPI stays as the
                     │ "user-facing" Python control plane)
                     ▼
            ┌──────────────────────────────────────────────┐
            │   FastAPI + Typer CLI (Python)               │
            │   ────────────────────                       │
            │   /jobs/* (unchanged contract)               │
            │   mdwb agent <verb> (NEW canonical JSON)     │
            │   mdwb doctor (NEW)                          │
            └──────────────────────────────────────────────┘
```

The Rust runtime is a separate process (`mdwb-runtime`) the Python side
talks to via Unix socket / loopback HTTP. This keeps Python's
ergonomic FastAPI surface and lets Rust own the long-running, hot,
structured-concurrency-friendly paths.

---

## 2. Non-negotiables (the "what we won't do")

1. **No drop-in breakage.** Every existing test must keep passing; every
   existing CLI flag must keep working. New behaviour is additive.
2. **No new required env vars** for users who don't enable Rust paths.
   The Python-only path remains the default.
3. **No async-blocking sync code** sneaks back in. Anything in the Rust
   runtime uses `&Cx` and `cx.checkpoint()`; anything in Python uses
   `asyncio.to_thread()`.
4. **No rewrite of capture logic.** Playwright + viewport sweep are the
   correct tool; we keep `app/capture.py:1-643` mostly untouched. The
   Rust side hosts Playwright over CDP only.
5. **No proprietary leaks.** All new crates are MIT/Apache; we don't
   ship Baidu model weights; `focr` models are downloaded on demand by
   `focr pull`.
6. **No regression in smoke SLOs.** Capture p95, OCR p95, total p95 stay
   within `benchmarks/production_set.json` budgets.

---

## 3. Workstreams (the "what beads we'll create")

Each workstream is a phase that contains a small set of beads. Bead
IDs follow `markdown_web_browser-<3-letter>`; dependencies form a DAG.

### Phase 0 — Repo hygiene & proof scaffold (foundation)

Establishes the tooling needed for every later phase.

| Bead | Title | Outcome | Blocks |
| --- | --- | --- | --- |
| `mdwb-001` | Pin Franken crate versions in `pyproject.toml` extras | Add `[franken]` optional extra depending on the Rust binaries' Python helpers; document paths in `.env.example`. | All Rust-touching beads |
| `mdwb-002` | Add `mdwb doctor` Python CLI that calls `focr robot health`, `focr robot selftest`, `fmd doctor`, `fmd verify --json` (when applicable), `frankensqlite` version probe, `asupersync` (via env-check), and the existing `check_env.py`. Single JSON envelope. | Provides live readiness proof. | All later phases |
| `mdwb-003` | Add `scripts/proof/run_franken_greets.sh` that exercises every Franken binary, asserts exit 0, writes `benchmarks/proof/franken_greets.json` with versions + selftest verdicts. Wired into `scripts/run_checks.sh` so CI fails when any binary regresses. | Single-source-of-truth for "the Franken pieces are live." | All Rust-touching beads |
| `mdwb-004` | Resolve-beads-protocol `markdown_web_browser-3px` import problem + commit the recovered `beads.db` (this is the immediate repo fix you saw at the top of this session). | Unblocks the rest of the planning work and the AGENTS.md-mandated bead workflow. | All planning beads |

### Phase 1 — Rust runtime scaffold (the new heartbeat)

Builds the `mdwb-runtime` binary that hosts capture + OCR + stitch.

| Bead | Title | Outcome | Blocks |
| --- | --- | --- | --- |
| `mdwb-010` | Scaffold `crates/mdwb-runtime` Cargo workspace under a new top-level `runtime/` directory. `Cargo.toml` deps: `asupersync` 0.4.9, `frankensqlite`, `frankensearch-ops`, `serde`, `clap`. Crate layout: `crates/mdwb-runtime/{src/{main.rs,capture,ocr,store,events,ipc}}.rs`. `asupersync::main` boots a `RuntimeBuilder` and exposes a `mdwb.runtime.v1` JSON-RPC over a Unix socket. | A Rust binary that starts, listens on `$XDG_RUNTIME_DIR/mdwb.sock`, and answers `ping`. | `mdwb-011`, `mdwb-012`, `mdwb-013` |
| `mdwb-011` | `mdwb-runtime` IPC: define `mdwb.runtime.v1` request/response schemas (capture, search, events.subscribe, store.append, ocr.submit, version). Use `asupersync` codec (length-delimited JSON Lines for now; `postcard` for hot paths). Implement `runtime.ipc.selftest` and wire into `mdwb doctor`. | Python can talk to Rust. | `mdwb-012`, `mdwb-013`, `mdwb-020` |
| `mdwb-012` | Structured-concurrency scaffold: a `Region` per capture with `Cx::spawn` for capture→tile→OCR→stitch→store. Each task carries an obligation token so cancellation propagates and partial results emit `state:FAILED` cleanly. `cx.checkpoint()` at every loop turn. Documented in `crates/mdwb-runtime/README.md`. | Hard proof that the runtime cancels deterministically; replaces `app/jobs.py:780-990` watchdog. | `mdwb-021`, `mdwb-030` |
| `mdwb-013` | `asupersync` native SQLite pool (calls into `frankensqlite` via the `asupersync::database` module) wrapping a single `index.fdb` file per job. Schema mirrors `app/store.py:200-500` RunRecord/LinkRecord/WebhookRecord tables. | FrankenSQLite becomes the durable store; Python `Store` reads through it. | `mdwb-014`, `mdwb-020` |

### Phase 2 — Capture delegation (Playwright stays Python)

Capture is correct as Python; we make Rust orchestrate it.

| Bead | Title | Outcome | Blocks |
| --- | --- | --- | --- |
| `mdwb-020` | `mdwb-runtime` capture-controller: spawns a `python -m app.headless_capture` child via `asupersync::process` (existing `app/capture.py:1-643` is unchanged). Child speaks `mdwb.runtime.v1` events back. Rust owns the state machine + cancellation. | Single source of truth for job state lives in Rust. | `mdwb-021` |
| `mdwb-021` | Streaming event bus: each capture stage emits `state`, `progress`, `manifest_delta`, `tile_ready`, `ocr_done`, `stitch_done`. Subscribers via `events.subscribe` get JSONL. Mirrors `app/jobs.py:380-540` event log + SSE. | One path for SSE, CLI tail, and TUI. | `mdwb-031` |

### Phase 3 — Local OCR via `focr` (drop-in for `app/local_ocr.py`)

| Bead | Title | Outcome | Blocks |
| --- | --- | --- | --- |
| `mdwb-030` | New `app/ocr_backends/focr.py` adapter: launches `focr ocr-batch` once per capture (subprocess), pipes per-tile paths, parses `focr robot run` NDJSON (stage / page / run_complete / run_error), returns Markdown per tile + bbox JSON. Existing `app/ocr_client.py:1-1585` keeps olmOCR hosted path. Settings gate via `MDWB_OCR_BACKEND=focr\|olmocr-hosted\|auto`. | Local OCR latency drops from ~3-5s/tile (vLLM) to ~0.4-0.8s/tile (focr batch). | `mdwb-031`, `mdwb-032` |
| `mdwb-031` | Backend capability matrix in `app/ocr_policy.py` (already exists at `app/ocr_policy.py:1-297`): add `focr` candidate with `BACKEND_MODE_FOCR = "focr"`; reason codes `policy.focr.local-preferred`, `policy.focr.cpu-only`. Update `app/settings.py` (`OCRSettings.focr_path`, `OCRSettings.focr_model_dir`). | OCR autopilot picks `focr` when local + healthy. | `mdwb-032`, `mdwb-040` |
| `mdwb-032` | `focr` benchmark harness: `scripts/bench_focr.py` runs `focr ocr-batch` over a fixed corpus (`benchmarks/production_set.json`) and emits a JSON + Markdown summary. Add to nightly smoke via `scripts/run_smoke.py --backend=focr`. | Live proof that local OCR keeps SLOs. | `mdwb-040` |

### Phase 4 — Storage & search (FrankenSQLite + Frankensearch)

| Bead | Title | Outcome | Blocks |
| --- | --- | --- | --- |
| `mdwb-040` | `app/store.py` migration: `Store` opens `index.fdb` via a `pyo3`-style helper (or via `subprocess mdwb-runtime store.open --db path` until a pyo3 binding lands). RunRecord/LinkRecord/WebhookRecord tables moved to FrankenSQLite. Existing SQLModel definitions become read-through wrappers. | Single MVCC store; concurrent captures stop serializing. | `mdwb-041` |
| `mdwb-041` | Embeddings migration: replace `app/embeddings.py:1-239` sqlite-vec logic with a `frankensearch-index` daemon (per the `frankensearch` TUI/daemon model at `/dp/frankensearch/crates/frankensearch-daemon`). `Store.embedding_search` becomes `mdwb-runtime search.embedding --query="…" --top-k=N`. Two-tier (lexical + vector) RRF fusion. | 10-100× faster, semantic-aware section search. | `mdwb-042`, `mdwb-043` |
| `mdwb-042` | `mdwb search <job-id> "<query>" --top-k=5 --json` CLI. Wraps `frankensearch-ops`. Emits the new agent envelope. | One canonical agent search verb. | `mdwb-043` |
| `mdwb-043` | Cross-job search: `mdwb search --all "<query>"` queries the global index. Stored in `~/.cache/mdwb/global.idx`. | Search across every capture an agent has ever made. | `mdwb-052` |

### Phase 5 — Markdown rendering (Franken Markdown)

| Bead | Title | Outcome | Blocks |
| --- | --- | --- | --- |
| `mdwb-050` | `scripts/export_html.py` thin wrapper around `fmd render`. `out.html` lives next to `out.md` inside `Store`. New `POST /jobs/{id}/artifact/out.html` endpoint. | Pure-Rust Markdown rendering, deterministic, font-subsetted. | `mdwb-051`, `mdwb-052` |
| `mdwb-051` | `out.pdf` opt-in via `fmd render --to pdf`. `--export-pdf` flag on `mdwb fetch` and on `POST /jobs`. | One command, one PDF, deterministic. | `mdwb-052` |
| `mdwb-052` | Browser UI: replace hand-rolled `markdown-it` on the frontend with `<iframe src="/jobs/{id}/artifact/out.html">` for the Rendered view; keep Monaco-style raw viewer. | Native-quality rendering without JS bloat. | `mdwb-053` |

### Phase 6 — TUI & operator experience (FrankenTUI)

| Bead | Title | Outcome | Blocks |
| --- | --- | --- | --- |
| `mdwb-060` | `crates/mdwb-tui` Cargo workspace; binary `mdwb-tui`. Built on `ftui-runtime` + `ftui-widgets`. Connects to `mdwb-runtime` over the IPC socket. Tabs: **Jobs**, **Tiles**, **Search**, **Events**, **OCR queue**. | First-class terminal UI for operators and agents. | `mdwb-061`, `mdwb-062` |
| `mdwb-061` | TUI ↔ JSON-RPC: every TUI action is also a CLI verb (`mdwb-tui --headless tui.tab=jobs action=capture …` prints the agent envelope). | TUI actions are agent-callable. | `mdwb-062` |
| `mdwb-062` | TUI captures `crates/mdwb-tui/snapshots/*.snap` via `tui-inspector` skill. Golden tests in `crates/mdwb-tui/tests/snapshots.rs`. | Stable visuals, no regressions. | All later TUI work |

### Phase 7 — Agent contracts (the "single JSON envelope")

| Bead | Title | Outcome | Blocks |
| --- | --- | --- | --- |
| `mdwb-070` | `app/agent_envelope.py` defines `AgentEnvelope` Pydantic schema (schema=`mdwb.agent.v1`). All new CLI verbs emit through it. JSON-schema file generated to `schemas/mdwb.agent.v1.json`. | One envelope, multiple consumers. | `mdwb-071` |
| `mdwb-071` | `mdwb agent <verb>` umbrella CLI: `capture`, `search`, `summarize`, `todos`, `extract`, `cite`, `watch`, `doctor`. Each verb has a JSON contract and a `--text` rendering for human use. | One entry point, multiple verbs. | `mdwb-072` |
| `mdwb-072` | Python helpers in `scripts/agents/` refactored: `summarize_article.py`, `generate_todos.py` become thin wrappers around `mdwb agent summarize` / `mdwb agent todos`. Add `scripts/agents/extract.py` (numbers / dates / tables) and `cite.py` (provenance → URL list). | Agent scripts stop duplicating logic. | `mdwb-080` |
| `mdwb-073` | `mdwb agent cite <job-id> "<span>"` returns `{url, tile_id, y, sha256, viewport_y, overlap_px, path, highlight}` JSON. Renders to a CLI table by default, JSON with `--json`. | Every assertion in `out.md` is traceable. | `mdwb-080` |

### Phase 8 — Verification & rollout

| Bead | Title | Outcome | Blocks |
| --- | --- | --- | --- |
| `mdwb-080` | Add `tests/test_agent_envelope.py` that snapshots every `mdwb agent <verb>` response and asserts they pass `jsonschema` validation against `schemas/mdwb.agent.v1.json`. | Envelope is a real contract. | `mdwb-081` |
| `mdwb-081` | Add `tests/test_franken_runtime.py` that spawns `mdwb-runtime`, asserts the IPC handshake, runs a fake capture against, asserts events arrive. | Rust runtime tested from Python. | `mdwb-082` |
| `mdwb-082` | Update `scripts/run_smoke.py` so it can target `MDWB_RUNTIME_MODE=rust` or `python`. Both modes emit identical manifest fields so dashboards stay green. | Smoke covers both paths. | `mdwb-083` |
| `mdwb-083` | Update `docs/architecture.md`, `docs/ops.md`, `docs/api.md`, `docs/release_checklist.md` for the new runtime + new artifacts + new CLI verbs. | Docs match reality. | `mdwb-090` |
| `mdwb-090` | Final ship-it: `benchmarks/production/weekly_summary.json` stays green for 48 h with the Rust runtime enabled in the smoke; `focr` SLO p95 stays under `2×p95` budget; cross-model review of the new surface via `flywheel` skill. | Release-ready. | _end_ |

---

## 4. Per-bead detail (the "implementable spec")

This section expands each phase-bead into a paragraph + acceptance
criteria + tests + dependencies. Agents picking up a bead only need to
read its section.

### 4.0 `mdwb-001` — Pin Franken crate versions in `pyproject.toml`

**Why:** Without a pinned, optional `[franken]` extra, users installing
`mdwb` don't get clear guidance that they need `focr`, `fmd`, etc. We
want a single source of truth.

**What:**
- Add `[project.optional-dependencies] franken = ["…"]` listing the
  `pip`-installable helper packages if/when they ship; for now list
  binary-only deps under `[tool.mdwb.franken]` with the resolved path
  to each binary (`focr`, `fmd`, `mdwb-runtime`).
- Update `.env.example` with `MDWB_RUNTIME_MODE=python|rust`,
  `MDWB_OCR_BACKEND=olmocr-hosted|focr|auto`, `FOCR_MODEL_DIR`,
  `MDWB_FDB_PATH`.
- Update `docs/config.md` and `docs/models.yaml` with a new
  `focr` model declaration: `name: baidu-unlimited-ocr-fp8`,
  `license_notice: "Baidu Unlimited-OCR - Copyright (c) 2026 Baidu, MIT License"`.

**Acceptance:**
- `pip install -e ".[franken]"` does not error.
- `mdwb doctor --json` reports versions of each binary.
- README updated.

**Tests:** `tests/test_doctor_envelope.py` (new) asserts every required
field appears in the envelope.

**Depends on:** none.

### 4.1 `mdwb-002` — `mdwb doctor` Python CLI

**Why:** Today's `scripts/check_env.py:1-…` only checks env vars. We
need a runtime readiness signal that proves the Franken stack is alive.

**What:**
- New `scripts/mdwb_doctor.py` Typer app. Subcommands: `env`, `bin`,
  `models`, `runtime`, `runtime.start`, `runtime.stop`. Every subcommand
  has `--json` and `--text`. Combined `doctor all` emits the agent
  envelope.
- Calls `focr robot health`, `focr robot selftest` (asserts `verdict=pass`),
  `focr robot backends`, `fmd doctor --json`, `fmd capabilities --json`,
  `frankensqlite --version` (binary probe), and pings the running
  `mdwb-runtime` socket if `MDWB_RUNTIME_MODE=rust`.
- Writes `benchmarks/production/doctor_latest.json` on every invocation.

**Acceptance:** `mdwb doctor --json` exits 0 when everything is
healthy, prints a list of failures otherwise, and is wired into
`scripts/run_checks.sh`.

**Tests:** `tests/test_mwb_doctor.py` (new). Mocks all binaries with
fixtures; asserts envelope shape; asserts exit codes.

**Depends on:** `mdwb-001`, `mdwb-003`.

### 4.2 `mdwb-003` — Franken-greets proof script

**Why:** A single command that emits the live proof that the Franken
stack is alive and matching expected versions. Used by CI + smoke.

**What:** `scripts/proof/run_franken_greets.sh` runs each binary's
`--version` (or equivalent) and `selftest`/`doctor` invocation; emits
`benchmarks/proof/franken_greets.json` containing `{binary, version,
selftest_exit, selftest_verdict, capabilities_hash}`. Returns 0 only if
all verdicts are pass.

**Acceptance:** Script exits 0 on a healthy host; CI fails fast when
broken; `benchmarks/proof/franken_greets.json` is committed.

**Depends on:** none.

### 4.3 `mdwb-004` — Beads recovery

**Why:** The current `beads.db` is corrupt; `br sync --import-only`
errors with `Import semantic verification failed: issue
markdown_web_browser-3px`. Without a healthy DB the AGENTS-mandated
bead workflow can't run.

**What:**
- Snapshot the current `.beads/` to `.beads/_recovery_<ts>/`.
- Run `br sync --merge --force-jsonl` (already verified working in this
  session) so the 113 historical issues are imported.
- Run `br sync --flush-only` so the canonical JSONL is regenerated by
  br and re-committed.
- Add `benchmarks/proof/beads_health.json` to the Franken-greets
  output.

**Acceptance:** `br status` returns a clean summary; `br doctor` is
`healthy`; all 113 issues resolve; new issues can be created without
errors.

**Depends on:** none.

### 4.4 `mdwb-010` — `mdwb-runtime` scaffold

**Why:** Foundation for the entire Rust side. Without a runtime, no
later phase can run.

**What:**
- New top-level `runtime/` Cargo workspace at repo root.
- `crates/mdwb-runtime/src/main.rs`: `#[asupersync::main]` boots a
  `RuntimeBuilder` with `LocalSet` and a worker count from
  `MDWB_RUNTIME_WORKERS` (default = ncpu). Listens on
  `$XDG_RUNTIME_DIR/mdwb.sock` or `MDWB_RUNTIME_SOCKET`.
- `crates/mdwb-runtime/src/ipc.rs`: codec for `mdwb.runtime.v1`. JSON
  Lines (length-delimited, newline-terminated).
- `crates/mdwb-runtime/src/version.rs`: emits a `--version` JSON with
  binary name, asupersync version, build SHA.
- First command: `runtime.ping` → `pong`.

**Acceptance:** `cargo build --release` succeeds; running
`mdwb-runtime --socket /tmp/mdwb.sock` accepts `{"op":"runtime.ping"}`
and replies `{"op":"runtime.pong"}`; `mdwb doctor runtime` reports it
as healthy when running.

**Tests:** Rust unit tests in `crates/mdwb-runtime/tests/ipc.rs`;
integration test in `tests/test_franken_runtime.py` that uses the
socket.

**Depends on:** `mdwb-001`, `mdwb-003`.

### 4.5 `mdwb-011` — `mdwb.runtime.v1` schemas

**Why:** Without a stable, versioned contract, every verb reinvent its
own JSON shape.

**What:**
- `schemas/mdwb.runtime.v1.json` declares every op:
  `runtime.ping`, `runtime.version`, `capture.start`,
  `capture.subscribe`, `tile.submit`, `ocr.submit`, `ocr.poll`,
  `stitch.run`, `store.append`, `store.query`, `events.subscribe`,
  `search.embedding`, `search.lexical`, `search.hybrid`.
- Each op has request/response `oneOf` branches with `$ref` to inner
  schemas.
- Generated Rust types via `schemars`; Python types via `datamodel-code-generator`.

**Acceptance:** Schema file validates against Draft 2020-12; Rust types
compile; Python types import; `mdwb agent capture --dry-run --json`
emits a request that round-trips through the runtime.

**Depends on:** `mdwb-010`.

### 4.6 `mdwb-012` — Structured concurrency scaffold

**Why:** This is the single biggest reason to use `asupersync`. The
current Python side has a watchdog loop in `app/jobs.py:780-990` that
is best-effort; cancellation can leak.

**What:**
- `crates/mdwb-runtime/src/capture/orchestrator.rs`: a `Region` per
  capture that spawns `cx.spawn` children for: capture-poll, tile-decode,
  ocr-submit, ocr-poll, stitch, store-append. Each child carries an
  obligation; on cancel, obligations are revoked cleanly.
- `cx.checkpoint()` in every loop turn (decode loop, OCR poll loop).
- A `runtime.selftest.capture` op that asserts a captured-but-cancelled
  job reaches the `FAILED` state with zero leaked threads (asserted via
  `asupersync::test_utils`).

**Acceptance:** Deterministic test that proves cancellation leaves no
panics, no leaked tiles, no orphan tiles in storage; matches the
asupersync-mega-skill non-negotiables (esp. v0.4.4–v0.4.9 cancellation
contract).

**Depends on:** `mdwb-010`, `mdwb-011`.

### 4.7 `mdwb-013` — Native SQLite pool

**Why:** Replace single-writer SQLite with MVCC; let multiple captures
write concurrently.

**What:**
- `crates/mdwb-runtime/src/store/mod.rs`: opens one `index.fdb` per job
  via the `asupersync::database::SqlitePool`. Schema mirrors
  `app/store.py:200-500`.
- `crates/mdwb-runtime/src/store/migration.rs`: idempotent migration
  runner. Versions stored in `schema_version` table.
- IPC ops: `store.append` (insert/upsert), `store.query` (typed query),
  `store.snapshot` (full dump for diagnostics).

**Acceptance:** Round-trip test: insert 10k RunRecord rows from N
concurrent callers; assert no write errors; query back the same set;
SQLModel reader in Python sees the same data.

**Depends on:** `mdwb-010`, `mdwb-011`.

### 4.8 `mdwb-020` — Capture controller

**Why:** Playwright stays Python; we don't reinvent it.

**What:**
- Spawn `python -m app.headless_capture --ipc-socket=$XDG_RUNTIME_DIR/mdwb.sock`
  as a child process via `asupersync::process`. Existing
  `app/capture.py:1-643` is unchanged except: it speaks the runtime IPC
  instead of returning synchronously.
- New `app/headless_capture.py` thin wrapper (~80 LoC) that imports
  `app.capture.capture_tiles` and pipes stdout lines through the IPC.
- The orchestrator in `mdwb-runtime` owns the state machine; cancellation
  propagates as a `SIGTERM` followed by a 5-second `SIGKILL` fallback.

**Acceptance:** `mdwb agent capture --url=https://example.com --backend=focr`
returns the agent envelope with `ok=true` and a populated
`data.markdown_path`; cancellation test kills mid-capture and asserts
state=`FAILED` with `warnings[0]=capture_cancelled`.

**Depends on:** `mdwb-012`, `mdwb-013`.

### 4.9 `mdwb-021` — Streaming event bus

**Why:** Today, SSE and CLI tail go through different code paths.

**What:**
- `crates/mdwb-runtime/src/events/mod.rs`: an `asupersync::channel`
  per subscriber. `events.subscribe` op yields `state`, `progress`,
  `manifest_delta`, `tile_ready`, `ocr_done`, `stitch_done` events.
- FastAPI SSE: a Python wrapper around the IPC `events.subscribe` op
  translates events into `data: {json}\n\n` frames.
- CLI `mdwb events <job-id> --follow` is also a wrapper.

**Acceptance:** Two subscribers (a CLI and the FastAPI SSE) see the
same event stream; the JSON shape is identical to what Python emits
today (so existing tests pass); the Rust side buffers at most 1k
events per subscriber.

**Depends on:** `mdwb-011`, `mdwb-020`.

### 4.10 `mdwb-030` — `focr` local OCR adapter

**Why:** Pure-Rust, CPU-only, ~10× faster than spinning vLLM.

**What:**
- New `app/ocr_backends/focr.py`. Spawns `focr ocr-batch` once; pipes
  per-tile PNGs via stdin; parses `focr robot run` NDJSON output
  (`stage`, `page`, `run_complete`); collects per-tile markdown.
- Default `--mdwb-ocr-focr-base-size=1024`, `--mdwb-ocr-focr-image-size=640`,
  `--crop-mode=gundam`. Configurable via `.env`.
- Add `app/ocr_client.py:register_backend("focr", FocrBackend)` so the
  existing concurrency autopilot picks it.
- Update `app/ocr_policy.py` with `BACKEND_MODE_FOCR` and reason codes
  `policy.focr.local-preferred` and `policy.focr.cpu-only`.

**Acceptance:** A capture against a static page that previously took
~30 s in vLLM completes in <8 s with `focr` on the same machine. The
manifest includes `ocr_backend=focr` and `focr_simd=<avx2|scalar>`.
Existing `app/ocr_client.py:1-1585` tests still pass.

**Depends on:** `mdwb-001`, `mdwb-002`, `mdwb-003`.

### 4.11 `mdwb-031` — OCR capability matrix

**Why:** The autopilot needs to pick focr when local + healthy.

**What:**
- Extend `app/ocr_policy.py:1-297` with an `OCRBackendCandidate`
  variant for `focr`.
- New env `MDWB_OCR_BACKEND=auto|focr|olmocr-hosted`. `auto` means:
  prefer `focr` if `focr robot health.status=ready`, else hosted olmOCR.
- Add capability probe `focr robot health` to `app/hardware.py:1-314`.

**Acceptance:** `app/ocr_policy.py` test suite still passes; new tests
cover `focr`-preferred path, `focr`-unhealthy-fallback path, and
hysteresis. Manifest `ocr_autotune` records the choice + reason.

**Depends on:** `mdwb-030`.

### 4.12 `mdwb-032` — `focr` benchmark harness

**Why:** We need a live number before claiming focr is faster.

**What:**
- New `scripts/bench_focr.py`. Runs `focr ocr-batch` over the
  production URL set (`benchmarks/production_set.json`). Outputs p50/p95/p99
  per category, total tile count, throughput (tiles/sec).
- Wired into nightly smoke: `MDWB_RUNTIME_MODE=rust scripts/run_smoke.py
  --backend=focr --date=$today`.
- Adds `benchmarks/production/focr_latest.json` + `focr_latest.md`.

**Acceptance:** `scripts/show_latest_smoke.py --backend=focr` shows the
new files; `benchmarks/production/focr_latest.md` shows p95 < 2×p95
budget from `benchmarks/production_set.json`.

**Depends on:** `mdwb-030`, `mdwb-031`.

### 4.13 `mdwb-040` — Storage migration

**Why:** Single MVCC store removes the writer bottleneck.

**What:**
- `app/store.py` opens `index.fdb` via the Rust runtime's
  `store.open` op. SQLModel definitions stay for typed schema; reads
  go through the Rust side, writes go through the Rust side.
- Migration runner: `Store.migrate_sqlmodel_to_fdb` reads from
  `runs.db` and writes to `index.fdb`. Idempotent. Logs every row.
- `app/store.py:find_cache_hit` uses `store.query` against the FDB
  cache table.

**Acceptance:** 50 parallel `POST /jobs` against `MDWB_RUNTIME_MODE=rust`
all complete without `database is locked` errors; existing
`tests/test_store_manifest.py` still passes against FDB.

**Depends on:** `mdwb-013`.

### 4.14 `mdwb-041` — Embeddings migration

**Why:** Hybrid lexical+vector search via frankensearch; richer than
sqlite-vec alone.

**What:**
- `app/embeddings.py` becomes a thin facade that delegates to
  `mdwb-runtime search.embedding`.
- `frankensearch-daemon` (per `/dp/frankensearch/crates/frankensearch-daemon`)
  indexes section embeddings + lexical terms per `job.id`.
- `Search` op returns RRF-fused top-k results in <50 ms for 100k
  sections.

**Acceptance:** `mdwb search <job-id> "<query>" --top-k=10` returns in
<50 ms; existing `tests/test_embeddings_search.py` passes; new
`tests/test_frankensearch_ranking.py` covers RRF vs vector-only paths.

**Depends on:** `mdwb-040`.

### 4.15 `mdwb-042` — `mdwb search <job-id>`

**Why:** One canonical agent verb for finding sections.

**What:**
- `scripts/mdwb_cli.py`: new `search` subcommand. Calls
  `mdwb-runtime search.hybrid` for the given job-id. Emits the agent
  envelope.
- `--top-k=N` (default 5), `--mode=hybrid|lexical|vector` (default hybrid),
  `--json` for raw envelope.

**Acceptance:** `mdwb search <job-id> "earnings" --json` exits 0,
returns the agent envelope; the search results include tile provenance
fields so the agent can chain `mdwb agent cite`.

**Depends on:** `mdwb-041`, `mdwb-070`.

### 4.16 `mdwb-043` — Cross-job search

**Why:** Agents often want to find something across every capture they
have made.

**What:**
- `mdwb search --all "<query>" --top-k=N` searches the global index at
  `~/.cache/mdwb/global.idx`. Results include `job_id`, `tile_id`,
  `score`, `url`.
- `scripts/agents/extract.py` (see `mdwb-072`) uses cross-job search
  to ground numeric extractions.

**Acceptance:** Cross-job search returns deduplicated results across
>2 jobs; `tests/test_cross_job_search.py` covers the merge logic.

**Depends on:** `mdwb-042`.

### 4.17 `mdwb-050` — `out.html` via `fmd`

**Why:** Render Markdown into a deterministic HTML doc; free of
markdown-it JS overhead.

**What:**
- `scripts/export_html.py` calls `fmd render out.md --to html --out
  out.html`. Output deterministic via `fmd --no-config`.
- `POST /jobs/{id}/artifact/out.html` returns the rendered HTML.
- `Store` records `out_html_path` next to `out_md_path`.

**Acceptance:** Byte-identical `out.html` across two consecutive runs
of the same job; `fmd verify out.html --json` exits 0.

**Depends on:** `mdwb-001`, `mdwb-003`.

### 4.18 `mdwb-051` — `out.pdf` opt-in

**Why:** Some users want a PDF; deterministic rendering is a bonus.

**What:**
- `fmd render out.md --to pdf --out out.pdf`.
- `--export-pdf` flag on `mdwb fetch` and on `POST /jobs`. Off by
  default.

**Acceptance:** PDF renders in <5 s for typical pages; PDF byte
identical across two runs of the same job.

**Depends on:** `mdwb-050`.

### 4.19 `mdwb-052` — Browser UI uses `out.html`

**Why:** Native rendering, less JS, deterministic.

**What:**
- `web/browser.html`: Rendered tab is now `<iframe src="/jobs/{id}/artifact/out.html">`.
- Raw tab still uses Monaco.
- New "PDF" button calls `GET /jobs/{id}/artifact/out.pdf` (when present).

**Acceptance:** Playwright smoke `tests/smoke_browser_ui.spec.ts`
loads the rendered tab; visible HTML matches `out.html`.

**Depends on:** `mdwb-050`.

### 4.20 `mdwb-060` — `mdwb-tui` scaffold

**Why:** Operators and agents benefit from a fast, scriptable TUI.

**What:**
- New top-level `runtime/crates/mdwb-tui/` workspace.
- Tabs: **Jobs** (list with progress bars), **Tiles** (grid with
  thumbnails), **Search** (text input + result list), **Events** (live
  tail), **OCR queue** (concurrency autopilot state).
- Connects to `mdwb-runtime` over the IPC socket.

**Acceptance:** `mdwb-tui --socket /tmp/mdwb.sock` launches; tabs
switch with `1`-`5`; `q` quits; `tui-inspector` snapshots captured for
all tabs.

**Depends on:** `mdwb-010`.

### 4.21 `mdwb-061` — TUI ↔ JSON-RPC

**Why:** TUI actions should be agent-callable, not just UI.

**What:**
- Every TUI action emits an IPC op AND logs the agent envelope to
  stdout when `MDWB_TUI_HEADLESS=1`.
- Example: `MDWB_TUI_HEADLESS=1 mdwb-tui --action capture --url=…
  --backend=focr` is equivalent to `mdwb agent capture …`.

**Acceptance:** Headless mode exits 0 with envelope on stdout; UI mode
behaves identically to today's manual workflow.

**Depends on:** `mdwb-060`.

### 4.22 `mdwb-062` — TUI snapshots

**Why:** Visuals must not regress.

**What:**
- `tui-inspector` (skill) records terminal UIs.
- `crates/mdwb-tui/tests/snapshots.rs` golden-tests the snapshots.

**Acceptance:** `cargo test -p mdwb-tui --test snapshots` passes.

**Depends on:** `mdwb-060`.

### 4.23 `mdwb-070` — `AgentEnvelope` schema

**Why:** One envelope, multiple consumers.

**What:**
- New `app/agent_envelope.py` Pydantic model with `schema=mdwb.agent.v1`.
- `scripts/agent_envelope_gen.py` generates `schemas/mdwb.agent.v1.json`
  (committed).
- Every new CLI verb emits through it.

**Acceptance:** `jsonschema -i schemas/mdwb.agent.v1.json
  tests/fixtures/envelopes/*.json` validates; envelope version is
  bumped if any field changes.

**Depends on:** none.

### 4.24 `mdwb-071` — `mdwb agent` umbrella CLI

**Why:** Single canonical entry point for agents.

**What:**
- `scripts/mdwb_cli.py`: new top-level `agent` group. Subcommands:
  `capture`, `search`, `summarize`, `todos`, `extract`, `cite`, `watch`,
  `doctor`. All verbs emit `AgentEnvelope`.

**Acceptance:** Every `mdwb agent <verb> --json` round-trips through
`jsonschema` validation; envelope `next_actions[]` provides helpful
follow-up commands.

**Depends on:** `mdwb-070`.

### 4.25 `mdwb-072` — Refactor agent starter scripts

**Why:** Stop duplicating logic.

**What:**
- `scripts/agents/summarize_article.py` becomes a thin wrapper that
  calls `mdwb agent summarize --url=…`.
- `scripts/agents/generate_todos.py` becomes `mdwb agent todos --url=…`.
- New `scripts/agents/extract.py` (`mdwb agent extract`) for numbers,
  dates, table cells; uses `mdwb search` + heuristics.
- New `scripts/agents/cite.py` (`mdwb agent cite`) for span-to-tile
  provenance; uses `mdwb-runtime search.cite`.

**Acceptance:** Existing `tests/test_agent_scripts.py` still passes;
new `tests/test_agent_extract.py`, `tests/test_agent_cite.py` cover the
new verbs.

**Depends on:** `mdwb-071`, `mdwb-042`.

### 4.26 `mdwb-073` — `mdwb agent cite`

**Why:** Every span in `out.md` is traceable to a tile pixel.

**What:**
- IPC op `search.cite` returns `{tile_id, y, sha256, viewport_y,
  overlap_px, path, highlight}` for a given span.
- `mdwb agent cite <job-id> "<span>"` emits these as JSON.

**Acceptance:** A span chosen from `out.md` returns a `highlight` URL
that, when opened in the browser UI, draws a red box on the right
tile. `tests/test_agent_cite.py` covers both success and no-match.

**Depends on:** `mdwb-071`, `mdwb-040`.

### 4.27 `mdwb-080` — Envelope validation tests

**Why:** Make the envelope a real contract.

**What:** `tests/test_agent_envelope.py` snapshots every
`mdwb agent <verb>` response; validates against the JSON schema.

**Depends on:** `mdwb-070`, `mdwb-071`.

### 4.28 `mdwb-081` — Rust runtime tests from Python

**Why:** Bridge the language gap.

**What:** `tests/test_franken_runtime.py` spawns `mdwb-runtime`,
runs a fake capture end-to-end, asserts events arrive. Uses the
public IPC contract only.

**Depends on:** `mdwb-010`, `mdwb-012`.

### 4.29 `mdwb-082` — Smoke covers both modes

**Why:** Production parity between Python and Rust runtimes.

**What:** `scripts/run_smoke.py` supports `MDWB_RUNTIME_MODE=rust`.
Both modes produce manifests with the same field set.

**Depends on:** `mdwb-013`.

### 4.30 `mdwb-083` — Docs refresh

**Why:** Docs must match reality.

**What:** Update `docs/architecture.md`, `docs/ops.md`, `docs/api.md`,
`docs/release_checklist.md`, `README.md` with the new runtime, new
artifacts, new CLI verbs, new envelope.

**Depends on:** all preceding phases.

### 4.31 `mdwb-090` — Ship-it gate

**Why:** Prove we're ready.

**What:**
- Nightly smoke green for 48 h with `MDWB_RUNTIME_MODE=rust`.
- `focr` p95 stays under `2×p95` budget.
- All envelope verbs validated by `tests/test_agent_envelope.py`.
- `benchmarks/production/weekly_summary.json` reflects both modes.

**Depends on:** all preceding beads.

---

## 5. Cross-cutting concerns

### 5.1 Test discipline

- Every Rust crate ships Rust unit + integration tests in
  `crates/<name>/tests/`.
- Every new Python module ships `tests/test_<module>.py` using the
  existing `conftest.py` fixtures.
- `tests/test_agent_envelope.py` is the single source of truth for the
  agent surface.
- The smoke set (`benchmarks/production_set.json`) stays the gate.
- No `tests/` placeholder files. Existing placeholders
  (`test_scroll_policy.py`, `test_pytest_cli_expansion.py`,
  `test_embeddings.py`, `test_e2e_small.py`) get deleted once their
  subject beads land.

### 5.2 Documentation discipline

- Each bead updates `docs/` if it changes a user-visible surface.
- `README.md` gains a "Franken Runtime" section at the end of the
  Quick Start, only after `mdwb-080` passes.
- `docs/release_checklist.md` adds a "Franken readiness" subsection.

### 5.3 Observability discipline

- New metrics: `mdwb_runtime_mode{python|rust}`,
  `mdwb_ocr_backend{focr|olmocr-hosted}`,
  `mdwb_frankensqlite_query_seconds`,
  `mdwb_frankensearch_query_seconds`,
  `mdwb_tui_active_sessions`.
- New warning log events: `focr_slow_tile`, `focr_simd_fallback`,
  `fdb_write_contention`.

### 5.4 License & provenance

- `focr` model weight downloads go through `focr pull`; license notice
  `Baidu Unlimited-OCR - Copyright (c) 2026 Baidu, MIT License` is
  surfaced in the manifest and in `mdwb doctor --json`.
- FrankenSQLite, Frankensearch, FrankenTUI, Franken Markdown are
  permissively licensed; cite versions in `benchmarks/proof/franken_greets.json`.
- asupersync is permissively licensed; `asupersync version` is part of
  every runtime boot log.

---

## 6. Risks & mitigations

| Risk | Likelihood | Mitigation |
| --- | --- | --- |
| Playwright over CDP from Rust is harder than expected | Medium | Reuse `app/capture.py:1-643` as a Python child process; orchestrator stays in Rust |
| `focr` model download hits Baidu rate limits | Medium | Cache model under `~/.cache/franken_ocr/models/`; surface `focr pull` rate-limit errors via `focr robot health` |
| Frankensearch index storage balloons | Low | Index pruning + `fscratch compact`; weekly summary includes index size |
| Asupersync cancellation contract drift | Low | Pin to `asupersync` 0.4.9; add `runtime.selftest.capture` for cancel proof; cross-check against `asupersync-mega-skill` non-negotiables |
| `tests/test_mdwb_cli_*.py` break with new envelopes | Medium | New commands emit envelopes; old commands keep their old shape until `mdwb-090` |
| Two writers (`runs.db` + `index.fdb`) confuse SQLite WAL semantics | Medium | `mdwb-040` migrates data, never writes both; `runs.db` becomes read-only after migration |

---

## 7. Acceptance for the whole plan

This plan is ready for beads conversion when:

1. Every phase-bead has at least one explicit test.
2. The dependency graph is acyclic.
3. Every architectural choice is justified (this document does that).
4. A fresh agent can read any bead's section and implement without
   clarification.

After conversion to beads, `bv --robot-triage` must show all phase
beads reachable from `mdwb-001` and `mdwb-004`.