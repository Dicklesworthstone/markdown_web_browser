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
  --seed 0
```

Add `--dry-run` when you want to exercise the pipeline without hitting `/jobs`
(useful before the API is live or when secrets are unavailable). Pair it with
`--seed <int>` (defaults to 0) so synthetic manifests remain deterministic.
Dry runs still write manifests, summary markdown, and weekly stats so downstream
tooling can be tested.

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

### Prerequisites
- `/jobs` API available on `API_BASE_URL` with credentials in `.env`.
- Chrome for Testing pin + Playwright version recorded in `manifest.json`.
- Enough quota on the hosted olmOCR endpoint for the nightly workload.

### Verification Checklist
1. `scripts/run_checks.sh` (ruff → ty → Playwright smoke) passes before the smoke run.
2. Each category report stays below its p95 latency budget (see `manifest_index.json`).
3. Failures must be triaged immediately; rerun `scripts/olmocr_cli.py run` on the
   offending URL with `--out-dir benchmarks/reruns` for deeper debugging.

## Weekly Latency Summary

`scripts/run_smoke.py` automatically refreshes `benchmarks/production/weekly_summary.json`
by folding the last seven days of `manifest_index.json` entries. The file contains:

- `generated_at`: ISO timestamp.
- `window_days`: currently 7.
- `categories`: list of `{name, runs, budget_ms, capture_ms.{p50,p95}, total_ms.{p50,p95}}`.

Publish the summary in Monday’s ops update and attach the most recent
`benchmarks/production/<DATE>/manifest_index.json` for traceability.

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
  URL, warning list, and blocklist version/hits so incidents can be triaged without
  scraping manifests.
- Use the CLI helper `uv run python scripts/mdwb_cli.py warnings --count 50`
  (or pass `--log-path` / `--json`) to review recent entries. This is the fastest way
  to confirm whether canvas/video/sticky or sweep warnings spiked overnight.
- Rotate/ship the log via your usual log aggregation tooling; the file is plain JSONL
  and safe to ingest into Loki/Elastic/GCS.

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
- Override the API base temporarily via `--api-base https://staging.mdwb.internal`
  if you need to target a different environment.
- `uv run python scripts/mdwb_cli.py events <job-id> --follow` tails the real
  `/jobs/{id}/events` JSONL feed we expose for agents/automation; combine with
  `--since` to resume from the last timestamp when running in cron or CI.
