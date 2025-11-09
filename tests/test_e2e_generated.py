from __future__ import annotations

import difflib
import json
import os
from pathlib import Path
from typing import Any

import pytest


def _cases_path() -> Path:
    env_path = os.environ.get("MDWB_GENERATED_E2E_CASES")
    if env_path:
        return Path(env_path)
    return Path("tests/fixtures/e2e_generated/cases.json")


def _load_cases() -> list[dict[str, Any]]:
    path = _cases_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:  # pragma: no cover - defensive
        raise RuntimeError(f"Invalid cases JSON: {path}") from exc
    if not isinstance(data, list):
        raise RuntimeError(f"Cases file must contain a list: {path}")
    return data


CASES = _load_cases()


def _case_id(case: dict[str, Any]) -> str:
    return str(case.get("name") or case.get("baseline") or "case")


@pytest.mark.skipif(not CASES, reason="No generative E2E cases configured")
@pytest.mark.parametrize("case", CASES, ids=_case_id)
def test_generated_markdown_diff(case: dict[str, Any]) -> None:
    baseline_path = Path(case["baseline"])
    candidate_path = Path(case["candidate"])
    tolerance = float(case.get("tolerance", 0.02))

    if not baseline_path.exists() or not candidate_path.exists():
        pytest.skip(f"Missing baseline/candidate for {case.get('name')}")

    baseline = baseline_path.read_text(encoding="utf-8")
    candidate = candidate_path.read_text(encoding="utf-8")
    ratio = difflib.SequenceMatcher(None, baseline, candidate).ratio()
    diff = max(0.0, 1.0 - ratio)
    assert diff <= tolerance, (
        f"Diff {diff:.2%} exceeds tolerance {tolerance:.2%} for {case.get('name')}"
    )
