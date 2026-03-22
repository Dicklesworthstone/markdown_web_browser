# Changelog

All notable changes to [Markdown Web Browser](https://github.com/Dicklesworthstone/markdown_web_browser) are documented here.

This project has no formal release tags or GitHub Releases. The changelog is organized into logical phases derived from the full commit history (138 commits, 2025-11-07 through 2026-03-11). Each entry links to the relevant commit on GitHub.

Repository: `https://github.com/Dicklesworthstone/markdown_web_browser`

---

## 2026-03-11 -- CI and Dependency Maintenance

Routine GH Actions version bumps and Dependabot dependency updates.

### Changed
- Bump GH Actions versions across all workflows ([`0cec4ff`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/0cec4ff7226de91cf27f6c71cb38fd7ae247d600))

### Dependencies
- Bump the actions group with 9 updates ([`91eadaf`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/91eadaffb9b2a052c9e32a92b37bbb6633db5578))
- Bump the actions group with 4 updates ([`3b925f2`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/3b925f2db319dda611a55d7cff27974bc729c615))

---

## 2026-02-25 -- Installer Bug Fixes and Playwright Chromium Migration

Three sequential installer bugs reported in [#12](https://github.com/Dicklesworthstone/markdown_web_browser/issues/12) are resolved, and Chrome for Testing references are replaced with Playwright Chromium terminology for clarity after Playwright 1.57+ dropped `--channel=cft`.

### Fixed
- **uv PATH**: uv 0.10+ installs to `~/.local/bin`, not `~/.cargo/bin`; installer now exports the correct path ([`e6b098e`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/e6b098e35cf0a3ecf0b81611e5d5327eb015cc55))
- **Playwright channel flag**: Removed `--channel=cft` from all four invocations (incompatible with Playwright 1.57+) ([`e6b098e`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/e6b098e35cf0a3ecf0b81611e5d5327eb015cc55))
- **Launcher invocation**: Added `if __name__ == "__main__": cli()` guard to `scripts/mdwb_cli.py` so the launcher actually runs ([`e6b098e`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/e6b098e35cf0a3ecf0b81611e5d5327eb015cc55))
- Replace "Chrome for Testing" / "CfT" references in install.sh with "Playwright Chromium" for accuracy ([`3e8d71e`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/3e8d71e786d87bdd37aeeb6d46d04c2e3a37f48b))

---

## 2026-02-22 -- Installer UX: --easy-mode

### Added
- `--easy-mode` as alias for `--yes` in `install.sh`, enabling the ACFS update orchestrator to call all managed tools uniformly; fixes [#9](https://github.com/Dicklesworthstone/markdown_web_browser/issues/9) ([`9603167`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/960316733b5ec73a2acd75c18c4d04f5a384d0fd))

### Fixed
- Hardcoded `anthropics` GitHub org URL in install.sh ([`6914f84`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/6914f84a28e7e0f1d14035790c69b26cd58104ea)); fixes [#10](https://github.com/Dicklesworthstone/markdown_web_browser/issues/10)

### Changed
- License updated to MIT with OpenAI/Anthropic Rider ([`7b25d36`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/7b25d36620e7b72c260cad70c1df7947a29167fe))
- GitHub social preview image added ([`d7773ec`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/d7773ec9719c21aeda1b97872886b51f2311fb5c))

---

## 2026-02-11 -- GLM (ZhipuAI) OCR Provider and Autopilot Policy

Multi-backend OCR support lands. GLM can run as a MaaS provider or locally on GPU/CPU, with hardware detection and an anti-flap autopilot policy governing backend selection.

### Added
- **GLM OCR provider** with both OpenAI-compatible chat API and native MaaS layout parsing API ([`94a1a70`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/94a1a70e165e31a1f6afd0f160806fd9296ac9d8))
  - `normalize_glm_file_reference()`, `build_glm_maas_payload()`, `build_glm_openai_chat_payload()`
  - `extract_glm_openai_markdown()`, `extract_glm_maas_markdown()` with multi-level fallback
- GLM provider documentation and expanded OCR test coverage ([`9df8f3a`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/9df8f3af9c56d32d04482c14d72a2e6669e6162b))
- OCR backend contract (`OcrBackend` protocol), hardware detection (`app/hardware.py`), and autopilot policy (`app/ocr_policy.py`) ([`50b66fa`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/50b66fadaa5571fb24f7b95b5e1f2d7e4720eaba))
- GLM MaaS/local GPU/CPU OCR adapter automation ([`eed5280`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/eed5280f2c984ec594dcb495096a50d42162b3d6))
- Local GLM OCR service lifecycle manager ([`5e312e4`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/5e312e44ce0e9572f58d6e302a904484755909d9))
- GLM OCR runtime provenance, anti-flap policy, and autopilot test suites ([`41d7b43`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/41d7b43393cad3aa138f2d56bcc12467157e75e5))

### Fixed
- OpenAI OCR content-list extraction regression ([`6f79f68`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/6f79f68a08b05c81fd4cffa2b94de019e88e09d5))

---

## 2026-02-04 -- README Install URL Fix

### Fixed
- Correct install script URLs in README ([`aec0491`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/aec04918f35dfd9fe1af457e64061bd60dc4a10a)); fixes [#6](https://github.com/Dicklesworthstone/markdown_web_browser/issues/6)

---

## 2026-01-28 -- CLI --stats Flag and Output Format Env Vars

### Added
- `--stats` flag on `resume_status`, `demo_snapshot`, and `demo_links` commands showing TOON-vs-JSON savings ([`5d61852`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/5d61852f9e585086dca5a6645a90bc117a7905f0))
- Output format resolution chain: `--format` > `--json` > `MWB_OUTPUT_FORMAT` > `TOON_DEFAULT_FORMAT` > default ([`5d61852`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/5d61852f9e585086dca5a6645a90bc117a7905f0))

---

## 2026-01-27 -- ACFS Notification Workflows

### Added
- CI workflow to notify ACFS lesson registry on installer changes ([`77dcd30`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/77dcd301bd8a4d3cec9333614e4b0c7eefb89b0a))
- CI workflow for ACFS lesson registry sync ([`931edf6`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/931edf6cc1f83db4b5eb45a4e416fcd71249b7f8))

---

## 2026-01-25 -- TOON Format Support

### Added
- TOON format output for `mdwb` CLI commands: any `--json`-capable command now also accepts `--format toon` ([`d758646`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/d758646703c7ba76fb411b9ddfb6978782537183))

### Fixed
- Format `mdwb_cli.py` to pass ruff format check ([`40df5f5`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/40df5f5f271a3f0848d23a784120fc84171ddafa))
- CI test failures for capture sweeps and stitch ([`5d5a8af`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/5d5a8afa3cdbcad347e0dcf9685f98fd8224511b))

---

## 2026-01-24 -- Lint, Type-Checking, and Test Fixes

### Fixed
- Resolve `ty` type checker and `ruff` lint errors ([`66b437c`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/66b437cb94310aa739250f3353f3b26e222f37f0))
- Add missing required env vars to nightly smoke workflow ([`06e9d5d`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/06e9d5dc8b6903026c14e895d27da3ca277bf6e0))

### Changed
- Update browser capture and pyvips tests ([`f9b088d`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/f9b088d20a1f2eb95377c84501607388e3f267cf))

---

## 2026-01-17 -- Comprehensive CI Pipelines

First proper CI/CD setup: lint, type-check, test, Playwright, Docker build, plus a release workflow for GHCR images.

### Added
- **CI workflow** (`ci.yml`): ruff check/format, `ty` type checker, pytest, Playwright tests, Docker build; concurrency control and uv caching ([`c8fa588`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/c8fa5881d289f573d48bbed687a31584cb79db03))
- **Release workflow** (`release.yml`): multi-platform Docker images (amd64/arm64) to GHCR on version tags ([`c8fa588`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/c8fa5881d289f573d48bbed687a31584cb79db03))
- **Dependabot** config for Python, GH Actions, and Docker grouped updates ([`c8fa588`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/c8fa5881d289f573d48bbed687a31584cb79db03))
- Improved nightly smoke workflow with uv caching, 60-min timeout, artifact retention, failure log upload ([`c8fa588`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/c8fa5881d289f573d48bbed687a31584cb79db03))

### Dependencies
- Bump Python Docker base from 3.13-slim to 3.14-slim ([`894f80e`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/894f80eae1639fdecf03fd8c5c098933328191ab))
- Update Python dependencies to latest stable versions ([`842e6e2`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/842e6e29dd016d7beeb1af2b5640e33ccc84727d))

---

## 2026-01-09 -- OCR Limiter Race Condition Fix

### Fixed
- **Race condition** in `_AdaptiveLimiter.slot()`: added lock protection around `_pending_reduction` in finally block ([`777d082`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/777d082040c2ee8927fbcd135ea1d02b0ac50e57))
- Switched to official olmOCR prompt from allenai/olmocr (simpler, more reliable) ([`777d082`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/777d082040c2ee8927fbcd135ea1d02b0ac50e57))
- Increased `max_tokens` from 4096 to 8000 per olmOCR toolkit config ([`777d082`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/777d082040c2ee8927fbcd135ea1d02b0ac50e57))
- Added `temperature=0.1` for deterministic OCR output ([`777d082`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/777d082040c2ee8927fbcd135ea1d02b0ac50e57))
- Cache key bumped to v8 to invalidate stale cached results ([`777d082`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/777d082040c2ee8927fbcd135ea1d02b0ac50e57))

### Changed
- Enhanced Prism syntax highlighting for markdown in Browser UI (bold, italic, lists) ([`777d082`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/777d082040c2ee8927fbcd135ea1d02b0ac50e57))
- Restored MIT LICENSE file ([`51c3a59`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/51c3a59ba9a56a5e279554dc18b9df3fc2e42f4d))

---

## 2025-12-15 -- Beads Update

### Changed
- Update beads tracking to v0.30.0 ([`9e1bc77`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/9e1bc77ec6bd092dc46540e24f419f1d956e828d))

---

## 2025-11-09 -- Major Release: Browser UI, Intelligent Deduplication, Security Hardening

The largest single day of development. Introduces the Browser UI, a multi-tier tile deduplication engine, comprehensive bot detection evasion, production auth/rate-limiting, Kubernetes manifests, a web crawler, and extensive security fixes.

### Added -- Browser UI
- Complete Chrome-inspired browser interface at `/browser` with address bar, back/forward navigation, dual rendered/raw markdown views, smart caching (1h TTL), progress bars, and keyboard shortcuts (Alt+arrows, Ctrl+R/U/L) ([`a63e26c`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/a63e26cb9c2a8b98dac427a9d86428a16f6ceb76))
- Vanilla JS (no framework), Stripe-quality CSS design system with dark mode, responsive layout, marked.js + Prism.js for rendering ([`a63e26c`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/a63e26cb9c2a8b98dac427a9d86428a16f6ceb76))

### Added -- Intelligent Tile Overlap Deduplication
- Four-tier deduplication engine in `app/dedup.py`: pixel verification, exact boundary match, sequence similarity (difflib, 90% threshold), fuzzy line-by-line fallback (85% threshold) ([`a63e26c`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/a63e26cb9c2a8b98dac427a9d86428a16f6ceb76))
- Safety limits (never removes >3x estimated overlap), configurable via `DeduplicationSettings`, telemetry in manifests ([`a63e26c`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/a63e26cb9c2a8b98dac427a9d86428a16f6ceb76))
- 22 tests covering all tiers and edge cases ([`a63e26c`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/a63e26cb9c2a8b98dac427a9d86428a16f6ceb76))

### Added -- OCR Enhancements
- OCR client migrated to OpenAI-compatible API format; auto-detects `/chat/completions` endpoints and forces batch_size=1; supports GPT-4 Vision, Azure OpenAI, DeepInfra ([`7578541`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/75785417f89bb001338cad4a196004fc135e7a05))
- Comprehensive OCR instruction prompt for dramatically improved extraction completeness ([`417dc6c`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/417dc6c1f3eeee061a8133522087296a02c89727), [`d07f3b5`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/d07f3b52aee7bbc943eeb767b68d9e2bfe96b4b4))

### Added -- Bot Detection Evasion
- Multi-layer bot detection evasion: 60+ lines of stealth JavaScript for navigator.webdriver, plugins, permissions API, hardware fingerprinting ([`66f1128`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/66f11288fd97b97d0d03f4d06acb21eae041013f))
- Changed page load strategy from `networkidle` to `domcontentloaded` to avoid Cloudflare hangs ([`66f1128`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/66f11288fd97b97d0d03f4d06acb21eae041013f))
- Visual proof of Cloudflare bypass with finviz.com screenshots in docs ([`77b2e54`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/77b2e54941e2240cbb7043aef6dc4babbc7e9ce5))

### Added -- Production Infrastructure
- Production-ready auth (API key middleware), rate limiting, background job queue ([`687054e`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/687054e6867d63f84a56bfced7b13982927c9901))
- Kubernetes manifests and hardened installer with safety checks ([`dde5c06`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/dde5c06caf46ea24e8ad28331e485060513bce44))
- Complete cache implementation with API docs and local OCR infrastructure ([`2932a98`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/2932a989332b42443c77edbe5d3f78a534b12336))
- Web crawler with comprehensive feature documentation ([`49e6466`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/49e6466ab8dba940dcfa4b290a0faa8fc7307fcc))
- Production deployment infrastructure and critical PNG encoding bug fix ([`eeed9cb`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/eeed9cbcf36cce824ed3357c48937c0b266796dc))
- Three-tiered integration testing infrastructure (unit, integration, e2e) ([`5637023`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/56370230e3dfc3f8dd8eb830026642350b0684cc))

### Added -- Installer
- All-in-one `install.sh` (460 lines): multi-OS support (Debian/Ubuntu, RHEL, Arch, macOS), auto-installs uv/libvips/Playwright, creates launcher script (`./mdwb`), runs validation tests ([`9340fb9`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/9340fb951767e6cfcb2ec3c886016923dce72a06))
- **Critical fix**: installer now installs Chrome for Testing (`--channel=cft`) instead of regular Chromium, which was silently breaking deterministic rendering ([`687054e`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/687054e6867d63f84a56bfced7b13982927c9901))

### Added -- Other
- Semantic post-processing module (`app/semantic_post.py`) for optional LLM-based markdown refinement ([`9340fb9`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/9340fb951767e6cfcb2ec3c886016923dce72a06))
- Comprehensive input validation and XSS protections ([`2889e88`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/2889e8806abe10ed93503f6acc6c0f75cdb84f51))
- Ad-hoc URL testing via `--url` flag in e2e test runner ([`a63e26c`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/a63e26cb9c2a8b98dac427a9d86428a16f6ceb76))

### Fixed -- Security
- Prevent shell injection in CLI hooks ([`12823cb`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/12823cb736bd6d65fe30b6646cfa99f731c83ddb))
- Prevent XSS in seam marker rendering ([`3ef4854`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/3ef485402f9b35f0435fb7a97c22cbebf0dcc7e7))
- SQL injection prevention in `app/store.py` via column name regex validation and type whitelisting ([`a63e26c`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/a63e26cb9c2a8b98dac427a9d86428a16f6ceb76))

### Fixed -- Reliability
- Thread safety for `Store` singleton and datetime timezone comparison bug ([`3c3a619`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/3c3a619c655baa7ff81817cd12fd1c93d50d559f))
- Thread safety for rate limiter; prevent division by zero ([`c745d27`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/c745d2780fcf3e00b359ff9b15f80bb7f7d5c046))
- Sequence matching algorithm corrected with comprehensive test coverage ([`7251503`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/725150340db54450a7486b6580d921280963e784))
- URL validation error handling and iterator safety ([`23cf577`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/23cf577a9834217278a4ae2201833599d0fbe190))
- OCR autotune serialization: dataclass instances now properly converted to dicts for JSON ([`a63e26c`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/a63e26cb9c2a8b98dac427a9d86428a16f6ceb76))
- Job timeout watchdog: fixed aware/naive datetime comparison ([`a63e26c`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/a63e26cb9c2a8b98dac427a9d86428a16f6ceb76))
- Cache TTL comparison: handled SQLite timezone stripping ([`a63e26c`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/a63e26cb9c2a8b98dac427a9d86428a16f6ceb76))
- Hardened production auth and added job watchdog for stuck jobs ([`883a96f`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/883a96f2ab9fc5443fe7014fef495aace9f71705))

### Performance
- Database connection pooling and throttled timestamp updates for massive performance gains ([`5ede8f9`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/5ede8f93504d24faaa76da354163a75bb0c8ec48))
- Memory leak prevention: auto-cleanup of completed job data older than 2 hours ([`a63e26c`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/a63e26cb9c2a8b98dac427a9d86428a16f6ceb76))
- Improved syntax highlighting reliability and optimized screenshot file size ([`a9d7c96`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/a9d7c96731c757fbd23f51e1c9b211b4a20ed396))
- Fixed SQL correctness, hardened installer, removed dead code ([`ec565a1`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/ec565a13141d8b0ac495984ec79cc174b9d84b87))

---

## 2025-11-08 -- DOM-Assisted OCR Recovery, HTMX SSE, Links UI, Granian, Autotune

A major capability expansion adding hybrid OCR recovery, modernized streaming, a professional links interface, production server support, and intelligent OCR concurrency management.

### Added -- DOM-Assisted Hybrid Text Recovery
- Intelligent OCR confidence detection with multiple heuristics (replacement chars, mixed numeric/punctuation bursts, hyphen-break patterns, low alpha-numeric ratio) ([`2a406ba`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/2a406bad0620f9294b729e3012399066d9d675f0))
- DOM text overlay patches low-confidence OCR regions with DOM-derived text; `DOMAssist` dataclass tracks tile index, line number, reason, and replacements ([`2a406ba`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/2a406bad0620f9294b729e3012399066d9d675f0))
- Manifest records `dom_assists` array; CLI `diag`/`watch` commands display summaries ([`2a406ba`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/2a406bad0620f9294b729e3012399066d9d675f0))

### Added -- HTMX SSE Extension
- Replaced custom EventSource JavaScript with declarative HTMX SSE extension (`hx-ext="sse"`, `sse-connect`) ([`2a406ba`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/2a406bad0620f9294b729e3012399066d9d675f0))
- JavaScript bridge for `htmx:sseMessage` events; DOM assist events render inline in Events tab ([`2a406ba`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/2a406bad0620f9294b729e3012399066d9d675f0))

### Added -- Enhanced Links Tab
- `LinkRecord` extended with `rel`, `target`, `kind`, `domain` metadata ([`2a406ba`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/2a406bad0620f9294b729e3012399066d9d675f0))
- Domain-grouped display with `(relative)` and `(fragment)` buckets ([`2a406ba`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/2a406bad0620f9294b729e3012399066d9d675f0))
- Coverage badges (DOM vs OCR), attribute badges, inline actions: "Open in new job", "Copy Markdown", "Mark crawled" with `localStorage` persistence ([`2a406ba`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/2a406bad0620f9294b729e3012399066d9d675f0))

### Added -- Granian ASGI Server
- `scripts/run_server.py` with `SERVER_IMPL` toggle (uvicorn default, Granian optional) ([`2a406ba`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/2a406bad0620f9294b729e3012399066d9d675f0))
- `RunRecord` persists `server_runtime` in SQLite; manifests record `environment.server_runtime` ([`2a406ba`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/2a406bad0620f9294b729e3012399066d9d675f0))

### Added -- OCR Concurrency Autotune
- Adaptive concurrency controller: starts at `OCR_MIN_CONCURRENCY`, scales toward `OCR_MAX_CONCURRENCY` on healthy latency (<3.5s), throttles on 408/429/5xx or high latency (>7s) ([`2a406ba`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/2a406bad0620f9294b729e3012399066d9d675f0))
- `AutotuneState` tracks initial/peak/final limits and adjustment events; manifest `ocr_autotune` section records scaling decisions ([`2a406ba`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/2a406bad0620f9294b729e3012399066d9d675f0))

### Added -- Browser Profiles and Cache Reuse
- Profile persistence under `CACHE_ROOT/profiles/<id>/storage_state.json`; profile-aware context creation with slug sanitization ([`67bad76`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/67bad76e138a3c58407553af6de0a4fae08124aa))
- Content-addressed cache reuse: cache key from `url + CfT + viewport + DSF + OCR model + profile`; identical requests return immediately with `cache_hit=true` ([`67bad76`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/67bad76e138a3c58407553af6de0a4fae08124aa))
- `POST /replay` endpoint for re-running captures from stored manifests ([`67bad76`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/67bad76e138a3c58407553af6de0a4fae08124aa))
- `playwright.config.mjs` with environment-based configuration (transport, viewport, masks, channel) ([`67bad76`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/67bad76e138a3c58407553af6de0a4fae08124aa))

### Added -- Stitching Improvements
- Seam markers (`<!-- seam-marker ... -->`) at tile boundaries with enriched provenance (`viewport_y`, `overlap_px`) ([`2a406ba`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/2a406bad0620f9294b729e3012399066d9d675f0))
- Highlight links (`/jobs/{id}/artifact/highlight?tile=X&y0=Y&y1=Z`) for visual review of tile boundaries ([`2a406ba`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/2a406bad0620f9294b729e3012399066d9d675f0))
- Smart table header deduplication using overlap-aware similarity scoring ([`2a406ba`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/2a406bad0620f9294b729e3012399066d9d675f0))

### Added -- Observability
- SLO tracking, DOM assist summaries, and seam marker persistence ([`af1b968`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/af1b968a4401d661ffb1b7ba9a3746de3fa48f6e))
- Prometheus metrics for DOM assist density ([`d3ca56f`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/d3ca56fab9b95460b213f2057d58f22fc1ade5eb))
- SLO computation pipeline and seam marker tracking with usage events ([`f64876b`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/f64876b125c47a59cc01670ba9a2c5d3a3b513ac))
- Seam watermark instrumentation, FlowLogger v2 integration ([`25eefd7`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/25eefd766ca5b0809d13aecf4103fa8e0eed41a8))
- Seam marker event tracking and CLI stream job terminal state handling ([`3106500`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/31065006278df275abcb43fe7f568d5aface2827))
- SLO validation in metrics checks; resume orphan flag detection ([`4ce9e59`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/4ce9e59b5ad5d3462ec0c854cca047cd02980453))
- Cache-keyed storage paths ([`5a32136`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/5a321363c56870b9ae763d368556242f28e298d3))

### Fixed
- CLI watch hooks in raw mode; added SSE fallback testing ([`38e3ed8`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/38e3ed82dc816e2a80d285585e94193da816ea80))

---

## 2025-11-07 -- Initial Release: Core Platform

Project created and rapidly iterated on in a single day. Delivers the full capture pipeline, FastAPI server, CLI, web dashboard, OCR integration, event streaming, webhooks, DOM link extraction, testing infrastructure, agent automation scripts, and resume functionality.

### Added -- Core Capture Pipeline
- Playwright-based deterministic viewport sweep (Chromium CfT, 1280x2000, DPR 2, reduced motion) ([`7e571b9`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/7e571b9dc59acdfd4662fd9c53afff324f1da9a1))
- `pyvips` tiler: slices sweeps into <=1288px tiles with ~120px overlap; each tile carries offsets, DPR, SHA256 hashes ([`7e571b9`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/7e571b9dc59acdfd4662fd9c53afff324f1da9a1))
- OCR client submitting tiles to hosted or local olmOCR with retries ([`7e571b9`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/7e571b9dc59acdfd4662fd9c53afff324f1da9a1))
- Stitcher: merges markdown, aligns headings with DOM outline, trims overlaps via SSIM + fuzzy text comparisons, injects provenance comments ([`7e571b9`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/7e571b9dc59acdfd4662fd9c53afff324f1da9a1))
- Content-addressed `Store` with SQLite + sqlite-vec metadata for embeddings search ([`7e571b9`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/7e571b9dc59acdfd4662fd9c53afff324f1da9a1))

### Added -- Server and API
- FastAPI `/jobs` endpoint with background `JobManager` ([`7e571b9`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/7e571b9dc59acdfd4662fd9c53afff324f1da9a1))
- SSE/NDJSON event streaming (`/jobs/{id}/stream`, `/jobs/{id}/events`) ([`1019c41`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/1019c413ba5fdd381d5341042663fc278211d66d))
- HTMX/Alpine.js web dashboard with real-time job monitoring ([`0998cdd`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/0998cdd2868e48682f97fa17de6c02916312bd9f))
- Webhook infrastructure for job lifecycle callbacks ([`badc66e`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/badc66e7a0afa5c6cf2aab7ce05e1df38541650e))
- Blocklist system for domains/URL patterns ([`bee427a`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/bee427a98ea9145b805d481cfcdd14bc87dd8c6b))

### Added -- CLI (`scripts/mdwb_cli.py`)
- `fetch`, `show`, `stream`, `watch`, `events`, `diag`, `warnings`, `dom links`, `resume status`, `demo` subcommands ([`346a1aa`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/346a1aa9aaee25803fd95c862ce5255fccf66459), [`9972f7a`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/9972f7ae0ef3bfab412698d2f68246666c3d9c8b))
- Resume functionality with `done_flags/` tracking for incremental processing ([`9ad7612`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/9ad76125d47e17603d3198700799ad66e567b7f6))
- Progress tracking with ETA calculations ([`60bf48f`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/60bf48fb3856ae0220d8ec478474f2c25f6d76f5))

### Added -- DOM and Links
- DOM snapshot capture and automated links extraction pipeline ([`26675cb`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/26675cb14158317f47caaa2fbe0d9d968a6c4057))
- DOM link extraction with CLI diagnostics and smoke runner ([`0b391fe`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/0b391fe6a65b9a6e2fd96dc84a040f0475c9eea1))

### Added -- Observability
- Prometheus metrics, warning telemetry (`ops/warnings.jsonl`), capture_warnings module ([`77cedfd`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/77cedfd2df4ae621ad14f627105ee632fea92d12), [`5cf0d88`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/5cf0d8820334f40b46f2381214bea7176df155ad))
- OCR pipeline integration and webhook management ([`899be50`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/899be50de3fd3fc330cfdf8cecd53cee58e4a917))

### Added -- Agent Automation
- `scripts/agents/summarize_article.py` and `scripts/agents/generate_todos.py` for downstream LLM workflows ([`fc43319`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/fc43319947141939359d5c76cf5a0e1c710e299c))
- Agent shared utilities with retry logic, error handling, validation ([`fc43319`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/fc43319947141939359d5c76cf5a0e1c710e299c))

### Added -- Testing
- Comprehensive testing infrastructure with pytest summary reporting, JUnit XML, JSON summaries ([`fc43319`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/fc43319947141939359d5c76cf5a0e1c710e299c))
- Test suites: `test_e2e_cli.py`, `test_mdwb_cli_events.py`, `test_check_metrics.py`, `test_store_manifest.py`, `test_agent_scripts.py` ([`fc43319`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/fc43319947141939359d5c76cf5a0e1c710e299c))
- Smoke test automation with SLO computation pipeline ([`77cedfd`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/77cedfd2df4ae621ad14f627105ee632fea92d12))

### Added -- Configuration
- `python-decouple` based settings (`app/settings.py`) with `.env` support ([`7e571b9`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/7e571b9dc59acdfd4662fd9c53afff324f1da9a1))
- Structured logging via structlog, optional OpenTelemetry integration ([`7e571b9`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/7e571b9dc59acdfd4662fd9c53afff324f1da9a1))

### Fixed
- Critical bugs in gallery documentation and resource management ([`b501e66`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/b501e663c361d0c96b00ef2720c7f4dcef0feb4a), [`6a327d1`](https://github.com/Dicklesworthstone/markdown_web_browser/commit/6a327d11fb7657017d7daa56cb7f0e73396115fa))

---

## How to Read This Changelog

- **Commit links**: Every `[hash]` link points to `https://github.com/Dicklesworthstone/markdown_web_browser/commit/<full-sha>`. Agent tools can fetch diff details via `gh api repos/Dicklesworthstone/markdown_web_browser/commits/<sha>`.
- **Issue links**: `#N` references link to `https://github.com/Dicklesworthstone/markdown_web_browser/issues/N`.
- **No tags/releases exist** in this repository as of 2026-03-21. All history is on the `main` branch.
- **Module paths**: `app/` is the FastAPI application, `scripts/` contains the CLI and ops tooling, `web/` has the frontend, `tests/` has the test suites.
