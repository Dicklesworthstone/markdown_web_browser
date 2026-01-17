RULE NUMBER 1 (NEVER EVER EVER FORGET THIS RULE!!!): YOU ARE NEVER ALLOWED TO DELETE A FILE WITHOUT EXPRESS PERMISSION FROM ME OR A DIRECT COMMAND FROM ME. EVEN A NEW FILE THAT YOU YOURSELF CREATED, SUCH AS A TEST CODE FILE. YOU HAVE A HORRIBLE TRACK RECORD OF DELETING CRITICALLY IMPORTANT FILES OR OTHERWISE THROWING AWAY TONS OF EXPENSIVE WORK THAT I THEN NEED TO PAY TO REPRODUCE. AS A RESULT, YOU HAVE PERMANENTLY LOST ANY AND ALL RIGHTS TO DETERMINE THAT A FILE OR FOLDER SHOULD BE DELETED. YOU MUST **ALWAYS** ASK AND *RECEIVE* CLEAR, WRITTEN PERMISSION FROM ME BEFORE EVER EVEN THINKING OF DELETING A FILE OR FOLDER OF ANY KIND!!!

---

## 1. Irreversible git & filesystem actions (DO-NOT-EVER BREAK GLASS)

1. **Absolutely forbidden commands:** `git reset --hard`, `git clean -fd`, `rm -rf`, or any other destructive command **must never be run** unless the user explicitly provides the exact command and confirms the irreversible consequences in the same message.
2. **No guessing:** If you are not 100% certain what a command will delete/overwrite, stop and ask. “Pretty sure” is a failure.
3. **Safer alternatives first:** prefer `git status`, `git diff`, `git stash`, copies/backups, or manual edits over destructive shortcuts.
4. **Mandatory explicit plan:** even after explicit user approval, restate the destructive command verbatim, list what will change, and wait for a confirmation before executing.
5. **Document everything:** if a destructive command is ever run (with permission), log the authorizing quote, the exact command, and the timestamp in your response. If the log is missing, the action did not happen.

---

## 2. Toolchain, runtime, and packaging

- We use `uv` for everything (env management, installs, running tools). **Never** call `pip`, `poetry`, or ad-hoc `python -m venv`.
- Target runtime is **Python 3.13 only**, matching `PLAN_TO_IMPLEMENT_MARKDOWN_WEB_BROWSER_PROJECT.md`. No backward-compat shims.
- Packaging lives exclusively in `pyproject.toml`; never introduce `requirements*.txt` or duplicate config files.
- Playwright must run the **Chrome for Testing** channel, pinned and recorded in `manifest.json` (see Plan §19.2 and §20.3). Record **both** the CfT label (e.g., `Stable-1`) and exact build number for every run/report.
- For hosted olmOCR operations, rely on `scripts/olmocr_cli.py` + `docs/olmocr_cli.md` (copied from `/data/projects/olmocr`). Use the CLI for batch reproductions, latency spot-checks, and env introspection (`show-env`).
- Image work happens through `pyvips`; keep Pillow only for edge formats. Tiling must honor the 1288 px longest-side policy (Plan §0, §3, §19.3).
- sqlite-vec is part of the default dependency set; keep embeddings/data files aligned with Plan §19.6 and Appendix §21.

---

## 3. Configuration & secrets

- `.env` already exists and must **never** be overwritten. Load config exclusively via `python-decouple` in this pattern:

```
from decouple import Config as DecoupleConfig, RepositoryEnv

decouple_config = DecoupleConfig(RepositoryEnv(".env"))
API_BASE_URL = decouple_config("API_BASE_URL", default="http://localhost:8007")
```

- Mirror any new env var you introduce in `.env.example` (Plan §11-12) and document it in `docs/config.md`. Manifest entries must echo effective values (CfT version, screenshot style hash, concurrency caps, etc.).
- Never call `os.getenv`, `dotenv.load_dotenv`, or read `.env` manually.

---

## 4. Database & persistence guidelines (SQLModel + SQLAlchemy async)

Do:
- Create engines with `create_async_engine()` and sessions via `async_sessionmaker(...)`; wrap usage inside `async with` so sessions close automatically.
- Await every async DB call: `await session.execute(...)`, `await session.scalars(...)`, `await session.commit()`, `await engine.dispose()`, etc.
- Keep **one** `AsyncSession` per request/task and avoid sharing across concurrent coroutines.
- Use `selectinload`/`joinedload` or `await obj.awaitable_attrs.<rel>` to avoid sync lazy loads.
- Wrap sync helpers with `await session.run_sync(...)` as needed.

Don’t:
- Reuse a single session concurrently.
- Trigger implicit lazy loads inside async code.
- Mix sync engines/drivers (e.g., psycopg2) with async sessions—use asyncpg.
- “Double-await” helper results (e.g., `result.scalars().all()` is sync after the initial await).
- Block the event loop with CPU/batch work—push it into `run_sync()` or background workers.

---

## 5. Capture, OCR, and tiling expectations (align with Plan §§3, 19)

- **Chrome for Testing + deterministic context**: `viewport=1280×2000`, `deviceScaleFactor=2`, `colorScheme="light"`, reduced motion, animations disabled. Always log CfT + Playwright versions in the manifest.
- **Viewport sweeps only**: never fall back to `full_page=True`. Scroll via the stabilization routine (scrollHeight + `IntersectionObserver`) and retry if SPA height shrinks.
- **Tiling**: keep ≈120 px overlap, enforce 1288 px longest side, store offsets/DPI/scale/hash metadata, and compute SSIM over overlaps before stitching.
- **pyvips pipeline**: slicing/resizing/PNG encode (`Q=9`, palette off, non-interlaced) must use `pyvips`; Pillow is for edge cases only.
- **OCR policies**: all models must be declared in `models.yaml` with keys like `long_side_px`, `prompt_template`, `fp8_preferred`, `max_tiles_in_flight`. Default remote model is `olmOCR-2-7B-1025-FP8`; local adapters point to vLLM/SGLang servers when `OCR_LOCAL_URL` is set.
- **Provenance**: every stitched block gets a `<!-- source: tile_i, y=..., sha256=..., scale=... -->` comment. Links Appendix comes from `links.json` (DOM harvest) and must note DOM vs OCR deltas.
- **Manifest discipline**: include CfT version, Playwright version, screenshot style hash, long-side px, concurrency thresholds, and timing metrics (`capture_ms`, `ocr_ms`, etc.) so Ops dashboards stay accurate (§20).
- **Capture metadata checklist**: every bug report, REAL/MacroBench bundle, or ops escalation must state (a) CfT label + build, (b) `browser_transport` (CDP vs BiDi), (c) Playwright version (≥1.50), (d) screenshot style hash, (e) OCR model/policy key, and (f) whether viewport sweep retries triggered. Missing data = re-run.
- **Screenshot config**: keep `playwright.config.(ts|mjs)` in sync with Plan defaults (`use.screenshot.animations="disabled"`, `caret="hide"`, shared mask selectors). Prefer `expect(page|locator).toHaveScreenshot()` with mask lists over bespoke CSS tweaks.

---

## 6. Code hygiene, edits, and verification

- **No bulk “search & replace” scripts**. Every change gets applied manually or via targeted assistants; do not run regex-based patch scripts on the repo.
- No file proliferations: modify existing modules; only create new files for genuinely new functionality (Plan §10 rationale).
- Console/log output should be informative and stylish; prefer structured logging via `structlog` with Prometheus counters where appropriate.
- When unsure about a third-party library, **search the latest documentation (mid‑2025)** before writing/rewriting code.
- After substantive Python changes, always run:
  - `ruff check --fix --unsafe-fixes`
  - `uvx ty check`
  Resolve every issue thoughtfully; do not ignore or blanket-disable diagnostics.
- After capture-facing changes, also run `uv run playwright test tests/smoke_capture.spec.ts` (or the latest smoke file) so we confirm Playwright 1.50+ screenshot APIs still freeze animations under the pinned CfT label/build and whichever transport (CDP/BiDi) we exercised.
- Before marking a capture fix “done,” run through the stabilization checklist: ensure `[aria-busy]` cleared, volatile widgets are masked in Playwright config, blocklist entries updated, and viewport sweep retries noted. Document completion in your handoff message.

---

## 7. Multi-agent coordination (MCP Agent Mail)

What it is
- Mail-like coordination via MCP tools with inbox/outbox, searchable threads, and advisory file reservations stored in Git (see `docs/architecture.md` + Plan §20-21).

How to use (same repo)
1. Call `ensure_project` with this repo’s absolute path, then `register_agent` to claim an identity.
2. Reserve files **before** editing: `file_reservation_paths(project_key, agent_name, ["app/**"], ttl_seconds=3600, exclusive=true)`.
3. Communicate in-thread: `send_message(..., thread_id="FEAT-123")`, read via `fetch_inbox`, acknowledge with `acknowledge_message`.
4. Fast reading: `resource://inbox/{Agent}?project=<abs-path>&limit=20` or `resource://thread/{id}?project=<abs-path>&include_bodies=true`.
5. Set `AGENT_NAME` in your env so hooks can block commits when exclusive reservations exist.

Cross-repo coordination
- **Shared project key**: for tightly-coupled repos, reuse the same `project_key` but reserve distinct globs (e.g., `frontend/**`, `backend/**`).
- **Separate project keys**: otherwise, register separately and use `macro_contact_handshake` / `request_contact` to exchange permissions before messaging.

Macros vs granular tools
- Macros: `macro_start_session`, `macro_prepare_thread`, `macro_file_reservation_cycle`, `macro_contact_handshake` for quick starts.
- Granular: `register_agent`, `file_reservation_paths`, `send_message`, `fetch_inbox`, `acknowledge_message`, `release_file_reservations` when you need fine control.

Common pitfalls
- “from_agent not registered”: always register before hitting Mail APIs.
- “FILE_RESERVATION_CONFLICT”: adjust patterns, wait it out, or request non-exclusive reservations.
- Auth errors: include the proper bearer/JWT token specified by the server; static bearer only works if JWT is disabled.

---

## 8. Integrating with Beads (dependency‑aware task planning)

Beads (`bd` CLI) tracks task priority/dependencies; Agent Mail handles conversations, artifacts, and reservations. Follow this split of responsibilities.

Conventions
- Treat **Beads as the single source of truth** for status and dependencies; Agent Mail is the audit trail.
- Use the Beads issue id (e.g., `bd-123`) as the Mail `thread_id` and prefix subjects with `[bd-123]`.
- Include the issue id in `file_reservation_paths(..., reason="bd-123")` and release reservations when done.

Typical flow
1. `bd ready --json` → pick the highest-priority unblocked task.
2. Reserve edit surface (`file_reservation_paths(..., ttl_seconds=3600, exclusive=true, reason="bd-123")`).
3. Announce start via Mail (`send_message(..., thread_id="bd-123", subject="[bd-123] Start: <title>", ack_required=true)`).
4. Work + update in the same thread; attach artifacts/screenshots as needed.
5. Finish: `bd close bd-123 --reason "Completed"`, release reservations, and post a final Mail summary.

Mapping cheat-sheet
- Mail thread_id ↔ `bd-###`
- Mail subject → `[bd-###] …`
- Reservation reason → `bd-###`
- Optional: include `bd-###` in commit messages for traceability.

Event mirroring & pitfalls
- If `bd update --status blocked`, send a high-importance Mail note in the matching thread with details.
- If Mail shows “ACK overdue” for a decision, label the Beads issue (e.g., `needs-ack`) or adjust its priority.
- Do **not** open/track tasks solely inside Mail; always keep Beads in sync.
- Always include the Beads id in Mail thread ids to avoid drift between systems.

---

## 9. Reference docs (project-specific)

- `PLAN_TO_IMPLEMENT_MARKDOWN_WEB_BROWSER_PROJECT.md` — canonical architecture, capture/OCR policies, Ops playbooks (§§0‑21). Read it first.
- `docs/architecture.md`, `docs/blocklist.md`, `docs/models.yaml`, `docs/config.md` — supplementary specs referenced throughout the Plan.
- `PLAN` + Ops Appendix describe CfT pinning, viewport sweeps, sqlite-vec indexing, HTMX SSE usage, and manifest requirements. When in doubt, search those sections before coding.

If the instructions above ever appear to conflict, defer to this `AGENTS.md`, then the Plan document, then the most recent user directive.

---

## 10. Production smoke coverage & latency reporting

- **Nightly smoke run**: reserve `benchmarks/production/**`, run the curated URL set from Plan §22 (via `uv run scripts/olmocr_cli.py run --workspace ... --pdf ...` or equivalent), and stash manifests + tiles under `benchmarks/production/<date>/<slug>`. Log `capture_ms`, `ocr_ms`, `stitch_ms`, tile count, CfT label/build, transport, and OCR request IDs for every URL.
- **Weekly latency report**: generate `benchmarks/production/weekly_summary.json` capturing p50/p95 per category and highlight any budget violations.
- **Regression handling**: if a URL exceeds its latency or diff budget twice in a week, update the relevant Mail thread with links to tiles, manifest, DOM snapshot, and OCR request IDs so others can reproduce; pause shipments touching capture/OCR until it’s green again.
- **Readiness check**: before handoff, confirm (a) nightly smoke green for 48 hours, (b) weekly summary within budgets, (c) generative E2E tests green, (d) hosted OCR usage <80 %. Mention these in your status update.


## Advanced `ast-grep` use (and when to keep `rg`)

**Quick run tips**

* One-off pattern: `ast-grep run -l <lang> -p '<PATTERN>' [--rewrite '<FIX>'] [PATHS]`
* Single-rule YAML: `ast-grep scan -r path/to/rule.yml [PATHS] -U`
* Speed combo: `rg -l -t ts 'Promise\.all\(' | xargs ast-grep scan -r rules/no-await-in-promise-all.yml -U`

**When to prefer `rg`:** raw text/regex hunts, giant prefilters, or non-code assets. Use it to shortlist files, then let `ast-grep` make precise matches/rewrites.

---

### 1) Ban `await` inside `Promise.all([...])` (auto-fix)

```yaml
# rules/no-await-in-promise-all.yml
id: no-await-in-promise-all
language: typescript
rule:
  pattern: await $A
  inside:
    pattern: Promise.all($_)
    stopBy:
      not: { any: [{kind: array}, {kind: arguments}] }
fix: $A
```

**Works by** matching `await $A` only when the node is inside a `Promise.all(...)` call; `stopBy` prevents the relation from leaking past the call’s array/args boundary. 

---

### 2) Imports without a file extension (flag all)

```yaml
# rules/find-import-file-without-ext.yml
id: find-import-file
language: typescript
rule:
  regex: "/[^.]+[^/]$"
  kind: string_fragment
  any:
    - inside: { stopBy: end, kind: import_statement }
    - inside:
        stopBy: end
        kind: call_expression
        has: { field: function, regex: "^import$" }
```

**Works by** finding string literal fragments used in import specifiers where the module path lacks an extension, covering both static and dynamic imports.

---

### 3) Find usages of a specifically imported symbol (code-aware grep)

```yaml
# rules/find-import-usage.yml
id: find-import-usage
language: typescript
rule:
  kind: identifier
  pattern: $MOD
  inside:
    stopBy: end
    kind: program
    has:
      kind: import_statement
      has:
        stopBy: end
        kind: import_specifier
        pattern: $MOD
```

**Works by** ensuring a `$MOD` identifier use appears in a file that **also** imports the same `$MOD`, via nested `inside/has` relations to tie usage to its import.

---

### 4) Prefer `||=` over `a = a || b` (tight codemod)

```bash
ast-grep -p '$A = $A || $B' -r '$A ||= $B' -l ts
```

**Works by** back‑referencing the same `$A` on both sides, guaranteeing you only rewrite the self‑fallback form.

---

### 5) Disallow `console` except `console.error` inside `catch` (policy)

```yaml
# rules/no-console-except-error.yml
id: no-console-except-error
language: typescript
rule:
  any:
    - pattern: console.error($$$)
      not: { inside: { kind: catch_clause, stopBy: end } }
    - pattern: console.$METHOD($$$)
constraints:
  METHOD: { regex: "log|debug|warn" }
```

**Works by** allowing `console.error` only when it’s **inside** a `catch`, and blocking `log/debug/warn` anywhere, using `any` + `constraints`.

---

### 6) React/TSX: replace `cond && <JSX/>` with ternary (auto-fix)

```yaml
# rules/no-and-short-circuit-in-jsx.yml
id: no-and-short-circuit-in-jsx
language: tsx
rule:
  kind: jsx_expression
  has: { pattern: $A && $B }
  not: { inside: { kind: jsx_attribute } }
fix: "{$A ? $B : null}"
```

**Works by** targeting `&&` expressions **only** in JSX expression blocks (not attributes) and rewriting to a safe ternary to avoid rendering `0`.

---

### 7) TSX/SVG: hyphenated attributes → camelCase (auto-fix with transform)

```yaml
# rules/svg-attr-to-camel.yml
id: rewrite-svg-attribute
language: tsx
rule:
  pattern: $PROP
  regex: ([a-z]+)-([a-z])
  kind: property_identifier
  inside: { kind: jsx_attribute }
transform:
  NEW_PROP: { convert: { source: $PROP, toCase: camelCase } }
fix: $NEW_PROP
```

**Works by** capturing the kebab‑cased prop name and using `convert/toCase: camelCase` to synthesize the replacement identifier. 

---

### 8) HTML/Vue templates: `:visible` → `:open` on specific tags (scoped rewrite)

```yaml
# rules/antd-visible-to-open.yml
id: upgrade-ant-design-vue
language: html
utils:
  inside-tag:
    inside:
      kind: element
      stopBy: { kind: element }
      has:
        stopBy: { kind: tag_name }
        kind: tag_name
        pattern: $TAG_NAME
rule:
  kind: attribute_name
  regex: :visible
  matches: inside-tag
constraints:
  TAG_NAME: { regex: a-modal|a-tooltip }
fix: :open
```

**Works by** discovering the **enclosing element** with a util rule, capturing its tag name into `$TAG_NAME`, then constraining the rewrite to modal/tooltip components only.

---

### 9) C/C++: fix format‑string vulnerabilities (auto‑insert `"%s"`)

```yaml
# rules/cpp-fmt-string.yml
id: fix-format-security-error
language: cpp
rule: { pattern: $PRINTF($S, $VAR) }
constraints:
  PRINTF: { regex: "^sprintf|fprintf$" }
  VAR:
    not:
      any:
        - { kind: string_literal }
        - { kind: concatenated_string }
fix: $PRINTF($S, "%s", $VAR)
```

**Works by** matching `sprintf/fprintf` calls where the second arg isn’t already a literal, then inserting an explicit `"%s"` format. 

---

### 10) YAML configs: flag host/port and emit a custom message (linting)

```yaml
# rules/detect-host-port.yml
id: detect-host-port
language: yaml
message: You are using $HOST on Port $PORT, please change it to 8000
severity: error
rule:
  any:
    - pattern: "port: $PORT"
    - pattern: "host: $HOST"
```

**Works by** matching YAML key/value pairs and surfacing a structured error with captured values in the message.

---

### 11) Multi-step codemod (XState v4 → v5) with `utils` and `transform`

```yaml
# rules/xstate-migration.yml
id: migrate-import-name
utils:
  FROM_XS: { kind: import_statement, has: { kind: string, regex: xstate } }
  XS_EXPORT:
    kind: identifier
    inside: { has: { matches: FROM_XS }, stopBy: end }
rule: { regex: ^Machine|interpret$, pattern: $IMPT, matches: XS_EXPORT }
transform:
  STEP1: { replace: { by: create$1, replace: (Machine), source: $IMPT } }
  FINAL: { replace: { by: createActor, replace: interpret, source: $STEP1 } }
fix: $FINAL
---
id: migrate-to-provide
rule: { pattern: $MACHINE.withConfig }
fix: $MACHINE.provide
---
id: migrate-to-actors
rule:
  kind: property_identifier
  regex: ^services$
  inside: { pattern: $M.withConfig($$$ARGS), stopBy: end }
fix: actors
```

**Works by** constraining import‑bound identifiers with `utils`, then composing staged `transform` replacements; separate rules finish the API rename and options shape update.

---

### 12) When one‑node rewrites aren’t enough: use **Rewriters**

If you need to **explode a single import into many**, apply a rewriter over captured descendants and join the generated edits:

```yaml
# rules/barrel-to-single.yml
id: barrel-to-single
language: javascript
rule: { pattern: "import {$$$IDENTS} from './module'" }
rewriters:
- id: rewrite-identifier
  rule: { kind: identifier, pattern: $IDENT }
  transform: { LIB: { convert: { source: $IDENT, toCase: lowerCase } } }
  fix: "import $IDENT from './module/$LIB'"
transform:
  IMPORTS:
    rewrite:
      rewriters: [rewrite-identifier]
      source: $$$IDENTS
      joinBy: "\n"
fix: $IMPORTS
```

**Works by** applying a rewriter to each captured identifier in `$$$IDENTS`, producing multiple import lines and joining them into a single replacement.

---

### Practical heuristics (structure vs. text)

* **Structure-sensitive changes** or **bulk refactors** → `ast-grep` rules (often YAML) with `inside/has/not`, `constraints`, `transform`, and sometimes `rewriters`.
* **Fast reconnaissance** or **non‑code assets** → `ripgrep`; pipe file lists into `ast-grep` when precision or rewriting begins.

---

## bv — Graph-Aware Triage Engine

bv is a graph-aware triage engine for Beads projects (`.beads/beads.jsonl`). It computes PageRank, betweenness, critical path, cycles, HITS, eigenvector, and k-core metrics deterministically.

**CRITICAL: Use ONLY `--robot-*` flags. Bare `bv` launches an interactive TUI that blocks your session.**

### The Workflow: Start With Triage

**`bv --robot-triage` is your single entry point.** It returns:
- `quick_ref`: at-a-glance counts + top 3 picks
- `recommendations`: ranked actionable items with scores, reasons, unblock info
- `quick_wins`: low-effort high-impact items
- `blockers_to_clear`: items that unblock the most downstream work
- `project_health`: status/type/priority distributions, graph metrics
- `commands`: copy-paste shell commands for next steps

```bash
bv --robot-triage        # THE MEGA-COMMAND: start here
bv --robot-next          # Minimal: just the single top pick + claim command
```

### Command Reference

**Planning:**
| Command | Returns |
|---------|---------|
| `--robot-plan` | Parallel execution tracks with `unblocks` lists |
| `--robot-priority` | Priority misalignment detection with confidence |

**Graph Analysis:**
| Command | Returns |
|---------|---------|
| `--robot-insights` | Full metrics: PageRank, betweenness, HITS, eigenvector, critical path, cycles, k-core |
| `--robot-label-health` | Per-label health: `health_level`, `velocity_score`, `staleness`, `blocked_count` |

### jq Quick Reference

```bash
bv --robot-triage | jq '.quick_ref'                        # At-a-glance summary
bv --robot-triage | jq '.recommendations[0]'               # Top recommendation
bv --robot-plan | jq '.plan.summary.highest_impact'        # Best unblock target
bv --robot-insights | jq '.Cycles'                         # Circular deps (must fix!)
```

---

## UBS — Ultimate Bug Scanner

**Golden Rule:** `ubs <changed-files>` before every commit. Exit 0 = safe. Exit >0 = fix & re-run.

### Commands

```bash
ubs file.py file2.py                        # Specific files (< 1s) — USE THIS
ubs $(git diff --name-only --cached)        # Staged files — before commit
ubs --only=python src/                      # Language filter (3-5x faster)
ubs .                                       # Whole project
```

### Output Format

```
⚠️  Category (N errors)
    file.py:42:5 – Issue description
    💡 Suggested fix
Exit code: 1
```

Parse: `file:line:col` → location | 💡 → how to fix | Exit 0/1 → pass/fail

### Fix Workflow

1. Read finding → category + fix suggestion
2. Navigate `file:line:col` → view context
3. Verify real issue (not false positive)
4. Fix root cause (not symptom)
5. Re-run `ubs <file>` → exit 0
6. Commit

### Bug Severity

- **Critical (always fix):** Command injection, unquoted variables, eval with user input
- **Important (production):** Missing error handling, unset variables, unsafe pipes
- **Contextual (judgment):** TODO/FIXME, echo debugging

---

## Morph Warp Grep — AI-Powered Code Search

**Use `mcp__morph-mcp__warp_grep` for exploratory "how does X work?" questions.** An AI agent expands your query, greps the codebase, reads relevant files, and returns precise line ranges with full context.

**Use `ripgrep` for targeted searches.** When you know exactly what you're looking for.

### When to Use What

| Scenario | Tool | Why |
|----------|------|-----|
| "How is OCR tiling implemented?" | `warp_grep` | Exploratory; don't know where to start |
| "Where is the viewport sweep handler?" | `warp_grep` | Need to understand architecture |
| "Find all uses of `pyvips`" | `ripgrep` | Targeted literal search |
| "Replace all `os.getenv` with decouple" | `ast-grep` | Structural refactor |

### warp_grep Usage

```
mcp__morph-mcp__warp_grep(
  repoPath: "/data/projects/markdown_web_browser",
  query: "How does the capture system handle viewport sweeps?"
)
```

### Anti-Patterns

- **Don't** use `warp_grep` to find a specific function name → use `ripgrep`
- **Don't** use `ripgrep` to understand "how does X work" → wastes time with manual reads

---

## cass — Cross-Agent Session Search

`cass` indexes prior agent conversations (Claude Code, Codex, Cursor, Gemini, ChatGPT, etc.) so we can reuse solved problems.

**Rules:** Never run bare `cass` (TUI). Always use `--robot` or `--json`.

### Examples

```bash
cass health
cass search "OCR tiling" --robot --limit 5
cass view /path/to/session.jsonl -n 42 --json
cass expand /path/to/session.jsonl -n 42 -C 3 --json
cass capabilities --json
cass robot-docs guide
```

### Tips

- Use `--fields minimal` for lean output
- Filter by agent with `--agent`
- Use `--days N` to limit to recent history

stdout is data-only, stderr is diagnostics; exit code 0 means success.

Treat cass as a way to avoid re-solving problems other agents already handled.

---

<!-- bv-agent-instructions-v1 -->

## Beads Workflow Integration

This project uses [beads_viewer](https://github.com/Dicklesworthstone/beads_viewer) for issue tracking. Issues are stored in `.beads/` and tracked in git.

### Essential Commands

```bash
# CLI commands for agents
bd ready              # Show issues ready to work (no blockers)
bd list --status=open # All open issues
bd show <id>          # Full issue details with dependencies
bd create --title="..." --type=task --priority=2
bd update <id> --status=in_progress
bd close <id> --reason="Completed"
bd close <id1> <id2>  # Close multiple issues at once
bd sync               # Commit and push changes
```

### Workflow Pattern

1. **Start**: Run `bd ready` to find actionable work
2. **Claim**: Use `bd update <id> --status=in_progress`
3. **Work**: Implement the task
4. **Complete**: Use `bd close <id>`
5. **Sync**: Always run `bd sync` at session end

### Key Concepts

- **Dependencies**: Issues can block other issues. `bd ready` shows only unblocked work.
- **Priority**: P0=critical, P1=high, P2=medium, P3=low, P4=backlog (use numbers, not words)
- **Types**: task, bug, feature, epic, question, docs

<!-- end-bv-agent-instructions -->

---

## Landing the Plane (Session Completion)

**When ending a work session**, you MUST complete ALL steps below. Work is NOT complete until `git push` succeeds.

**MANDATORY WORKFLOW:**

1. **File issues for remaining work** - Create issues for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **PUSH TO REMOTE** - This is MANDATORY:
   ```bash
   git pull --rebase
   bd sync
   git push
   git status  # MUST show "up to date with origin"
   ```
5. **Clean up** - Clear stashes, prune remote branches
6. **Verify** - All changes committed AND pushed
7. **Hand off** - Provide context for next session

**CRITICAL RULES:**
- Work is NOT complete until `git push` succeeds
- NEVER stop before pushing - that leaves work stranded locally
- NEVER say "ready to push when you are" - YOU must push
- If push fails, resolve and retry until it succeeds

---

## Note for Codex/GPT-5.2

You constantly bother me and stop working with concerned questions that look similar to this:

```
Unexpected changes (need guidance)

- Working tree still shows edits I did not make in pyproject.toml, app/capture.py, app/ocr.py, tests/smoke_capture.spec.ts. Please advise whether to keep/commit/revert these before any further work. I did not touch them.

Next steps (pick one)

1. Decide how to handle the unrelated modified files above so we can resume cleanly.
```

NEVER EVER DO THAT AGAIN. The answer is literally ALWAYS the same: those are changes created by the potentially dozen of other agents working on the project at the same time. This is not only a common occurence, it happens multiple times PER MINUTE. The way to deal with it is simple: you NEVER, under ANY CIRCUMSTANCE, stash, revert, overwrite, or otherwise disturb in ANY way the work of other agents. Just treat those changes identically to changes that you yourself made. Just fool yourself into thinking YOU made the changes and simply don't recall it for some reason.

