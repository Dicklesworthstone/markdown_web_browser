# Ops Automation Playbook

_Last updated: 2025-11-08 (UTC)_

This guide explains how to run the nightly smoke captures and weekly latency rollups
specified in PLAN §22 using the shared CLI + automation scripts.

## Nightly Smoke Run

```
uv run python scripts/run_smoke.py \
  --date $(date -u +%Y-%m-%d) \
  --http2 \
  --poll-interval 1.0 \
  --timeout 900 \
  --seed 0 \
  --category docs_articles \
  --category dashboards_apps
```

- Add `--dry-run` when you want to exercise the pipeline without hitting `/jobs`
  (useful before the API is live or when secrets are unavailable). Pair it with
  `--seed <int>` (defaults to 0) so synthetic manifests remain deterministic.
  Dry runs still write manifests, summary markdown, and weekly stats so downstream
  tooling can be tested.
- Use `--category <name>` (repeatable) to scope the run to specific categories from
  `benchmarks/production_set.json`. This is useful when a particular slice is flaky
  and you want to rerun it without exercising the entire set.

- Loads `benchmarks/production_set.json` (docs/articles, dashboards/apps,
  lightweight pages) and runs each URL via `scripts/olmocr_cli.py`.
- Stores outputs under `benchmarks/production/<DATE>/<category>/<timestamp_slug>/`.
  Each directory contains `manifest.json`, `out.md`, `links.json`, and the tile
  PNGs under `artifact/`.
- Writes a daily `manifest_index.json` aggregating the job IDs, budgets, and timing data,
  plus `summary.md` (Markdown table with per-category budgets vs. observed p95 capture/total).
  The latest run is always mirrored into:
  - `benchmarks/production/latest.txt` (date stamp)
  - `benchmarks/production/latest_manifest_index.json`
  - `benchmarks/production/latest_summary.md`
  so dashboards/automation can point at a stable path.
- Run `uv run python scripts/show_latest_smoke.py --manifest --metrics --weekly --slo --limit 5 --json` to inspect
  the latest summary/manifest pointers plus the rolling `weekly_summary.json`. Manifest rows include
  overlap ratios and validation failure counts when the data exists, so seam regressions are visible
  without opening individual manifests. The CLI highlights categories that exceed their p95 budgets
  and accepts `--limit` to trim the manifest table. Set `MDWB_SMOKE_ROOT=/path/to/runs` (or pass `--root`)
  if the pointer files live outside `benchmarks/production/`, and add `--json` when automation needs
  structured output instead of tables.
- For automation/health checks, run `uv run python scripts/show_latest_smoke.py check --root benchmarks/production`
  (add `--no-weekly` if you only care about summary/manifest/metrics). Pass `--json` when you want a structured payload
  (fields: `status`, `missing`, `root`, `weekly_required`, `run_date`) and rely on the exit code instead of parsing text.
  The command exits non-zero when any required pointer file is missing, making it ideal for CI/dashboards.
  Both `show` and `check` also exit when `latest.txt` exists but is empty; refresh the pointers via
  `scripts/update_smoke_pointers.py <run-dir>` before rerunning the CLI so the run date is always recorded.

### Prerequisites
- `/jobs` API available on `API_BASE_URL` with credentials in `.env`.
- Chrome for Testing pin + Playwright version recorded in `manifest.json`.
- Enough quota on the hosted olmOCR endpoint for the nightly workload.

### API runtime toggle (uvicorn vs Granian)
- `scripts/dev_run.sh` now wraps `scripts/run_server.py`, so setting `SERVER_IMPL=uvicorn` (default) or `SERVER_IMPL=granian` flips the ASGI runtime without changing any other tooling.
- For local dev, keep the default: `scripts/dev_run.sh` → uvicorn + reload.
- For production-style tests or deployments, disable reload and bump workers: `SERVER_IMPL=granian UVICORN_RELOAD=false HOST=0.0.0.0 PORT=8000 scripts/dev_run.sh --workers 4 --granian-runtime-threads 2`. The manifest’s `environment.server_runtime` and the SQLite `runs` table will reflect the choice so cache hits/logs clearly show which runtime handled each job.

### Verification Checklist
1. `scripts/run_checks.sh` (ruff → ty → targeted pytest suite → Playwright smoke) passes before the smoke run. The script accepts `PLAYWRIGHT_BIN=/path/to/runner` when you need to invoke the Node-based Playwright test harness; otherwise it prefers `npx playwright test --config=playwright.config.mjs` and falls back to `uv run playwright test`. When libvips isn’t installed, set `SKIP_LIBVIPS_CHECK=1` to bypass the preflight until the dependency can be installed. The bundled pytest step now covers the CLI suites, `scripts/show_latest_smoke.py` (env-root + pointer/weekly variants), `scripts/update_smoke_pointers.py` (weekly-source guard), the store/manifest persistence tests (`tests/test_store_manifest.py`, `tests/test_manifest_contract.py`), and the Prometheus CLI health checks (`tests/test_check_metrics.py`) so regressions surface before captures run. Each run writes `tmp/pytest_report.xml` (`PYTEST_JUNIT_PATH`) and `tmp/pytest_summary.json` (`PYTEST_SUMMARY_PATH`) so ops/CI can read the failing test list without re-running pytest.
2. Playwright CLI runs inherit the same Chrome for Testing defaults as the capture stack. Leave `PLAYWRIGHT_CHANNEL` at `cft` (or omit it entirely) so local smoke runs match production; only set `PLAYWRIGHT_CHANNEL=chromium` when CfT binaries are unavailable on the host. You can specify a comma-separated preference (e.g., `PLAYWRIGHT_CHANNEL="cft,chromium"`) to document the fallback order. The transport behaves the same way: keep `PLAYWRIGHT_TRANSPORT=cdp` unless you opt into WebDriver BiDi (`PLAYWRIGHT_TRANSPORT="bidi,cdp"`).
2. Each category report stays below its p95 latency budget (see `manifest_index.json`).
3. Failures must be triaged immediately; rerun `scripts/olmocr_cli.py run` on the
   offending URL with `--out-dir benchmarks/reruns` for deeper debugging.

## Weekly Latency Summary

`scripts/run_smoke.py` automatically refreshes `benchmarks/production/weekly_summary.json`
by folding the last seven days of `manifest_index.json` entries. The file contains:

- `generated_at`: ISO timestamp.
- `window_days`: currently 7.
- `categories`: list of `{name, runs, budget_ms, capture_ms.{p50,p95}, total_ms.{p50,p95}}`.

Run `uv run python scripts/show_latest_smoke.py --weekly --slo --manifest --metrics --no-summary`
to review the latest weekly summary alongside the pointer files (set
`MDWB_SMOKE_ROOT` or `--root` if the artifacts live outside `benchmarks/production/`). The CLI
highlights categories whose p95 totals exceed their budgets so you can spot regressions
before publishing the Monday report. Append `--json` when you need the payload for dashboards/CI.

Publish the summary in Monday’s ops update and attach the most recent
`benchmarks/production/<DATE>/manifest_index.json` for traceability.

> **GPU/vLLM workflows:** if you need to run the smoke suite against a local olmOCR deployment,
> bootstrap the CUDA 12.6 stack with `scripts/setup_olmocr_cuda12.sh` first. The script installs
> the required CUDA/GCC toolchain, provisions `.venv`, and runs a CLI smoke test using the
> settings documented in `docs/olmocr_cli_tool_documentation.md`.

### Seam marker telemetry (bd-md3)
- `benchmarks/production/weekly_summary.json` now records seam health for each category under
  `categories[].seam_markers`: `count.p50/p95` tracks how many stitched seams the average run produced,
  while `hashes.p50/p95` tracks how many unique watermark hashes we saw (duplicate hashes across tiles
  often indicate scroll glitches). Each manifest also includes `seam_marker_events`, and the CLI/manifest tab
  surfaces the corresponding "Seam fallback events" table whenever those hints were needed. The same block now includes `usage.p50/p95` so you can see how often
  seam fallback events fired (i.e., when the stitcher had to rely on the watermark hints because overlap
  hashes disagreed). `scripts/run_smoke.py` writes these values automatically, and
  `scripts/show_latest_smoke.py --weekly --slo --json` exposes them for dashboards/automation (the `--slo`
  flag appends the capture/OCR SLO summary so dashboards can ingest both JSON blocks in one call).
- Wire Grafana panels directly to those JSON fields (or to the rendered CLI output) so ops can
  spot regressions without opening manifests. A simple approach is to have CI upload the weekly
  summary as an artifact, then use the JSON API data source with the path
  `categories[?(@.name=="docs_articles")].seam_markers.count.p95`. Alert whenever seam counts jump
  2× above their seven-day p95 baseline or when unique hashes collapse toward zero (duplicate seams).
- Include the seam markers row in Monday’s ops update: copy the `p50/p95` pairs from the CLI output
  (or run `jq '.categories[] | {name, seam: .seam_markers}' benchmarks/production/weekly_summary.json`)
  so reviewers understand whether the latest regressions stem from stitching vs. OCR latency. Call out
  any spike in `usage` since those runs relied on seam hints rather than raw overlap hashes.
- When an alert fires, grab the affected job id from the manifest index, run
  `mdwb diag <job_id>` (or `mdwb jobs show`) to print the seam marker table, and attach it to the bead
  or Agent Mail thread so everyone has the hashes/tiles handy for repro.

## Troubleshooting
- **API unreachable**: make sure `API_BASE_URL` resolves from the machine running the
  script; set `MDWB_API_KEY` if the deployment requires auth.
- **Typer CLI partial exits (code 10)**: inspect the job directory for `manifest.json`
  and `links.json` to see where OCR failed; re-run with `--timeout` bumped if tiles
  are still streaming.
- **OCR throttling**: temporarily reduce `OCR_MAX_CONCURRENCY` in `.env` and rerun,
  then notify the hosted OCR contact listed in `docs/olmocr_cli.md`.
- **Seam debugging**: viewport sweeps include hashed watermark lines at the top/bottom edges; the resulting `seam_hash` in Markdown/CLI output should match for adjacent tiles. Mismatches usually indicate capture scroll glitches.

## OCR concurrency autotune
- The OCR client now adjusts concurrency dynamically inside each job. It starts at `OCR_MIN_CONCURRENCY`, scales up when back-to-back batches stay under ~3.5 s with no retries, and throttles immediately on 408/429/5xx responses, retries, or >7 s latency.
- Manifest `ocr_autotune` blocks capture the initial/peak/final limits plus a short list of adjustments (also streamed via the Events tab / `mdwb watch` as `ocr_telemetry`). Use these breadcrumbs to explain sudden throughput drops or remote throttling.
- To lock concurrency (e.g., during incident mitigation), set `OCR_MIN_CONCURRENCY` and `OCR_MAX_CONCURRENCY` to the same value before running `scripts/run_checks.sh` or smoke; the controller respects those caps and still logs that scaling was disabled.

## Warning & Blocklist Logs

- Every capture that emits warnings or blocklist hits appends a JSON line to
  `WARNING_LOG_PATH` (defaults to `ops/warnings.jsonl`). The record includes the job ID,
  URL, warning list, blocklist hits, sweep stats, and `validation_failures` so incidents
  can be triaged without scraping manifests.
- Use the CLI helper `uv run python scripts/mdwb_cli.py warnings tail --count 50 --json`
  (add `--follow` to stream, or `--log-path` to override the default) to review recent entries.
  JSON output now includes derived fields (`validation_failure_count`, `overlap_match_ratio`,
  `sweep_summary`) so dashboards/automation can ingest seam health without parsing the pretty table.
  When `--follow` is set the CLI waits for the log to appear and automatically recovers from
  truncation/logrotate events, so long-lived tails keep running overnight. The pretty output still
  summarizes warning codes, blocklist selectors, sweep overlap ratios, and validation failures.
- Job watchers (`mdwb fetch --watch`, `mdwb jobs watch`, `mdwb demo stream`) print the same
  sweep/blocklist/validation summaries whenever manifests expose those fields, so noisy runs
  surface the breadcrumbs even without tailing the log.
- Rotate/ship the log via your usual log aggregation tooling; the file is plain JSONL
  and safe to ingest into Loki/Elastic/GCS.

Need a deeper dive into the CLI roadmap? See `docs/olmocr_cli_integration.md` for the plan to
merge the upstream CUDA-friendly CLI with our existing API helpers so ops aren’t surprised when
the workflows converge.

## Prometheus Metrics

- FastAPI is instrumented via `prometheus-fastapi-instrumentator`; scrape `GET /metrics`
  on the API host for request-level stats. A background exporter also listens on
  `PROMETHEUS_PORT` (default `9000`) so dashboards can point at a dedicated scrape port when
  the API is behind auth.
- Custom metrics include:
  - `mdwb_capture_duration_seconds`, `mdwb_ocr_duration_seconds`, `mdwb_stitch_duration_seconds`
    (histograms for stage timing telemetry, already bucketed for the alert budgets in PLAN §20).
  - `mdwb_dom_assist_density` (assist density per run) and `mdwb_dom_assist_reason_ratio{reason="…"}` so dashboards can alert when hybrid recovery spikes (alert when density >0.02 or any reason ratio >0.01 for critical categories).
- `mdwb_capture_warnings_total{code="…"}` and `mdwb_blocklist_hits_total{selector="…"}` so
  noisy overlays or duplicate seams trigger dashboards without parsing manifests.
- `mdwb_job_completions_total{state="DONE|FAILED"}` to track success rates and
  `mdwb_sse_heartbeat_total` to alert on stalled `/jobs/{id}/stream` feeds.
- Quick smoke check (API + exporter):

```
uv run python scripts/check_metrics.py --timeout 3.0 --json

# Include weekly SLO validation (fails fast when capture/OCR p99 exceed 2×p95 budgets)
uv run python scripts/check_metrics.py --check-weekly --weekly-summary benchmarks/production/weekly_summary.json

# Compute capture/OCR SLO summary for dashboards (PLAN §20.1)
uv run python scripts/compute_slo.py \
  --root benchmarks/production \
  --manifest benchmarks/production/latest_manifest_index.json \
  --budget-file benchmarks/production_set.json \
  --out benchmarks/production/latest_slo_summary.json \
  --prom-output tmp/mdwb_slo.prom
curl -s http://localhost:8000/metrics | grep mdwb_capture_duration_seconds
curl -s http://localhost:9000/metrics | head -n 5  # dedicated Prom port
```
`scripts/run_smoke.py` runs the SLO helper automatically at the end of every smoke run, copying
`slo_summary.json` → `benchmarks/production/latest_slo_summary.json` plus the matching `latest_slo.prom`
textfile so node exporter/Grafana can ingest the `mdwb_slo_*` metrics without additional steps. Use the
manual command above when you need to recompute SLOs from historical manifests or on a different root directory.
The CLI command above reads `.env` for `API_BASE_URL`/`PROMETHEUS_PORT` (override with
`--api-base`/`--exporter-port` and `--exporter-url` when talking to a remote exporter). Use
`--json` for structured output (payload now includes `status`, `generated_at`, `ok_count`,
`failed_count`, `total_duration_ms`, and per-target entries with `duration_ms`/errors) or `--no-include-exporter` when only the primary `/metrics`
endpoint is exposed. `scripts/prom_scrape_check.py` simply wraps the same CLI for older
automation; prefer calling `check_metrics.py` directly.
When you pass `--check-weekly --json`, the payload always includes a `weekly` block with `status`,
`summary_path`, and the recorded failures—even when the weekly summary cannot be read—so CI logs
can quote the exact reason the health check failed. `scripts/compute_slo.py` produces the capture/OCR
SLO rollup referenced in PLAN §20.1: it emits a JSON report (defaulting to
`benchmarks/production/latest_slo_summary.json`) plus optional Prometheus textfile metrics
(`mdwb_slo_*` gauges covering per-category p95 totals, breach ratios, and overall status) when
`--prom-output` is supplied. Point the node exporter textfile collector at the generated `.prom`
file so Grafana can alert whenever the rolling SLOs drift outside their budgets.
- Optional automation toggles:
  - `MDWB_CHECK_METRICS=1` (and optionally `CHECK_METRICS_TIMEOUT=<seconds>`) appends the Prometheus probe after
    the lint/type/pytest/Playwright stages so CI mirrors manual smoke commands.
  - `MDWB_RUN_E2E=1` runs the lightweight sentinel suite in `tests/test_e2e_small.py`.
  - `MDWB_RUN_E2E_RICH=1` runs the FlowLogger-heavy `tests/test_e2e_cli.py` scenarios and copies the transcript artifacts into
    `tmp/rich_e2e_cli/` (override with `RICH_E2E_ARTIFACT_DIR=/path/to/dir`) so operators can grab the `.log`/`.html` panels from CI artifacts.
  - `MDWB_RUN_E2E_GENERATED=1` runs the generative guardrail suite (`tests/test_e2e_generated.py`). Override `MDWB_GENERATED_E2E_CASES`
    when you need to point at a custom cases file (e.g., regenerated Markdown baselines).
- If legacy pipelines still call `scripts/prom_scrape_check.py`, they automatically inherit the latest CLI flags
  (including `--json` and exporter overrides). Document the wrapper usage in release notes whenever the CLI contract shifts.
- For ad-hoc diagnostics, `scripts/prom_scrape_check.py` is a backward-compatible wrapper around the same Typer CLI, so legacy automation can still invoke the check without code changes.
- CI/automation can set `MDWB_CHECK_METRICS=1` (plus `CHECK_METRICS_TIMEOUT` if needed) before
  running `scripts/run_checks.sh` to run the same health check after the pytest/Playwright stack.
- Set `MDWB_RUN_E2E=1` in CI/nightly jobs when you need the fast sentinel.
- Set `MDWB_RUN_E2E_RICH=1` (and optionally `RICH_E2E_ARTIFACT_DIR=tmp/rich_e2e_cli`) whenever you want the FlowLogger suite to run; the exported `.log`/`.html`
  transcripts under `tmp/rich_e2e_cli/` contain the Panels/Tables/Progress output described in PLAN §14.2, so attach that directory to build artifacts for easy debugging.
- Use `MDWB_RUN_E2E_GENERATED=1` alongside `MDWB_GENERATED_E2E_CASES=/path/to/cases.json` when validating refreshed Markdown baselines for the generative guardrail tests.

Tie the new counters into `ops/dashboards.json`/`ops/alerts.md` so Grafana can page when
warning spikes, job failures, or SSE stalls exceed their budgets.

## Automation Hooks
- Schedule the nightly job via cron or the CI runner (e.g., 02:00 UTC) and archive
  the resulting `benchmarks/production/<DATE>` directory as a build artifact.
- Use the weekly summary JSON to feed Grafana/Metabase until we switch to direct
  metrics ingestion.
- Immediately after refreshing the `latest_*` pointers, run `scripts/compute_slo.py` (see command above)
  to generate `latest_slo_summary.json`; that file feeds the PLAN §20.1 dashboards/alerts so release gates
  have a single JSON artifact capturing per-category p50/p95 totals and budget breaches.
- GitHub Actions example: `.github/workflows/nightly_smoke.yml` installs uv/Playwright,
  writes a minimal `.env` from repository secrets (`MDWB_API_BASE_URL`, `MDWB_API_KEY`,
  `OLMOCR_SERVER`, `OLMOCR_API_KEY`), runs `scripts/check_env.py` to fail fast on misconfigurations,
  then executes `scripts/run_smoke.py --date ${{ steps.dates.outputs.today }}`,
  and uploads `benchmarks/production/<DATE>` as an artifact.

## API CLI Helpers

- Use `uv run python scripts/mdwb_cli.py demo snapshot` (or `demo stream`/`demo events`) to
  interact with the built-in `/jobs/demo` endpoints. The CLI automatically reads
  `API_BASE_URL` and `MDWB_API_KEY` from `.env`, so authenticated deployments just need
  the secrets filled in once.
- The CLI now enforces the required `.env` keys (`API_BASE_URL`, `OLMOCR_SERVER`,
  `OLMOCR_MODEL`, `OLMOCR_API_KEY`) via `_required_config()`. If one is missing it fails
  fast with a clear error—fill in `.env` (or pass `--api-base/--server`) before rerunning.
- Webhook helpers: `mdwb jobs webhooks list`, `... add`, and `... delete --id/--url`
  manage `/jobs/{id}/webhooks` without hand-written curl calls. Each command accepts `--json`
  when automation needs structured output (`list` returns `{status: \"ok\", webhooks:[...]}` while `add`/`delete`
  respond with `{status: \"ok\"|\"error\", ...}` and exit non-zero on failures) so CI/pipelines can branch on the result immediately.
- Override the API base temporarily via `--api-base https://staging.mdwb.internal`
  if you need to target a different environment.
- `uv run python scripts/mdwb_cli.py watch <job-id>` streams the human-friendly
  view on `/jobs/{id}/events` (state/progress/warnings) and automatically falls
  back to the SSE stream if the NDJSON endpoint is unavailable. Pass
  `--raw/--since/--interval` to align with automation requirements, and add `--on EVENT=COMMAND`
  (repeatable) to run shell hooks when particular events fire (e.g., `--on state:DONE='notify-send mdwb done'`).
- DOM-assist events (emitted whenever hybrid recovery patches OCR output) now show a short summary in the watcher output (count, reasons, sample tile) so ops can spot noisy overlays without opening manifests.
- The Events tab/CLI watchers now show blocklist, sweep, and validation events emitted
  directly by the SSE feed, so the new manifest breadcrumbs surface even when the
  Manifest tab isn’t open.
- When you run smoke manually (e.g., re-testing a single date), refresh the pointer files via
  `uv run python scripts/update_smoke_pointers.py <run-dir> [--root benchmarks/production]`
  (add `--weekly-source benchmarks/production/weekly_summary.json` if you need to override the weekly summary). This keeps
  `latest_summary.md`, `latest_manifest_index.json`, and `latest_metrics.json` aligned with the run that ops dashboards should display.
  The script now recomputes `latest_slo_summary.json` automatically using the new manifest pointer plus the PLAN §22 budget file; pass
  `--budget-file` to point at a different definition or `--no-compute-slo` when you intentionally want to leave the previous summary untouched.
- `uv run python scripts/mdwb_cli.py events <job-id> --follow` tails the raw
  `/jobs/{id}/events` NDJSON feed for pipelines; combine with `--since` to resume
  from the last timestamp when running in cron or CI.
- `uv run python scripts/mdwb_cli.py fetch <url> --webhook-url https://example.com/hook`
  registers the webhook immediately after the job ID is created (repeat flag to add more);
  add `--webhook-event <STATE>` to override the default DONE/FAILED trigger set. Failed registrations
  are logged inline (CLI still returns zero so captures proceed), and you can re-run
  `mdwb jobs webhooks add` later if a selector needs to be corrected.
- `uv run python scripts/mdwb_cli.py jobs artifacts manifest <job-id> --out manifest.json`
  (or `markdown`, `links`) downloads the persisted files without curl; use `--pretty/--raw`
  to control JSON formatting when writing to disk.
- `uv run python scripts/mdwb_cli.py jobs show <job-id>` now includes "Sweep" and "Validation" rows summarizing overlap ratios, shrink/retry counts, and any tile-integrity failures right in the table—use this before diving into manifests when triaging seams.
- `uv run python scripts/mdwb_cli.py warnings tail --json` streams warning log entries with derived fields like `validation_failure_count`, `overlap_match_ratio`, and `sweep_summary`, making it easier to feed dashboards without parsing the text table.

- `uv run python scripts/mdwb_cli.py jobs bundle <job-id> --out bundle.tar.zst`
  fetches the tarball (`bundle.tar.zst`) that Store emits alongside the tiles/markdown/links/manifest, keeping
  incidents and reruns reproducible without hand-written curl commands.
- `uv run python scripts/mdwb_cli.py jobs replay manifest <manifest.json>` replays a saved manifest
  via `POST /replay` with path validation, HTTP/2 toggles, and optional JSON output. The legacy
  `scripts/replay_job.sh` script remains for legacy automation but new workflows should prefer the CLI.
- `uv run python scripts/mdwb_cli.py jobs embeddings search <job-id> --vector-file vector.json --top-k 5`
  posts a stored embedding vector to `/jobs/{id}/embeddings/search` so you can jump to the most relevant sections
  without cracking open sqlite. Pass `--vector "[0.1,0.2,...]"` for inline JSON or `--json` when automation needs
  structured matches.
- `uv run python scripts/mdwb_cli.py diag <job-id>` prints CfT/Playwright metadata, capture/OCR/stitch timings,
  warnings, blocklist hits, the active profile id, and the new cache status for a run (with `--json` for automation),
  matching the incident-response playbook in PLAN §20.

### CLI Quick Reference
- **Diag:** `mdwb_cli.py diag <job-id> [--json]` – one-stop capture/OCR/stitch metadata for incidents (now includes profile + cache hit/miss).
- **Hooks:** `mdwb_cli.py watch <job-id> --on state:DONE='notify-send mdwb done'` (and `fetch --watch --on …`) – run shell commands whenever streamed events fire; commands receive `MDWB_EVENT_*` env vars.
- **Warnings:** `mdwb_cli.py warnings tail --json --count 100` – stream warning/blocklist incidents with sweep/validation metadata ready for dashboards.
- **Artifacts & bundles:** `mdwb_cli.py jobs artifacts manifest|markdown|links` and `jobs bundle <job-id> --out bundle.tar.zst` – fetch Markdown/links/manifests or the tarball without manual curl.
- **Replay:** `mdwb_cli.py jobs replay <manifest.json> --json` – replay stored manifests via `/replay`, replacing the old shell helper.
- **Embeddings:** `mdwb_cli.py jobs embeddings search <job-id> --vector-file vec.json --top-k 5 --json` – query sqlite-vec directly from the CLI when agents need to jump to specific Markdown sections.
