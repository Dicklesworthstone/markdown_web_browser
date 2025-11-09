from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, cast

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

    def stream(self, method: str, url: str, params=None, **kwargs):  # noqa: ANN001
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
    @contextmanager
    def fake_client_ctx(settings, **_):  # noqa: ANN001
        try:
            yield fake_client
        finally:
            fake_client.close()

    monkeypatch.setattr(mdwb_cli, "_client_ctx", fake_client_ctx)
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


def test_client_ctx_preserves_explicit_timeout(monkeypatch):
    captured: dict[str, object] = {}

    class DummyClient:
        def close(self) -> None:
            captured["closed"] = True

    def fake_client(settings, http2=True, timeout=None):  # noqa: ANN001
        captured["timeout"] = timeout
        return DummyClient()

    monkeypatch.setattr(mdwb_cli, "_client", fake_client)
    with mdwb_cli._client_ctx(API_SETTINGS, timeout=None):
        pass

    assert "closed" in captured
    assert captured["timeout"] is None


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
            progress_meter=None,
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


def test_cli_events_invokes_watch_job_events(monkeypatch, tmp_path: Path) -> None:
    called: dict[str, Any] = {}

    def fake_watch_job_events(job_id, settings, cursor, follow, interval, output):  # noqa: ANN001
        called.update(
            {
                "job_id": job_id,
                "settings": settings,
                "cursor": cursor,
                "follow": follow,
                "interval": interval,
            }
        )
        output.write("{}\n")

    log_path = tmp_path / "events.log"
    log_path.write_text("existing\n", encoding="utf-8")
    monkeypatch.setattr(mdwb_cli, "_watch_job_events", fake_watch_job_events)
    monkeypatch.setattr(mdwb_cli, "_resolve_settings", lambda api_base: API_SETTINGS)

    result = runner.invoke(
        mdwb_cli.cli,
        [
            "events",
            "job123",
            "--since",
            "2025-11-08T00:00:00Z",
            "--follow",
            "--interval",
            "0.5",
            "--output",
            str(log_path),
        ],
    )

    assert result.exit_code == 0
    assert called["job_id"] == "job123"
    assert called["settings"] is API_SETTINGS
    assert called["cursor"] == "2025-11-08T00:00:00Z"
    assert called["follow"] is True
    assert called["interval"] == 0.5
    assert log_path.read_text(encoding="utf-8") == "existing\n{}\n"


def test_watch_events_with_fallback_streams_sse_on_http_error(monkeypatch):
    called: dict[str, Any] = {}

    def fake_watch_job_events_pretty(*_args, **_kwargs):  # noqa: ANN001
        raise httpx.HTTPError("boom")

    def fake_stream_job(job_id, settings, raw, hooks, on_terminal, progress_meter, client):  # noqa: ANN001
        called.update(
            {
                "job_id": job_id,
                "settings": settings,
                "raw": raw,
                "hooks": hooks,
                "on_terminal": on_terminal,
                "progress_meter": progress_meter,
                "client": client,
            }
        )

    fake_client = httpx.Client(base_url="http://localhost")
    monkeypatch.setattr(mdwb_cli, "_watch_job_events_pretty", fake_watch_job_events_pretty)
    monkeypatch.setattr(mdwb_cli, "_stream_job", fake_stream_job)

    with mdwb_cli.console.capture() as capture:
        mdwb_cli._watch_events_with_fallback(
            "job-xyz",
            API_SETTINGS,
            cursor="2025-11-08T00:00:00Z",
            follow=True,
            interval=0.5,
            raw=False,
            hooks={"snapshot": ["echo"]},
            on_terminal=lambda *_: None,
            progress_meter=mdwb_cli._ProgressMeter(),
            client=fake_client,
        )

    output = capture.get()
    assert "falling back to SSE stream" in output
    assert called["job_id"] == "job-xyz"
    assert called["client"] is fake_client
    fake_client.close()


def test_watch_job_events_pretty_triggers_hooks_in_raw_mode(monkeypatch):
    events = [
        json.dumps(
            {
                "event": "snapshot",
                "snapshot": {
                    "state": "BROWSER_STARTING",
                    "progress": {"done": 0, "total": 1},
                },
            }
        )
    ]
    monkeypatch.setattr(mdwb_cli, "_iter_event_lines", lambda *_, **__: iter(events))
    received: list[dict[str, Any]] = []
    monkeypatch.setattr(mdwb_cli, "_trigger_event_hooks", lambda entry, hooks: received.append(entry))

    with mdwb_cli.console.capture() as capture:
        mdwb_cli._watch_job_events_pretty(
            "job-hooks",
            API_SETTINGS,
            cursor=None,
            follow=False,
            interval=0.1,
            raw=True,
            hooks={"snapshot": ["echo"]},
        )

    output = capture.get()
    assert '"event": "snapshot"' in output
    assert received and received[0]["snapshot"]["state"] == "BROWSER_STARTING"


def test_iter_sse_parses_events():
    class DummyResponse:
        def __init__(self, lines: list[str]) -> None:
            self.lines = lines

        def iter_lines(self):  # noqa: ANN001
            yield from self.lines

    response = cast(
        httpx.Response,
        DummyResponse(
            [
                "data: hello",
                "",
                "event: state",
                "data: DONE",
                "",
                "data: tail",
            ]
        ),
    )

    events = list(mdwb_cli._iter_sse(response))

    assert events == [("message", "hello"), ("state", "DONE"), ("message", "tail")]


def test_stream_command_invokes_stream_job(monkeypatch):
    called: dict[str, Any] = {}

    def fake_stream(job_id, settings, raw, **_):  # noqa: ANN001
        called.update({"job_id": job_id, "settings": settings, "raw": raw})

    def fake_resolve(api_base):  # noqa: ANN001
        called["api_base"] = api_base
        return API_SETTINGS

    monkeypatch.setattr(mdwb_cli, "_stream_job", fake_stream)
    monkeypatch.setattr(mdwb_cli, "_resolve_settings", fake_resolve)

    result = runner.invoke(
        mdwb_cli.cli,
        [
            "stream",
            "job555",
            "--raw",
            "--api-base",
            "https://api.example",
        ],
    )

    assert result.exit_code == 0
    assert called["job_id"] == "job555"
    assert called["settings"] is API_SETTINGS
    assert called["raw"] is True
    assert called["api_base"] == "https://api.example"


def test_stream_job_triggers_hooks_and_terminal(monkeypatch):
    lines = [
        "event: progress",
        'data: {"done": 1, "total": 2}',
        "",
        "event: state",
        "data: DONE",
        "",
    ]

    class FakeResponse:
        def __init__(self, payload: list[str]) -> None:
            self.payload = payload

        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
            return None

        def iter_lines(self):  # noqa: ANN001
            yield from self.payload

        def raise_for_status(self) -> None:
            return None

    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def stream(self, method: str, url: str, **kwargs):  # noqa: ANN001
            self.calls.append((method, url))
            return FakeResponse(lines)

    fake_client = FakeClient()
    hook_calls: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        mdwb_cli,
        "_trigger_event_hooks",
        lambda entry, hooks: hook_calls.append((entry["event"], hooks)),
    )

    terminal_states: list[str] = []
    with mdwb_cli.console.capture() as capture:
        mdwb_cli._stream_job(
            "job999",
            API_SETTINGS,
            raw=False,
            hooks={"state": ["echo done"]},
            on_terminal=lambda state, snapshot: terminal_states.append(state),
            client=cast(httpx.Client, fake_client),
        )
    output = capture.get()

    assert fake_client.calls == [("GET", "/jobs/job999/stream")]
    assert "DONE" in output
    assert terminal_states == ["DONE"]
    assert hook_calls and hook_calls[-1][0] == "state"
    assert hook_calls[-1][1] == {"state": ["echo done"]}


def test_stream_job_stops_after_terminal_state():
    lines = [
        "event: state",
        "data: DONE",
        "",
        "event: progress",
        'data: {"done": 2, "total": 2}',
        "",
    ]

    class FakeResponse:
        def __init__(self, payload: list[str]) -> None:
            self.payload = payload

        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
            return None

        def iter_lines(self):  # noqa: ANN001
            yield from self.payload

        def raise_for_status(self) -> None:
            return None

    class FakeClient:
        def stream(self, method: str, url: str, **kwargs):  # noqa: ANN001
            return FakeResponse(lines)

    fake_client = cast(httpx.Client, FakeClient())
    terminal_states: list[str] = []

    with mdwb_cli.console.capture() as capture:
        mdwb_cli._stream_job(
            "job-terminal",
            API_SETTINGS,
            raw=False,
            hooks=None,
            on_terminal=lambda state, snapshot: terminal_states.append(state),
            client=fake_client,
        )

    output = capture.get()
    assert "DONE" in output
    assert '{"done": 2' not in output
    assert terminal_states == ["DONE"]


def test_format_progress_text_with_meter(monkeypatch):
    calls = iter([0.0, 5.0])

    def fake_monotonic():
        try:
            return next(calls)
        except StopIteration:
            return 5.0

    monkeypatch.setattr(mdwb_cli.time, "monotonic", fake_monotonic)
    meter = mdwb_cli._ProgressMeter()
    text = mdwb_cli._format_progress_text({"done": 5, "total": 10}, meter=meter)
    assert "50.0%" in text
    assert "ETA" in text


def test_cli_watch_invokes_fallback_with_hooks(monkeypatch):
    called: dict[str, Any] = {}

    class DummyClient:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    dummy_client = DummyClient()

    def fake_watch_events_with_fallback(
        job_id,
        settings,
        cursor,
        follow,
        interval,
        raw,
        hooks,
        progress_meter,
        client,
    ):  # noqa: ANN001
        called.update(
            {
                "job_id": job_id,
                "settings": settings,
                "cursor": cursor,
                "follow": follow,
                "interval": interval,
                "raw": raw,
                "hooks": hooks,
                "progress_meter": progress_meter,
                "client": client,
            }
        )

    monkeypatch.setattr(mdwb_cli, "_watch_events_with_fallback", fake_watch_events_with_fallback)
    monkeypatch.setattr(mdwb_cli, "_resolve_settings", lambda api_base: API_SETTINGS)
    monkeypatch.setattr(mdwb_cli, "_client", lambda settings: dummy_client)

    result = runner.invoke(
        mdwb_cli.cli,
        [
            "watch",
            "job123",
            "--since",
            "2025-11-08T01:00:00Z",
            "--once",
            "--interval",
            "0.75",
            "--raw",
            "--no-progress",
            "--reuse-session",
            "--on",
            "state:DONE=echo",
        ],
    )

    assert result.exit_code == 0
    assert called["job_id"] == "job123"
    assert called["settings"] is API_SETTINGS
    assert called["cursor"] == "2025-11-08T01:00:00Z"
    assert called["follow"] is False
    assert called["interval"] == 0.75
    assert called["raw"] is True
    assert called["hooks"] == {"state:DONE": ["echo"]}
    assert called["progress_meter"] is None
    assert called["client"] is dummy_client
    assert dummy_client.closed


def test_watch_events_with_fallback_streams_via_sse(monkeypatch):
    def fake_watch(*args, **kwargs):
        raise httpx.RequestError("boom", request=httpx.Request("GET", "http://test"))

    calls: dict[str, object] = {}

    def fake_stream(job_id: str, settings: mdwb_cli.APISettings, raw: bool, hooks=None, progress_meter=None, client=None, **_):  # noqa: ANN001,E501
        calls["job_id"] = job_id
        calls["raw"] = raw
        calls["progress_meter"] = progress_meter
        calls["client"] = client

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
            progress_meter=None,
            client=None,
        )

    output = capture.get()
    assert "falling back to SSE stream" in output
    assert calls == {"job_id": "job123", "raw": True, "progress_meter": None, "client": None}


def test_watch_command_invokes_helper(monkeypatch):
    invoked: list[tuple] = []
    monkeypatch.setattr(mdwb_cli, "_resolve_settings", lambda api_base: API_SETTINGS)

    def fake_helper(job_id, settings, cursor, follow, interval, raw, hooks, on_terminal=None, progress_meter=None, client=None, **_):  # noqa: ANN001,E501
        invoked.append((job_id, cursor, follow, interval, raw, hooks, progress_meter, client))

    monkeypatch.setattr(mdwb_cli, "_watch_events_with_fallback", fake_helper)

    result = runner.invoke(mdwb_cli.cli, ["watch", "job123", "--interval", "0.5", "--raw", "--on", "snapshot=echo hi"])

    assert result.exit_code == 0
    assert len(invoked) == 1
    entry = invoked[0]
    assert entry[:6] == ("job123", None, True, 0.5, True, {"snapshot": ["echo hi"]})
    assert isinstance(entry[6], mdwb_cli._ProgressMeter)
    assert entry[7] is None


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


def test_trigger_event_hooks_handles_state_events(monkeypatch):
    recorded: list[tuple[str, str]] = []

    def fake_run_hook(command, event, payload):  # noqa: ANN001
        recorded.append((command, event))

    monkeypatch.setattr(mdwb_cli, "_run_hook", fake_run_hook)
    hooks = {"state:DONE": ["cmd1"], "*": ["cmd2"]}
    mdwb_cli._trigger_event_hooks({"event": "state", "payload": "DONE"}, hooks)
    assert recorded == [("cmd1", "state"), ("cmd2", "state")]
