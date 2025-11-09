# Markdown Web Browser

Render any URL with a deterministic Chrome-for-Testing profile, tile the page into OCR-friendly slices, and stream Markdown + provenance back to agents, the web UI, and automation clients.

## Why it exists
- **Screenshot-first:** Captures exactly what users see—no PDF/print CSS surprises.
- **Deterministic + auditable:** Every run emits tiles, `out.md`, `links.json`, and `manifest.json` (with CfT label/build, Playwright version, screenshot style hash, warnings, and timings).
- **Agent-friendly extras:** DOM-derived `links.json`, sqlite-vec embeddings, SSE/NDJSON feeds, and CLI helpers so builders can consume Markdown immediately.
- **Ops-ready:** Python 3.13 + FastAPI + Playwright with uv packaging, structured settings via `python-decouple`, telemetry hooks, and smoke/latency automation.

## Architecture at a glance
1. FastAPI `/jobs` endpoint enqueues a capture via the `JobManager`.
2. Playwright (Chromium CfT, viewport 1280×2000, DPR 2, reduced motion) performs a deterministic viewport sweep.
3. `pyvips` slices sweeps into ≤1288 px tiles with ≈120 px overlap; each tile carries offsets, DPR, hashes.
4. The OCR client submits tiles (HTTP/2) to hosted or local olmOCR, with retries + concurrency autotune.
5. Stitcher merges Markdown, aligns headings with the DOM outline, trims overlaps via SSIM + fuzzy text comparisons, injects provenance comments (with tile metadata + highlight links), and builds the Links Appendix.
6. `Store` writes artifacts under a content-addressed path and updates sqlite + sqlite-vec metadata for embeddings search.
7. `/jobs/{id}`, `/jobs/{id}/stream`, `/jobs/{id}/events`, `/jobs/{id}/links.json`, etc., feed the HTMX UI, CLI, and agent automations.
8. The browser shell relies on the HTMX SSE extension, so real-time updates (state, manifest, warning pills) are declaratively wired via `hx-ext="sse"` without bespoke `EventSource` code.

See `PLAN_TO_IMPLEMENT_MARKDOWN_WEB_BROWSER_PROJECT.md` §§2–5, 19 for the full breakdown.

## Quickstart
1. **Install prerequisites**
   - Python 3.13, uv ≥0.8, and the system deps Playwright requires.
   - Install the CfT build Playwright expects: `playwright install chromium --with-deps --channel=cft`.
   - Create/sync the env: `uv venv --python 3.13 && uv sync`.
   - Optional (GPU/olmOCR power users): run `scripts/setup_olmocr_cuda12.sh` to provision CUDA 12.6 + the local vLLM toolchain described in `docs/olmocr_cli_tool_documentation.md`.
2. **Configure environment**
   - Copy `.env.example` → `.env`.
   - Fill in OCR creds, `API_BASE_URL`, CfT label/build, screenshot style hash overrides, webhook secret, etc.
   - Settings are loaded exclusively via `python-decouple` (`app/settings.py`), so keep `.env` private.
3. **Run the API/UI**
   - `scripts/dev_run.sh` (defaults to uvicorn with reload). Open `http://localhost:8000` for the HTMX/Alpine interface.
   - For production-style smoke, flip to Granian: `SERVER_IMPL=granian UVICORN_RELOAD=false HOST=0.0.0.0 PORT=8000 scripts/dev_run.sh --workers 4 --granian-runtime-threads 2`. This wraps `scripts/run_server.py`, so the same flags work in CI or systemd units.
4. **Trigger a capture**
   - UI Run button posts `/jobs`.
   - CLI example: `uv run python scripts/mdwb_cli.py fetch https://example.com --watch`

### Persistent Chromium profiles
- The UI profile dropdown and CLI `--profile <id>` flag reuse login/storage state under `CACHE_ROOT/profiles/<id>/storage_state.json`. Pick distinct IDs for red/blue teams or authenticated personas.
- Profiles are recorded in `manifest.profile_id`, surfaced via `/jobs/{id}`/SSE/CLI diagnostics, and stored in `runs.db` so ops can audit which captures used which credentials.
- Storage directories are slugged automatically (`[A-Za-z0-9._-]`), so feel free to pass human-friendly names (e.g., `agent.alpha`).

### Links tab (domain grouping + actions)
- Links now stream into domain-grouped sections so it is easy to scan anchors/forms per host (relative URLs and fragments fall into `(relative)` / `(fragment)` buckets).
- Coverage badges highlight whether a link came from the DOM, OCR, or both, and raise warnings for text mismatches; attribute badges summarize `target`/`rel` metadata, which is useful when triaging overlays or sandbox issues.
- Each row exposes inline actions:
  - **Open in new job** populates the toolbar URL field and immediately triggers a capture run.
  - **Copy Markdown** copies the Markdown anchor (or best-effort fallback) to the clipboard.
  - **Mark crawled** toggles a local badge + dimmed state so agents can keep track of which URLs they have already followed; the selection persists in `localStorage`.

### OCR concurrency autotune
- The OCR client now starts at `OCR_MIN_CONCURRENCY` and automatically scales up toward `OCR_MAX_CONCURRENCY` when latency is healthy, or throttles when responses turn slow/errored. The live Events tab and Manifest view both stream these adjustments so you can see when the controller steps in.
- Manifests (`ocr_autotune`) and CLI commands (`mdwb diag`, `mdwb jobs ocr-metrics`) include the initial/peak/final limits plus a short history of adjustments. Use `MDWB_SERVER_IMPL=granian` + higher worker counts when you want the autotune headroom to matter.

### Cache reuse
- `POST /jobs` now deduplicates captures using a content-address (`url + CfT + viewport + DSF + OCR model + profile`). By default the CLI enables this, so identical requests return immediately with `cache_hit=true` and reuse existing artifacts.
- Disable reuse with `mdwb fetch --no-cache` (or `reuse_cache=false` in the API payload) when you need a fresh capture even if nothing changed.
- Manifests, `/jobs/{id}` snapshots, SSE logs, and `mdwb diag` all expose `cache_hit` so downstream tooling can tell whether a job ran or reused cached output.

## CLI cheatsheet (`scripts/mdwb_cli.py`)
- `fetch <url> [--watch]` — enqueue + optionally stream Markdown as tiles finish (percent/ETA shown unless `--no-progress`; add `--reuse-session` to keep one HTTP/2 client alive across submit + stream).
- `fetch <url> --no-cache` — force a fresh capture even if an identical cache entry exists.
- `fetch <url> --resume [--resume-root path]` — skip URLs already recorded in `done_flags/` (optionally `work_index_list.csv.zst`) under the chosen root; the CLI auto-enables `--watch` so completed jobs write their flag/index entries. Override locations via `--resume-index/--resume-done-dir`.
- `fetch <url> --webhook-url https://... [--webhook-event DONE --webhook-event FAILED]` — register callbacks right after the job is created.
- `show <job-id> [--ocr-metrics]` — dump the latest job snapshot, optionally with OCR batch/quota telemetry.
- `stream <job-id>` — follow the SSE feed.
- `watch <job-id>` / `events <job-id> --follow --since <ISO>` — tail the `/jobs/{id}/events` NDJSON log (use `--on EVENT=COMMAND` for hooks; add `--no-progress` to suppress the percent/ETA overlay, `--reuse-session` to keep a single HTTP client). DOM-assist events now print counts/reasons so you immediately see when hybrid recovery patched a tile.
- `diag <job-id>` — print CfT/Playwright metadata, capture/OCR timings, warnings, and blocklist hits for incident triage.
- `jobs replay manifest <manifest.json>` — resubmit a stored manifest via `/replay` with validation/JSON output support.
- `jobs embeddings search <job-id> --vector-file vector.json [--top-k 5]` — search sqlite-vec section embeddings for a run (supports inline `--vector` strings and `--json` output).
- `jobs agents bead-summary <plan.md>` — convert a markdown checklist into bead-ready summaries (mirrors the intra-agent tracker described in PLAN §21).
- `warnings --count 50` — tail `ops/warnings.jsonl` for capture/blocklist incidents.
- `dom links --job-id <id>` — render the stored `links.json` (anchors/forms/headings/meta).
- `jobs ocr-metrics <job-id> [--json]` — summarize OCR batch latency, request IDs, and quota usage from the manifest.
- `resume status --root path [--limit 10 --pending --json]` — inspect the resume state; `--pending` shows outstanding URLs, `--json` emits `completed_entries` + `pending_entries` for automation.
- `demo snapshot|stream|events` — exercise the demo endpoints without hitting a live pipeline.

The CLI reads `API_BASE_URL` + `MDWB_API_KEY` from `.env`; override with `--api-base` when targeting staging. For CUDA/vLLM workflows, see `docs/olmocr_cli_tool_documentation.md` and `docs/olmocr_cli_integration.md` for detailed setup + merge notes.

## Agent starter scripts (`scripts/agents/`)
- `uv run python -m scripts.agents.summarize_article summarize --url https://example.com [--out summary.txt]` — submit (or reuse via `--job-id`) and print/save a short summary (defaults to `--reuse-session`).
- `uv run python -m scripts.agents.generate_todos todos --job-id <id> [--json] [--out todos.json]` — extract TODO-style bullets (JSON when `--json`, newline text otherwise); accepts `--url` to run a fresh capture and also defaults to `--reuse-session`.

Both helpers reuse the CLI’s auth + HTTP plumbing, accept the same `--api-base/--http2` flags, fall back to existing jobs when you only need post-processing, and now support `--out` so automations can ingest the results directly.

## Prerequisites & environment
- **Chrome for Testing pin:** Set `CFT_VERSION` + `CFT_LABEL` in `.env` so manifests and ops dashboards stay consistent. Re-run `playwright install` whenever the label/build changes.
- **Transport + viewport:** Defaults (`PLAYWRIGHT_TRANSPORT=cdp`, viewport 1280×2000, DPR 2) live in `app/settings.py` and must align with PLAN §§3, 19.
- **OCR credentials:** `OLMOCR_SERVER`, `OLMOCR_API_KEY`, and `OLMOCR_MODEL` are required unless you point at `OCR_LOCAL_URL`.
- **Warning log + blocklist:** Keep `WARNING_LOG_PATH` and `BLOCKLIST_PATH` writable so scroll/overlay incidents are persisted (`docs/config.md` documents every field).
- **System packages:** Install libvips 8.15+ so the pyvips-based tiler works (`sudo apt-get install libvips` on Debian/Ubuntu, `brew install vips` on macOS). `scripts/run_checks.sh` checks for `pyvips` and fails fast with install instructions unless you explicitly set `SKIP_LIBVIPS_CHECK=1` (for targeted CLI/unit runs on machines without libvips).

## Testing & quality gates
Run these before pushing or shipping capture-facing changes:

```bash
uv run ruff check --fix --unsafe-fixes
uvx ty check
npx playwright test --config=playwright.config.mjs  # or PLAYWRIGHT_BIN=/path/to/playwright-test …
```

`./scripts/run_checks.sh` wraps the same sequence for CI. Set `PLAYWRIGHT_BIN=/path/to/playwright-test`
if you need to invoke the Node-based runner; otherwise the script prefers `npx playwright test --config=playwright.config.mjs`
(which inherits the defaults from PLAN/AGENTS: viewport 1280×2000, DPR 2, reduced motion, light scheme, mask selectors, CDP/BiDi transport via `PLAYWRIGHT_TRANSPORT`). When Node Playwright isn’t installed it falls back to `uv run playwright test` and prints a warning if the Python CLI lacks `test`.
When you already know libvips isn’t available in a minimal container, export `SKIP_LIBVIPS_CHECK=1` to bypass the preflight warning. Optional toggles inside `scripts/run_checks.sh`:

- `MDWB_CHECK_METRICS=1` (optionally `CHECK_METRICS_TIMEOUT=<seconds>`) appends the Prometheus health check after pytest/Playwright.
- `MDWB_RUN_E2E=1` runs the lightweight placeholder suite in `tests/test_e2e_small.py` so CI can keep a fast E2E sentinel without invoking FlowLogger.
- `MDWB_RUN_E2E_RICH=1` runs the full FlowLogger scenarios in `tests/test_e2e_cli.py`; transcript artifacts are copied to `tmp/rich_e2e_cli/` (override via `RICH_E2E_ARTIFACT_DIR=/path/to/dir`) so operators can review the panels/tables/progress output without hunting through pytest temp dirs.
- `MDWB_RUN_E2E_GENERATED=1` runs the generative guardrail suite (`tests/test_e2e_generated.py`). Point `MDWB_GENERATED_E2E_CASES=/path/to/cases.json` at a bespoke cases file when you need to refresh or extend the Markdown baselines.

Grab the resulting `tmp/rich_e2e_cli/*.log|*.html` files in CI for postmortems.

- The bundled pytest targets now include the store/manifest persistence suite (`tests/test_store_manifest.py`, `tests/test_manifest_contract.py`),
  the Prometheus CLI health checks (`tests/test_check_metrics.py`), and the ops regressions for `show_latest_smoke`/`update_smoke_pointers` in addition
  to the CLI coverage. This keeps RunRecord fields, smoke pointer tooling, and metrics hooks under CI without needing a live API server.

- Playwright defaults to the Chrome for Testing build. Leave `PLAYWRIGHT_CHANNEL` unset (or set it to `cft`) so local smoke runs match the capture pipeline; if you have to fall back to stock Chromium, set `PLAYWRIGHT_CHANNEL=chromium` or use a comma-separated preference such as `PLAYWRIGHT_CHANNEL="chromium,cft"`. Likewise, keep `PLAYWRIGHT_TRANSPORT=cdp` unless you are explicitly exercising WebDriver BiDi—when you do, a value like `PLAYWRIGHT_TRANSPORT="bidi,cdp"` makes the preferred/fallback order obvious to anyone reading CI metadata.

- Every `run_checks` invocation now emits `tmp/pytest_report.xml` and `tmp/pytest_summary.json`
  (override with `PYTEST_JUNIT_PATH`/`PYTEST_SUMMARY_PATH`). The JSON digest lists totals and the first few
  failing test names, so CI/Agent Mail can quote failures without re-running pytest.

Also run `uv run python scripts/check_env.py` whenever `.env` changes—CI and nightly smokes depend on it to confirm CfT pins + OCR secrets.

Additional expectations (per PLAN §§14, 19.10, 22):
- Keep nightly smokes green via `uv run python scripts/run_smoke.py --date $(date -u +%Y-%m-%d)`.
- Refresh `benchmarks/production/weekly_summary.json` (generated automatically by the smoke script) for Monday ops reports.
- Run `uv run python scripts/check_metrics.py --check-weekly` (with the default `benchmarks/production/weekly_summary.json`) before handoff so we fail fast when capture/OCR SLO p99 values exceed their 2×p95 budgets.
- Tail `ops/warnings.jsonl` or `mdwb warnings` for canvas/video/overlay spikes.

## Day-to-day workflow
- **Reserve + communicate:** Before editing, reserve files and announce the pickup via Agent Mail (cite the bead id). Keep PLAN sections annotated with `_Status — <agent>` entries so the written record matches reality.
- **Track via beads:** Use `bd list/show` to pick the next unblocked issue, add comments for status updates, and close with findings/tests noted.
- **Run the required checks:** `ruff`, `ty`, Playwright smoke, `scripts/check_env.py`, plus any bead-specific tests (e.g., sqlite-vec search or CLI watch). Never skip the capture smoke after touching Playwright/OCR code.
- **Sync docs:** README, PLAN, `docs/config.md`, and `docs/ops.md` must stay consistent; update them alongside code changes so ops can trust the written guidance.
- **Ops handoff:** For capture/OCR fixes, capture job ids + manifest paths in your bead comment and Mail thread so others can reproduce issues quickly.

## Operations & automation
- `scripts/run_smoke.py` — nightly URL set capture + manifest/latency aggregation.
- `scripts/show_latest_smoke.py` — quick pointers to the latest smoke outputs; manifest rows now include overlap ratios, validation failure counts, and seam marker/hash counts so regressions stand out. The `--weekly` view prints seam marker percentiles plus capture/OCR SLO status (p99 vs 2×p95) using the data generated by the nightly smoke script, and `--slo` renders the aggregated `latest_slo_summary.json` table (counts, p50/p95 capture/OCR/total, budget breaches). It now fails fast when `latest.txt` exists but is empty, so rerun `scripts/update_smoke_pointers.py <run-dir>` whenever the pointer guard triggers.
- `scripts/olmocr_cli.py` + `docs/olmocr_cli.md` — hosted olmOCR orchestration/diagnostics.
- Weekly seam telemetry — run `uv run python scripts/show_latest_smoke.py --weekly --json` (or parse `benchmarks/production/weekly_summary.json`) to pull `seam_markers.count`/`hashes` p50/p95 for every category plus the new seam fallback counts. Feed those numbers straight into Grafana/Prometheus so seam regressions (or fallback spikes) page operators alongside capture/OCR SLO breaches.
- `mdwb jobs replay manifest <manifest.json>` — re-run a job with a stored manifest via `POST /replay` (accepts `--api-base`, `--http2`, `--json`); keep `scripts/replay_job.sh` around for legacy automation until everything points at the CLI.
- `mdwb jobs show <job-id>` — inspect the latest snapshot plus sweep stats/validation issues in one table. When manifests are missing (cached jobs, trimmed SSE payloads), the CLI still prints stored seam counts (`Seam markers: X (unique hashes: Y)`) so you can spot duplicate sweeps without spelunking manifests. `mdwb diag --ocr-metrics` shows the detailed seam marker table when manifests are available.
- `scripts/update_smoke_pointers.py <run-dir> [--root path]` — refresh `latest_summary.md`, `latest_manifest_index.json`, and `latest_metrics.json` after ad-hoc smoke runs so dashboards point at the right data (defaults to `MDWB_SMOKE_ROOT` unless `--root` is provided; add `--weekly-source` when overriding the rolling summary). The command now computes `latest_slo_summary.json` by default using the manifest index + PLAN §22 budget file; pass `--no-compute-slo` to skip or `--budget-file` to point at an alternate budget definition.
- `scripts/check_metrics.py` — ping `/metrics` plus the exporter; supports `--api-base`, `--exporter-url`, `--json`, and now `--check-weekly` (validates `benchmarks/production/weekly_summary.json` so release builds fail fast if the rolling SLOs are blown). When you pass `--check-weekly --json`, the CLI always emits a `weekly` block (status, `summary_path`, `failures`) even if the summary file is missing/unreadable, which makes automation logs self-explanatory. `scripts/prom_scrape_check.py` remains as a compatibility wrapper but simply re-exports the same Typer CLI.
- `scripts/compute_slo.py` — consumes the latest `latest_manifest_index.json` (or any manifest index) plus the benchmark budget file to produce capture/OCR SLO summaries. The CLI writes a JSON report (`--out benchmarks/production/latest_slo_summary.json`) and optionally emits Prometheus textfile metrics via `--prom-output tmp/mdwb_slo.prom`, enabling dashboards/alerts to track per-category p95 totals, breach ratios, and overall SLO status. `scripts/run_smoke.py` invokes this automatically after each smoke run so `benchmarks/production/latest_slo_summary.json` and `latest_slo.prom` stay fresh; rerun manually when you need ad-hoc SLO snapshots.
- Prometheus metrics now cover capture/OCR/stitch durations, warning/blocklist counts, job completions, and SSE heartbeats via `prometheus-fastapi-instrumentator`. Scrape `/metrics` on the API port or hit the background exporter on `PROMETHEUS_PORT` (default 9000); docs/ops.md lists the metric names + alert hooks.
- Set `MDWB_CHECK_METRICS=1` (optionally `CHECK_METRICS_TIMEOUT=<seconds>`) when running `scripts/run_checks.sh` to include the Prometheus smoke (`scripts/check_metrics.py`) alongside the usual lint/type/pytest/Playwright stack.

### Handy commands
```bash
# Validate env
uv run python scripts/check_env.py

# Run cli demo job
uv run python scripts/mdwb_cli.py demo stream

# Replay an existing manifest
uv run python scripts/mdwb_cli.py jobs replay manifest cache/example.com/.../manifest.json

# Search embeddings for a run (vector as JSON array)
uv run python scripts/mdwb_cli.py jobs embeddings search JOB_ID --vector "[0.12, 0.04, ...]" --top-k 3

# Tail warning log via CLI
uv run python scripts/mdwb_cli.py warnings --count 25

# Download a job's tar bundle (tiles + markdown + manifest)
uv run python scripts/mdwb_cli.py jobs bundle <job-id> --out path/to/bundle.tar.zst

# Run nightly smoke for docs/articles only (dry run)
uv run python scripts/run_smoke.py --date $(date -u +%Y-%m-%d) --category docs_articles --dry-run
```

## Artifacts you should expect per job
- `artifact/tiles/tile_*.png` — viewport-sweep tiles (≤1288 px long side) with overlap + SHA metadata.
- `/jobs/{id}/artifact/highlight?tile=…&y0=…&y1=…` — quick HTML viewer that overlays the region referenced by each provenance comment (handy for code reviews and incident reports).
- `out.md` — final Markdown with DOM-guided heading normalization plus provenance comments (`<!-- source: tile_i ... , path=…, highlight=/jobs/... -->`) and Links Appendix.
- `links.json` — anchors/forms/headings/meta harvested from the DOM snapshot.
- `manifest.json` — CfT label/build, Playwright version, screenshot style hash, warnings, sweep stats, timings, and the new `seam_marker_events` list whenever seam hints were required to align tiles.
- `dom_snapshot.html` — raw DOM capture used for link diffs and hybrid recovery (when enabled).
- `bundle.tar.zst` — optional tarball for incidents/export (`Store.build_bundle`).
- Markdown output now includes seam markers (`<!-- seam-marker … -->`) and enriched provenance comments (`viewport_y`, `overlap_px`, highlight links) plus detailed `<!-- table-header-trimmed reason=… -->` breadcrumbs so reviewers can jump straight to stitched regions.

Use `mdwb jobs bundle …` or `mdwb jobs artifacts manifest …` (or `/jobs/{id}/artifact/...`) to reproduce a job locally and fetch its artifacts for debugging.

## Communication & task tracking
- **Beads** (`bd ...`) track every feature/bug (map bead IDs to Plan sections in Agent Mail threads).
- **Agent Mail** (MCP) is the coordination channel—reserve files before editing, summarize work in the relevant bead thread, and note Plan updates inline (_see §§10–11 for example status notes_).

## Further reading
- `AGENTS.md` — ground rules (no destructive git cmds, uv usage, capture policies).
- `PLAN_TO_IMPLEMENT_MARKDOWN_WEB_BROWSER_PROJECT.md` — canonical spec + incremental upgrades.
- `docs/architecture.md` — best practices + data flow diagrams.
- `docs/blocklist.md`, `docs/config.md`, `docs/models.yaml`, `docs/ops.md`, `docs/olmocr_cli.md` — supporting specs.
- `docs/release_checklist.md` — step-by-step release & regression runbook (CfT/Playwright/model toggles, smoke commands, artifact list).

## Troubleshooting cheatsheet
- **Playwright/CfT mismatch:** Run `playwright install chromium --with-deps --channel=cft` and confirm `CFT_VERSION`/`CFT_LABEL` match the installed build. If CfT labels shifted, update `.env` + manifests before rerunning.
- **`.env` drift:** `uv run python scripts/check_env.py --json` pinpoints missing values. Required vars with `None` will fail CI.
- **OCR throttling:** Lower `OCR_MAX_CONCURRENCY`, restart the job, and capture request IDs from manifests for the ops thread.
- **Warning log explosions:** Tail `uv run python scripts/mdwb_cli.py warnings --count 100 --json` and look for repeated warning codes (canvas-heavy, scroll-shrink). Update `docs/blocklist.md` / selectors if overlays broke capture.
- **Warning log explosions:** Tail `uv run python scripts/mdwb_cli.py warnings --count 100 --json` and look for repeated warning codes (canvas-heavy, scroll-shrink); the JSON output now includes `validation_failure_count`, `overlap_match_ratio`, and `sweep_summary` to speed up dashboard ingestion. Update `docs/blocklist.md` / selectors if overlays broke capture.
- **SSE disconnects:** The UI should show an SSE health badge; check `/jobs/{id}/events` NDJSON output via `mdwb events --follow` to ensure the backend is still emitting. If not, inspect `app/jobs.py` logs for heartbeat gaps.
- **Manifest missing links/DOM snapshots:** Ensure the capture job has write access to `CACHE_ROOT`; the Store will refuse to emit `/jobs/{id}/links.json` when the DOM snapshot can’t be written.
- **Seam alignment questions:** Each viewport sweep now draws a subtle watermark line at the top/bottom edge; the resulting seam hashes (`seam_hash=…` in Markdown provenance) plus `mdwb diag --ocr-metrics` output help confirm adjacent tiles align correctly. When overlap hashes disagree, the stitcher now logs `seam_marker_events` so manifests/CLI output show exactly which tile pairs relied on the fallback hints.

Questions? Start a bead, announce it via Agent Mail, and keep PLAN/README/doc updates in lockstep.
