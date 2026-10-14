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
| `OCR_LOCAL_AUTOSTART` | `true` | Enables automatic probe/reuse + local server autostart for local OpenAI-compatible OCR backends. |
| `OCR_LOCAL_STARTUP_TIMEOUT_S` | `180` | Max seconds to wait for local OCR service readiness before deterministic failure/restart handling. |
| `OCR_LOCAL_HEALTHCHECK_TIMEOUT_S` | `5` | Timeout (seconds) for each local OCR health probe attempt. |
| `OCR_LOCAL_MAX_RESTARTS` | `1` | Number of restart attempts after a failed local OCR startup before marking local runtime unavailable. |
| `OCR_USE_FP8` | `true` | Whether FP8 inference is enabled; surfaced via `environment.ocr_use_fp8`. |
| `OCR_MAX_BATCH_TILES` | `3` | Maximum number of tiles bundled into each OCR HTTP request (helps keep payloads deterministic). |
| `OCR_MAX_BATCH_BYTES` | `25000000` | Byte ceiling for a single OCR request; batches exceeding this size are split automatically. |
| `OCR_DAILY_QUOTA_TILES` | *(unset)* | Optional hosted OCR quota (in tiles). When set, manifests emit warnings at 70 % usage. |
| `OCR_MIN_CONCURRENCY` | `2` | Minimum OCR concurrency window (`environment.ocr_concurrency.min`). |
| `OCR_MAX_CONCURRENCY` | `8` | Maximum OCR concurrency window (`environment.ocr_concurrency.max`). Must be ≥ min. |
| `CACHE_ROOT` | `.cache` | Root directory for content-addressed artifacts (tiles, manifests, tar bundles) and persistent Chromium profiles (`CACHE_ROOT/profiles/<id>/storage_state.json`). |
| `RUNS_DB_PATH` | `runs.db` | SQLite file storing `runs`, `links`, and sqlite-vec embeddings. |
| `CFT_VERSION` | `chrome-130.0.6723.69` | Chrome for Testing label/build pinned for Playwright; log in every manifest. |
| `CFT_LABEL` | `Stable-1` | CfT channel label surfaced in `environment.cft_label`. |
| `CFT_EXECUTABLE` | unset | Exact installed CfT executable. The Linux default is `~/.cache/mdwb/cft/<four-part-build>/chrome-linux64/chrome`. A missing executable or observed build mismatch fails before OCR; this never silently selects the installed stock Chrome. |
| `PLAYWRIGHT_FONTCONFIG_FILE` | unset | Linux fontconfig override for captures. By default the browser uses an isolated configuration scanning system font directories, without user aliases. The configuration digest enters the manifest/cache identity; a zero-width glyph probe fails before OCR. Host font configuration is untouched. |
| `PLAYWRIGHT_CHANNEL` | `cft` | Browser channel launched by Playwright. Accepts a comma-separated preference list (e.g., `cft,chromium`) so operators can note the fallback order when CfT isn’t available locally. |
| `PLAYWRIGHT_TRANSPORT` | `cdp` | The installed Python Chromium adapter exercises CDP. A `bidi,cdp` preference selects and reports CDP; BiDi-only and unknown transports fail before OCR. |
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
| `MDWB_RUN_E2E_GENERATED` | `false` | Enable fresh authored-fixture capture/OCR/stitch/persistence tests. Disabled mode is explicitly skipped and supplies no quality qualification. Enabled runs fail on missing/empty/invalid cases or absent evidence. |
| `MDWB_GENERATED_E2E_CASES` | `tests/fixtures/e2e_generated/cases.json` | Hash-bound case list for the generated guardrail. Paths resolve relative to this file. No saved candidate is accepted; the suite creates fresh retained candidates with cache disabled. |
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
| `MDWB_SERVER_IMPL` | `uvicorn` | API server runtime used by `scripts/run_server.py` (`uvicorn` for dev, `granian` for higher throughput). |
| `MDWB_SERVER_WORKERS` | `1` | Worker processes for the launcher (set higher in production or when using Granian). |
| `MDWB_GRANIAN_RUNTIME_THREADS` | `1` | Runtime threads per worker when running under Granian. |
| `MDWB_SERVER_LOG_LEVEL` | `info` | Verbosity for both runtimes; mirrors `--log-level`. |

Add any new variables to `.env.example`, document them here, and update the
manifest schema if they need to be echoed downstream.

### Hosted model identity

The selected model is a request, not proof of the provider's deployed model.
Each manifest batch records `requested_model`, `served_model` and `request_id`.
An `ocr-model-substitution` warning identifies a different reported model.
Missing provider model metadata leaves served identity unverified.

On 2026-10-13, the configured DeepInfra endpoint returned
`google/gemma-4-31B-it` for `allenai/olmOCR-2-7B-1025` requests, consistent
with the [deprecation notice recorded in the olmOCR repository](https://github.com/allenai/olmocr/issues/460).
A successful extraction through that endpoint does not qualify olmOCR quality
or availability. To exercise olmOCR, configure a provider or local deployment
that actually serves the selected model; verify its reported identity and the
resulting content before advertising availability. Existing `.env` is preserved.

### Generated capture cases

Enable `MDWB_RUN_E2E_GENERATED=1` and set `MDWB_GENERATED_E2E_CASES` to a
nonempty JSON array. Each case names an authored HTML fixture, immutable expected
Markdown, the corresponding capture manifest and ordered expected PNG tiles.
Every file descriptor contains `path` and its actual64-character lowercase
`sha256`. Relative paths resolve against the cases file's directory. For example
(replace each digest placeholder with the reviewed file's actual digest):

```json
[
  {
    "name": "long-table",
    "scenario_author": "author/model and date of the authored scenario",
    "fixture": {"path": "long-table.html", "sha256": "REAL_64_HEX_SHA256"},
    "baseline": {"path": "expected/out.md", "sha256": "REAL_64_HEX_SHA256"},
    "baseline_manifest": {"path": "expected/manifest.json", "sha256": "REAL_64_HEX_SHA256"},
    "baseline_job_id": "RECORDED_32_HEX_JOB_ID",
    "baseline_tiles": [
      {"path": "expected/tile_0000.png", "sha256": "REAL_64_HEX_SHA256"},
      {"path": "expected/tile_0001.png", "sha256": "REAL_64_HEX_SHA256"}
    ],
    "request": {"viewport_width": 1280, "viewport_height": 2000, "device_scale_factor": 2},
    "tolerance": 0.02,
    "ssim_min": 0.99,
    "min_sweeps": 2,
    "timeout_seconds": 300.0
  }
]
```

Expected captures must have verified CfT/CDP and actual Playwright/style/font/
viewport/model/backend identity. The suite checks that identity against the new
candidate, requires genuine scrolling and pipeline stages/timings/OCR request IDs,
and refuses cache reuse. `request` accepts supported `JobCreateRequest` options;
it cannot provide a URL or cache flag. The fixture runs on an isolated local HTTP
listener; the actual configured OCR service processes its captured PNGs.

Each invocation retains its candidate database, Markdown, manifest, PNGs, DOM and
`candidate-evidence.json` beneath pytest's unique retained temporary root (shown
in failures). Supply a new `--basetemp` path for a predictable retained directory;
an existing path is refused. Expected files are never rewritten. To refresh a
baseline, inspect the fresh artifacts against the independently expected content
and approve a new immutable baseline separately; do not copy failures into expected
output merely to obtain a passing result. To generate a new candidate for review
without an existing baseline:

```bash
uv run --python 3.13 python -m tests.test_e2e_generated \
  --fixture path/to/authored.html --out path/to/new-candidate \
  --viewport-height 500 --scenario-author "author/model and date"
```

This development tool requires the dev dependency group, uses the real configured
OCR service and refuses an existing output path. It creates fresh candidate data;
it never approves or rewrites a baseline. Default local checks skip this real-
provider suite explicitly; enabled missing/invalid evidence fails. Production
corpus accuracy, hosted-model availability and48-hour smoke remain separate gates.

The Markdown limit remains at most2%. The comparison validates provenance
highlight ownership against `baseline_job_id`/the actual candidate ID and
neutralizes only that execution nonce inside producer provenance comments. Raw
and compared differences are both reported. Text, body links, fenced code,
geometry, hashes and all other provenance remain unchanged and count toward the
limit. This addresses the observed false loss from a fresh random job ID; it does
not excuse content changes or provenance pointing at another job.

Tile comparisons require SSIM≥0.99 without
resampling: valid Gaussian windows are11×11 or the largest odd window fitting a
short final tile; strips under3px require exact decoded pixels. Mismatched tile
cardinality/geometry or unverified identities fail before quality can pass.

### GLM-OCR Contract Notes (bd-361.1.2)
- No new environment keys are required for contract validation work.
- For **local OpenAI-compatible GLM-OCR** serving, existing `OCR_LOCAL_URL` can point at
  a `/v1` endpoint (for example vLLM/SGLang with `glm-ocr` served model name).
  - The adapter now preserves configured served model names for local OpenAI endpoints
    (it no longer force-prefixes `allenai/` in local mode).
  - If a local server returns model-not-found errors (`400/404`) the client
    automatically retries with known GLM aliases (for example `glm-ocr`,
    `GLM-4.1V-9B-Thinking`, `THUDM/GLM-4.1V-9B-Thinking`) before consuming
    transport retry budget.
  - Local GPU runtime exposes adaptive concurrency hints in backend probe metadata
    (`tuning_profile=local-gpu-adaptive` and effective min/max concurrency).
  - Local CPU runtime uses conservative defaults (`tuning_profile=local-cpu-conservative`)
    so OCR requests do not starve capture/stitch workloads on CPU-only hosts.
  - Local lifecycle manager now performs probe/reuse first, then optional autostart with
    bounded restart policy; lifecycle metadata is emitted under `ocr_local_service`.
  - CPU local retries/latency spikes now emit policy reevaluation signals in submit
    results (`reevaluate_policy`, `reevaluation_reason_code`) so failover orchestration
    can switch to alternate backends automatically.
- For **GLM MaaS** contract validation, treat the request as a separate provider contract:
  `{"model":"glm-ocr","file":"<url-or-data-uri>"}` where `file` is URL or data URI.
- During adapter rollout beads, manifest metadata must explicitly capture
  backend mode (`maas` vs `openai-compatible`) and normalized reason codes.

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
* `overlap_match_ratio` shortcut so dashboards/CLIs can summarize seam health without unpacking the stats block
* `validation_failures` array that records any tile integrity errors caught by `validate_tiles()` and feeds the warning log even when no other warnings fired
* OCR model + FP8 status + concurrency window
* OCR backend provenance (`backend_id`, `backend_mode`, `hardware_path`, `fallback_chain`) so adapters/policy decisions are auditable per run
* Policy trace metadata (`backend_reason_codes`, `backend_reevaluate_after_s`) so failover behavior is explainable and replayable
* Host hardware capability snapshot (`hardware_capabilities`) so policy decisions can be replayed against the detected CPU/GPU inventory
* OCR request telemetry (`ocr_batches`) including latency, HTTP status, request IDs, and payload sizes, plus hosted quota status (`ocr_quota`) so ops can correlate throttling with DOM complexity
* Failover telemetry (`ocr_failover_events`) with backend failure/skip/selection events and stable reason codes for runtime transitions
* Runtime policy suppression metadata in `ocr_autotune` (`reevaluation_reason_code`, suppression counters, switch-window count) so anti-flap behavior is auditable in events/manifests
* Timing metrics (`capture_ms`, `ocr_ms`, `stitch_ms`, `total_ms`) once stages
  execute
* Cache metadata (`cache_hit`, `cache_seed`, `cache_key`) so dashboards/CLI output can distinguish fresh captures from cache replays and confirm runtime backend-specific cache partitioning
* DOM assist overlays (`dom_assists`) plus the summary fields (`dom_assist_summary`) that report counts, assist density (assists ÷ tiles_total), and per-reason ratios so ops/debuggers can monitor hybrid recovery health

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
  },
  "backend_id": "olmocr-remote-openai",
  "backend_mode": "openai-compatible",
  "hardware_path": "remote",
  "backend_reason_codes": ["policy.remote.fallback"],
  "backend_reevaluate_after_s": 120,
  "fallback_chain": ["olmocr-remote-openai"],
  "hardware_capabilities": {
    "cpu_logical_cores": 16,
    "gpu_count": 1,
    "has_gpu": true,
    "preferred_hardware_path": "gpu"
  },
  "sweep_stats": {
    "sweep_count": 6,
    "total_scroll_height": 13200,
    "shrink_events": 1,
    "retry_attempts": 1,
    "overlap_pairs": 8,
    "overlap_match_ratio": 0.94
  },
  "overlap_match_ratio": 0.94,
  "validation_failures": [],
  "dom_assists": [
    {"tile_index": 0, "line": 3, "reason": "low-alpha", "dom_text": "Revenue Q4", "original_text": "Rev3nue?" }
  ],
  "cache_hit": false,
  "cache_seed": "<backend-agnostic-sha256>",
  "cache_key": "<runtime-backend-sha256>"
}
```

## Operational Defaults & Notes

* Always run Playwright with `viewport=1280×2000`, `deviceScaleFactor=2`,
  `colorScheme="light"`, reduced motion, and animation disabling so CfT output
  is deterministic. The shared `playwright.config.mjs` (used by `scripts/run_checks.sh`
  and `npx playwright test`) encodes these defaults and respects
  `PLAYWRIGHT_TRANSPORT` (cdp/bidi) plus mask selectors from env.
* Tiling policy: longest side ≤1288 px with ≈120 px overlap (Plan §§3, 19.3).
* Use HTTP/2 (`httpx.AsyncClient(http2=True)`) when sending many OCR tiles to
  the same host; document rate limits + concurrency in manifests so Ops can
  correlate spikes.
* Prometheus + HTMX SSE heartbeat intervals come directly from this config—keep
  dashboards/tests in sync with any changes.
* `MDWB_SERVER_IMPL` controls whether `scripts/run_server.py` launches uvicorn or Granian. The chosen runtime is echoed into manifests (`environment.server_runtime`) and stored in SQLite so cache hits/logs can be filtered by server type.
* The warning/blocklist JSONL log now includes `sweep_stats`, overlap ratios, and any `validation_failures`, so Ops can spot retries or seam duplication even when DOM warnings don’t fire. Ensure the log rotation/search tooling ingests the new keys.
* Prometheus scraping now covers capture/OCR/stitch latencies, warning/blocklist totals, SSE heartbeats, and job completions. Scrape `/metrics` directly or hit the exporter bound to `PROMETHEUS_PORT` when you need a dedicated port.
* OCR runtime hysteresis defaults are currently fixed in policy code: cooldown `45s`, flap window `180s`, threshold `3` switches/window. Suppression reason codes use `policy.reeval.suppressed.cooldown` and `policy.reeval.suppressed.flapping`.
* Persistent Chromium profiles live under `CACHE_ROOT/profiles/<id>/storage_state.json`. The UI dropdown and CLI `--profile` flag reuse these directories, and manifests/RunRecords echo `profile_id` so audits can trace which persona captured a run.
* OCR manifests now include `ocr_autotune` (initial/final/peak concurrency plus the most recent adjustments). The CLI + UI render this block so operators know when the controller scaled up/down; set `OCR_MIN_CONCURRENCY`/`OCR_MAX_CONCURRENCY` to shape the window and override via env when debugging throttling.
# API access

Set `REQUIRE_API_KEY=true` to require a stored API key for API routes. Health,
static UI shells and signed public shares remain public. `API_KEY_HEADER`
defaults to `X-API-Key`; the CLI's `Authorization: Bearer` header is also accepted.
`RATE_LIMIT_PER_MINUTE` defaults to 60 and must be positive. A key's explicit
positive rate overrides the default. Quotas are atomic SQLite token buckets
shared by API workers; exhausted buckets return 429 with `Retry-After`.

Create keys locally with `uv run python -c "from app.auth import cli_generate_key;
cli_generate_key('research', owner='research-team')"`. Add `is_admin=True` only
for keys that need `/admin/` operations. Keys sharing an owner can access that
owner's jobs; unowned keys have separate namespaces. Existing keys have no
administrator privilege after migration. Existing anonymous jobs remain outside
authenticated namespaces. Profiles, cache identity, listing and artifact access
are scoped to the authenticated owner. Profile IDs use 1–128 ASCII letters,
digits, periods, underscores or hyphens and cannot start with a period.

Both browser pages offer a password field when authentication is required.
Sign-in uses the configured header and sets a session cookie marked HttpOnly
and SameSite=Strict (Secure over HTTPS); the key is not written to browser
storage or URLs. The cookie is verified on every request, so revocation applies
to API calls and new SSE connections immediately. Cookie-authenticated writes
require the same Origin. Sign out clears the session and rendered content.

Signed public views omit profile IDs, tags, URL credentials/query strings and
private manifest fields. Authenticated deployments must configure a private
`WEBHOOK_SECRET` of at least 32 characters before minting shares.

Capture connections use an ephemeral fixed HTTP proxy. It resolves
the target once, checks every IPv4/IPv6 answer, then connects to a validated
numeric address and verifies the actual peer. Browser redirects, subresources
and WebSockets use the same proxy, with loopback bypass and QUIC disabled.
Webhook delivery uses HTTPcore's public network-backend interface with the same
all-answer validation and numeric connection pinning, preserving the original
HTTP Host and TLS SNI. It ignores ambient proxy variables and refuses redirects; configured
OCR and embedding endpoints remain separate operator-owned service clients.

| Variable | Default | Effect |
| --- | --- | --- |
| `NETWORK_PRIVATE_CIDRS` | empty | Strict, comma-separated CIDRs available only to explicitly trusted subjects. |
| `NETWORK_TRUSTED_OWNERS` | empty | Exact server-owned identities such as `owner:research-team` or `key:12` eligible for those CIDRs. Administrator role alone grants no network access. |
| `NETWORK_TRUST_LOCAL_OPERATOR` | false | Enables the same CIDRs for anonymous local operation only when `REQUIRE_API_KEY=false`. |

For a controlled local fixture, set `NETWORK_PRIVATE_CIDRS=127.0.0.1/32` and
explicitly trust its stored owner, or enable local operator access in an
unauthenticated local deployment. Tenant request/profile fields cannot grant
network access. Link-local and cloud metadata destinations, mapped IPv6 and
transition addresses stay denied even with grants. Mixed allowed/disallowed DNS
answers are refused together before any target connection. URL credentials are
refused; use owner-scoped browser profiles for intentional authenticated sites.

The capture manifest records policy mode, grant count, policy digest, connection
count and destination-free denial reasons. Policy identity participates in the
cache key, so withdrawing a grant cannot reuse a previously granted capture.
Webhook events use the stored job owner's policy; logs omit destination URLs and
resolved addresses. Proxy headers are limited to64KiB, concurrent connections
to64, establishment to15s and relay lifetime to120s. Capture connection-boundary
tests do not establish container isolation or protection from a compromised
browser process; broader deployment qualification remains open.

`DELETE /jobs/{id}` removes a stopped run's metadata and retains files by
default. Filesystem purging requires `?purge_artifacts=true`; it refuses paths
outside the configured cache and artifacts shared with another run.

Request `ocr_policy` selects an enabled entry from `docs/models.yaml` (names are
case insensitive). It controls the provider model, FP8 preference, tile limit
and maximum concurrency. Disabled/unknown policies and unsupported request
fields are rejected. `long_side_px` may lower a policy's size limit. Manifests
record requested policy, selected model and any served model reported by the
provider; an omitted provider model remains unknown.

The optional embedding backend reads `OPENAI_BASE_URL`, `OPENAI_API_KEY` and
`OPENAI_EMBEDDING_MODEL` (default `text-embedding-3-small`). Semantic processing
uses `SEMANTIC_POST_ENDPOINT`, `SEMANTIC_POST_MODEL`, and `SEMANTIC_POST_API_KEY`.
These use the same python-decouple loader as the application.

Queue configuration reads `REDIS_URL` and `QUEUE_MAX_JOBS` (10),
`QUEUE_JOB_TIMEOUT` (3600 seconds), `QUEUE_KEEP_RESULT` (3600 seconds),
`QUEUE_MAX_RETRIES` (3), and `QUEUE_RETRY_DELAY` (60 seconds). Concurrency and
timeout must be positive; retention/retry counts/delay must be nonnegative.
Durable API-to-worker integration remains tracked separately from these settings.
