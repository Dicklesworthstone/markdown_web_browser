# Configuration Reference

_Last updated: 2025-11-07 (UTC)_

All runtime configuration flows through `python-decouple`. Import
`app.settings.settings` instead of reading environment variables directly so the
browser/OCR defaults stay consistent across services and manifests.

```python
from app.settings import settings

print(settings.browser.cft_version)
print(settings.ocr.server_url)
```

## Environment Variables

| Name | Default | Purpose / Manifest Echo |
| --- | --- | --- |
| `OLMOCR_SERVER` | `https://ai2endpoints.cirrascale.ai/api` | Remote olmOCR endpoint recorded as `ocr.server_url`. Required for `scripts/olmocr_cli.py`. |
| `OLMOCR_API_KEY` | *(unset)* | API key for hosted olmOCR. Required outside local dev (CLI now enforces this). |
| `OLMOCR_MODEL` | `olmOCR-2-7B-1025-FP8` | Default OCR policy identifier included in manifests + provenance comments (required by CLI). |
| `API_BASE_URL` | `http://localhost:8000` | FastAPI base URL consumed by the CLI + automation; the CLI now fails fast if unset. |
| `MDWB_API_KEY` | *(unset)* | Optional bearer token for `/jobs` HTTP calls (CLI + automation). |
| `OCR_LOCAL_URL` | *(unset)* | Optional self-hosted olmOCR endpoint; overrides `OLMOCR_SERVER` when set. |
| `OCR_USE_FP8` | `true` | Whether FP8 inference is enabled; surfaced via `environment.ocr_use_fp8`. |
| `OCR_MIN_CONCURRENCY` | `2` | Minimum OCR concurrency window (`environment.ocr_concurrency.min`). |
| `OCR_MAX_CONCURRENCY` | `8` | Maximum OCR concurrency window (`environment.ocr_concurrency.max`). Must be ≥ min. |
| `CACHE_ROOT` | `.cache` | Root directory for content-addressed artifacts (tiles, manifests, tar bundles). |
| `RUNS_DB_PATH` | `runs.db` | SQLite file storing `runs`, `links`, and sqlite-vec embeddings. |
| `CFT_VERSION` | `chrome-130.0.6723.69` | Chrome for Testing label/build pinned for Playwright; log in every manifest. |
| `CFT_LABEL` | `Stable-1` | CfT channel label surfaced in `environment.cft_label`. |
| `PLAYWRIGHT_CHANNEL` | `cft` | Browser channel launched by Playwright. |
| `PLAYWRIGHT_TRANSPORT` | `cdp` | Transport used to drive Chromium (`cdp` or `bidi`); needed for ops escalations. |
| `CAPTURE_VIEWPORT_WIDTH` | `1280` | Pixel width for captures + manifest `viewport.width`. |
| `CAPTURE_VIEWPORT_HEIGHT` | `2000` | Pixel height for captures + manifest `viewport.height`. |
| `CAPTURE_DEVICE_SCALE_FACTOR` | `2` | DSF used for capture; feeds screenshot style hash + manifest viewport block. |
| `CAPTURE_COLOR_SCHEME` | `light` | Color scheme forced during capture (manifest `viewport.color_scheme`). |
| `CANVAS_WARNING_THRESHOLD` | `3` | Canvas element count that emits a `canvas-heavy` capture warning. |
| `VIDEO_WARNING_THRESHOLD` | `2` | Video element count that emits a `video-heavy` capture warning. |
| `SEAM_WARNING_RATIO` | `0.9` | Overlap match ratio that triggers a duplicate-seam warning. |
| `SEAM_WARNING_MIN_PAIRS` | `5` | Minimum overlap pair count before seam warnings fire. |
| `SCROLL_SHRINK_WARNING_THRESHOLD` | `1` | Number of scroll-height shrink events before emitting a `scroll-shrink` warning. |
| `OVERLAP_WARNING_RATIO` | `0.65` | Minimum acceptable overlap match ratio (0–1); lower ratios trigger `overlap-low`. |
| `BLOCKLIST_PATH` | `config/blocklist.json` | JSON selectors injected during capture; recorded via `manifest.blocklist_version`. |
| `WARNING_LOG_PATH` | `ops/warnings.jsonl` | JSONL file that stores capture warning/blocklist incidents. |
| `MDWB_SMOKE_ROOT` | `benchmarks/production` | Optional override for the smoke pointer directory consumed by `scripts/show_latest_smoke.py`; set when the latest summary/manifest/weekly files live elsewhere (e.g., CI artifacts). |
| `VIEWPORT_OVERLAP_PX` | `120` | Pixels of overlap between viewport sweeps (Plan §19.2). |
| `CAPTURE_LONG_SIDE_PX` | `1288` | Tile longest side enforced by pyvips tiler + CLI defaults. |
| `TILE_OVERLAP_PX` | `120` | Overlap inside the pyvips tiler; must match SSIM stitching heuristics. |
| `SCROLL_SETTLE_MS` | `350` | Wait time between scrolls so lazy-loaded content settles. |
| `MAX_VIEWPORT_SWEEPS` | `200` | Guardrail to prevent infinite scroll loops. |
| `SCROLL_SHRINK_RETRIES` | `2` | Number of times to re-sweep when SPA height shrinks mid-run. Logged in manifest stats. |
| `SCREENSHOT_MASK_SELECTORS` | *(empty)* | Comma-separated selectors masked during screenshots (cookie banners, tickers). |
| `SCREENSHOT_STYLE_HASH` | auto-derived if blank | Hash of viewport/mask settings included in manifests & bug reports. |
| `PROMETHEUS_PORT` | `9000` | Port for the standalone Prometheus exporter (the API also exposes `/metrics`). |
| `HTMX_SSE_HEARTBEAT_MS` | `4000` | Interval (ms) for SSE heartbeat events streamed to the UI. |
| `WEBHOOK_SECRET` | `mdwb-dev-webhook` | Shared secret used to sign `/jobs/{id}/webhooks` callbacks. |

Add any new variables to `.env.example`, document them here, and update the
manifest schema if they need to be echoed downstream.

### Validating configuration
- Run `uv run python scripts/check_env.py` to verify all required variables are present.
  Use `--json` for machine-readable output in CI.
- The script fails (exit code 1) when a required setting is missing, helping catch
  misconfigured environments before capture jobs run.

## Manifest Metadata

`app.settings.Settings.manifest_environment()` returns the canonical dictionary
used by the capture pipeline when building `manifest.json`. The Pydantic models
in `app/schemas.py` (see `ManifestEnvironment`, `ManifestTimings`, and
`ManifestMetadata`) ensure each manifest records:

* CfT version + Playwright channel/version
* Browser transport (CDP vs BiDi)
* Viewport + overlap metadata (width/height/DSF, long-side policy, settle timers, mask selectors)
* Screenshot style hash for the masked/blocked CSS bundle
* Warning entries (canvas/video-heavy) with counts + thresholds so Ops can escalate overlays
* Sweep stats (`sweep_stats`) that show shrink events, retry attempts, and overlap match ratios to explain seam trims or viewport restarts
* `validation_failures` array that records any tile integrity errors caught by `validate_tiles()` and feeds the warning log even when no other warnings fired
* OCR model + FP8 status + concurrency window
* Timing metrics (`capture_ms`, `ocr_ms`, `stitch_ms`, `total_ms`) once stages
  execute

```jsonc
{
  "environment": {
    "cft_version": "chrome-130.0.6723.69",
    "cft_label": "Stable-1",
    "playwright_channel": "cft",
    "playwright_version": "1.50.0",
    "browser_transport": "cdp",
    "viewport": {
      "width": 1280,
      "height": 2000,
      "device_scale_factor": 2,
      "color_scheme": "light"
    },
    "viewport_overlap_px": 120,
    "tile_overlap_px": 120,
    "scroll_settle_ms": 350,
    "max_viewport_sweeps": 200,
    "screenshot_style_hash": "dev-sweeps-v1",
    "screenshot_mask_selectors": [],
    "ocr_model": "olmOCR-2-7B-1025-FP8",
    "ocr_use_fp8": true,
    "ocr_concurrency": { "min": 2, "max": 8 }
  },
  "timings": {
    "capture_ms": 1480,
    "ocr_ms": 4230,
    "stitch_ms": 510,
    "total_ms": 6220
  }
}
```

## Operational Defaults & Notes

* Always run Playwright with `viewport=1280×2000`, `deviceScaleFactor=2`,
  `colorScheme="light"`, reduced motion, and animation disabling so CfT output
  is deterministic.
* Tiling policy: longest side ≤1288 px with ≈120 px overlap (Plan §§3, 19.3).
* Use HTTP/2 (`httpx.AsyncClient(http2=True)`) when sending many OCR tiles to
  the same host; document rate limits + concurrency in manifests so Ops can
  correlate spikes.
* Prometheus + HTMX SSE heartbeat intervals come directly from this config—keep
  dashboards/tests in sync with any changes.
* The warning/blocklist JSONL log now includes `sweep_stats`, overlap ratios, and any `validation_failures`, so Ops can spot retries or seam duplication even when DOM warnings don’t fire. Ensure the log rotation/search tooling ingests the new keys.
* Prometheus scraping now covers capture/OCR/stitch latencies, warning/blocklist totals, SSE heartbeats, and job completions. Scrape `/metrics` directly or hit the exporter bound to `PROMETHEUS_PORT` when you need a dedicated port.
