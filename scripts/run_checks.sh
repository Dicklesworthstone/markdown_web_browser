#!/usr/bin/env bash

# Run the mandatory verification suite (ruff, ty, Playwright smoke).
# Additional args override the default Playwright target.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ $# -gt 0 ]]; then
  PLAYWRIGHT_TARGETS=("$@")
else
  PLAYWRIGHT_TARGETS=("tests/smoke_capture.spec.ts")
fi

run_step() {
  local label="$1"
  shift
  echo "→ ${label}"
  "$@"
  echo
}

run_step "ruff check" uv run ruff check --fix --unsafe-fixes
run_step "ty check" uvx ty check
run_step "pytest" uv run pytest \
  tests/test_mdwb_cli_events.py \
  tests/test_mdwb_cli_webhooks.py \
  tests/test_mdwb_cli_fetch.py \
  tests/test_olmocr_cli_config.py \
  tests/test_check_env.py \
  tests/test_show_latest_smoke.py \
  tests/test_api_webhooks.py
run_step "playwright" uv run playwright test "${PLAYWRIGHT_TARGETS[@]}"
