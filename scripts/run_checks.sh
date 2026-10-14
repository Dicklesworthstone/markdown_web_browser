#!/usr/bin/env bash

# Run strict checks in the project's Python 3.13 environment.
# Native Playwright capture and UI tests are part of the full pytest suite.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR" || exit 1

PYTEST_JUNIT_PATH="${PYTEST_JUNIT_PATH:-tmp/pytest_report.xml}"
PYTEST_SUMMARY_PATH="${PYTEST_SUMMARY_PATH:-tmp/pytest_summary.json}"

if [[ $# -gt 0 ]]; then
  echo "ERROR: run_checks always runs tests/; use uv run pytest directly for focused checks."
  exit 2
fi

run_step() {
  local label="$1"
  shift
  echo "→ ${label}"
  "$@"
  echo
}

run_step "ruff check" uv run --python 3.13 ruff check
run_step "ruff format check" uv run --python 3.13 ruff format --check
PROJECT_PYTHON="$(uv run --python 3.13 python -c 'import sys; print(sys.executable)')"
run_step "ty check" uvx --python 3.13 ty check --python "$PROJECT_PYTHON" app scripts tests
echo "→ real browser and libvips preflight (observed builds; pin qualification is separate)"
uv run --python 3.13 python - <<'PY'
import asyncio
import pyvips
from playwright.async_api import async_playwright
from app.capture import _launch_browser
from app.settings import get_settings

print("libvips", ".".join(str(pyvips.version(i)) for i in range(3)))

async def main():
    async with async_playwright() as playwright:
        settings = get_settings()
        browser, channel, transport = await _launch_browser(
            playwright, settings.browser.playwright_channel,
            expected_build=settings.browser.cft_version,
            transport=settings.browser.browser_transport,
            executable=settings.browser.cft_executable,
            fontconfig_file=settings.browser.fontconfig_file,
        )
        try:
            print("capture adapter", channel, browser.version, transport)
        finally:
            await browser.close()
        for channel in ("chromium", "chrome"):
            browser = await playwright.chromium.launch(channel=channel, headless=True)
            try:
                print(channel, browser.version)
            finally:
                await browser.close()

asyncio.run(main())
PY

run_pytest() {
  echo "→ pytest"
  mkdir -p "$(dirname "$PYTEST_JUNIT_PATH")" "$(dirname "$PYTEST_SUMMARY_PATH")"
  set +e
  # tests/test_e2e_generated.py reads the opt-in flag via python-decouple;
  # enabled cases execute here once and fail on missing/invalid evidence.
  uv run --python 3.13 pytest tests/ -x --junitxml "$PYTEST_JUNIT_PATH"
  local status=$?
  set -e
  uv run --python 3.13 python scripts/report_pytest_summary.py \
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

if [[ "${MDWB_CHECK_METRICS:-0}" == "1" ]]; then
  METRICS_TIMEOUT="${CHECK_METRICS_TIMEOUT:-5.0}"
  run_step "metrics" uv run --python 3.13 python scripts/check_metrics.py --timeout "$METRICS_TIMEOUT"
fi
