from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from contextlib import ExitStack
from dataclasses import replace

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


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    """Retain each run's artifacts; pytest's default prunes older run folders."""
    requested = config.option.basetemp
    if requested is None:
        parent = Path(tempfile.mkdtemp(prefix="mdwb-pytest-"))
        config.option.basetemp = str(parent / "artifacts")
    else:
        path = Path(requested)
        if path.exists() or path.is_symlink():
            raise pytest.UsageError(
                "--basetemp must be a new path: pytest otherwise deletes its existing contents"
            )


@pytest.fixture
def capture_fixture_url():
    """Serve authored bytes over actual TCP; retain all source/output files."""
    from tests.test_e2e_generated import _serve_fixture

    with ExitStack() as listeners:
        yield lambda data: listeners.enter_context(_serve_fixture(data))


@pytest.fixture
def operator_network_settings():
    """Explicit server grant for controlled loopback capture fixtures only."""
    from app.settings import get_settings

    return replace(
        get_settings(),
        REQUIRE_API_KEY=False,
        NETWORK_TRUST_LOCAL_OPERATOR=True,
        NETWORK_PRIVATE_CIDRS=("127.0.0.1/32",),
        NETWORK_TRUSTED_OWNERS=(),
    )
