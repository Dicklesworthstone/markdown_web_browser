from __future__ import annotations

from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from scripts import mdwb_cli

runner = CliRunner()


class StubClient:
    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses

    def get(self, path: str):  # noqa: ANN001
        return self.responses[path]

    def post(self, path: str, json=None):  # noqa: ANN001
        return self.responses[path]


class StubResponse:
    def __init__(self, status_code: int, text: str = "", payload=None) -> None:
        self.status_code = status_code
        if not text and payload is not None:
            text = mdwb_cli.json.dumps(payload)
        self.text = text
        self._payload = payload
        self.content: bytes | None = None

    def json(self):  # noqa: ANN001
        if self._payload is not None:
            return self._payload
        return mdwb_cli.json.loads(self.text)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(self.text or f"HTTP {self.status_code}")


def _fake_settings():
    return mdwb_cli.APISettings(base_url="http://localhost", api_key=None, warning_log_path=Path("ops/warnings.jsonl"))


def test_jobs_manifest_writes_pretty_json(monkeypatch, tmp_path: Path):
    response = StubResponse(200, payload={"cft_version": "chrome-130"})
    stub = StubClient({"/jobs/job123/manifest.json": response})
    monkeypatch.setattr(mdwb_cli, "_client", lambda settings: stub)
    monkeypatch.setattr(mdwb_cli, "_resolve_settings", lambda base: _fake_settings())
    out_path = tmp_path / "manifest.json"

    result = runner.invoke(mdwb_cli.cli, ["jobs", "artifacts", "manifest", "job123", "--out", str(out_path)])

    assert result.exit_code == 0
    assert out_path.read_text().strip() == mdwb_cli.json.dumps({"cft_version": "chrome-130"}, indent=2)


def test_jobs_markdown_prints_to_stdout(monkeypatch):
    response = StubResponse(200, text="# Hello")
    stub = StubClient({"/jobs/job321/result.md": response})
    monkeypatch.setattr(mdwb_cli, "_client", lambda settings: stub)
    monkeypatch.setattr(mdwb_cli, "_resolve_settings", lambda base: _fake_settings())

    result = runner.invoke(mdwb_cli.cli, ["jobs", "artifacts", "markdown", "job321"])

    assert result.exit_code == 0
    assert "# Hello" in result.output


def test_jobs_links_handles_not_found(monkeypatch):
    response = StubResponse(404, text="not found")
    stub = StubClient({"/jobs/missing/links.json": response})
    monkeypatch.setattr(mdwb_cli, "_client", lambda settings: stub)
    monkeypatch.setattr(mdwb_cli, "_resolve_settings", lambda base: _fake_settings())

    result = runner.invoke(mdwb_cli.cli, ["jobs", "artifacts", "links", "missing"])

    assert result.exit_code != 0
    assert "not found" in result.output.lower()


def test_jobs_replay_manifest(monkeypatch, tmp_path: Path):
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(mdwb_cli.json.dumps({"url": "https://example.com"}), encoding="utf-8")
    response = StubResponse(200, payload={"job_id": "replay-1"})
    stub = StubClient({"/replay": response})
    monkeypatch.setattr(mdwb_cli, "_client", lambda settings, **_: stub)
    monkeypatch.setattr(mdwb_cli, "_resolve_settings", lambda base: _fake_settings())

    result = runner.invoke(mdwb_cli.cli, ["jobs", "replay", str(manifest_path)])

    assert result.exit_code == 0
    assert "Replay submitted" in result.output


def test_jobs_bundle_writes_file(monkeypatch, tmp_path: Path):
    response = StubResponse(200, text="", payload=None)
    response.content = b"bundle-bytes"
    stub = StubClient({"/jobs/job789/artifact/bundle.tar.zst": response})
    monkeypatch.setattr(mdwb_cli, "_client", lambda settings: stub)
    monkeypatch.setattr(mdwb_cli, "_resolve_settings", lambda base: _fake_settings())
    out_path = tmp_path / "bundle.tar.zst"

    result = runner.invoke(
        mdwb_cli.cli,
        ["jobs", "artifacts", "bundle", "job789", "--out", str(out_path)],
    )

    assert result.exit_code == 0
    assert out_path.read_bytes() == b"bundle-bytes"


def test_jobs_bundle_alias_defaults_output_path(monkeypatch, tmp_path: Path):
    response = StubResponse(200, text="", payload=None)
    response.content = b"default-bundle"
    stub = StubClient({"/jobs/job000/artifact/bundle.tar.zst": response})
    monkeypatch.setattr(mdwb_cli, "_client", lambda settings: stub)
    monkeypatch.setattr(mdwb_cli, "_resolve_settings", lambda base: _fake_settings())

    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(mdwb_cli.cli, ["jobs", "bundle", "job000"])

        assert result.exit_code == 0
        assert Path("job000-bundle.tar.zst").read_bytes() == b"default-bundle"
