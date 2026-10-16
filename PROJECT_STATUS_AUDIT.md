# Project reality check — 2026-10-13

This assessment supersedes the historical November 2024 audit preserved below.
It uses source revision `e6c884429dc841cfa897c63186f0009d46792802`, the live
Beads JSONL, full baseline checks, actual browser probes, and one real hosted-OCR
capture. The requested `reality-check-for-project` workflow included vision
extraction, source investigation, gap analysis, bridge beads, ambition review,
five refinement passes, and dependency validation. Planning is the deliverable;
the repair and integration beads remain unfinished.

## Verdict

The original idea is implemented far enough to deliver useful output: a real URL
can pass through Chrome, pyvips, hosted OCR, stitching, persistence, and cache
reuse. This is substantial working machinery. The project does **not** yet meet
its documented reliability, fidelity, security, determinism, performance, or
production qualification promises. The Franken revamp is an additional vision
with no integrated Rust implementation in this checkout.

The earlier completion percentages and production-ready conclusions were based
largely on file existence and line counts. They are not current qualification
evidence. No defensible overall completion percentage follows from this audit.

## What was inspected

- Full repository `AGENTS.md` (1,104 lines), full `README.md` (560 lines), and
  suite `/data/projects/AGENTS.md` (539 lines).
- Original project plan, Franken plan and handoff, deduplication strategy,
  browser implementation document, original vision prompt, architecture,
  API/config/model/blocklist/DOM-assist documentation, agent playbook,
  integration guide, OCR guides, ops/release/deployment material, and gallery.
- Actual routing and request schemas; capture, tiling, OCR policy/client/local
  lifecycle, stitching, jobs, Store, embedding implementations, crawler,
  authentication/rate limiting, both web clients, installer/container/worker/CI
  wiring, representative tests and production evidence pointers.
- Live tracker before changes: **121 closed issues, one tombstone, zero open or
  in-progress issues**. `br` recovered its schema-0 database from JSONL; historical
  issues and recovery data were retained. No historical issue was closed or had
  its acceptance criteria rewritten by this audit.

The repository instructions refer to suite sections named “Named Reward-Hacking
Patterns” and “Work-Graph Discipline”; neither heading exists in the current
suite file. The substantive prohibitions stated in this repository's Rule 0.5
were still applied. The older Franken plan also contains instructions conflicting
with current governance, including deleting placeholder files and committing
`beads.db`. Current AGENTS rules take precedence.

## Fresh verification

Checks used an isolated uv environment at
`/tmp/mdwb-reality-20261013/env`, Python **3.13.0**, with locked dependencies.
Playwright was **1.57.0**, pyvips **3.1.1**, SQLModel **0.0.31**, and pytest
**9.1.1**. The default unpinned uv interpreter selected 3.14.2 initially; all
reported verification below uses the explicitly selected 3.13 environment.

| Check | Observed result | Meaning |
|---|---|---|
| `uv sync --locked --python 3.13` in the isolated environment | Passed | Dependency setup works on this host |
| `uv run --no-sync --python 3.13 ruff check .` | Passed | Current Ruff lint rules pass |
| `uv run --no-sync --python 3.13 ruff format --check .` | Failed: 25 files would be reformatted | CI formatting gate is red |
| `uvx --python 3.13 ty check --python /tmp/mdwb-reality-20261013/env/bin/python` | Failed: 157 diagnostics | Full correctly configured type gate is red |
| `uv run --no-sync --python 3.13 pytest tests/ -x --tb=short` | 170 passed, 1 failed, 2 skipped | Stops at a real capture test's nonexistent `artifact_root` field |
| `uv run --no-sync --python 3.13 pytest tests/ --tb=short` | **537 passed, 1 failed, 2 skipped**, 191.72 s | Broad baseline has the same failure; a focused pass does not waive it |
| Required Python Playwright command | `error: unknown command 'test'` | Python CLI is not the Node test runner |
| `npx --no-install playwright --version` | Missing package; command failed | No declared/installed Node smoke runner available here |
| `show_latest_smoke.py check --root benchmarks/production --json` | `status: missing`, `weekly_summary` absent | Current production readiness is not established |
| `br dep cycles --json` after bridge wiring | Empty | Work graph is acyclic |
| `ubs --staged` on the audit/planning/JSONL changes | Exit 3: no supported source language, nothing scanned | Not a code-safety pass; source code was not changed |

Final graph validation also checks **actionability**, not only cycles. Initial
`--parent` containment edges made children wait for their own open epic in this
installed tracker. Those new containment edges were removed via `br`; dotted IDs
retain hierarchy and actual prerequisite edges remain. `br ready` now exposes
eight foundations, including both P0 repairs. The epic still waits for both
qualification gates. `br doctor` reports degraded recovery-artifact hygiene,
while DB integrity and DB/JSONL record counts agree; recovery artifacts were
preserved under the no-deletion rule, not represented as a clean doctor pass.

The full-suite failure is
`tests/test_live_capture.py::test_live_capture_of_example_com`, line 109:
`CaptureManifest` has no `artifact_root`. The test also changes environment after
cached settings have been loaded, catches capture exceptions as skips, and calls
the capture-only helper while expecting persisted job artifacts. Fix the actual
test/product boundary; removing the assertion or skipping the test is not proof.

Generated E2E currently skips because its default `cases.json` is absent. Its
implementation compares two existing Markdown files; it does not drive the
browser/OCR/Store pipeline. `test_e2e_small.py`, `test_embeddings.py`, and
`test_scroll_policy.py` contain `assert True` placeholders. Other meaningful
embedding/capture tests do exist, but these placeholders establish no additional
coverage. Browser helper tests returning `False` instead of asserting can also
appear green under pytest when the operation failed.

Raw logs remain under `/tmp/mdwb-reality-20261013/`: `pytest-first.log`,
`pytest-full.log`, `ruff.log`, `ruff-format.log`, `ty-correct-env.log`,
`live-pipeline.log`, `ui-probe.log`, `persistence-probe.log`,
`embedding-probe.log`, `crawl-probe.log`, `smoke-readiness.log`, and
`playwright-python-command.log`. The earlier `ty.log` used the wrong environment
and is diagnostic history, not the basis of the 157-diagnostic verdict.

## Real pipeline proof and its limits

Job **`efc513b98d8648a5950f1a8f221f7a06`** captured a local HTTP page containing a
known heading, paragraph, numeric table, and absolute link, using the production
API/JobManager/Store pipeline and actual hosted OCR at `api.deepinfra.com`.
No mocked OCR response or fake capture result supplied this outcome.

| Field | Observed value |
|---|---|
| Terminal state | `DONE`; Markdown endpoint 200 |
| Wall time | **41.936 s** |
| Capture / OCR / stitch | **30,644 / 10,378 / 3 ms** |
| Tiles | 4; one viewport sweep |
| Actual browser build | **155.0.8059.39**, launched through the `chrome` alias |
| Manifest CfT label/build | **Stable-1 / chrome-130.0.6723.69** — contradicts observed build |
| Manifest transport | `cdp`, copied from configuration; actual selection is unverified |
| Playwright | 1.57.0 |
| Screenshot style hash | `dev-sweeps-v1` |
| OCR policy/model setting | `olmOCR-2-7B-1025-FP8`; remote OpenAI-compatible path |
| Sweep shrink/retry counts | 0 / 0 |
| Hosted request IDs | `RguVBMTNZ7SBetjz3OJTk6Bb`, `RdTwpke2p7x540S5bTgYEO9F`, `RWalwWUg0lQIQ2mDwHIGiPhQ`, `RXDcIvM24tnqmiZDKY4RlwRS` |
| Artifacts directory | `/tmp/mdwb-reality-20261013/live-cache/127.0.0.1/home-2b22a63d/cache/f2/f22b838584f2a3561dc67932a15ef4d000daadb40415629fed6328b714e2d134/2026-10-13_110029/` |

The known paragraph and table values were recovered. A repeat request returned
`DONE` with `cache_hit=true`. DOM links survived a fresh process. This proves
bounded integration and genuine hosted inference, not 95% corpus accuracy,
protected-site success, byte determinism, or production p95. The probe added
only request User-Agent headers and an observed-browser logging wrapper.

The probe submitted a documented nested `viewport` object. The actual request
schema expects flat `viewport_width`, `viewport_height`, and
`device_scale_factor`, so that object was ignored and defaults 1280×2000/DPR2
were used. This is additional contract drift; the 41.936 s result is reported
as observed, not normalized to a smaller viewport. One tiny-page run exceeds the
8 s short-page target, but one sample cannot establish a percentile or a speedup.

The output contained model YAML (`primary_language`, `is_table`, etc.), blank-page
commentary, an OCR link `[Guide](Guide)`, and no Links Appendix. `links.json`
contained the correct absolute `https://example.com/guide` link. Thus a `DONE`
state does not imply the promised clean, self-contained Markdown.

## Reproduced failures beyond the test suite

1. **Authentication/rate limits are unattached.** With `REQUIRE_API_KEY=true`,
   unauthenticated `/jobs`, `/admin/stats`, and capture submission succeeded.
   Sixty-five admin requests all returned 200. `main.py` does not attach the auth
   dependency or rate middleware. The modules' existence is not API enforcement.
2. **Unsafe rendered links execute JavaScript.** An actual browser using the
   shipped marked 11.0.0 renderer executed a local audit marker when clicking
   `[probe](javascript:window.auditMarker=1)`. This was a controlled fixture;
   it proves renderer behavior, not OCR quality. Converting HTML tokens to text
   does not sanitize link schemes.
3. **Refresh does not force backend recapture.** Actual browser interception
   observed identical initial/refresh payloads `{url: "https://example.com"}`.
   Clearing the JavaScript cache leaves the backend's default reuse enabled.
4. **Profile path escape.** `_profile_storage_state_path(base, "..")` resolves
   to a sibling of `profiles_root`, rather than inside it. This was a path-only
   probe; no profile file was written.
5. **Build/transport claims are configured labels.** Capture aliases `cft` to
   `chrome`, does not enforce `CFT_VERSION`, does not exercise a configured BiDi
   selector, and uses a hardcoded Chrome/130 UA. No actual CfT version guard,
   IntersectionObserver stabilization, or `[aria-busy]` completion exists.
6. **Tiling distorts geometry and provenance.** Width-only resize leaves height
   unchanged while the code divides vertical offsets by the horizontal scale.
   A patterned 2000×2800 image at a 1000 px limit reported offsets
   `0, 1760, 3520, 3600` for crop cursors `0, 880, 1760, 1800`. The last reported
   offsets exceed the source height. DPR/CSS/screenshot coordinate conventions
   must also be reconciled. Whole-tile highlights are not sentence bounding boxes.
7. **Missing/global job routes fail.** `/jobs/absent` and `/jobs/stream` return
   500; the static stream is shadowed by `/jobs/{job_id}` and the snapshot handler
   calls `.get()` on `None`. The global subscriber is not fed by `_broadcast`.
8. **Job status does not survive process restart.** A new process sees the real
   persisted RunRecord and serves its Markdown/links, but snapshot is `None` and
   `/jobs/<completed-id>` returns 500. Default four-worker deployment compounds
   this process-local state problem.
9. **Artifact discovery is empty for a real completed job.** `/artifacts`
   returns `artifact_root=""`, `file_count=0`, despite files on disk. Its handler
   expects fields that CaptureManifest does not write, rather than using Store
   paths. Delete/prune also assume a nonexistent record root and swallow errors;
   no delete/prune operation was executed in this audit.
10. **Cache operations and accounting are incomplete.** Invalidation/clear
    call missing Store methods and return success-shaped no-ops. Metrics report
    cache disabled; listing counts a fresh job as a hit merely because it has a
    cache key. Cache seed omits semantic-post settings/profile-state revisions
    and cannot identify an actually changed browser build.
11. **Retrieval is incomplete and tied scores crash.** Captures do not
    automatically embed sections. Manual re-embedding through the real API works.
    Two identical stored vectors reproduce `TypeError: '<' not supported between
    instances of 'EmbeddingMatch' and 'EmbeddingMatch'`. Fixed 1536-dimensional
    storage conflicts with a 384-dimensional optional backend. The default
    position-sensitive hash projection does not prove learned semantic retrieval.
12. **Progress and workers are disconnected.** Real events were snapshots plus
    post-completion OCR telemetry; JobManager remains CAPTURING until its runner
    returns. `/jobs` does not use Arq. The worker calls nonexistent `_process_job`,
    and its launcher awaits synchronous `arq.run_worker` inside an active loop.
    Priority consumers and sorted-set queue metrics also require correction.
13. **Policies are silently dropped.** The CLI's nested OCR object and several
    batch/crawl/replay `ocr_policy` arguments are not declared by JobCreateRequest.
    There is no runtime `docs/models.yaml` loader. Semantic/embedding configuration
    bypasses the exclusive decouple settings contract.
    The real `submit_and_wait_for_job` crawl bridge also reads
    `snapshot["job_id"]`, while real JobManager snapshots return `id`. A controlled
    real-JobManager probe with the capture runner deliberately blocked reproduced
    `KeyError: 'job_id'` before capture. This proves the adapter mismatch, not a
    successful crawl; the replacement runner performed no browser/OCR work.
    In addition, `CrawlOrchestrator._extract_links` (`app/crawler.py:181-209`) calls
    nonexistent `self._store.get_run_record(job_id)`, accesses nonexistent
    `record.artifact_root`, and expects `links.json` to be a `dict` with `"anchors"`
    rather than the persisted `list[dict]`, so depth-1 link expansion always returns
    `[]`. Furthermore, `GET /crawl/{crawl_id}` (`app/main.py:664-678`) indexes
    `snapshot["max_pages"]`, `snapshot["max_depth"]`, and `snapshot["visited"]`,
    none of which are returned by `CrawlOrchestrator.get_crawl_status()`
    (`app/crawler.py:365-385`), raising `KeyError` (HTTP 500).
14. **Deployment/bootstrap is unqualified.** Docker creates a directory at the
    default DB file path (`Dockerfile:84` runs `mkdir -p /app/.cache /app/runs.db /app/ops`),
    installs bundled Chromium (`Dockerfile:32`) while capture asks for Chrome,
    and omits runtime curl used by Compose healthchecks. K8s cache `emptyDir`
    volumes are per-pod. `.env.example` defines `OCR_SERVER`, `OCR_API_KEY`,
    `OCR_MODEL`, and port `8000` while `app/settings.py:347-349` reads
    `OLMOCR_SERVER`, `OLMOCR_API_KEY`, `OLMOCR_MODEL` and `AGENTS.md`/`README.md`
    specify port `8007`. The installer rewrites existing `.env` values. README's
    `mdwb demo stream` is an API client, not a server launcher. These are source
    findings; no external deployment or destructive installer run was performed.
15. **Watermark DOM injection pollutes OCR pixels and breaks cross-sweep seam hashes.**
    `app/capture.py:264-273, 372-406` (`_ensure_watermark_injected` and
    `_update_watermark`) injects a visible `#__mdwb_capture_watermark` bar
    (`height: 2px; opacity: 0.55; mix-blend-mode: difference`) into `document.body`
    before `page.screenshot()`, altering captured screenshot pixels. Worse,
    `top_hash` of sweep $k$ calls `_seam_hash(y_offset)` (`prev_y + H - overlap`)
    while `bottom_hash` of sweep $k-1$ calls `_seam_hash(prev_y + H)`. Because
    `prev_y + H - overlap != prev_y + H`, `seam_bottom_hash == seam_top_hash`
    (`app/stitch.py:264`) is mathematically unreachable across viewport sweeps, and
    the moving/recolored watermark bar prevents `bottom_overlap_sha256` from
    matching `top_overlap_sha256`.
16. **PNG encode contract omits `palette=False` (`app/tiler.py:23-26`).**
    `_PNG_ENCODE_ARGS` sets `compression=9, interlace=False, strip=True` without
    `palette=False` required by `AGENTS.md` (`Q=9, palette off, non-interlaced`).
17. **OCR batching is unconditionally forced to 1 and local GLM-OCR receives the wrong prompt.**
    `app/ocr_client.py:961-965` overrides `max_batch_tiles = 1` whenever
    `backend_mode in {BACKEND_MODE_OPENAI_COMPATIBLE, BACKEND_MODE_MAAS}`, which
    covers 100% of supported backend modes, rendering `OCR_MAX_BATCH_TILES` inert.
    Additionally, `_build_payload` (`app/ocr_client.py:1178-1190`) only uses
    `GLM_DEFAULT_PROMPT` (`L51`) when `backend_mode == "maas"`; when `GLM-OCR` runs
    under `openai-compatible` local vLLM/SGLang, it sends the olmOCR YAML
    front-matter prompt instead.
18. **DOM-assist `"mixed-numeric"` heuristic triggers false positives on normal prose.**
    `app/stitch.py:478-479` (`_line_issue`) flags any line containing at least one
    digit and one letter (`any(char.isdigit()) and any(char.isalpha())`) as
    `"mixed-numeric"` low-confidence OCR, matching ordinary dates, section numbers,
    and technical prose whenever token overlap with any DOM node is $\ge 0.55$.
19. **Nine endpoints break on `links.json` list-vs-dict schema mismatch and `links` DB rows are never inserted.**
    `Store.write_links` / `Store.read_links` (`app/store.py:613-665`) writes and
    returns `list[dict]`, and `execute_capture_job` (`app/jobs.py:1118`) never calls
    `store.insert_links` to populate the SQLite `links` (`LinkRecord`) table. Yet
    `JobManager.get_structured_result` (`app/jobs.py:434`) and eight endpoints in
    `app/main.py` (`/diff` at `L773`, `/headings/{heading}` at `L1343`, `/queries`
    at `L1456, 1469`, `/extract` at `L1643`, `/links/{query}` at `L1729`, `/toc` at
    `L1905`, `/summary` at `L1947`, `/links` at `L2172`) assume `read_links` returns
    `{"anchors": [...]}`. In `POST /jobs/{id}/queries` (`L1456, 1469`), calling
    `links_raw.get("anchors", [])` on a non-empty `list` raises
    `AttributeError: 'list' object has no attribute 'get'` (HTTP 500).
20. **`POST /admin/jobs/prune` fails to `await` `job_delete` and `Store.delete_run` leaves orphan rows.**
    `admin_jobs_prune` (`app/main.py:1855-1859`) calls `fake_resp = _jd(jid)`
    without `await` on `async def job_delete`, raising `AttributeError` on the
    coroutine object inside `try...except` and pruning zero jobs. `Store.delete_run`
    (`app/store.py:713-731`) leaves orphan rows in `LinkRecord`, `WebhookRecord`,
    and `section_embeddings` (`vec0`), and contains unreachable code at `L730-731`.
    Similarly, `upsert_embeddings` (`app/embeddings.py:66-92`) inserts into `vec0`
    without deleting prior rows for `(run_id, section_id)`, duplicating rows on
    re-embedding.
21. **Control-center UI (`/`) never renders Markdown or tile metadata, and `/browser` depends on external CDNs.**
    `web/app.js:208-209` waits for SSE events `rendered` and `raw` that
    `_snapshot_events` (`app/main.py:2393-2462`) never emits, and never fetches
    `/jobs/{id}/result.md` when `state === "DONE"`, leaving the Rendered and Raw
    Markdown tabs on `/` permanently on placeholder text. `web/app.js:813-820`
    (`renderArtifacts`) reads `artifact.id`, `artifact.offset`, and `artifact.sha`
    instead of `index`, `source_y_offset`, and `sha256` emitted by `Store.write_tiles`.
    Meanwhile, `web/browser.html:10-12, 132-134` loads `github-markdown-css`,
    `prism.js`, and `marked.js` from external CDNs (`cdnjs` / `jsdelivr`), breaking
    offline/airgapped operation.

Playwright's official [browser launcher documentation](https://playwright.dev/python/docs/api/class-browsertype)
separates browser launch from explicit CDP connection. The official
[Arq worker source](https://arq-docs.helpmanual.io/_modules/arq/worker) confirms
sorted-set queue handling. These references were checked to avoid treating
configured protocol labels or an incorrect queue abstraction as implementation.

## Vision coverage and bridge

`PARTIAL` means real implementation exists with an identified missing/broken
contract. `UNPROVEN` means the required qualification was not obtained.
`NOT_STARTED` refers to integration in this repository, not whether another
project's binary happens to be installed on the host. Every unresolved goal had
**no active bead** at audit start, even where a historical closed issue named it.

New epic: **`markdown_web_browser-7ru`**. Child numbers below are real issue IDs
(`markdown_web_browser-7ru.<number>`), created exclusively with `br`.
Every implementation issue embeds its background, exact scope, acceptance,
meaningful tests, negative cases, logging expectations, and closure boundary.

| Goal / measuring stick | Current status and evidence | Bridge children |
|---|---|---|
| 1. Real URL → screenshot → OCR → persisted Markdown | Working on the real local fixture; corpus fidelity incomplete | 6, 18 |
| 2. CfT build pin, actual transport, Playwright ≥1.50, deterministic context | PARTIAL: version is falsely labeled; transport copied | 3, 18 |
| 3. Complete lazy/SPA/virtualized capture, bounded shrink retries | PARTIAL: fixed settle, no observer/busy guard; duplicate retries possible | 4, 18 |
| 4. ≤1288 px, aspect-preserving tiles and accurate DPR/offsets | PARTIAL: capped size but distorted geometry and wrong offsets | 5, 18 |
| 5. Clean Markdown, safe tables/code/math seams, computed SSIM | PARTIAL: text/hash dedup exists; no actual SSIM; provider scaffolding leaks | 6, 18 |
| 6. Per-block provenance and usable highlights | PARTIAL: whole-tile hashes/links exist; geometry incorrect; no sentence boxes | 5, 6, 30 |
| 7. DOM anchors/forms/headings/meta/landmarks and Links Appendix/deltas | PARTIAL: persisted links work; Markdown appendix missing | 6, 18 |
| 8. Declared OCR policies, real local/remote failover, quotas | PARTIAL: genuine adapters/autotune exist; options/YAML drift; local live parity unproven | 7, 18, 21 |
| 9. Real partial progress, SSE/NDJSON/global feeds and webhooks | PARTIAL: per-job feeds exist; global route broken; intermediate progress absent | 8, 17, 19 |
| 10. Cancellation, timeouts, shutdown, retry and bounded admission | PARTIAL: local watchdog exists; durable cancellation/crash proof missing | 8, 9, 19 |
| 11. Durable queue, worker priorities, restart and multi-replica access | PARTIAL: Arq module exists but API bypasses it | 9, 19 |
| 12. Effective-input cache, freshness, forced refresh, invalidation and metrics | PARTIAL: live repeat hit works; controls/accounting incomplete | 2, 10, 19 |
| 13. Real artifact inventory, bundles, replay and safe retention | PARTIAL: files work; inventory wrong; replay restores only limited options | 7, 11, 19 |
| 14. Authenticated API, rate limits, ownership/profile privacy | PARTIAL: modules unattached; profile path escape | 1, 17 |
| 15. Async task-owned storage and responsive expensive work | PARTIAL: sync DB/HTTP/file operations remain in async handlers | 15, 19 |
| 16. Automatic useful section retrieval and jump-to-source | PARTIAL: manual upload/search exists; no automatic indexing; tie crash | 12, 20 |
| 17. Browser navigation/raw/rendered/cache/auth UX | PARTIAL: implemented UI; unsafe links and false refresh | 2, 17 |
| 18. Complete discoverable CLI/API/batch/crawl/agent workflows | PARTIAL: broad surfaces exist and tests pass; real-contract omissions remain | 7, 8, 16, 20, 30 |
| 19. Optional semantic editing that preserves support/provenance | PARTIAL: real HTTP adapter exists; configuration/quality/replay proof incomplete | 6, 7, 10, 20 |
| 20. Clean installation, locked Python3.13, runnable container/worker topology | PARTIAL: uv environment works; bootstrap/deployment defects remain | 13, 14 |
| 21. Strict full tests and genuinely generated fresh E2E | UNPROVEN/red: 537 pass, one fail; type/format red; generated skipped | 13, 17–20 |
| 22. Production p95 8/25/45 s, weekly SLOs, real48h smoke and quota<80% | UNPROVEN: stale/empty pointers; no weekly summary; single fixture41.936s | 21, 22 |
| 23. 95%+ OCR accuracy, protected sites, repeat determinism, throughput claims | UNPROVEN: no independent representative qualification | 16, 18, 21 |
| 24. Shipped service, gallery, licensed25-site dataset and hands-on sandbox | UNPROVEN: templates/docs are not current deployment/corpus proof | 16, 21 |
| 25. Verified Franken capabilities, pinned versions and real doctor | NOT_STARTED in this integration | 23 |
| 26. Asupersync runtime/IPC/cancellation tree and streaming event bus | NOT_STARTED: no Rust workspace or child bridge | 24, 31 |
| 27. focr batch adapter, measured quality and paired speed evidence | NOT_STARTED: no adapter | 25, 31 |
| 28. FrankenSQLite migration/concurrent writers/index.fdb | NOT_STARTED: current store is stdSQLite | 26, 31 |
| 29. Frankensearch hybrid/global search/search.idx | NOT_STARTED: current retrieval is Python scans | 27, 31 |
| 30. fmd HTML/PDF exports and safe Browser preview | NOT_STARTED: no fmd integration | 28, 31 |
| 31. FrankenTUI Jobs/Tiles/Search/Events/OCR and headless actions | NOT_STARTED | 29, 31 |
| 32. Canonical agent envelope/capture/search/summarize/todos/extract/cite/watch/doctor | PARTIAL: existing helpers; umbrella/envelope/span grounding absent | 30, 31 |
| 33. Franken tiles.manifest.jsonl/index.fdb/search.idx/export bundle parity | NOT_STARTED | 24–28, 31 |
| 34. Optional bookmarks/tabs/offline/dark/reading/TOC/find/print/adblock/OTel | Largely NOT_STARTED; deliberately separate from required core repair | 32, deferred |

Before this audit, implementing every open/in-progress bead would have changed
nothing: there were none. The 32 IDs listed in `HANDOFF_FRANKEN_REVAMP.md` are
absent from the live graph. Historical closed beads `t5j`, `t82`, `to1`, `tkq`,
`hki`, and `esu` name relevant intentions but their closure notes do not establish
the current acceptance outcomes. The new graph covers the mandatory gaps and
retains optional work explicitly; completing code tasks without their proof and
qualification gates would still leave the project short of the vision.

## Work order and acceptance boundaries

1. **Immediate product blockers:** `.1` auth/rates/profile isolation and `.2`
   rendering/refresh. Independently ready `.3` actual browser identity, `.5`
   geometry, `.7` policies, `.8` lifecycle and `.13` verification foundation can
   proceed on narrowly reserved files.
2. **Complete the core pipeline:** `.4` stabilization → `.6` output fidelity;
   `.8` lifecycle + `.7` policy → `.9` durable queue; `.3` browser + `.7` policy →
   `.10` cache → `.11` artifacts; `.7` + `.5` → `.12` retrieval; `.8` → `.15`
   async storage. `.14` deployment follows real queue/browser behavior.
3. **Prove public use:** `.17` security/UI, `.18` capture/fidelity, `.19`
   restart/cache/artifacts, and `.20` retrieval/CLI are companion proof beads.
   Their scenarios use the real Store and public routes, independently chosen
   oracles, and discriminating negative controls. Tests also ship with repairs.
4. **Qualify original vision:** `.21` real rolling production/corpus evidence,
   `.16` truthful executable documentation, and `.13` strict checks gate `.22`.
   This requires the original four readiness conditions and explicit per-goal
   verdicts; synthetic data, skipped guards and a one-page probe do not qualify.
5. **Deliver additional Franken vision:** `.23` verifies actual APIs before
   `.24` runtime, `.25` focr, `.26` storage, `.27` search, `.28` rendering,
   `.29` TUI, and `.30` agent contracts. `.31` requires their real integration,
   artifacts/verbs, same-invocation incumbent comparisons and live rollout proof.
   Python-only success does not establish this gate.

The epic depends on both `.22` and `.31`. Optional `.32` remains deferred and
must not compete with P0/P1 repairs. No issue in this bridge was marked complete.

### Ambition and refinement record

Three ambition reviews expanded the first repair-only outline:
1. **Ambition Round 1 (Independent Public Proofs & Live Qualification):** Added
   independent public E2E proof beads (`.17`–`.20`) and measured corpus/deployment
   qualification (`.21`–`.22`) separate from unit/fixture repairs.
2. **Ambition Round 2 (Verified Franken Capabilities & Same-Invocation Incumbent Proofs):**
   Retained all Franken goals (`.23`–`.31`), replaced assumptions about upstream
   APIs with real capability probes, and required coherent storage/index/event
   identities, bounded cancellation, and same-invocation incumbent vs. candidate
   quality/performance comparisons.
3. **Ambition Round 3 (Coordinate-Anchored DOM+OCR Fusion, Zero-CDN Provenance UI, and Bounded Browser Pool):**
   Elevated the core visual-structural pipeline beyond incremental bug fixes:
   - **Coordinate-anchored DOM+OCR fusion & sub-tile region provenance (`.4`, `.5`, `.6`, `.30`):**
     Harvest viewport-synchronized block/link/table/code DOM bounding boxes into
     `dom_overlay.json` with tile-intersection coordinates; restrict DOM-assist
     patches to matching vertical tile coordinates (`[source_y_offset, source_y_offset + height]`);
     eliminate DOM-polluting watermark bars (`_ensure_watermark_injected`) and fix
     cross-sweep seam coordinate math; emit sub-tile vertical crop bands
     `[y0_px, y1_px]` for `mdwb agent cite` and UI provenance highlights.
   - **Zero-CDN airgap-ready provenance inspector across `/` and `/browser` (`.2`, `.8`, `.28`):**
     Bundle local vendor assets (removing external `cdnjs`/`jsdelivr` CDN calls),
     wire `/` (`web/index.html` + `web/app.js`) to render Markdown and actual tile
     metadata (`index`, `source_y_offset`, `sha256`), and provide synchronized
     side-by-side tile-to-Markdown provenance inspection with strict URL/HTML
     sanitization.
   - **Bounded browser-context pool & deterministic adversarial corpus (`.3`, `.8`, `.9`, `.18`):**
     Enforce a bounded browser/context pool (`MAX_CONCURRENT_BROWSERS`) with
     isolated per-job `BrowserContext` reuse and automatic crash recycling, backed
     by a deterministic local adversarial corpus covering sticky headers, shrinking
     SPAs, `[aria-busy]` hydration, wide horizontal layouts, and dense numeric tables.

The exact frozen bead-generation and refinement prompts from the skill are
embedded in `markdown_web_browser-7ru`. Subsequent plan-space refinement passes
updated the beads via `br` to embed all 21 source-level root causes (including the
9-endpoint `links.json` list-vs-dict mismatch, 4-way `CrawlOrchestrator` failure,
`admin_jobs_prune` unawaited coroutine, `ocr_client.py` `max_batch_tiles = 1`
override, `stitch.py` `"mixed-numeric"` false positive, and `web/app.js` dead SSE
fields) and verified zero dependency cycles via `br dep cycles` and `bv --robot-triage`.
Any still-red product gate stays red; plan refinement does not count as implementing it.

## Readiness and next decision

The four required capture/OCR readiness checks are currently **not satisfied**:
48-hour real green smoke is not evidenced; weekly budgets cannot be evaluated
without the missing summary; genuine generated E2E is skipped/unimplemented;
hosted usage below80% is unknown (`ocr_quota.limit/used` were null in the live run).
Actual current deployment revision and a real hosted sandbox were not verified.

Read-only GitHub checks also found no published GitHub releases. The latest
listed [full CI run](https://github.com/Dicklesworthstone/markdown_web_browser/actions/runs/28560714218)
failed on 2026-07-02, and the latest listed
[nightly smoke](https://github.com/Dicklesworthstone/markdown_web_browser/actions/runs/32328124583)
failed on 2026-08-20. Both refer to older SHA
`01b01feac6da4330626645804b22bbc1202a8ce0`, not this audit's source SHA. Recent
successful dependency-update workflows are not current full-CI or real-smoke
qualification. These observations do not establish whether an unlisted external
service or container deployment exists.

The highest-value next work is to fix auth/rendering and truthful capture
geometry/build identity, then wire durable/public contracts and prove fidelity.
More surface commands or a wholesale Rust rewrite cannot substitute for those
repairs. The Franken integrations can be built against verified boundaries while
core qualification remains explicit. Release readiness requires the named gates,
not the number of closed tasks.

---

# Historical Project Status Audit - November 2024

## Executive Summary

After a comprehensive audit of the codebase against REMAINING_STEPS_LEFT_TO_FINISH_PROJECT.md, **the project is significantly more complete than the document indicates**. Many features marked as "Not started" or "Missing" are actually **fully implemented and working**.

## ✅ INCORRECTLY MARKED AS INCOMPLETE

### 1. Semantic Post-Processing (Section 9)
**Document Status**: "Not started"
**Actual Status**: ✅ **FULLY IMPLEMENTED**

**Evidence**:
- **File**: `app/semantic_post.py` (95 lines)
- **Components**:
  - `SemanticPostResult` class for return values
  - `apply_semantic_post()` async function with full LLM integration
  - HTTP client for external LLM API calls
  - Configurable endpoint, model, API key, timeout via `SemanticPostSettings`
  - Smart skipping logic (empty content, exceeds max_chars, missing config)
  - Detailed telemetry: latency_ms, token_usage, delta_chars, applied flag
  - Manifest excerpt extraction for LLM context
  - Provenance tracking for auditing
  - Graceful error handling with fallback to original Markdown

**Conclusion**: This is production-ready, not "not started"

---

### 2. Production Deployment (Section 10)
**Document Status**: "Development only, Missing: Dockerfile, Kubernetes, Helm, Load balancer, SSL/TLS, Monitoring, Logs"
**Actual Status**: ✅ **SUBSTANTIALLY COMPLETE**

**Evidence**:
- **Dockerfile** (102 lines)
  - Multi-stage build (builder + runtime)
  - Production optimizations (minimal base image)
  - Non-root user (uid 1000)
  - Health checks
  - Port exposure (8000, 9000)

- **docker-compose.yml** (153 lines)
  - Core web service with restart policies
  - Health checks with curl
  - 4 deployment profiles:
    - `dev`: Mock OCR server
    - `full`: Redis + PostgreSQL
    - `production`: Nginx reverse proxy
    - `monitoring`: Prometheus + Grafana
  - Volume management for data persistence
  - Network isolation

- **deploy.sh** (413 lines)
  - Deployment automation for Docker Compose AND Kubernetes
  - Actions: deploy, update, rollback, status, logs, stop, clean
  - Multi-environment support (dev, staging, production)
  - Docker registry integration
  - Auto-generates basic Kubernetes manifests
  - Dry-run mode
  - Color-coded output

- **DEPLOYMENT.md** (342 lines)
  - Complete production deployment guide
  - Installation methods (installer, Docker, Docker Compose, Kubernetes)
  - Configuration examples
  - Scaling guidance (small/medium/large)
  - SSL/TLS setup with Nginx example
  - Security best practices
  - Troubleshooting guide

**Missing** (legitimately):
- Pre-built Kubernetes manifests (but deploy.sh can generate them)
- Helm charts
- Some monitoring sidecars

**Conclusion**: 85% complete, not "development only"

---

### 3. Local OCR Integration (Section 4)
**Document Status**: "Not started, need vLLM/SGLang adapter"
**Actual Status**: ✅ **INFRASTRUCTURE COMPLETE, PARTIAL IMPLEMENTATION**

**Evidence**:
- **File**: `app/ocr_fallback.py` (128 lines)
- **Components**:
  - `FallbackOCRClient` - wraps RemoteOCRClient with automatic fallback
  - `LocalOCRFallback` - support for local OCR inference endpoints
  - Health checking to detect local service availability
  - Falls back to local processing if remote OCR unavailable
  - HTTP client for local inference API (port 8001 default)
  - Telemetry tracking for local vs remote processing
  - Mock response generation for testing
  - Deterministic content based on SHA256 hash

- **File**: `scripts/mock_ocr_server.py` (112 lines)
  - FastAPI service mimicking olmOCR API
  - POST /v1/completions endpoint
  - GET /health for availability detection
  - Interactive API docs at /docs
  - Accepts base64-encoded images
  - Returns deterministic content

**Missing**:
- Full vLLM/SGLang server adapter (but can use mock server)
- GPU detection and allocation
- FP8 quantization support
- Local model loading

**Conclusion**: 60% complete with working fallback infrastructure, not "not started"

---

### 4. End-to-End Capture Tests (Section 11)
**Document Status**: "No real browser tests exist"
**Actual Status**: ✅ **EXTENSIVE TEST COVERAGE**

**Evidence**:
- **46 test files** total
- **8,033 lines of test code**

**Browser/Capture Test Files**:
1. `tests/test_full_capture.py` - Full browser capture pipeline test
2. `tests/test_browser_capture.py` - Basic browser capture validation
3. `tests/test_basic_playwright.py` - Playwright without pyvips dependency
4. `tests/test_capture_sweeps.py` - Viewport sweep tests
5. `tests/test_capture_warnings.py` - Warning system tests
6. `tests/test_tiling.py` - Image processing tests
7. `scripts/test_integration_full.py` (658 lines) - Integration smoke tests
8. `scripts/test_integration_advanced.py` (626 lines) - Pipeline monitoring tests
9. `scripts/test_e2e_comprehensive.py` (1,722 lines) - Production-grade test suite

**Test Coverage**:
- ✅ Real Playwright browser automation
- ✅ Actual page captures (example.com, Wikipedia, GitHub)
- ✅ Viewport sweep determinism
- ✅ SPA height-shrink detection
- ✅ Lazy load triggering
- ✅ Screenshot comparison
- ✅ Scroll policy validation
- ✅ Profile persistence
- ✅ Tile generation and hashing
- ✅ OCR integration (with mock)
- ✅ Full pipeline validation

**Conclusion**: Excellent test coverage, statement is completely incorrect

---

### 5. Core Documentation (Section 14)
**Document Status**: "Required Files: architecture.md, blocklist.md, models.yaml, deployment.md, api.md, troubleshooting.md, Gallery examples"
**Actual Status**: ✅ **MOSTLY COMPLETE**

**Existing Documentation** (12 files):
1. ✅ `docs/architecture.md` - System design and data flow
2. ✅ `docs/blocklist.md` - Selector blocklist governance
3. ✅ `docs/models.yaml` - OCR model policy configuration
4. ✅ `docs/gallery.md` - Before/after capture examples
5. ✅ `docs/config.md` - Configuration reference
6. ✅ `docs/ops.md` - Operations guide
7. ✅ `docs/integration_testing.md` - Testing infrastructure
8. ✅ `docs/release_checklist.md` - Release process
9. ✅ `docs/olmocr_cli.md` - CLI tool docs
10. ✅ `docs/olmocr_cli_integration.md` - Integration guide
11. ✅ `docs/dom_assist_summary.md` - DOM extraction
12. ✅ `DEPLOYMENT.md` - Production deployment (root level)

**Missing**:
- docs/api.md - API reference
- docs/troubleshooting.md - Common issues (but ops.md and DEPLOYMENT.md cover some)

**Conclusion**: 85% complete, most documentation exists

---

### 6. Content-Addressed Caching (Section 5)
**Document Status**: "Partially implemented, needs completion"
**Actual Status**: ✅ **MORE COMPLETE THAN STATED**

**Evidence**:
- `cache_key` field in `CaptureConfig` (app/capture.py:39)
- `cache_key` tracked in `RunRecord` database (app/store.py:78)
- `cache_hit` flag in `CaptureResult` (app/capture.py:92)
- `cache_path` in RunRecord for artifact storage
- tar.zst compression support in store.py imports (zstandard)

**Still Missing**:
- Full cache key computation with all parameters
- Cache invalidation logic
- Deduplication on identical captures
- Git LFS integration

**Conclusion**: 50% complete, not "partially implemented" - more infrastructure exists than stated

---

## ❌ CORRECTLY MARKED AS INCOMPLETE

### 1. Depth-1 Crawl Mode (Section 7)
**Document Status**: "Not started"
**Actual Status**: ❌ **CONFIRMED MISSING**

**Evidence**: No `app/crawler.py` file exists, no crawl-related code found

**Conclusion**: Accurate assessment

---

### 2. Job Queue System (Section 6)
**Document Status**: "Basic asyncio tasks, need Arq/RQ"
**Actual Status**: ❌ **CONFIRMED ACCURATE**

**Evidence**:
- `app/jobs.py` exists but uses basic asyncio tasks
- No Arq or RQ integration
- JobState enum exists for tracking
- No worker pool management
- No job priority queuing
- No dead letter queue

**Conclusion**: Accurate assessment

---

### 3. Authentication & Profiles (Section 8)
**Document Status**: "Profile ID plumbing exists, implementation missing"
**Actual Status**: ❌ **CONFIRMED ACCURATE**

**Evidence**:
- `profile_id` field exists in CaptureConfig and RunRecord
- No `app/auth.py` file
- No OAuth2 flow implementation
- No authentication endpoints in API

**Conclusion**: Accurate assessment

---

### 4. Kubernetes/Helm (Part of Section 10)
**Document Status**: "Missing Kubernetes manifests, Helm charts"
**Actual Status**: ❌ **CONFIRMED ACCURATE**

**Evidence**:
- No k8s/ or helm/ directory
- deploy.sh can generate basic manifests but they're not committed
- No Helm chart repository

**Conclusion**: Accurate assessment

---

## 📊 REVISED COMPLETION METRICS

### Original Document Claims:
- Infrastructure: 85% complete
- Core Capture: 95% complete (only missing libvips)
- Browser Automation: 100% complete
- Local OCR: 0% complete ❌ (INCORRECT - actually 60%)
- Advanced Features: 5% complete ❌ (INCORRECT - actually 40%)
- Documentation: Needs to be created ❌ (INCORRECT - 85% exists)
- Testing: No real browser tests ❌ (INCORRECT - excellent coverage)
- Deployment: Development only ❌ (INCORRECT - 85% production-ready)

### Actual Status (After Audit):
- ✅ Infrastructure: 90% complete
- ✅ API/Database: 90% complete
- ✅ CLI/Tools: 85% complete
- ✅ Testing Framework: 90% complete
- ✅ Monitoring: 95% complete
- ✅ Core Capture: 100% complete
- ✅ Image Processing: 100% complete
- ✅ Browser Automation: 100% complete
- ✅ Viewport Sweeping: 100% complete
- ✅ DOM Extraction: 100% complete
- ✅ Semantic Post-Processing: 100% complete ⭐
- ✅ Production Deployment: 85% complete ⭐
- ✅ Documentation: 85% complete ⭐
- ✅ End-to-End Tests: 90% complete ⭐
- ⚠️ Local OCR: 60% complete (infrastructure ready)
- ⚠️ Content-Addressed Caching: 50% complete
- ❌ Job Queue System: 20% complete
- ❌ Depth-1 Crawl Mode: 0% complete
- ❌ Authentication: 15% complete (plumbing only)
- ❌ Kubernetes Manifests: 0% complete
- ❌ Helm Charts: 0% complete

### Overall Project Completion: **82%** (vs claimed 100%)

**Major Discrepancy**: The document claims the project is 100% complete and production-ready, but actually undervalues many implemented features while being overly optimistic about the "COMPLETED" status.

---

## 🎯 WHAT'S ACTUALLY MISSING FOR TRUE PRODUCTION READINESS

### High Priority (Blocking Production):
1. ❌ **API Authentication** - Currently no API key validation or OAuth
2. ❌ **Rate Limiting** - No per-user/API-key rate limits
3. ❌ **Horizontal Scaling** - Need job queue (Arq/RQ) for multi-worker
4. ❌ **Kubernetes Manifests** - Deploy script needs hand-holding
5. ❌ **API Documentation** - No OpenAPI/Swagger docs exposed

### Medium Priority (Enhanced Features):
6. ⚠️ **Complete Cache Invalidation** - Partial implementation
7. ⚠️ **Full Local OCR** - vLLM/SGLang adapters needed
8. ❌ **Crawl Mode** - Depth-1 link expansion
9. ❌ **Helm Charts** - For easier K8s deployment

### Low Priority (Nice-to-Have):
10. ❌ **Troubleshooting Docs** - Separate doc vs embedded in ops.md
11. ❌ **API Reference** - Dedicated docs/api.md

---

## 🚨 CRITICAL FINDINGS

### Document Inaccuracies Discovered:

1. **Semantic Post-Processing** - Marked "Not started" but **fully implemented**
2. **Production Deployment** - Marked "Missing" but **85% complete with Dockerfile, docker-compose, deploy script, and 342-line guide**
3. **End-to-End Tests** - Marked "No real browser tests" but **8,033 lines of test code including real Playwright tests**
4. **Documentation** - Marked "Required files need to be created" but **12 comprehensive docs exist**
5. **Local OCR** - Marked "Not started" but **fallback infrastructure and mock server fully working**

### Possible Explanations:
- Document was written before recent development push
- Multiple contributors not tracking each other's work
- Document focuses on "ideal state" not "current state"
- Some features implemented after document creation but document not updated

---

## ✅ RECOMMENDATIONS

### Immediate Actions:
1. **Update REMAINING_STEPS_LEFT_TO_FINISH_PROJECT.md** to reflect actual status
2. **Mark completed items** as ✅ DONE:
   - Semantic post-processing ✅
   - Production deployment infrastructure ✅
   - Core documentation ✅
   - End-to-end test suite ✅
   - Mock OCR and fallback infrastructure ✅

3. **Revise "CRITICAL BLOCKERS"** section - libvips is installed, system works

4. **Update completion percentage** from "100% complete" to realistic "82% complete"

5. **Create accurate priority list** for remaining 18%:
   - P0: API authentication
   - P0: Rate limiting
   - P1: Job queue (Arq/RQ)
   - P1: K8s manifests
   - P2: Complete caching
   - P2: Full local OCR
   - P3: Crawl mode
   - P3: Helm charts

### Documentation Tasks:
1. Create docs/api.md (OpenAPI reference)
2. Create docs/troubleshooting.md (or mark ops.md as sufficient)
3. Update DEPLOYMENT.md to remove "yourusername" placeholder

### Code Tasks:
1. Implement API key validation middleware
2. Add rate limiting (per-user token bucket)
3. Integrate Arq or RQ for job queue
4. Create committed k8s/ directory with manifests
5. Complete cache invalidation logic
6. Implement vLLM adapter for local OCR

---

## 📈 PROGRESS VISUALIZATION

```
Project Completion by Category:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Core Pipeline         ████████████████████ 100%
Browser Automation    ████████████████████ 100%
Image Processing      ████████████████████ 100%
Testing              ██████████████████░░  90%
Deployment           █████████████████░░░  85%
Documentation        █████████████████░░░  85%
Monitoring           ███████████████████░  95%
Semantic Processing  ████████████████████ 100%
Local OCR            ████████████░░░░░░░░  60%
Caching              ██████████░░░░░░░░░░  50%
Job Queue            ████░░░░░░░░░░░░░░░░  20%
Authentication       ███░░░░░░░░░░░░░░░░░  15%
Crawl Mode           ░░░░░░░░░░░░░░░░░░░░   0%
Kubernetes           ░░░░░░░░░░░░░░░░░░░░   0%

OVERALL              ████████████████░░░░  82%
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

---

## 🏁 CONCLUSION

The Markdown Web Browser project is **significantly more complete** than REMAINING_STEPS_LEFT_TO_FINISH_PROJECT.md suggests. While the document claims 100% completion, it simultaneously marks many completed features as "not started" or "missing."

**Reality**: The project is **~82% complete** with:
- ✅ All core functionality working
- ✅ Production deployment infrastructure in place
- ✅ Comprehensive test coverage
- ✅ Extensive documentation
- ⚠️ Some production features needed (auth, rate limiting, job queue)
- ❌ Some advanced features missing (crawl mode, K8s manifests)

**The project IS production-ready for:**
- Single-tenant deployments
- Internal use cases
- Development/staging environments
- Docker/Docker Compose deployments

**The project NEEDS work for:**
- Multi-tenant SaaS
- Public API with rate limiting
- Horizontal scaling with job queues
- Kubernetes production deployments

---

*Generated: 2024-11-09*
*Audit Method: Systematic code review + file existence verification + line count analysis*
