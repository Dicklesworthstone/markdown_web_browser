# Overlay Blocklist & Overlay Detection Playbook
_Last updated: 2025-11-08 (UTC)_

The capture pipeline masks anti-automation overlays in two deterministic layers: network filtering (future) and selector-based CSS hides. This document tracks the selector blocklist contract plus the review workflow.

## Blocklist JSON (`config/blocklist.json`)

Structure:

```jsonc
{
  "version": "2025-11-07",
  "global": ["#onetrust-consent-sdk", "[data-testid='cookie-banner']"],
  "domains": {
    "nytimes.com": ["#gateway-content"],
    "*.substack.com": ["div[class*='paywall']"]
  }
}
```

* `global`: selectors applied to every capture.
* `domains`: per-host or wildcard overrides (`*.example.com`). Selectors are appended to the global list in declaration order.
* `version`: free-form string logged in manifests for forensic triage.

**Environment variable:** `BLOCKLIST_PATH` overrides the default (`config/blocklist.json`). Document the location in `.env.example` and `docs/config.md` whenever it changes.

## Runtime Behavior

1. `app.blocklist.cached_blocklist()` loads the JSON once per process.
2. `app.capture` calls `apply_blocklist(page, url=..., config=...)` after navigation, which:
   - Injects CSS via `page.add_style_tag` to hide selectors.
   - Returns per-selector hit counts by running `document.querySelectorAll()`.
3. Capture manifests record `blocklist_hits` plus structured warnings (`canvas-heavy`, `video-heavy`, `sticky-chrome`) emitted by `app.capture_warnings`.
4. Blocklist hits also emit SSE warnings (`event:warning`) so the UI can highlight when overlays were removed.

## Editing Workflow

1. Update `config/blocklist.json` (preserve alphabetical sort by domain).
2. Run `uv run ruff check --fix --unsafe-fixes` + `uvx ty check`.
3. Document the change in this file (date + rationale) and note it in the relevant PLAN section.
4. Mention the update in the daily Agent Mail thread so Ops knows to refresh caches.

## Selector Guidelines

- Target the smallest stable wrapper (IDs or data attributes). Avoid brittle class chains unless no better hook exists.
- Never trigger user-visible actions (no `click()`); we only hide via CSS.
- For paywalls that gate scroll height, prefer selectors that cover the overlay/backdrop combo.
- When in doubt, capture before/after tiles and attach in `docs/blocklist/` for review.

## Future Work

- Layer network-level filtering (python-adblock) ahead of CSS injection for domains with hostile CMP scripts.
- Extend heuristics to auto-hide `position:fixed` elements taller than 25% viewport and record them in manifests for review.
- Expose a CLI (`mdwb blocklist add <domain> <selector>`) once Admin UI lands, writing through to the JSON + docs.
