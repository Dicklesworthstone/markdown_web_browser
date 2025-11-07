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
| `OLMOCR_SERVER` | `https://ai2endpoints.cirrascale.ai/api` | Remote olmOCR endpoint recorded as `ocr.server_url`. |
| `OLMOCR_API_KEY` | *(unset)* | API key for hosted olmOCR. Required outside local dev. |
| `OLMOCR_MODEL` | `olmOCR-2-7B-1025-FP8` | Default OCR policy identifier included in manifests + provenance comments. |
| `OCR_LOCAL_URL` | *(unset)* | Optional self-hosted olmOCR endpoint; overrides `OLMOCR_SERVER` when set. |
| `OCR_USE_FP8` | `true` | Whether FP8 inference is enabled; surfaced via `environment.ocr_use_fp8`. |
| `API_BASE_URL` | `http://localhost:8000` | FastAPI base URL consumed by the CLI + automation; recorded in manifests when replaying jobs. |
| `MDWB_API_KEY` | *(unset)* | Optional bearer token for `/jobs` HTTP calls (CLI + automation). |
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
| `BLOCKLIST_PATH` | `config/blocklist.json` | JSON selectors injected during capture; recorded via `manifest.blocklist_version`. |
| `VIEWPORT_OVERLAP_PX` | `120` | Pixels of overlap between viewport sweeps (Plan §19.2). |
| `TILE_LONG_SIDE_PX` | `1288` | Tile longest side enforced by pyvips tiler + CLI defaults. |
| `TILE_OVERLAP_PX` | `120` | Overlap inside the pyvips tiler; must match SSIM stitching heuristics. |
| `SCROLL_SETTLE_MS` | `350` | Wait time between scrolls so lazy-loaded content settles. |
| `MAX_VIEWPORT_SWEEPS` | `200` | Guardrail to prevent infinite scroll loops. |
| `SCROLL_SHRINK_RETRIES` | `2` | Number of times to re-sweep when SPA height shrinks mid-run. Logged in manifest stats. |
| `SCREENSHOT_MASK_SELECTORS` | *(empty)* | Comma-separated selectors masked during screenshots (cookie banners, tickers). |
| `SCREENSHOT_STYLE_HASH` | auto-derived if blank | Hash of viewport/mask settings included in manifests & bug reports. |
| `PROMETHEUS_PORT` | `9000` | Port for the Prometheus metrics endpoint. |
| `HTMX_SSE_HEARTBEAT_MS` | `4000` | Interval (ms) for SSE heartbeat events streamed to the UI. |
| `CANVAS_WARNING_THRESHOLD` | `3` | Number of `<canvas>` nodes before we emit a `canvas_heavy` warning. |
| `VIDEO_WARNING_THRESHOLD` | `2` | Number of `<video>` nodes before we emit a `video_overlay` warning. |

Add any new variables to `.env.example`, document them here, and update the
manifest schema if they need to be echoed downstream.

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
