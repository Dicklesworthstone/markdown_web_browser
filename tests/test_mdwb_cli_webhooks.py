from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import httpx
from typer.testing import CliRunner

from scripts import mdwb_cli

runner = CliRunner()


class StubResponse:
    def __init__(self, status_code: int, payload=None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):  # noqa: ANN001
        return self._payload if self._payload is not None else {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(self.text or f"status {self.status_code}")


class StubClient:
    def __init__(self, *, get=None, post=None, delete=None):  # noqa: ANN001
        self._get = get or []
        self._post = post or []
        self._delete = delete or []
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get(self, url: str):  # noqa: ANN001
        return self._get.pop(0)

    def post(self, url: str, json=None):  # noqa: ANN001
        self.calls.append((url, json or {}))
        return self._post.pop(0)

    def delete(self, url: str, json=None):  # noqa: ANN001
        self.calls.append((url, json or {}))
        return self._delete.pop(0)

    def request(self, method: str, url: str, json=None):  # noqa: ANN001
        if method != "DELETE":
            raise AssertionError(f"Unexpected method {method}")
        return self.delete(url, json=json)


def _monkeypatch_client(monkeypatch, *, get=None, post=None, delete=None):  # noqa: ANN001
    client = StubClient(get=get, post=post, delete=delete)
    monkeypatch.setattr(mdwb_cli, "_client", lambda settings, **_: client)
    return client


def _fake_settings():
    return mdwb_cli.APISettings(base_url="http://localhost", api_key=None, warning_log_path=Path("ops/warnings.jsonl"))


def test_jobs_webhooks_list_success(monkeypatch):
    _monkeypatch_client(
        monkeypatch,
        get=[StubResponse(200, payload=[{"url": "https://example.com", "events": ["DONE"]}])],
    )
    monkeypatch.setattr(mdwb_cli, "_resolve_settings", lambda base: _fake_settings())

    result = runner.invoke(mdwb_cli.cli, ["jobs", "webhooks", "list", "job123"])

    assert result.exit_code == 0
    assert "https://example.com" in result.output


def test_jobs_webhooks_list_not_found(monkeypatch):
    _monkeypatch_client(monkeypatch, get=[StubResponse(404)])
    monkeypatch.setattr(mdwb_cli, "_resolve_settings", lambda base: _fake_settings())

    result = runner.invoke(mdwb_cli.cli, ["jobs", "webhooks", "list", "job-missing"])

    assert result.exit_code != 0
    assert "not found" in result.output.lower()


def test_jobs_webhooks_add_handles_bad_request(monkeypatch):
    _monkeypatch_client(monkeypatch, post=[StubResponse(400, payload={"detail": "invalid"})])
    monkeypatch.setattr(mdwb_cli, "_resolve_settings", lambda base: _fake_settings())

    result = runner.invoke(
        mdwb_cli.cli,
        ["jobs", "webhooks", "add", "job123", "https://example.com/callback", "--event", "DONE"],
    )

    assert result.exit_code != 0
    assert "invalid" in result.output.lower()


def test_jobs_webhooks_delete_success(monkeypatch):
    client = _monkeypatch_client(
        monkeypatch,
        delete=[StubResponse(200, payload={"job_id": "job123", "deleted": 1})],
    )
    monkeypatch.setattr(mdwb_cli, "_resolve_settings", lambda base: _fake_settings())

    result = runner.invoke(
        mdwb_cli.cli,
        ["jobs", "webhooks", "delete", "job123", "--id", "5"],
    )

    assert result.exit_code == 0
    assert client.calls[-1][0].endswith("/jobs/job123/webhooks")


def test_jobs_webhooks_delete_requires_identifier():
    result = runner.invoke(mdwb_cli.cli, ["jobs", "webhooks", "delete", "job123"])

    assert result.exit_code != 0
    assert "Provide --id or --url" in result.output


def test_jobs_webhooks_delete_json_success(monkeypatch):
    _monkeypatch_client(
        monkeypatch,
        delete=[StubResponse(200, payload={"job_id": "job321", "deleted": 2})],
    )
    monkeypatch.setattr(mdwb_cli, "_resolve_settings", lambda base: _fake_settings())

    result = runner.invoke(
        mdwb_cli.cli,
        ["jobs", "webhooks", "delete", "job321", "--url", "https://example.com/hook", "--json"],
    )

    assert result.exit_code == 0
    assert '"deleted": 2' in result.output
    assert '"request": {' in result.output
    assert '"url": "https://example.com/hook"' in result.output


def test_jobs_webhooks_delete_json_error(monkeypatch):
    _monkeypatch_client(
        monkeypatch,
        delete=[StubResponse(404, payload={"detail": "not found"})],
    )
    monkeypatch.setattr(mdwb_cli, "_resolve_settings", lambda base: _fake_settings())

    result = runner.invoke(
        mdwb_cli.cli,
        ["jobs", "webhooks", "delete", "job321", "--url", "https://example.com/hook", "--json"],
    )

    assert result.exit_code == 1
    assert '"status": "error"' in result.output
    assert "not found" in result.output


def test_jobs_webhooks_delete_reports_bad_request(monkeypatch):
    _monkeypatch_client(
        monkeypatch,
        delete=[StubResponse(400, payload={"detail": "selector invalid"})],
    )
    monkeypatch.setattr(mdwb_cli, "_resolve_settings", lambda base: _fake_settings())

    result = runner.invoke(
        mdwb_cli.cli,
        ["jobs", "webhooks", "delete", "job789", "--id", "9", "--url", "https://example.com"],
    )

    assert result.exit_code == 1
    assert "selector invalid" in result.output


def test_fetch_registers_webhooks(monkeypatch):
    post_responses = [
        StubResponse(200, payload={"id": "job-77"}),
        StubResponse(200, payload={"registered": True}),
        StubResponse(200, payload={"registered": True}),
    ]
    client = _monkeypatch_client(monkeypatch, post=post_responses)
    monkeypatch.setattr(mdwb_cli, "_resolve_settings", lambda base: _fake_settings())

    result = runner.invoke(
        mdwb_cli.cli,
        [
            "fetch",
            "https://example.com",
            "--no-watch",
            "--webhook-url",
            "https://hook/a",
            "--webhook-url",
            "https://hook/b",
            "--webhook-event",
            "DONE",
        ],
    )

    assert result.exit_code == 0
    webhook_calls = [call for call in client.calls if call[0].endswith("/webhooks")]
    assert len(webhook_calls) == 2
    assert "Registered 2 webhook(s)" in result.output


def test_delete_helper_builds_payload_for_id():
    client = StubClient(delete=[StubResponse(200, payload={"deleted": 1})])

    response, payload = mdwb_cli._delete_job_webhooks(
        cast(httpx.Client, client),
        "job123",
        webhook_id=5,
        url=None,
    )

    assert response.status_code == 200
    assert payload == {"id": 5}
    assert client.calls[-1][1] == {"id": 5}


def test_delete_helper_builds_payload_for_url():
    client = StubClient(delete=[StubResponse(200, payload={"deleted": 1})])

    response, payload = mdwb_cli._delete_job_webhooks(
        cast(httpx.Client, client),
        "job123",
        webhook_id=None,
        url="https://example.com/hook",
    )

    assert response.status_code == 200
    assert payload == {"url": "https://example.com/hook"}
    assert client.calls[-1][1] == {"url": "https://example.com/hook"}
def test_jobs_webhooks_add_json(monkeypatch):
    _monkeypatch_client(
        monkeypatch,
        post=[StubResponse(200, payload={"job_id": "job123", "url": "https://example.com/callback", "events": ["DONE"]})],
    )
    monkeypatch.setattr(mdwb_cli, "_resolve_settings", lambda base: _fake_settings())

    result = runner.invoke(
        mdwb_cli.cli,
        ["jobs", "webhooks", "add", "job123", "https://example.com/callback", "--json"],
    )

    assert result.exit_code == 0
    assert '"status": "ok"' in result.output
def test_jobs_webhooks_add_json_error(monkeypatch):
    _monkeypatch_client(monkeypatch, post=[StubResponse(404, payload={"detail": "job missing"})])
    monkeypatch.setattr(mdwb_cli, "_resolve_settings", lambda base: _fake_settings())

    result = runner.invoke(
        mdwb_cli.cli,
        ["jobs", "webhooks", "add", "job404", "https://example.com/callback", "--json"],
    )

    assert result.exit_code != 0
    assert '"status": "error"' in result.output
def test_jobs_webhooks_list_json(monkeypatch):
    _monkeypatch_client(
        monkeypatch,
        get=[StubResponse(200, payload=[{"url": "https://example.com", "events": ["DONE"]}])],
    )
    monkeypatch.setattr(mdwb_cli, "_resolve_settings", lambda base: _fake_settings())

    result = runner.invoke(mdwb_cli.cli, ["jobs", "webhooks", "list", "job123", "--json"])

    assert result.exit_code == 0
    assert '"status": "ok"' in result.output
    assert '"webhooks":' in result.output


def test_jobs_webhooks_list_json_not_found(monkeypatch):
    _monkeypatch_client(monkeypatch, get=[StubResponse(404, payload={"detail": "missing"})])
    monkeypatch.setattr(mdwb_cli, "_resolve_settings", lambda base: _fake_settings())

    result = runner.invoke(mdwb_cli.cli, ["jobs", "webhooks", "list", "job404", "--json"])

    assert result.exit_code != 0
    assert '"status": "error"' in result.output
