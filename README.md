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
5. Stitcher merges Markdown, trims overlaps via SSIM + fuzzy text comparisons, injects provenance comments, and builds the Links Appendix.
6. `Store` writes artifacts under a content-addressed path and updates sqlite + sqlite-vec metadata for embeddings search.
7. `/jobs/{id}`, `/jobs/{id}/stream`, `/jobs/{id}/events`, `/jobs/{id}/links.json`, etc., feed the HTMX UI, CLI, and agent automations.

See `PLAN_TO_IMPLEMENT_MARKDOWN_WEB_BROWSER_PROJECT.md` §§2–5, 19 for the full breakdown.

## Quickstart
1. **Install prerequisites**
   - Python 3.13, uv ≥0.8, and the system deps Playwright requires.
   - Install the CfT build Playwright expects: `playwright install chromium --with-deps --channel=cft`.
   - Create/sync the env: `uv venv --python 3.13 && uv sync`.
2. **Configure environment**
   - Copy `.env.example` → `.env`.
   - Fill in OCR creds, `API_BASE_URL`, CfT label/build, screenshot style hash overrides, webhook secret, etc.
   - Settings are loaded exclusively via `python-decouple` (`app/settings.py`), so keep `.env` private.
3. **Run the API/UI**
   - `uv run python -m app.main`
   - Open `http://localhost:8000` for the HTMX/Alpine interface.
4. **Trigger a capture**
   - UI Run button posts `/jobs`.
   - CLI example: `uv run python scripts/mdwb_cli.py fetch https://example.com --watch`

## CLI cheatsheet (`scripts/mdwb_cli.py`)
- `fetch <url> [--watch]` — enqueue + optionally stream Markdown as tiles finish.
- `show <job-id>` — dump the latest job snapshot (state, warnings, manifest paths).
- `stream <job-id>` — follow the SSE feed.
- `watch <job-id>` / `events <job-id> --follow --since <ISO>` — tail the `/jobs/{id}/events` NDJSON log.
- `warnings --count 50` — tail `ops/warnings.jsonl` for capture/blocklist incidents.
- `dom links --job-id <id>` — render the stored `links.json` (anchors/forms/headings/meta).
- `demo snapshot|stream|events` — exercise the demo endpoints without hitting a live pipeline.

The CLI reads `API_BASE_URL` + `MDWB_API_KEY` from `.env`; override with `--api-base` when targeting staging.

## Prerequisites & environment
- **Chrome for Testing pin:** Set `CFT_VERSION` + `CFT_LABEL` in `.env` so manifests and ops dashboards stay consistent. Re-run `playwright install` whenever the label/build changes.
- **Transport + viewport:** Defaults (`PLAYWRIGHT_TRANSPORT=cdp`, viewport 1280×2000, DPR 2) live in `app/settings.py` and must align with PLAN §§3, 19.
- **OCR credentials:** `OLMOCR_SERVER`, `OLMOCR_API_KEY`, and `OLMOCR_MODEL` are required unless you point at `OCR_LOCAL_URL`.
- **Warning log + blocklist:** Keep `WARNING_LOG_PATH` and `BLOCKLIST_PATH` writable so scroll/overlay incidents are persisted (`docs/config.md` documents every field).

## Testing & quality gates
Run these before pushing or shipping capture-facing changes:

```bash
uv run ruff check --fix --unsafe-fixes
uvx ty check
uv run playwright test tests/smoke_capture.spec.ts
```

`./scripts/run_checks.sh` wraps the same sequence for CI.

Also run `uv run python scripts/check_env.py` whenever `.env` changes—CI and nightly smokes depend on it to confirm CfT pins + OCR secrets.

Additional expectations (per PLAN §§14, 19.10, 22):
- Keep nightly smokes green via `uv run python scripts/run_smoke.py --date $(date -u +%Y-%m-%d)`.
- Refresh `benchmarks/production/weekly_summary.json` (generated automatically by the smoke script) for Monday ops reports.
- Tail `ops/warnings.jsonl` or `mdwb warnings` for canvas/video/overlay spikes.

## Day-to-day workflow
- **Reserve + communicate:** Before editing, reserve files and announce the pickup via Agent Mail (cite the bead id). Keep PLAN sections annotated with `_Status — <agent>` entries so the written record matches reality.
- **Track via beads:** Use `bd list/show` to pick the next unblocked issue, add comments for status updates, and close with findings/tests noted.
- **Run the required checks:** `ruff`, `ty`, Playwright smoke, `scripts/check_env.py`, plus any bead-specific tests (e.g., sqlite-vec search or CLI watch). Never skip the capture smoke after touching Playwright/OCR code.
- **Sync docs:** README, PLAN, `docs/config.md`, and `docs/ops.md` must stay consistent; update them alongside code changes so ops can trust the written guidance.
- **Ops handoff:** For capture/OCR fixes, capture job ids + manifest paths in your bead comment and Mail thread so others can reproduce issues quickly.

## Operations & automation
- `scripts/run_smoke.py` — nightly URL set capture + manifest/latency aggregation.
- `scripts/show_latest_smoke.py` — quick pointers to the latest smoke outputs.
- `scripts/olmocr_cli.py` + `docs/olmocr_cli.md` — hosted olmOCR orchestration/diagnostics.
- `scripts/replay_job.sh` — re-run a job with a stored manifest via `POST /replay`.
- Prometheus metrics exposed via FastAPI (`prometheus-client`); see `ops/dashboards.json` & `ops/alerts.md`.

### Handy commands
```bash
# Validate env
uv run python scripts/check_env.py

# Run cli demo job
uv run python scripts/mdwb_cli.py demo stream

# Replay an existing manifest
scripts/replay_job.sh cache/example.com/.../manifest.json

# Tail warning log via CLI
uv run python scripts/mdwb_cli.py warnings --count 25

# Run nightly smoke for docs/articles only (dry run)
uv run python scripts/run_smoke.py --date $(date -u +%Y-%m-%d) --category docs_articles --dry-run
```

## Artifacts you should expect per job
- `artifact/tiles/tile_*.png` — viewport-sweep tiles (≤1288 px long side) with overlap + SHA metadata.
- `out.md` — final Markdown with provenance comments (`<!-- source: tile_i ... -->`) and Links Appendix.
- `links.json` — anchors/forms/headings/meta harvested from the DOM snapshot.
- `manifest.json` — CfT label/build, Playwright version, screenshot style hash, warnings, sweep stats, timings.
- `dom_snapshot.html` — raw DOM capture used for link diffs and hybrid recovery (when enabled).
- `bundle.tar.zst` — optional tarball for incidents/export (`Store.build_bundle`).

Use `scripts/replay_job.sh` or `/jobs/{id}/artifact/...` endpoints to fetch any of the above for debugging.

## Communication & task tracking
- **Beads** (`bd ...`) track every feature/bug (map bead IDs to Plan sections in Agent Mail threads).
- **Agent Mail** (MCP) is the coordination channel—reserve files before editing, summarize work in the relevant bead thread, and note Plan updates inline (_see §§10–11 for example status notes_).

## Further reading
- `AGENTS.md` — ground rules (no destructive git cmds, uv usage, capture policies).
- `PLAN_TO_IMPLEMENT_MARKDOWN_WEB_BROWSER_PROJECT.md` — canonical spec + incremental upgrades.
- `docs/architecture.md` — best practices + data flow diagrams.
- `docs/blocklist.md`, `docs/config.md`, `docs/models.yaml`, `docs/ops.md`, `docs/olmocr_cli.md` — supporting specs.

## Troubleshooting cheatsheet
- **Playwright/CfT mismatch:** Run `playwright install chromium --with-deps --channel=cft` and confirm `CFT_VERSION`/`CFT_LABEL` match the installed build. If CfT labels shifted, update `.env` + manifests before rerunning.
- **`.env` drift:** `uv run python scripts/check_env.py --json` pinpoints missing values. Required vars with `None` will fail CI.
- **OCR throttling:** Lower `OCR_MAX_CONCURRENCY`, restart the job, and capture request IDs from manifests for the ops thread.
- **Warning log explosions:** Tail `uv run python scripts/mdwb_cli.py warnings --count 100 --json` and look for repeated warning codes (canvas-heavy, scroll-shrink). Update `docs/blocklist.md` / selectors if overlays broke capture.
- **SSE disconnects:** The UI should show an SSE health badge; check `/jobs/{id}/events` NDJSON output via `mdwb events --follow` to ensure the backend is still emitting. If not, inspect `app/jobs.py` logs for heartbeat gaps.
- **Manifest missing links/DOM snapshots:** Ensure the capture job has write access to `CACHE_ROOT`; the Store will refuse to emit `/jobs/{id}/links.json` when the DOM snapshot can’t be written.

Questions? Start a bead, announce it via Agent Mail, and keep PLAN/README/doc updates in lockstep.
