from __future__ import annotations

from pathlib import Path
from typing import Any

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


class StubClient:
    def __init__(self, response: StubResponse) -> None:
        self.response = response
        self.posts: list[tuple[str, dict[str, Any]]] = []

    def post(self, url: str, json=None):  # noqa: ANN001
        self.posts.append((url, json or {}))
        return self.response


def _fake_settings():
    return mdwb_cli.APISettings(base_url="http://localhost", api_key=None, warning_log_path=Path("ops/warnings.jsonl"))


def _monkeypatch_client(monkeypatch, response: StubResponse):  # noqa: ANN001
    client = StubClient(response)
    monkeypatch.setattr(mdwb_cli, "_client", lambda settings, http2=True: client)
    return client


def _invoke_cli(path: Path, *extra_args: str):
    return runner.invoke(mdwb_cli.cli, ["jobs", "replay", "manifest", str(path), *extra_args])


def test_jobs_replay_success(monkeypatch, tmp_path: Path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text('{"url": "https://example.com"}', encoding="utf-8")
    client = _monkeypatch_client(monkeypatch, StubResponse(200, payload={"id": "job-123"}))
    monkeypatch.setattr(mdwb_cli, "_resolve_settings", lambda base: _fake_settings())

    result = _invoke_cli(manifest)

    assert result.exit_code == 0
    assert client.posts[-1][0] == "/replay"
    assert client.posts[-1][1] == {"manifest": {"url": "https://example.com"}}
    assert "Replay submitted" in result.output
    assert "job-123" in result.output


def test_jobs_replay_json_output(monkeypatch, tmp_path: Path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text('{"url": "https://example.com"}', encoding="utf-8")
    _monkeypatch_client(monkeypatch, StubResponse(200, payload={"id": "job-123"}))
    monkeypatch.setattr(mdwb_cli, "_resolve_settings", lambda base: _fake_settings())

    result = _invoke_cli(manifest, "--json")

    assert result.exit_code == 0
    assert '"status": "ok"' in result.output
    assert '"job"' in result.output


def test_jobs_replay_invalid_json(monkeypatch, tmp_path: Path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{invalid", encoding="utf-8")
    monkeypatch.setattr(mdwb_cli, "_resolve_settings", lambda base: _fake_settings())

    result = _invoke_cli(manifest)

    assert result.exit_code != 0
    assert "Manifest is not valid JSON" in result.output


def test_jobs_replay_http_error(monkeypatch, tmp_path: Path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text('{"url": "https://example.com"}', encoding="utf-8")
    _monkeypatch_client(monkeypatch, StubResponse(500, payload={"detail": "boom"}))
    monkeypatch.setattr(mdwb_cli, "_resolve_settings", lambda base: _fake_settings())

    result = _invoke_cli(manifest)

    assert result.exit_code == 1
    assert "Replay failed" in result.output
