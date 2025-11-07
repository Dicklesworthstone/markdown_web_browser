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
  --timeout 900
```

- Loads `benchmarks/production_set.json` (docs/articles, dashboards/apps,
  lightweight pages) and runs each URL via `scripts/olmocr_cli.py`.
- Stores outputs under `benchmarks/production/<DATE>/<category>/<timestamp_slug>/`.
  Each directory contains `manifest.json`, `out.md`, `links.json`, and the tile
  PNGs under `artifact/`.
- Writes a daily `manifest_index.json` aggregating the job IDs, budgets, and timing data,
  plus `summary.md` (Markdown table with per-category budgets vs. observed p95 capture/total).

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

## Automation Hooks
- Schedule the nightly job via cron or the CI runner (e.g., 02:00 UTC) and archive
  the resulting `benchmarks/production/<DATE>` directory as a build artifact.
- Use the weekly summary JSON to feed Grafana/Metabase until we switch to direct
  metrics ingestion.
- GitHub Actions example: `.github/workflows/nightly_smoke.yml` installs uv/Playwright,
  writes a minimal `.env` from repository secrets (`MDWB_API_BASE_URL`, `MDWB_API_KEY`,
  `OLMOCR_SERVER`, `OLMOCR_API_KEY`), runs `scripts/run_smoke.py --date ${{ steps.dates.outputs.today }}`,
  and uploads `benchmarks/production/<DATE>` as an artifact.
