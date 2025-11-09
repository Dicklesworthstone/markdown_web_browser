from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any, Dict, cast

from typer.testing import CliRunner

from scripts import run_server


runner = CliRunner()


def test_run_server_uvicorn(monkeypatch):
    calls: Dict[str, Any] = {}

    class FakeUvicorn:
        def run(self, app, host, port, reload, workers, log_level):  # noqa: D401
            calls.update(
                {
                    "app": app,
                    "host": host,
                    "port": port,
                    "reload": reload,
                    "workers": workers,
                    "log_level": log_level,
                }
            )

    monkeypatch.setattr(run_server, "uvicorn", FakeUvicorn())

    result = runner.invoke(
        run_server.app,
        [
            "--server",
            "uvicorn",
            "--host",
            "0.0.0.0",
            "--port",
            "9001",
            "--app",
            "app.main:app",
            "--workers",
            "2",
            "--no-reload",
            "--log-level",
            "debug",
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert calls["app"] == "app.main:app"
    assert calls["host"] == "0.0.0.0"
    assert calls["port"] == 9001
    assert calls["reload"] is False
    assert calls["workers"] == 2
    assert calls["log_level"] == "debug"


def test_run_server_granian(monkeypatch):
    captured: Dict[str, Any] = {}

    class FakeGranian:
        def __init__(self, target, **kwargs):  # noqa: D401
            captured["target"] = target
            captured["kwargs"] = kwargs

        def serve(self):  # noqa: D401
            captured["served"] = True

    fake_constants = SimpleNamespace(
        Interfaces=SimpleNamespace(ASGI="ASGI"),
        Loops=SimpleNamespace(auto="auto"),
    )

    monkeypatch.setitem(sys.modules, "granian", SimpleNamespace(Granian=FakeGranian))
    monkeypatch.setitem(sys.modules, "granian.constants", fake_constants)
    monkeypatch.setattr(run_server, "_granian_log_level", lambda value: value)

    result = runner.invoke(
        run_server.app,
        [
            "--server",
            "granian",
            "--host",
            "0.0.0.0",
            "--port",
            "9100",
            "--workers",
            "3",
            "--granian-runtime-threads",
            "2",
            "--no-reload",
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert captured["target"] == "app.main:app"
    kwargs = cast(Dict[str, Any], captured["kwargs"])
    assert kwargs["address"] == "0.0.0.0"
    assert kwargs["port"] == 9100
    assert kwargs["workers"] == 3
    assert kwargs["runtime_threads"] == 2
    assert kwargs["reload"] is False
    assert captured["served"] is True
