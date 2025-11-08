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
- Run `uv run python scripts/show_latest_smoke.py --manifest --metrics --weekly` to inspect
  the latest summary/manifest pointers plus the rolling `weekly_summary.json`. The CLI now
  highlights categories that exceed their p95 budgets and accepts `--limit` to trim the manifest
  table. Set `MDWB_SMOKE_ROOT=/path/to/runs` (or pass `--root`) if the pointer files live outside
  `benchmarks/production/`, and add `--json` when automation needs structured output instead of tables.
- For automation/health checks, run `uv run python scripts/show_latest_smoke.py check --root benchmarks/production`
  (add `--no-weekly` if you only care about summary/manifest/metrics). Pass `--json` when you want a structured payload
  (fields: `status`, `missing`, `root`, `weekly_required`, `run_date`) and rely on the exit code instead of parsing text.
  The command exits non-zero when any required pointer file is missing, making it ideal for CI/dashboards.

### Prerequisites
- `/jobs` API available on `API_BASE_URL` with credentials in `.env`.
- Chrome for Testing pin + Playwright version recorded in `manifest.json`.
- Enough quota on the hosted olmOCR endpoint for the nightly workload.

### Verification Checklist
1. `scripts/run_checks.sh` (ruff → ty → targeted pytest suite → Playwright smoke) passes before the smoke run. The bundled pytest step covers CLI events/webhooks, olmOCR CLI config, check_env, show_latest_smoke (including `check`/`--json`), and API webhook tests so regressions surface before captures run.
2. Each category report stays below its p95 latency budget (see `manifest_index.json`).
3. Failures must be triaged immediately; rerun `scripts/olmocr_cli.py run` on the
   offending URL with `--out-dir benchmarks/reruns` for deeper debugging.

## Weekly Latency Summary

`scripts/run_smoke.py` automatically refreshes `benchmarks/production/weekly_summary.json`
by folding the last seven days of `manifest_index.json` entries. The file contains:

- `generated_at`: ISO timestamp.
- `window_days`: currently 7.
- `categories`: list of `{name, runs, budget_ms, capture_ms.{p50,p95}, total_ms.{p50,p95}}`.

Run `uv run python scripts/show_latest_smoke.py --weekly --manifest --metrics --no-summary`
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

## Troubleshooting
- **API unreachable**: make sure `API_BASE_URL` resolves from the machine running the
  script; set `MDWB_API_KEY` if the deployment requires auth.
- **Typer CLI partial exits (code 10)**: inspect the job directory for `manifest.json`
  and `links.json` to see where OCR failed; re-run with `--timeout` bumped if tiles
  are still streaming.
- **OCR throttling**: temporarily reduce `OCR_MAX_CONCURRENCY` in `.env` and rerun,
  then notify the hosted OCR contact listed in `docs/olmocr_cli.md`.

## Warning & Blocklist Logs

- Every capture that emits warnings or blocklist hits appends a JSON line to
  `WARNING_LOG_PATH` (defaults to `ops/warnings.jsonl`). The record includes the job ID,
  URL, warning list, blocklist hits, sweep stats, and `validation_failures` so incidents
  can be triaged without scraping manifests.
- Use the CLI helper `uv run python scripts/mdwb_cli.py warnings tail --count 50 --json`
  (add `--follow` to stream, or `--log-path` to override the default) to review recent entries.
  When `--follow` is set the CLI now waits for the log to appear and automatically recovers from
  truncation/logrotate events, so long-lived tails keep running overnight. The pretty output
  summarizes warning codes, blocklist selectors, sweep overlap ratios, and validation failures
  so ops can spot duplicate seams or retries immediately.
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
  - `mdwb_capture_warnings_total{code="…"}` and `mdwb_blocklist_hits_total{selector="…"}` so
    noisy overlays or duplicate seams trigger dashboards without parsing manifests.
  - `mdwb_job_completions_total{state="DONE|FAILED"}` to track success rates and
    `mdwb_sse_heartbeat_total` to alert on stalled `/jobs/{id}/stream` feeds.
- Quick smoke check:

```
uv run python scripts/check_metrics.py run --timeout 3.0
curl -s http://localhost:8000/metrics | grep mdwb_capture_duration_seconds
curl -s http://localhost:9000/metrics | head -n 5  # dedicated Prom port
```

The CLI command above reads `.env` for `API_BASE_URL`/`PROMETHEUS_PORT` (override with
`--api-base`/`--exporter-port`). Use `--no-include-exporter` when only the primary `/metrics`
endpoint is exposed.

Tie the new counters into `ops/dashboards.json`/`ops/alerts.md` so Grafana can page when
warning spikes, job failures, or SSE stalls exceed their budgets.

## Automation Hooks
- Schedule the nightly job via cron or the CI runner (e.g., 02:00 UTC) and archive
  the resulting `benchmarks/production/<DATE>` directory as a build artifact.
- Use the weekly summary JSON to feed Grafana/Metabase until we switch to direct
  metrics ingestion.
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
- The Events tab/CLI watchers now show blocklist, sweep, and validation events emitted
  directly by the SSE feed, so the new manifest breadcrumbs surface even when the
  Manifest tab isn’t open.
- When you run smoke manually (e.g., re-testing a single date), refresh the pointer files via
  `uv run python scripts/update_smoke_pointers.py <run-dir> --root benchmarks/production`
  (add `--weekly-source benchmarks/production/weekly_summary.json` if you need to override the weekly summary). This keeps
  `latest_summary.md`, `latest_manifest_index.json`, and `latest_metrics.json` aligned with the run that ops dashboards should display.
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
  warnings, and blocklist hits for a run (with `--json` for automation), matching the incident-response playbook in PLAN §20.
