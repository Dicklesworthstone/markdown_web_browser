from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from scripts import mdwb_cli

runner = CliRunner()


class DummyResponse:
    def __init__(self, status_code: int, payload=None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):  # noqa: ANN001
        return self._payload or {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(self.text or f"status {self.status_code}")


def _fake_settings():
    return mdwb_cli.APISettings(
        base_url="http://localhost",
        api_key=None,
        warning_log_path=Path("ops/warnings.jsonl"),
    )


def test_fetch_with_webhook_urls(monkeypatch):
    calls: list[tuple[str, dict]] = []

    class FakeClient:
        def __init__(self) -> None:
            self.calls = calls

        def post(self, url: str, json=None):  # noqa: ANN001
            payload = json or {}
            self.calls.append((url, payload))
            if url == "/jobs":
                return DummyResponse(200, {"id": "job-123"})
            return DummyResponse(202)

    monkeypatch.setattr(mdwb_cli, "_client", lambda settings, http2=True: FakeClient())
    monkeypatch.setattr(mdwb_cli, "_resolve_settings", lambda base: _fake_settings())

    result = runner.invoke(
        mdwb_cli.cli,
        [
            "fetch",
            "https://example.com",
            "--webhook-url",
            "https://foo/hook",
            "--webhook-url",
            "https://bar/hook",
        ],
    )

    assert result.exit_code == 0
    assert calls[0][0] == "/jobs"
    assert calls[1][0] == "/jobs/job-123/webhooks"
    assert calls[2][0] == "/jobs/job-123/webhooks"


def test_fetch_handles_webhook_failure(monkeypatch):
    calls: list[tuple[str, dict]] = []

    class FakeClient:
        def post(self, url: str, json=None):  # noqa: ANN001
            payload = json or {}
            calls.append((url, payload))
            if url == "/jobs":
                return DummyResponse(200, {"id": "job-123"})
            return DummyResponse(500, text="boom")

    monkeypatch.setattr(mdwb_cli, "_client", lambda settings, http2=True: FakeClient())
    monkeypatch.setattr(mdwb_cli, "_resolve_settings", lambda base: _fake_settings())

    result = runner.invoke(
        mdwb_cli.cli,
        [
            "fetch",
            "https://example.com",
            "--webhook-url",
            "https://foo/hook",
            "--webhook-event",
            "DONE",
            "--webhook-event",
            "FAILED",
        ],
    )

    assert result.exit_code == 0
    assert "Failed to register webhook" in result.output
    assert calls[-1][1] == {"url": "https://foo/hook", "events": ["DONE", "FAILED"]}
