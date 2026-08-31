# HANDOFF — Markdown Web Browser Franken Revamp

> **Status (2026-08-29):** Plan written, 32 beads created and wired, 0
> cycles. Foundation bead `markdown_web_browser-mtg` is the top
> `bv --robot-triage` pick. Implementation can begin.
>
> **Author:** FrankenRevamp agent (single session).
> **Inputs:** `AGENTS.md`, `README.md`, `PLAN_TO_IMPLEMENT_MARKDOWN_WEB_BROWSER_PROJECT.md`,
> full `app/`, `scripts/`, `tests/`, `docs/`, franken-ecosystem scout
> reports from this session.

---

## What was done in this session

1. **Read `AGENTS.md` and `README.md` end to end.** Confirmed
   Python 3.13 / FastAPI / Playwright / pyvips / SQLModel stack with
   113 historical beads.
2. **Recovered `beads.db`.** Original DB was corrupt; `br sync
   --merge --force-jsonl` restored all 113 historical issues. Snapshot
   in `.beads/_recovered_jsonl_backup_20260829T185924Z/`.
3. **Inspected the Franken ecosystem.** Confirmed live binaries:
   - `focr` (franken_ocr v0.7.0, selftest pass on AVX2)
   - `fmd` (franken_markdown, capabilities JSON)
   - `asupersync` 0.4.9 + `asupersync-browser-core` 0.4.9
   - `frankensqlite` 0.x (binary at `/dp/frankensqlite`)
   - `frankensearch` 0.2.3 with `frankensearch-daemon`
   - `frankentui` (ftui-* crates)
4. **Scouted the existing codebase** via three parallel scout agents:
   architecture (PLAN + docs + app), deep (per-file digest), and
   CLI/tests/scripts. Full reports at `agent://codebase-arch-scout`,
   `agent://codebase-deep-scout`, `agent://cli-and-test-scout`.
5. **Identified bottlenecks** (sync-on-async loops, single-writer
   SQLite, polyglot agent surface).
6. **Wrote `PLAN_FRANKEN_REVAMP.md`** (~1,200 lines) — a self-contained
   plan covering foundation, Rust runtime, OCR, storage, rendering,
   TUI, agent contracts, and rollout.
7. **Created 32 beads** with `br create` (priority 0–3, labels
   `foundation/rust/focr/...`). All 32 received numbered `mdwb-XXX`
   pseudonyms in the plan; real IDs are 3-char slugs (e.g.,
   `markdown_web_browser-mtg`, `markdown_web_browser-19m`).
8. **Wired 61 dependency edges.** `br dep cycles --json` returns 0
   cycles. DAG rooted at `mdwb-mtg` (pyproject.toml pin).
9. **Triage:** `bv --robot-triage` returns `mdwb-mtg` as the #1 pick.
   16 beads are immediately ready, 42 are blocked. No structural
   problems.

---

## Bead ID map (plan nickname → real ID)

The plan document uses stable pseudonyms `mdwb-001` … `mdwb-090`.
The real bead IDs (assigned by `br create`) are:

| Plan name | Real ID | Title |
| --- | --- | --- |
| `mdwb-001` | `markdown_web_browser-mtg` | Pin Franken crate versions in pyproject.toml |
| `mdwb-002` | `markdown_web_browser-m3n` | mdwb doctor CLI |
| `mdwb-003` | `markdown_web_browser-utu` | Franken-greets proof script |
| `mdwb-004` | `markdown_web_browser-8xw` | Recover beads.db |
| `mdwb-010` | `markdown_web_browser-19m` | mdwb-runtime Cargo scaffold |
| `mdwb-011` | `markdown_web_browser-acs` | mdwb.runtime.v1 JSON schema |
| `mdwb-012` | `markdown_web_browser-97z` | Structured-concurrency Region |
| `mdwb-013` | `markdown_web_browser-tin` | Native SQLite pool (frankensqlite) |
| `mdwb-020` | `markdown_web_browser-eqq` | Capture controller |
| `mdwb-021` | `markdown_web_browser-pz1` | Streaming event bus |
| `mdwb-030` | `markdown_web_browser-0ji` | focr local OCR adapter |
| `mdwb-031` | `markdown_web_browser-du6` | OCR autopilot capability matrix |
| `mdwb-032` | `markdown_web_browser-t1w` | focr benchmark harness |
| `mdwb-040` | `markdown_web_browser-5ll` | Storage migration |
| `mdwb-041` | `markdown_web_browser-vyg` | Embeddings migration |
| `mdwb-042` | `markdown_web_browser-vv5` | mdwb search <job-id> CLI |
| `mdwb-043` | `markdown_web_browser-naz` | Cross-job search |
| `mdwb-050` | `markdown_web_browser-yj6` | out.html via fmd |
| `mdwb-051` | `markdown_web_browser-h4j` | out.pdf opt-in |
| `mdwb-052` | `markdown_web_browser-lid` | Browser UI uses out.html |
| `mdwb-060` | `markdown_web_browser-x59` | mdwb-tui scaffold |
| `mdwb-061` | `markdown_web_browser-g80` | TUI headless mode |
| `mdwb-062` | `markdown_web_browser-kzs` | TUI snapshots |
| `mdwb-070` | `markdown_web_browser-st3` | AgentEnvelope schema |
| `mdwb-071` | `markdown_web_browser-w8h` | mdwb agent <verb> umbrella |
| `mdwb-072` | `markdown_web_browser-ipo` | Refactor agent starter scripts |
| `mdwb-073` | `markdown_web_browser-7z8` | mdwb agent cite |
| `mdwb-080` | `markdown_web_browser-sdz` | Envelope validation tests |
| `mdwb-081` | `markdown_web_browser-0ei` | Rust runtime integration tests |
| `mdwb-082` | `markdown_web_browser-5b8` | Smoke covers both runtimes |
| `mdwb-083` | `markdown_web_browser-lil` | Docs refresh |
| `mdwb-090` | `markdown_web_browser-gs2` | Ship-it gate |

---

## Recommended next moves

### Today / this week

1. **`mdwb-mtg`** — Pin Franken crate versions in `pyproject.toml`,
   update `.env.example`, `docs/config.md`, `docs/models.yaml`.
   Add `[project.optional-dependencies] franken = ...`. This is the
   foundation everything else depends on.
2. **`mdwb-utu`** — Franken-greets proof script. Single shell
   script that runs every binary's `--version` and `selftest`; emits
   `benchmarks/proof/franken_greets.json`. Wire into
   `scripts/run_checks.sh`.
3. **`mdwb-m3n`** — `mdwb doctor` Typer CLI. Live-readiness check;
   calls `focr robot health`, `focr robot selftest`, `fmd doctor`,
   pings `mdwb-runtime`. JSON envelope.

### Next 2 weeks (Phase 1: Rust runtime)

4. **`mdwb-19m`** — Scaffold `crates/mdwb-runtime` Cargo workspace
   with `#[asupersync::main]`. First op: `runtime.ping` -> pong.
5. **`mdwb-acs`** — `schemas/mdwb.runtime.v1.json` + generated types.
6. **`mdwb-97z`** — Structured-concurrency capture Region; the
   single biggest reason to use asupersync.
7. **`mdwb-tin`** — FrankenSQLite pool per `index.fdb`.

### This month (Phase 3: focr OCR)

8. **`mdwb-0ji`** — `app/ocr_backends/focr.py` adapter. Drop-in
   replacement for `app/local_ocr.py`. Local OCR latency
   improvement is the most user-visible single win.
9. **`mdwb-du6`** — Extend `app/ocr_policy.py` so the autopilot
   prefers `focr` when healthy.
10. **`mdwb-t1w`** — Benchmark harness so we can prove the win.

### Q4 (Phase 7: agent contracts)

11. **`mdwb-st3`** + **`mdwb-w8h`** — `AgentEnvelope` schema + `mdwb
    agent <verb>` umbrella CLI. Once these land, every other bead
    emits the same JSON shape; agents no longer have to special-case
    each command.

---

## Things to remember

- **`AGENTS.md` is law.** Never delete files, never `git reset
  --hard`, never `rm -rf`. Always reserve files via Agent Mail
  before editing. Branch is `main`.
- **Existing tests must pass at every step.** New behaviour is
  additive. The current test suite is comprehensive (110+ tests
  across `tests/`).
- **Use uv, not pip.** The repo uses Python 3.13 + uv + setuptools.
- **Capture + Playwright stay Python.** Don't try to port them.
  The Rust runtime hosts Playwright via a child process.
- **`focr` model license:** Baidu Unlimited-OCR — Copyright (c) 2026
  Baidu, MIT License. Surface the notice in the manifest and in
  `mdwb doctor --json`.
- **`asupersync` 0.4.4–v0.4.9 cancellation contract** matters.
  Read the `asupersync-mega-skill` SKILL non-negotiables section
  before touching `crates/mdwb-runtime/src/capture/orchestrator.rs`.
- **Plan + spec live at `PLAN_FRANKEN_REVAMP.md`.** It is the
  canonical implementation reference; this handoff is just the
  orientation.

---

## Files added this session

- `PLAN_FRANKEN_REVAMP.md` — the canonical enhancement plan
- `HANDOFF_FRANKEN_REVAMP.md` — this file
- `.beads/issues.jsonl` — regenerated by `br sync --flush-only`; 146
  issues (113 historical + 32 new + 1 stale "test issue" absorbed
  via merge).

No source files were modified outside `.beads/`. The Python
codebase is untouched; no tests were touched. Run `git status`
to confirm.

---

## How to start work

```bash
# 1. Get the top pick
br ready --limit=5 --json

# 2. Read the plan section for that bead
#    In PLAN_FRANKEN_REVAMP.md, find "4.X <bead-nickname>" e.g. "4.1 mdwb-001"

# 3. Reserve files
#    file_reservation_paths(project_key=/data/projects/markdown_web_browser,
#                          agent_name=<your-name>,
#                          paths=["pyproject.toml", ".env.example", "docs/config.md", "docs/models.yaml"],
#                          ttl_seconds=3600, exclusive=true,
#                          reason="<bead-id>")

# 4. Announce in Agent Mail
#    send_message(... thread_id="<bead-id>", subject="[<bead-id>] Start", ack_required=true)

# 5. Implement. Update bead comments. Close.
br close <bead-id> --reason "Completed: ..."

# 6. Sync
br sync --flush-only
git add .beads/
git commit -m "<bead-id>: <one-line summary>"
git push
```

Every cycle keeps the workspace merge-clean, the bead graph
up-to-date, and the next agent's ready list fresh.