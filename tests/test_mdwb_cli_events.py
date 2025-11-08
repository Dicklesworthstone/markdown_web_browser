from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import httpx
import pytest
import typer
from typer.testing import CliRunner

from scripts import mdwb_cli

API_SETTINGS = mdwb_cli.APISettings(
    base_url="http://localhost",
    api_key=None,
    warning_log_path=Path("ops/warnings.jsonl"),
)

runner = CliRunner()


class FakeResponse:
    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        return None

    def iter_lines(self):  # noqa: D401
        yield from self._lines

    def raise_for_status(self) -> None:
        return None


class FakeClient:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self._responses = responses
        self.calls: list[str | None] = []
        self.closed = False

    def stream(self, method: str, url: str, params=None):  # noqa: ANN001
        since = params.get("since") if params else None
        self.calls.append(since)
        return self._responses.pop(0)

    def close(self) -> None:
        self.closed = True


def test_cursor_from_line_prefers_top_level_timestamp():
    base = "2025-11-08T00:00:00+00:00"
    line = json.dumps({"timestamp": base})
    bumped = mdwb_cli._cursor_from_line(line, None)
    assert bumped is not None
    assert datetime.fromisoformat(bumped) > datetime.fromisoformat(base)


def test_cursor_from_line_uses_snapshot_timestamp_when_missing_top_level():
    base = "2025-11-08T00:00:00+00:00"
    line = json.dumps({"snapshot": {"timestamp": base}})
    bumped = mdwb_cli._cursor_from_line(line, None)
    assert bumped is not None
    assert datetime.fromisoformat(bumped) > datetime.fromisoformat(base)


def test_iter_event_lines_updates_cursor_and_closes_client(monkeypatch):
    responses = [FakeResponse([json.dumps({"timestamp": "2025-11-08T00:00:00+00:00"})])]
    fake_client = FakeClient(responses)
    monkeypatch.setattr(mdwb_cli, "_client", lambda settings: fake_client)
    monkeypatch.setattr(mdwb_cli.time, "sleep", lambda _: None)

    lines = list(
        mdwb_cli._iter_event_lines(
            "job123",
            API_SETTINGS,
            cursor=None,
            follow=False,
            interval=0.1,
        )
    )

    assert lines == [json.dumps({"timestamp": "2025-11-08T00:00:00+00:00"})]
    assert fake_client.calls == [None]
    assert fake_client.closed


def test_watch_job_events_pretty_renders_snapshot(monkeypatch):
    events = [
        json.dumps(
            {
                "snapshot": {
                    "state": "BROWSER_STARTING",
                    "progress": {"done": 0, "total": 2},
                }
            }
        ),
        json.dumps(
            {
                "snapshot": {
                    "state": "DONE",
                    "progress": {"done": 2, "total": 2},
                }
            }
        ),
    ]

    monkeypatch.setattr(mdwb_cli, "_iter_event_lines", lambda *_, **__: iter(events))
    with mdwb_cli.console.capture() as capture:
        mdwb_cli._watch_job_events_pretty(
            "job123",
            API_SETTINGS,
            cursor=None,
            follow=True,
            interval=0.1,
            raw=False,
        )
    output = capture.get()
    assert "BROWSER_STARTING" in output
    assert "DONE" in output


def test_log_event_formats_blocklist_and_sweep():
    with mdwb_cli.console.capture() as capture:
        mdwb_cli._log_event("blocklist", "{\"#cookie\":2}")
        mdwb_cli._log_event("sweep", "{\"sweep_stats\":{\"shrink_events\":1},\"overlap_match_ratio\":0.92}")
        mdwb_cli._log_event("validation", "[\"Tile checksum mismatch\"]")
    output = capture.get()
    assert "#cookie:2" in output
    assert "ratio 0.92" in output
    assert "Tile checksum mismatch" in output


def test_watch_events_with_fallback_streams_via_sse(monkeypatch):
    def fake_watch(*args, **kwargs):
        raise httpx.RequestError("boom", request=httpx.Request("GET", "http://test"))

    calls: dict[str, object] = {}

    def fake_stream(job_id: str, settings: mdwb_cli.APISettings, raw: bool, hooks=None):  # noqa: ANN001
        calls["job_id"] = job_id
        calls["raw"] = raw

    monkeypatch.setattr(mdwb_cli, "_watch_job_events_pretty", fake_watch)
    monkeypatch.setattr(mdwb_cli, "_stream_job", fake_stream)

    with mdwb_cli.console.capture() as capture:
        mdwb_cli._watch_events_with_fallback(
            "job123",
            API_SETTINGS,
            cursor=None,
            follow=True,
            interval=1.0,
            raw=True,
        )

    output = capture.get()
    assert "falling back to SSE stream" in output
    assert calls == {"job_id": "job123", "raw": True}


def test_watch_command_invokes_helper(monkeypatch):
    invoked: list[tuple] = []
    monkeypatch.setattr(mdwb_cli, "_resolve_settings", lambda api_base: API_SETTINGS)

    def fake_helper(job_id, settings, cursor, follow, interval, raw, hooks):  # noqa: ANN001
        invoked.append((job_id, cursor, follow, interval, raw, hooks))

    monkeypatch.setattr(mdwb_cli, "_watch_events_with_fallback", fake_helper)

    result = runner.invoke(mdwb_cli.cli, ["watch", "job123", "--interval", "0.5", "--raw", "--on", "snapshot=echo hi"])

    assert result.exit_code == 0
    assert invoked == [("job123", None, True, 0.5, True, {"snapshot": ["echo hi"]})]


def test_parse_event_hooks_valid():
    hooks = mdwb_cli._parse_event_hooks(["snapshot=echo hi", "state:DONE=touch done", "*=logger"])
    assert hooks == {"snapshot": ["echo hi"], "state:DONE": ["touch done"], "*": ["logger"]}


def test_parse_event_hooks_invalid():
    with pytest.raises(typer.BadParameter):
        mdwb_cli._parse_event_hooks(["novalue"])


def test_trigger_event_hooks_runs_matching(monkeypatch):
    recorded: list[tuple[str, str]] = []

    def fake_run_hook(command, event, payload):  # noqa: ANN001
        recorded.append((command, event))

    monkeypatch.setattr(mdwb_cli, "_run_hook", fake_run_hook)
    entry = {"event": "snapshot", "snapshot": {"state": "DONE"}}
    hooks = {"snapshot": ["cmd1"], "state:DONE": ["cmd2"], "*": ["cmd3"]}
    mdwb_cli._trigger_event_hooks(entry, hooks)
    assert recorded == [("cmd1", "snapshot"), ("cmd2", "snapshot"), ("cmd3", "snapshot")]


def test_trigger_event_hooks_ignores_non_match(monkeypatch):
    recorded: list[str] = []
    monkeypatch.setattr(mdwb_cli, "_run_hook", lambda cmd, event, payload: recorded.append(cmd))
    mdwb_cli._trigger_event_hooks({"event": "tile"}, {"snapshot": ["cmd"]})
    assert recorded == []
