from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from scripts import mdwb_cli

runner = CliRunner()


class StubResponse:
    def __init__(self, status_code: int, payload=None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):  # noqa: ANN001
        return self._payload


class StubClient:
    def __init__(self, *, get=None, post=None, delete=None):  # noqa: ANN001
        self._get = get or []
        self._post = post or []
        self._delete = delete or []
        self.calls: list[tuple[str, dict]] = []

    def get(self, url: str):  # noqa: ANN001
        return self._get.pop(0)

    def post(self, url: str, json=None):  # noqa: ANN001
        self.calls.append((url, json or {}))
        return self._post.pop(0)

    def request(self, method: str, url: str, json=None):  # noqa: ANN001
        self.calls.append((url, json or {}))
        return self._delete.pop(0)


def _monkeypatch_client(monkeypatch, *, get=None, post=None, delete=None):  # noqa: ANN001
    client = StubClient(get=get, post=post, delete=delete)
    monkeypatch.setattr(mdwb_cli, "_client", lambda settings: client)
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

    assert result.exit_code == 0
    assert "not found" in result.output.lower()


def test_jobs_webhooks_add_handles_bad_request(monkeypatch):
    _monkeypatch_client(
        monkeypatch,
        post=[StubResponse(400, payload={"detail": "invalid"})],
    )
    monkeypatch.setattr(mdwb_cli, "_resolve_settings", lambda base: _fake_settings())

    result = runner.invoke(
        mdwb_cli.cli,
        ["jobs", "webhooks", "add", "job123", "https://example.com/callback", "--event", "DONE"],
    )

    assert result.exit_code == 0
    assert "rejected" in result.output.lower()


def test_jobs_webhooks_delete_success(monkeypatch):
    client = _monkeypatch_client(
        monkeypatch,
        delete=[StubResponse(200)],
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
