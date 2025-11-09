#!/usr/bin/env bash

# Run the mandatory verification suite (ruff, ty, Playwright smoke).
# Additional args override the default Playwright target.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

PYTEST_JUNIT_PATH="${PYTEST_JUNIT_PATH:-tmp/pytest_report.xml}"
PYTEST_SUMMARY_PATH="${PYTEST_SUMMARY_PATH:-tmp/pytest_summary.json}"

PYTEST_TARGETS=(
  tests/test_mdwb_cli_events.py
  tests/test_mdwb_cli_webhooks.py
  tests/test_mdwb_cli_fetch.py
  tests/test_mdwb_cli_artifacts.py
  tests/test_agent_scripts.py
  tests/test_update_smoke_pointers.py
  tests/test_show_latest_smoke.py
  tests/test_check_metrics.py
  tests/test_mdwb_cli_diag.py
  tests/test_mdwb_cli_embeddings.py
  tests/test_mdwb_cli_ocr.py
  tests/test_mdwb_cli_replay.py
  tests/test_mdwb_cli_show.py
  tests/test_mdwb_cli_warnings.py
  tests/test_mdwb_cli_resume.py
  tests/test_olmocr_cli_config.py
  tests/test_check_env.py
  tests/test_store_manifest.py
  tests/test_capture_sweeps.py
  tests/test_manifest_contract.py
  tests/test_metrics.py
  tests/test_api_webhooks.py
  tests/test_report_pytest_summary.py
)

check_libvips() {
  if [[ "${SKIP_LIBVIPS_CHECK:-0}" == "1" ]]; then
    echo "→ libvips preflight skipped (SKIP_LIBVIPS_CHECK=1)"
    return
  fi
  if uv run python - >/dev/null 2>&1 <<'PY'
import pyvips  # noqa
PY
  then
    return
  fi
  cat <<'EOF'
ERROR: libvips/pyvips is not available in this environment.
Install the system package (e.g., `sudo apt-get install libvips` on Debian/Ubuntu)
before running scripts/run_checks.sh so the tiler tests can import pyvips.
EOF
  exit 1
}

if [[ $# -gt 0 ]]; then
  PLAYWRIGHT_TARGETS=("$@")
else
  PLAYWRIGHT_TARGETS=()
fi

run_step() {
  local label="$1"
  shift
  echo "→ ${label}"
  "$@"
  echo
}

check_libvips

run_step "ruff check" uv run ruff check --fix --unsafe-fixes
run_step "ty check" uvx ty check

run_pytest() {
  echo "→ pytest"
  mkdir -p "$(dirname "$PYTEST_JUNIT_PATH")" "$(dirname "$PYTEST_SUMMARY_PATH")"
  set +e
  uv run pytest --junitxml "$PYTEST_JUNIT_PATH" "${PYTEST_TARGETS[@]}"
  local status=$?
  set -e
  uv run python scripts/report_pytest_summary.py \
    --junit "$PYTEST_JUNIT_PATH" \
    --summary "$PYTEST_SUMMARY_PATH" \
    --exit-code "$status"
  echo
  if [[ $status -ne 0 ]]; then
    echo "Pytest summary saved to $PYTEST_SUMMARY_PATH (JUnit: $PYTEST_JUNIT_PATH)"
    exit $status
  fi
}

run_pytest

if [[ "${MDWB_RUN_E2E:-0}" == "1" ]]; then
  run_step "pytest (e2e)" uv run pytest tests/test_e2e_cli.py
else
  echo "→ pytest (e2e)"
  echo "SKIP: Set MDWB_RUN_E2E=1 to include tests/test_e2e_cli.py"
  echo
fi

if [[ "${MDWB_RUN_E2E_GENERATED:-0}" == "1" ]]; then
  run_step "pytest (e2e generated)" uv run pytest tests/test_e2e_generated.py
else
  echo "→ pytest (e2e generated)"
  echo "SKIP: Set MDWB_RUN_E2E_GENERATED=1 to include tests/test_e2e_generated.py"
  echo
fi

run_playwright() {
  local args=("${PLAYWRIGHT_TARGETS[@]}")
  if [[ -n "${PLAYWRIGHT_BIN:-}" ]]; then
    run_step "playwright" "${PLAYWRIGHT_BIN}" "${args[@]}"
    return
  fi

  if [[ -x "node_modules/.bin/playwright" ]]; then
    run_step "playwright" npx playwright test --config=playwright.config.mjs "${args[@]}"
    return
  fi

  if uv run playwright --help 2>/dev/null | grep -q "test"; then
    run_step "playwright" uv run playwright test "${args[@]}"
    return
  fi

  echo "→ playwright"
  echo "WARN: Playwright CLI missing 'test' subcommand; skipping smoke run."
  echo "      Install @playwright/test (Node) or set PLAYWRIGHT_BIN to point at your runner."
  echo
}

run_playwright

if [[ "${MDWB_CHECK_METRICS:-0}" == "1" ]]; then
  METRICS_TIMEOUT="${CHECK_METRICS_TIMEOUT:-5.0}"
  run_step "metrics" uv run python scripts/check_metrics.py --timeout "$METRICS_TIMEOUT"
fi
