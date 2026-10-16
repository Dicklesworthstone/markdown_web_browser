from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.append(str(ROOT))


@pytest.fixture(autouse=True)
def _reset_rate_limiter_between_tests():
    from app.rate_limit import reset_rate_limiter

    reset_rate_limiter()
    yield
    reset_rate_limiter()

