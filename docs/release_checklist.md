# Release & Regression Checklist
_Last updated: 2025-11-09_

Use this document whenever we cut a Markdown Web Browser release (CfT/Playwright/model/runtime changes) or when verifying a hotfix. Each section lists the required actions plus the artifacts that must be attached to the release/hand-off thread.

---

## 0. Prerequisites
1. `.env` in sync with `.env.example` (especially `CFT_VERSION`, `CFT_LABEL`, `PLAYWRIGHT_CHANNEL`, `PLAYWRIGHT_TRANSPORT`, `SERVER_IMPL`/`MDWB_SERVER_IMPL`, OCR keys, `CACHE_ROOT`).
2. Chrome for Testing channel installed locally: `playwright install chromium --with-deps --channel=cft`.
3. uv environment synced (`uv sync`) and Playwright smoke deps installed.
4. Access to warning log + Prometheus exporter (for smoke verification) and hosted olmOCR quota dashboards.
5. Update PLAN/README refs if any new env vars were added.

---

## 1. Configuration capture
Record the following in the release PR / Agent Mail thread:
- CfT label + build (`CFT_LABEL`, `CFT_VERSION`).
- Playwright version (`npx playwright --version`).
- Server runtime (`SERVER_IMPL` / manifest `environment.server_runtime`).
- OCR policy + concurrency window (`OCR_MIN_CONCURRENCY`, `OCR_MAX_CONCURRENCY`).
- Blocklist version (`config/blocklist.json` → `version`).
- CLI toggles used (`MDWB_CHECK_METRICS`, `MDWB_RUN_E2E`, `MDWB_SMOKE_ROOT`, etc.).
- Links to the job IDs used for final validation (see §3).

---

## 2. Test Matrix (run in order)
| Step | Command | Notes |
| --- | --- | --- |
| 1 | `uv run ruff check --fix --unsafe-fixes` | no lint regressions |
| 2 | `uvx ty check` | type safety (Typer CLI + web helpers) |
| 3 | `MDWB_CHECK_METRICS=1 CHECK_METRICS_TIMEOUT=10 bash scripts/run_checks.sh` | lints + typer tests + targeted pytest + Prometheus smoke + Playwright smoke (set `MDWB_RUN_E2E=1` when release contains CLI changes) |
| 4 | `SERVER_IMPL=granian HOST=0.0.0.0 PORT=8000 scripts/dev_run.sh --workers 4 --granian-runtime-threads 2` | launch API using the production runtime |
| 5 | `uv run python scripts/mdwb_cli.py fetch <url> --watch --webhook-url ...` | exercise CLI submit/watch with webhooks |
| 6 | `uv run python scripts/run_smoke.py --date $(date -u +%Y-%m-%d) --category docs_articles --category dashboards_apps` | run partial smoke (full nightly before GA); use `--dry-run` first if API is down |
| 7 | `uv run python scripts/show_latest_smoke.py --manifest --metrics --weekly --limit 5 --json` | verify pointers + weekly budget |
| 8 | `uv run python scripts/compute_slo.py --root benchmarks/production --manifest benchmarks/production/latest_manifest_index.json --budget-file benchmarks/production_set.json --out benchmarks/production/latest_slo_summary.json` | capture per-category SLO summary JSON for dashboards/release notes |
| 9 | `uv run python scripts/check_metrics.py --json --include-exporter` | final telemetry probe |

Document pass/fail + links/attachments for each step.

---

## 3. Required artifacts
Attach or link the following in the release hand-off:
1. **Three representative captures** (`/jobs/<id>` links):
   - Long-form article (docs/articles category)
   - App/dashboard (canvas-heavy)
   - Lightweight marketing page
   Each should include `mdwb diag <job_id>` output (JSON) and highlight any OCR autotune events.
2. **Warning log excerpt** from the last successful smoke (`ops/warnings.jsonl` → last 10 entries).
3. **`benchmarks/production/<date>/manifest_index.json` + `summary.md`** produced by `scripts/run_smoke.py`.
4. **`benchmarks/production/latest_slo_summary.json`** from `scripts/compute_slo.py` (attach raw JSON so ops/Grafana can mirror it).
4. **`tmp/pytest_summary.json`** from `scripts/run_checks.sh`.
5. **Release notes skeleton** (include CfT/Playwright/model versions, env changes, toggles flipped).

---

## 4. Regression checklist
1. Confirm `manifest.json` for each validation job shows:
   - `environment.cft_label`/`cft_version` and `server_runtime` as expected.
   - `ocr_autotune` block present (verify controller did not throttle unexpectedly).
   - `warnings` only contain known baseline entries (no unexplained `canvas-heavy`, `scroll-shrink`, etc.).
2. Review the Links tab delta table for each job; document any DOM-vs-OCR mismatches that require follow-up.
3. Inspect `ops/warnings.jsonl` for new codes; update `docs/blocklist.md` if overlays were added.
4. Ensure the `mdwb jobs ocr-metrics <job_id>` output shows reasonable latency (<4s) and no quota warnings.
5. Verify CLI replays still work: `uv run python scripts/mdwb_cli.py jobs replay manifest <manifest.json> --json`.
6. If CfT/Playwright changed, rerun the viewport sweep regression harness + table seam fuzzers (see PLAN §17) and attach logs.

---

## 5. Sign-off & Handoff
- Update `PLAN_TO_IMPLEMENT_MARKDOWN_WEB_BROWSER_PROJECT.md` §20.3 status line with the release date + summary.
- Tag the release (Git + Agent Mail) with:
  - Command output from §2
  - Artifacts from §3
  - Notable findings from §4
- Ensure `README.md` “Troubleshooting” + relevant docs mention any new env vars or toggles introduced in this release.
- Post final status in Agent Mail `[release]` thread and confirm downstream consumers (ops dashboards, CLI owners) acknowledge.

---

## Appendix A – Toggle cheat sheet
| Toggle | Purpose |
| --- | --- |
| `MDWB_CHECK_METRICS` | Enables Prometheus smoke inside `scripts/run_checks.sh`. |
| `MDWB_RUN_E2E` | Runs heavy CLI E2E suite during run_checks. |
| `MDWB_SMOKE_ROOT` | Overrides the directory `scripts/show_latest_smoke.py` reads from. |
| `SERVER_IMPL` / `MDWB_SERVER_IMPL` | Chooses uvicorn vs Granian for API server. |
| `OCR_MIN_CONCURRENCY` / `OCR_MAX_CONCURRENCY` | Bounds the autotune controller; set equal to lock concurrency. |
| `SKIP_LIBVIPS_CHECK` | Skips the libvips preflight (only for CI environments without libvips). |
