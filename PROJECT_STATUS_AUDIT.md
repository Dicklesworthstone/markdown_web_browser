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
14. **Deployment/bootstrap is unqualified.** Docker creates a directory at the
    default DB file path, installs bundled Chromium while capture asks for Chrome,
    and omits runtime curl used by Compose healthchecks. K8s cache `emptyDir`
    volumes are per-pod. The installer rewrites existing `.env` values. README's
    `mdwb demo stream` is an API client, not a server launcher. These are source
    findings; no external deployment or destructive installer run was performed.

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

Two ambition reviews expanded the first repair-only outline. The first added
independent public E2E proofs and measured corpus/deployment qualification. The
second retained all Franken goals, replaced assumptions about upstream APIs with
real capability probes, and required coherent storage/index/event identities,
bounded cancellation and same-invocation quality/performance comparisons. These
are concrete acceptance requirements in the beads, not claimed results.

The exact frozen bead-generation and refinement prompts from the skill are
embedded in `markdown_web_browser-7ru`. Five subsequent review passes cover
completeness, dependency/order optimality, test/oracle discrimination, effective
configuration/data identity, and final coherence. A sixth fresh review checks
convergence after the crawl-bridge contract refinement. The corrections and
validation are recorded in the epic's comments. Any still-red product gate stays
red; plan refinement does not count as implementing it.

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

## Implementation TODO — active work on 2026-10-13

This checklist records code and verification progress, not issue closure. The
original acceptance criteria remain in Beads; completion requires cited proof.
The operator requested this detailed TODO and the full reality-check workflow.
Retire this active checklist into historical evidence when the bridge epic is
qualified; do not create a competing plan.

- `.1` authentication and ownership:
  - [x] Validate active database keys before protected-route admission; support
    configured headers and CLI bearer credentials, revocation and admin roles.
  - [x] Persist job/crawl ownership; isolate profiles, cache, listings, artifacts
    and embedding surfaces; reject traversal, unsafe profile IDs and symlinks.
  - [x] Consume quotas atomically in shared SQLite; assert 429/Retry-After.
  - [x] Exercise actual browser sign-in/out against TCP API and SQLite, using
    cached fixture text; check HttpOnly cookies, foreign-owner denial and no
    credentials in DOM/localStorage/sessionStorage. No hosted OCR claim.
  - [x] Wire all 43 formerly raw CLI HTTP clients to the credential-scoped
    factory; actual TCP/SQLite commands cover listing, admin, sections, search
    and embedding, plus missing/revoked keys and cross-origin/redirect denial.
    Persisted structured results now hydrate through the durable lookup.
  - [x] Enforce the outbound network trust boundary (`.33`) at actual browser
    and webhook connections, with explicit server-owned grants and numeric
    peer pinning. Exact-revision qualification remains open under `.33`.
  - [ ] Bind final strict checks and fresh acceptance review to the committed
    revision before closure; independent public proof remains `.17`.
- `.2` safe viewers and real refresh:
  - [x] Sanitize final Marked/Prism HTML with local DOMPurify; preserve tables,
    math, highlighting and disabled task checkboxes; reject executable schemes.
  - [x] Wire dashboard SSE to the same renderer; supersede stale fetches and
    update cached navigation state; refresh/retry submit `reuse_cache=false`.
  - [x] Run actual Chrome tests for both viewers, navigation and malformed XSS;
    unsanitized negative control executes its marker and establishes the oracle.
  - [ ] Final revision review and strict checks; independent proof remains `.17`.
- `.3` actual browser identity:
  - [x] Install and launch exact CfT 130.0.6723.69 without replacing stock
    Chrome or `.env`; verify observed build and real CDP `Browser.getVersion`.
  - [x] Refuse missing/mismatched pins, unknown channels and BiDi-only requests;
    record actual channel/build/transport and requested preferences separately.
  - [x] Hash actual screenshot CSS, masks, viewport/DPR and font configuration;
    use observed build in persisted cache identity and user agent.
  - [x] Diagnose a real blank-text capture: host WOFF font aliases produced
    zero-height glyphs in CfT130. Isolate system fonts without modifying host
    configuration; require positive glyph width and black text pixels, and
    reject a deliberately empty font configuration before OCR.
  - [x] Align Node smoke with the exact executable/build and actual CDP; native
    glyph, live-motion, frozen-pixel/mask and hidden-text controls pass. Installer
    distinguishes bundled Chromium from CfT and preserves existing `.env`.
  - [ ] Prove fallback cache reuse; finish revision-bound checks and fresh
    acceptance review before closure.
- `.5` geometry:
  - [x] Uniform pyvips resize; explicit source rectangles, axis scales, DPR and
    CSS document offsets; compute actual final-tile/viewport overlap strips.
  - [x] Real patterned-pixel proof at DPR1/2/3 for wide/tall/boundary/final tiles,
    inverse mapping, full coverage and longest-side caps; width-only mutant
    rejected. Provenance distinguishes tile-pixel heights from CSS offsets.
  - [ ] Finish public highlight geometry proof and final revision checks.
- `.7` request policies and configuration:
  - [x] Load/validate declared YAML policies; reject disabled/unknown policies
    and oversized tiles; apply selected model, FP8 and concurrency limits.
  - [x] Record requested/selected/provider-reported models distinctly, persist
    request options and preserve them during replay/rerun.
  - [x] Preserve custom endpoint model identifiers, retain per-batch requested
    and served identity/request IDs, and warn on provider model substitution.
    The configured DeepInfra endpoint serves Gemma for the retired olmOCR name;
    successful content extraction does not establish olmOCR availability.
  - [x] Route embedding/semantic/queue settings through python-decouple and
    document actual variables; include semantic request options in cache identity.
  - [ ] Prove configured backend execution, failover identity and every remaining
    policy/cache/config acceptance condition. Keep disabled adapters disabled.
- `.8` lifecycle:
  - [x] Add an append-only SQLite event journal with atomic job/owner sequence
    allocation and durable owner namespaces. Runtime publishes after commit,
    with1024 pending entries,128 per-subscriber slots and500 replay entries.
  - [x] Restore identical per-job/global suffixes in fresh managers and two cold
    TCP API processes; observe another process's committed update by idle polling.
    This is real API/SQLite proof with fixture capture text, not OCR quality.
  - [x] Refuse foreign/future/expired cursors; retain cancellation across the
    terminal run/event crash window and append one explicitly labeled current
    terminal observation when its run write outlived the missing event commit.
    Concurrent recovery does not duplicate the observation or earlier stages.
  - [x] Gate publication on actual commit; reject bounded pending overflow,
    preserve missing/purged result refusal and return typed503 for journal read
    or admission failures. Four competing writers allocate48 dense sequences.
  - [x] Keep all five job/event routes404 in anonymous operator mode; fix the
    previously unhandled KeyError on `/jobs/{id}/raw`.
  - [ ] Complete final strict/native/scanner checks and losing mutants for this
    journal continuation. Current focused checkpoint:156 passes plus8 new
    recovery/fault/cold-process cases and one anonymous-route check, with source
    changes between these scoped runs; a frozen full wrapper is now running.
  - [ ] Keep failover progress, durable job admission/requeue/worker recovery,
    wider storage migration and exact-revision independent qualification open.
    Event replay does not restart interrupted capture work. Historical journal
    storage/purge policy remains `.11`; active replay remains500 entries.
  - [x] Report real capture/OCR/stitch/persistence stages and batch progress;
    connect owner-filtered bounded global and per-job subscriptions.
  - [x] Persist cancellation after allocation, prevent late success, clean tasks
    on shutdown, and hydrate terminal records after manager restart.
  - [x] Exercise allocation races/failures, cancellation swallowing, callbacks,
    stream bounds, sequence consistency, owner isolation and persisted status.
  - [x] Track/reap webhook tasks, bound shutdown, prevent cancellation-swallowing
    runners from later reporting success, and publish DONE after the terminal
    database write. Real gated-write/cancellation tests cover those boundaries.
  - [x] Drain admitted SQLite writes even after repeated cancellation, so late
    CAPTURING/DONE transactions cannot overwrite CANCELLED. A separately run
    non-draining-write mutant fails both gated-write tests; actual SQLite is used.
  - [x] Include watchdog cleanup in the shutdown deadline and cancel a timed-out
    runner before persisting FAILED, preventing a racing terminal-success write.
  - [x] Reconnect NDJSON with exact sequence cursors, include the first event in
    JSON history, recover bounded subscriber drops while history remains, and
    report an explicit 409/stream gap after expiration instead of silently losing
    events. Two per-job subscribers and the global feed agree on sequences.
  - [x] Give HTML SSE the canonical event log and exact reconnect sequences;
    honor native Last-Event-ID before the original query cursor. Commit each ID
    after the complete state/Markdown group so interrupted updates replay.
  - [x] Give global SSE owner-scoped opaque cursors and independently bounded
    500-entry owner histories; recover 150 queued updates and explicitly refuse
    650-entry overflow, foreign/future/malformed/restarted cursors. Heartbeats
    never advance cursors and subscriber cleanup runs on disconnect.
  - [x] Reconnect the dashboard NDJSON panel by sequence; ignore duplicates,
    recover HTTP409 and streamed gaps, and prevent superseded streams from
    appending to the new job. Real browser proof uses identical timestamps,
    a future heartbeat, both gap forms and a duplicate successor.
  - [x] Prove two SSE subscribers plus the global feed agree through the actual
    authenticated TCP API, HttpOnly browser session and SQLite; both render the
    persisted Markdown before cursor commit. Fixture text is not OCR evidence.
  - [x] Escape provider error HTML and prefix every multiline payload as SSE
    data, preventing error text from manufacturing protocol fields.
  - [x] Offload ordered DOM/tile/manifest/Markdown/link persistence from the
    API event loop and drain admitted writes before terminal cancellation.
    Keep CANCELLED even when that writer subsequently fails; its failure is
    retained as the cancellation cause. Four real SQLite/filesystem barrier
    cases pass, and inline-write/old-cancellation-error mutants fail.
  - [ ] Complete failover progress and qualify persistent restart replay, then bind
    all original lifecycle criteria to fresh revision-bound evidence. Thread
    writes cannot be forcibly stopped; a shutdown deadline reports pending work
    honestly. Durable queue remains `.9`; wider storage/embedder offloading and
    async-session migration remain `.15`. No acceptance conditions were moved.
  - [x] Finish strict/native verification at source inventory
    `d520bcf0278dbd2d359a8da94cd22c3c227fa91e91b52262b178b04bc6daeb08`;
    148 runtime/test/config inputs, six changed since the previous verified set.
    Full wrapper:803 passed/1 explicit generated-quality skip/11 warnings in
    1037.86s, with Ruff/format/type/native preflight green. Both native Node smoke
    tests pass against the owned TCP API/isolated database/cache; that server is
    stopped. Post-verification input rehash reports zero changed inputs.
    Reports: `lifecycle-sse-full.xml`, `lifecycle-sse-full-summary.json` and
    `lifecycle-node-smoke-20261014`, under `/tmp/mdwb-reality-20261013/`.
    This identifies uncommitted bytes, not an exact-commit qualification.
  - [x] Exercise the new persistence path through actual CfT130/CDP and the
    configured provider: job `eb00b0db803b4a58ae0e122f45555ceb`, two sweeps,
    all authored Invoice/7319/42/8831 markers retained, served Gemma, request IDs
    `RrDteZlnagDCWnbZp1dgPh9H` and `RmkBBbKF2B1ri9D7zPQn2O8r`.
    Receipt: `lifecycle-artifact-provider-20261014/candidate-evidence.json`.
    This is actual configured-provider execution, not approved corpus quality
    or olmOCR availability; baseline_approved remains false.
  - [ ] Resolve the mandatory Python scanner gate and perform original-criteria
    independent/revision-bound qualification. Latest complete Python scan:
    five changed Python inputs,13 critical/16 warnings, exit1. Complete JS scan:
    one changed input, zero critical/78 warnings, exit0. These denominators differ
    from the earlier ten-file scan; the results are not a before/after metric.
    Reports: `lifecycle-ubs-python.json` and `lifecycle-ubs-js-valid.json`.
    Explicit shell cleanup retention changes no detector rule/severity; no gate
    suppression, waiver, implementation commit or issue closure was introduced.
- `.13` executable verification:
  - [x] Repair concrete type contracts instead of suppressing diagnostics;
    replace empty capture/vector/small-E2E placeholders with executable tests.
  - [x] Run Python 3.13 exclusively; declare the used YAML dependency directly.
  - [x] Replace the invalid Python Playwright `test` command and warning/skip
    fallback with full pytest, native browser tests and required preflight.
  - [x] Retain generated diff tolerances, strict oracles and original criteria;
    refuse synthetic smoke publication and missing/invalid production metrics.
  - [x] Wire CI to the same full local gate with native dependencies and exact
    CfT archive digest/observed build; workflow syntax passes. GitHub execution
    remains unverified.
  - [x] Preserve pytest artifacts in a unique base for every invocation; reject
    an existing `--basetemp` before pytest's automatic deletion. Two real runs
    preserve a sentinel; existing-path negative control returns exit4.
  - [x] Fresh peer mutants expose and correct false-passing ownership and overlap
    hash oracles; signature tampering now changes signed bytes, not padding bits.
  - [x] Reopen the old generated-E2E issue `markdown_web_browser-hki` with an
    incident comment: the present text-file comparator performs no fresh capture/
    OCR/SSIM and the configured-corpus case is skipped. Preserve its original
    criteria; `.13` now depends on that original repair rather than accepting its
    unsupported closed status.
  - [x] Publish final required-check/scanner scopes and failures honestly, without
    treating the skipped generated test or partial vendor scan as green.
  - [ ] Restore genuinely fresh generated candidates and the original 2% guard;
    resolve the required scanner gate and bind acceptance to the final revision.
- Original `hki` generated-E2E task — continuation:
  - [x] Claim the reopened original task; preserve its scope and2% maximum.
  - [x] Replace saved candidate paths with a fresh real capture/OCR/stitch/Store
    invocation and disable cache reuse. Retain candidates instead of modifying
    expected artifacts; use python-decouple for the explicit opt-in flag.
  - [x] Reject missing/empty enabled cases, invalid/widened budgets, altered or
    absent baseline files, undeclared candidates and cache overrides.
  - [x] Require actual CfT/CDP/model/context identity, stages, timings, request
    IDs and scrolling; compare tile counts/geometry and real pyvips SSIM.
  - [x] Add analytic SSIM positive/damage controls, tiny-strip/geometry tests,
    real fixture-listener proof and baseline/refusal controls. Initial focused
    run:18 passed and1 explicitly disabled provider case (5.62s).
  - [x] Mirror flag/case configuration in `.env.example`/README/config docs and
    run the enabled suite once within the full wrapper; no duplicate E2E pass.
  - [x] Execute the known prior fixture through real configured OCR and retain
    candidate evidence. Known invoice text and PNGs survive (SSIM1.0 each);
    qualification refuses this short fixture's missing actual scrolling.
  - [x] Detect a real guard defect: fresh job IDs in provenance affect the raw
    diff. Validate ownership and neutralize only the execution ID there; text,
    geometry/hash edits, body links, fenced code and forged IDs remain blocked.
    Unit controls publish both the newly admitted valid case and retained losses.
  - [x] Add a safe candidate-generation command using the same real pipeline;
    never automatically promote output to a baseline or replace existing paths.
  - [x] Run two actual three-sweep candidates for the authored long invoice
    fixture: both recover Invoice/7319/42/bottom8831, three PNG SSIM values are
    1.0, raw Markdown difference4.298385% and nonce-neutralized difference0.210723%.
    This is same-input repeatability, not independent baseline/corpus approval.
  - [x] Full pre-egress wrapper:757 passed,1 explicitly disabled quality case,
    337.442s. Latest focused guard:22 passed,1 disabled case; late ownership
    validation/in-process CLI assertion were verified at focused scope.
  - [x] Investigate the fixed-executable CLI subprocess scanner finding; test
    the same parser/refusals in-process, retaining both negative conditions.
    Subsequent scan has0 critical/5 warnings but exit1 under a cleanup-retention
    wrapper; do not call that gate green. New broader scan remains pending.
  - [ ] Execute configured-provider cases against immutable independent expected
    outputs; publish any failure without refreshing the expected output.
  - [ ] Verify strict whole-suite checks, scanner scope and fresh original-criteria
    review. No generated corpus/quality qualification or task closure yet.
- `.33` outbound network boundary — active continuation:
  - [x] Claim the next ready blocker and reserve the exact runtime/test surface.
  - [x] Implement a fixed, connection-pinned proxy: resolve once, validate all
    DNS answers, connect to a numeric address and check the observed peer.
  - [x] Bind private CIDR grants to exact server-owned owner identities; local
    anonymous grants require an explicit flag and authentication disabled.
    Metadata, link-local, mapped IPv6 and transition addresses stay refused.
  - [x] Wire capture, redirects/subresources/WebSockets and both webhook senders;
    remove implicit Chrome loopback bypass and QUIC, refuse webhook redirects,
    ignore ambient proxies and use the stored job owner for delivery grants.
  - [x] Record destination-free policy/denial evidence and include policy in cache
    identity. Convert native capture fixtures to actual TCP with explicit grants.
  - [x] Exercise the real public HTTP/TLS route with no private grant: example.com
    returns200/known title through the proxy (one connection, no denial). This
    proves public transport capability only, not OCR/corpus quality.
  - [x] Finish scoped positive/negative real IPv4/IPv6, mixed DNS/rebinding, browser
    redirect/subresource/WebSocket, webhook/signature/owner and cache controls.
    First probes exposed assertion/CORS/empty-denial setup issues; retain red
    reports. HTTPX emitted an invalid unbracketed IPv6 proxy target; webhook
    delivery now uses HTTPcore's public network backend and shared connection
    validation directly. Authorized actual IPv6 delivery succeeds, ordinary
    ownership refuses; no compatibility parser or relaxed URL validation.
   33 focused network controls and the repaired live OCR test pass (34 total).
  - [x] Run safe removing-enforcement, DNS-re-resolution and shutdown-order
    mutants against controlled listeners: each fails for its planted defect;
    no live metadata or unrelated private service probes.
  - [x] Fix the observed Class2 async shutdown cycle: cancel/close client handlers
    before Python3.13 server wait_closed, also reject delayed accept callbacks.
    Regression covers1 and10 open clients plus a delayed callback; the old order
    times out. Repository wait_closed/start_server search finds only this runtime
    server; controlled test listeners close finite handlers/client sockets before
    leaving their server scopes. No global concurrency-clean claim.
  - [x] Prove public native capture (CfT130/CDP, two actual opened connections,
    preserved DOM/PNGs), and direct public backend TLS/HTTP with certificate
    verification and original example.com SNI. No external public webhook POST
    or rolling production qualification claim.
  - [x] Repair the inherited live full-page OCR fixture after its required full
    run fails at Page.captureScreenshot:492 passed/1 failed/1 skipped. Use the
    supported viewport/CfT/pyvips/public-egress path and retain input, manifest,
    requested/served model and request IDs. Actual inspected input shows an icon
    and multilingual paragraphs; the HTML title is absent from the image. The
    OCR oracle now checks the visible source DOM paragraph, with empty/title-only/
    altered-text losses. No baseline regeneration or2% budget change.
  - [x] Declare HTTPcore asyncio/HTTP2 usage directly and update the uv lock;
    mirror grants in environment/config docs and correct invalid CfT setup prose.
  - [x] Publish scanner result:10-file Python scan exits1,9 critical/37 warnings;
    source taint findings and controlled test findings remain separately reviewed.
    Scanner cleanup requests are retained by an explicit shell cleanup no-op;
    detector rules/severity/timeouts are unchanged. This is not a green gate.
  - [ ] Complete final full strict/native/scanner checks and original-criteria
    review; capture/egress changes are uncommitted and unqualified.
  - [x] Final unchanged runtime/test input set: Ruff/type/preflight and792-test
    full wrapper finish with791 passed/1 explicitly disabled generated-quality
    case/11 warnings (633.20s). Both native Node smoke tests pass against the
    owned API/isolated SQLite/cache; the default-port failure remains retained.
    Scanner9-critical/37-warning result, corpus approval and exact-revision fresh
    acceptance remain open. No implementation commit or Beads closure.
- Partial repairs under `.11` and `.12`: actual persisted artifact inventory,
  read-path confinement, shared-root purge refusal, default file retention,
  vector validation/replacement and deterministic equal-score ordering exist.
  Complete layout/retention/races/automatic indexing/dimension contracts and their
  public proofs before closing those issues.
- `.14` partial installer repair:
  - [x] Preserve existing `.env`/symlinks, create new configuration exclusively,
    retain Python3.13/locked uv, provide truthful pinned-browser guidance and
    fail missing required preflights. Installer was not executed.
  - [x] Replace unverified uv installer piping with a versioned official script
    and a reviewed fixed SHA256 before shell execution; retain download artifacts.
    Real official bytes verify; altered bytes refuse. A download-boundary test
    checks refusal before untrusted code can run; cold installation is unqualified.
  - [ ] Complete fresh installation, deployment, durable-worker and service
    configuration acceptance; no source-only installer readiness claim.
- Remaining core work, in dependency order: [ ] `.3` browser alignment/qualification;
  [ ] `.5` public highlight proof; [ ] `.4` stabilization; [ ] `.6` OCR/output
  fidelity; [ ] `.9` durable bounded queue; [ ] `.10` cache freshness/invalidation;
  [ ] `.11` artifacts; [ ] `.12` retrieval; [ ] `.15` off-loop storage;
  [ ] `.14` install/deploy; [ ] `.17` authenticated public proof;
  [ ] `.18` capture/fidelity proof; [ ] `.19` restart/cache/artifact proof;
  [ ] `.20` retrieval/CLI/crawl proof; [ ] `.16` complete executable docs;
  [ ] `.21` live corpus/rolling readiness; [ ] `.22` core qualification.
- Additional vision: [ ] `.23` verified upstream capabilities; [ ] `.24`
  Asupersync runtime/IPC; [ ] `.25` focr; [ ] `.26` FrankenSQLite;
  [ ] `.27` Frankensearch; [ ] `.28` fmd HTML/PDF; [ ] `.30` agent contracts;
  [ ] `.29` TUI; [ ] `.31` integrated Franken qualification.
  `.32` optional extensions remains deferred. The epic remains open.
  [ ] `.33` connection-boundary egress policy is newly covered by a self-contained
  security bead and polishing comment; `.1` is blocked on it. No scope was moved
  out of an original issue to make that issue closable.

**Continuation verification, not qualification:** retained full evidence is
`/tmp/mdwb-reality-20261013/egress-verified-full.xml` and its matching summary:
791 passed/1 explicit generated-quality skip/11 warnings, with Ruff/format/type
and actual native preflight green. Native Node smoke has2 passes against the
owned API, and focused network/live-OCR controls have34 passes. The148-input
digest above remained unchanged after both suites; HEAD is still `8e2f030` with
uncommitted implementation. Initial failing/aborted checks and planted mutants
remain retained separately. No independent approved generated corpus, actual
olmOCR availability, public webhook POST or exact-revision acceptance is claimed.
Nightly48-hour green history, weekly latency budgets, generated quality and
provider utilization below80% are unverified readiness conditions. The remaining
durable queue, fidelity/cache/artifact/retrieval/storage/install/Franken work stays
in the dependency graph rather than being silently waived.

**Earlier implementation evidence, not qualification:** the preceding full required-check
run executed 729 tests: **728 passed, one generated-quality test skipped**,
11 warnings, 543.00s. Ruff/format/ty and actual CfT130/CDP/libvips preflight
passed. Reports: `/tmp/mdwb-reality-20261013/final-python.xml` and
`final-python-summary.json`. Afterwards the overlap hash oracle was strengthened;
22 focused geometry/font tests passed in 132.91s and the missing-hash mutant was
rejected. The no-op ownership-grant mutant also fails the reopened-store oracle.
Later installer changes passed51 focused agent-script tests (16.76s), Bash syntax/
shellcheck/help, official-checksum positive and altered-bytes refusal controls.
Lifecycle/cursor changes passed41 focused tests (43.95s); authenticated cursor
routes passed their own real SQLite/FastAPI check (13.71s). The subsequent whole
required gate finished with **736 passed, one generated-quality test skipped**,
11 warnings, 1023.36s, across737 collected tests, including the latest watchdog
test. Final Ruff/format/ty and real browser/libvips preflight passed. Reports:
`/tmp/mdwb-reality-20261013/lifecycle-final-python.xml` and
`lifecycle-final-python-summary.json`. This is working-tree implementation
evidence; it does not turn the skipped generated-quality gate green or establish
an exact committed revision, independent qualification or a performance win.

The 87-path UBS union returned **partial, exit2**: Python66files/33critical/
139warnings/1702info; Bash2files/2critical/3warnings; JavaScript timed out at300s.
The two Bash critical findings were unverified installer piping and are repaired;
a subsequent final Bash scan has zero critical findings/one warning across two
files. A separate scan of all five
handwritten JS/TS files completed with zero critical, 165 warnings and 395 info
findings. Vendor scanning is separate and timed out again at600s (exit2, four
requested vendor assets, zero completed files); it remains unverified. A bounded
read-only diagnostic found the64 regex patterns completed promptly on each asset;
this narrows the timeout investigation and supplies no scanner-gate substitute.
These are distinct
scopes, not a green union. No suppression, partial-result override or baseline
waiver has been used. Precise reviewed false positives include bound SQL hash
lookup, comparisons with the public insecure-secret sentinel, server-generated
numeric quota headers and fixture-only credentials; the network-egress gap is
real and remains `.33`. Implementation changes remain uncommitted while this
required scanner gate and final acceptance remain unresolved.

The later four-file Python lifecycle scan returned exit1 with14 critical,
10 warning and372 info findings. Its snapshot predates the final watchdog and
test typing edits; it is neither an all-files scan nor final-revision proof.
The epic remains open: its in-progress claim was refused by `.22`/`.31` blockers;
the graph gate was respected. The reopened original generated-E2E task remains
open too. No repaired issue was closed in this implementation block.
Final triage explicitly reads `.beads/issues.jsonl` as authority. A real ready-
work check exposed a bad new `.33` parent-child edge: it waited for its own epic.
That containment edge was removed to match the original numbered-task structure;
authentication still depends on `.33` and epic qualification still depends on
authentication. No acceptance gate or criterion was removed. The final graph is
cycle-free; the ready network and generated-E2E tasks are real remaining work.

The first actual authenticated browser/API/SQLite/CfT130/hosted-OCR probe failed
known-text assertions because screenshots were blank. Artifacts remain under
`/tmp/mdwb-reality-20261013/public-live-1d04df3b661942e09525d2a64375de7f`.
After the font repair the repeat probe returned and rendered Invoice 42,
Widgets 7319 and the quantity 42; job `24e0820f4bb041dbbce43d9ea12fd56f`.
Artifacts and `proof.json` remain under
`/tmp/mdwb-reality-20261013/public-live-342997dbe23543bdaedd808222916db9`.
Observed metadata: CfT130.0.6723.69, CDP, Playwright1.57.0, two tiles, zero
shrink retries, screenshot digest
`f65ab724238bea116dd3db6b195b6d443fdb8c6b3381a9735fa47ff441ec5b8b`.
Request IDs: `Rem8dyjMzapXKkZyZd5qhstX`, `R9wdNrp1RoBFOoFAZK7yEbJB`.
Provider-reported model: `google/gemma-4-31B-it`, requested olmOCR FP8.
This one known local page proves a positive public path in the working tree;
it does not establish production corpus accuracy, protected-site success,
olmOCR service availability, paired performance, release or exact-commit proof.

**Node evidence:** isolated Playwright1.57 smoke passed two tests; the final
enhanced canary passed with actual CfT130/CDP, rendered glyph ink, observed
animation motion, equal frozen screenshots under masks and a differing hidden-
text counterfactual. Retained PNG:
`/tmp/mdwb-reality-20261013/node-final.K0tHZg/results/` (`frozen-viewport.png`,
SHA256 `04e06833bd1d45810a00018b316dfc64a7a33255f81e864c9a36c3780ab822f2`).
Missing executable, mismatched build and BiDi-only configurations were rejected.

### Root anti-ceremony inventory — implementation window 2026-10-13

Current journal continuation integrity control: the operator and `.8` acceptance
review consume the minimal source input receipt; it gates citing full checks for
the actual uncommitted bytes. Prior scoped checks preceded later recovery/error
changes, so stale proof subject is an observed risk. Retire its active role after
committed/reviewed qualification, retaining historical evidence. HEAD alone and
tracked-only diffs miss these inputs;148 path/digest pairs are the smallest
existing reliable control. The journal/cursor tables themselves are FEATURE:
API/UI runtime branches on committed rows. No new process framework is needed.

**Later lifecycle continuation (UTC2026-10-14):** bounded to the six runtime/test
inputs changed since the preceding148-input receipt, the actual commands below
and the unchanged Git head. Zero implementation commits, zero closures and no
new agents. Running USER changes are reconnectable SSE, exact dashboard replay
and responsive/cancellation-ordered artifact persistence. ENABLER work is the
new positive/negative controls and required checks. The requested TODO/Beads
update is PROCESS and earns no capability credit. Verdict: HEALTHY for this
bounded implementation block, with no shipment or qualification claim.

Process worksheet: runtime cursor/history/writer changes are FEATURE because
API/UI execution consumes them. For the requested TODO, the consumer is the
operator; it gates losing track of the explicitly requested complete backlog;
the observed defects are missing replay and blocking artifact writes; retire
the active checklist at bridge qualification. The minimal source receipt prevents
claiming old green checks for new uncommitted bytes (integrity-controlQ1); the
shared dirty tree makes it necessary (Q2);148 path/digest pairs are sufficient
(Q3); HEAD or tracked-only diffs omit changed/untracked inputs (no cheaper Q4).
More speculative process artifacts were not created. The best current USER item
is existing `.8`; another hour of product work is preferable to audit expansion.

Real-work worksheet: nothing was committed/released in this window; the
two-minute working-tree demo is two authenticated SSE viewers agreeing with the
global feed and SQLite Markdown. Omitting PROCESS updates changes no running
capability but loses the requested work inventory. Tests consumed the enablers
through actual TCP/browser/SQLite and deliberately broken runtime functions.
Old generated-quality `hki` remains open because no independently approved
corpus exists; lifecycle work improves a separately observed runtime defect.
There is no current swarm/high-close pane, follow-up issue or spec-edit stand-in.

Honesty inventory, current bounded window:
1. No weakening (checked: six changed inputs, unchanged strict wrapper/config,
   retained failure reports and original Beads criteria). Existing NDJSON tests
   now supply the ASGI receive channel needed by real disconnect handling.
2. Controlled stream/persistence fixtures were added, explicitly scoped to
   protocol/race behavior; actual SQLite/files and losing mutants exercise the
   boundary. They are not capture/provider quality substitutes.
3. No golden regeneration (checked: no baseline/golden changed; new native
   provider receipt remains baseline_approved=false).
4. Tests changed with the runtime; no gate/config/suppression/tolerance was
   changed (checked: required wrapper still runs the complete tests directory).
5. No release-gate shortcut (checked: different scanner denominators are stated,
   the mandatory Python scanner remains red and the work remains uncommitted).
6. No zero-run green (checked: initial wrong UBS language flag exits3 with zero
   files and supplies no proof; corrected JS scan completes one file).
7. No unobserved execution claim (checked: full suite remains pending here;
   observed targeted/TCP/native/provider results are reported separately).
8. No fixture-as-live/model inflation (checked: browser/TCP text is a fixture;
   configured provider actually serves Gemma, not claimed olmOCR).
9. Initial request-fixture/hidden-control failures and type diagnostics remain
   retained. One combined diagnostic command ended0 because Ruff followed a red
   ty result; it was not a valid combined gate. The actual nullable type was
   fixed, standalone ty passed and the strict wrapper uses set-e.
10. No stderr suppression (checked: retained command outputs and scanner JSON).
11. No incomplete closure (checked: live Beads; zero closes in this window).
12. No goalpost edit (checked: canonical plan/original acceptance unchanged;
    API documentation describes implemented cursor behavior).
13–17. No current delegation or correlated peer proof (checked: six changed
       input paths are root-owned; no new agent dispatch or peer close).
18. No post-result performance denominator (checked: no speedup claim; complete
    collected test count and scoped scanner file counts are explicit).
19. Explain before replay: direct-file candidate invocation lacked the package
    import path; the documented module invocation succeeds. Node dependencies
    were absent from the old temporary tool environment and were restored there,
    with project assertions unchanged. CASS history search timed out after120s,
    so this is not a clean-history verdict. Framework-managed temporary cleanup
    is not universally proven retained; the earlier disclosure below still holds.
20. Strongest current proof: authenticated TCP/browser/SQLite feed agreement,
    real configured-provider output and two failed artifact-write mutants;
    exact-commit independent qualification is still unavailable.
Disposition: bounded checks found no false closure or gate waiver. Initial
failures remain on record, invalid diagnostic/zero-file attempts earn no green
credit (RH-1/RH-2), and working-tree evidence never becomes independent release
qualification (RH-9). This inventory retires with the session reporting window.

End-of-window addendum: the full804-case wrapper actually completes with803
passes/one explicit generated-quality skip/11 warnings, and both native Node
tests pass. Post-check rehash of all148 inputs remains unchanged. Python UBS
still exits1 (13 critical/16 warnings on five files); JS exits0 (zero critical/
78 warnings on one file). The scanner reports contain HEAD permalinks that do
not identify these uncommitted bytes; the explicit source inventory does.
For committed/closed items the worksheet tally is USER0/ENABLER0/PROCESS0/
UNKNOWN0, because no implementation was committed or closed. Working-tree
capabilities and their scoped tests are described separately above. `.8` stays
in_progress with its original unmet conditions; no shipping claim is implied.

Earlier guard/network continuation inventory: `hki` and `.33` are in_progress; that continuation has
zero implementation commits and zero closures. Fresh pipeline guard/test tools
are ENABLER work consumed by actual three-sweep configured-provider runs. The
connection boundary, owner grants, webhook backend and shutdown repair are USER
work with actual listener/browser/provider evidence. This requested TODO/Beads
record remains PROCESS and earns no product-completion credit. Runtime/test/input
inventory digest is `e91bb3569f415fc0d63c6cab6914e444e683a70ac8c95a864a3bcba2fcd2b4a4`
(148 inputs); it binds retained working-tree evidence and does not replace an
exact committed revision or independent review.

Gate-change review preserves the original2% generated maximum and0.99 image
threshold. Ownership-neutralized execution IDs admit identical fresh output;
content/hash/geometry/body/fence changes and forged IDs remain losses. The live
OCR smoke's visible-DOM oracle replaces a demonstrably absent title heading:
the inspected PNG contains the exact English/multilingual paragraphs, which the
provider extracted. Empty, title-only and altered English outputs fail. The
first mistaken title assertion and earlier full-page screenshot failure remain
retained; no golden was refreshed. The ordinary Python generated-quality skip
is still an unmet opt-in corpus gate. Scanner9 critical/37 warnings remains red;
no suppression, partial waiver or release claim was introduced.

The native Node capture control passes, but the first UI invocation used the
config's default localhost8000 and reached another project's Swagger page.
That failed UI assertion is retained; it is not markdown_web_browser evidence.
An owned ephemeral API and isolated cache/database are now used for the full
Node smoke. One initial server used an ignored DB_PATH variable and was stopped;
the final server verifies the actual RUNS_DB_PATH/cache before testing. These
corrections do not change the source assertions or count the failed invocations
as passes. The shell guard also misclassified a Python socket-close mutant as
machine shutdown; the blocked command performed no system action. A plainly
named Python-only mutant then ran and timed out as expected. No guard permission
or suppression was requested.

This is the operator-requested status/TODO artifact, not a capability. Its
consumer is the project owner who requested the complete reality check and
granular TODO; it gates that requested review and prevents known false-green
capture/type/quality claims from being used as closure evidence. Retire the
active checklist and this inventory after bridge qualification; retain historical
evidence, and never delete it without separate explicit authorization. No
additional recurring report or governance loop was created. Time on the next
ready runtime repair has more user value than expanding this artifact.

Mechanical record: current implementation has zero commits, zero Beads closures
and one evidence-based reopening (`hki`); HEAD remains `8e2f030`. The preceding audit commit is PROCESS. Dirty
runtime/UI/CLI/installer changes are USER work in progress; executable tests,
CI/type repairs and evidence-integrity guards are ENABLER work; this requested
audit/Beads graph is PROCESS. No shipped-feature count or completion percentage
is inferred from these classifications.

Real-work questions:
1. No implementation has been committed/qualified in this window. The strongest
   two-minute local demonstration is authenticated browser sign-in followed by
   known-page capture/OCR/rendering, plus persisted CLI sections after restart.
2. Without the requested audit/graph, the code outcome could still exist, but the
   owner would lack the requested complete gap inventory and work order. Those
   artifacts earn no runtime capability credit.
3. Enablers have consumers: the full check exercised native capture, UI, SQLite,
   vectors and CLI; the live probe exposed and then verified the font repair.
   New runtime/Franken infrastructure was not built speculatively.
4. Durable queue/restart and faithful OCR output remain user-visible open work.
   Authentication, unsafe rendering, wrong browser identity and broken geometry
   were repaired first because they blocked trustworthy first use.
5. All helpers produced zero closes. Their type/test, CLI and browser/CI changes
   are counted only at their demonstrated scope; peer agreement is not quality
   or release qualification.
6. No original issue was closed by moving its unmet conditions to a follow-up.
   The new egress issue adds a discovered boundary and blocks authentication.
Verdict: product repair dominates this block, but shipment is unqualified. Stop
expanding review machinery; resolve the red runtime/scanner/quality gates and
continue through the existing dependency graph.

Honesty inventory (bounded to the working diff, observed commands and peer
reports, not an assertion about all historical sessions):
1. **Yes:** inherited physical-purge tests now use a recording destructive
   boundary. They prove dispatch/refusal/counts, not physical removal; original
   artifact qualification remains open. No tolerance was widened for green.
2. **No (checked: fixture purposes, real TCP/SQLite/browser/image tests):**
   controlled OCR/API fixtures are labelled and never substituted for live OCR.
3. **No (checked: tracked diff and golden/snapshot paths):** no golden was
   regenerated to match current output.
4. **Yes:** checks/config/test oracles changed alongside implementation. A
   separate peer pass found non-discriminating ownership/hash controls and
   incorrect transport/padding expectations; corrected controls reject planted
   mutants. No blanket lint/UBS disable or bypass was introduced.
5. **No (checked: runtime capture/policy/cache branches and proof subjects):**
   no production fixture-name exception was added; no performance win is claimed.
6. **Yes:** generated quality has no configured corpus and reports a skip with
   exit0. This remains an explicit unmet gate, never a release-green result.
7. **No (checked: command outputs, retained reports and rejected commands):**
   only observed executions are credited. Installer/GitHub/production runs are
   explicitly unperformed; peer probe wrapper failure is preserved.
8. **No (checked: proof labels in this audit):** fixture, local-component, live
   provider and rolling production evidence are kept distinct.
9. **Yes:** earlier temporary-file removal and automatic retention were
   overlooked. They are disclosed below; cleanup tests and retained pytest roots
   correct recurrence. First blank live capture and served-model mismatch remain
   visible; a successful repeat does not erase the first failure.
10. **No (checked: cited test/scanner command streams and retained reports):**
    stderr failures were not discarded to produce evidence.
11. **No (checked: live Beads statuses/comments and closure list):** no repair
    issue was closed or certified complete before its original criteria passed.
12. **No (checked: original acceptance text and unchanged quality/latency goals):**
    README qualification notes correct current claims without lowering the vision.
13. **No (checked: helper handoffs and live tracker):** helpers did not mutate or
    close Beads, and root has made no implementation closure. Root reopened the
    unsupported generated-E2E closure with the actual missing positive conditions.
14. **Yes:** early mechanical type tasks were less explicit about positive and
    planted-negative observables than later runtime assignments. Reports remain
    enabler evidence; later delegation named positive paths, negative controls,
    owned files and No-Claim boundaries.
15. **Yes:** some initial helper handoffs preceded complete root diff inspection.
    Root subsequently reran the full suite and reviewed source/test/gate changes;
    this is not a claim that every historical diff or acceptance condition has
    been independently verified. Unqualified items remain open.
16. **No (checked: zero closes plus positive browser/CLI/geometry tests):**
    refusal tests were paired with positive behavior; refusal-only work stays open.
17. **No (checked: evidence descriptions and source-sharing scope):** peer
    agreement is not presented as independent production qualification.
18. **No (checked: preselected suites, planned mutants and reported counts):**
    full-suite, focused, scanner and live-page denominators are stated separately.
19. **Yes:** the owner should see the temporary deletion, false-green oracles,
    uncommitted state and remaining quality/scanner failures before any success
    declaration. Those facts are stated here and in the handoff.
20. Strongest positive evidence is the retained actual authenticated browser/TCP
    API/SQLite/CfT130/hosted-provider run with known invoice text and request IDs.
    A skeptic can rerun the retained probe; it still does not verify olmOCR,
    production accuracy or the uncommitted final revision.

Older-session CASS check is incomplete: health reported a stale usable index and
quarantined sessions; six workspace-scoped searches hit the explicit15s cap.
Timeouts/empty output do not establish a clean history. No unrelated archive
rebuild or deletion was attempted.

Disposition: corrected and disclosed the identified oracle/retention defects;
temporary removals cannot be undone. Countermeasures name RH-1/RH-13 (separate
gate/suppression review), RH-2 (proof boundaries), RH-5 (positive independent
oracles and mutants), RH-9 (unmet original criteria stay open) and SM-11 (retain
first failures). Missing quality, egress and exact-revision scanner evidence are
uncorrected open work, with no completion or release claim.

**Test-operation disclosure:** earlier runs of inherited tests physically deleted
temporary purge-fixture folders and generated smoke-pointer files. This violated
the no-file-deletion instruction and cannot be undone. No tracked source was
deleted. Cleanup tests now record the destructive boundary and assert retained
bytes; missing-file cases omit the files at construction. Purge dispatch proof
does not establish actual filesystem deletion. The generated quality guard is
still skipped; passing the rest of pytest does not qualify release readiness.
Earlier pytest invocations used its default rolling retention before root
noticed that automatic pruning. Root cannot reconstruct every removed temporary
artifact. Later runs use unique retained roots and refuse existing basetemps.
Earlier Node runs used distinct descriptive output names without proving prior
nonexistence; the final run uses a guaranteed fresh parent and preserved output.
No session-wide claim that framework-managed temporary artifacts were untouched
is made. Scanner source inspection also found internal temporary cleanup; root
did not establish retention for every scanner-owned temporary directory. Tracked
source and the existing `.env` were preserved.

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
