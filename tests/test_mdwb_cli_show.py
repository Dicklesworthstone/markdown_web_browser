from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from scripts import mdwb_cli

runner = CliRunner()


class StubResponse:
    def __init__(self, payload: Any, status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code

    def json(self):  # noqa: ANN001
        return self.payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class StubClient:
    def __init__(self, responses: dict[str, StubResponse]) -> None:
        self.responses = responses

    def get(self, path: str):  # noqa: ANN001
        response = self.responses.get(path)
        if response is None:
            raise KeyError(f"unexpected path {path}")
        return response

    def close(self) -> None:  # pragma: no cover - simple stub
        return None


def _fake_settings():
    return mdwb_cli.APISettings(
        base_url="http://localhost", api_key=None, warning_log_path=Path("ops/warnings.jsonl")
    )


def _patch_client_ctx(monkeypatch, responses: dict[str, StubResponse]) -> None:
    def fake_client_ctx(settings, http2=True, timeout=None):  # noqa: ANN001
        @contextmanager
        def _ctx():
            yield StubClient(responses)

        return _ctx()

    monkeypatch.setattr(mdwb_cli, "_client_ctx", fake_client_ctx)


def test_jobs_show_prints_sweep_and_validation_summary(monkeypatch):
    manifest = {
        "warnings": [],
        "blocklist_hits": {},
        "sweep_stats": {
            "sweep_count": 5,
            "shrink_events": 1,
            "retry_attempts": 1,
            "overlap_pairs": 4,
            "overlap_match_ratio": 0.82,
        },
        "overlap_match_ratio": 0.82,
        "validation_failures": ["tile checksum mismatch"],
    }
    snapshot = {
        "id": "job123",
        "state": "DONE",
        "url": "https://example.com",
        "progress": {"done": 5, "total": 5},
        "manifest": manifest,
    }
    responses = {"/jobs/job123": StubResponse(snapshot)}

    _patch_client_ctx(monkeypatch, responses)
    monkeypatch.setattr(mdwb_cli, "_resolve_settings", lambda base: _fake_settings())

    result = runner.invoke(mdwb_cli.cli, ["show", "job123"])

    assert result.exit_code == 0
    assert "sweep" in result.output.lower()
    assert "ratio=0.82" in result.output
    assert "validation" in result.output.lower()
    assert "checksum mismatch" in result.output


def test_jobs_show_prints_seam_summary_without_manifest(monkeypatch):
    snapshot = {
        "id": "job789",
        "state": "DONE",
        "url": "https://example.com/seams",
        "progress": {"done": 1, "total": 1},
        "manifest": None,
        "seam_markers": {
            "count": 2,
            "unique_tiles": 2,
            "unique_hashes": 2,
            "sample": [
                {"tile_index": 0, "position": "top", "hash": "hasha"},
                {"tile_index": 1, "position": "bottom", "hash": "hashb"},
            ],
        },
    }
    responses = {"/jobs/job789": StubResponse(snapshot)}
    _patch_client_ctx(monkeypatch, responses)
    monkeypatch.setattr(mdwb_cli, "_resolve_settings", lambda base: _fake_settings())

    result = runner.invoke(mdwb_cli.cli, ["show", "job789"])

    assert result.exit_code == 0
    assert "Seam Markers" in result.output
    assert "hasha" in result.output


def test_jobs_show_prints_seam_counts_when_summary_missing(monkeypatch):
    snapshot = {
        "id": "job788",
        "state": "DONE",
        "url": "https://example.com/seams2",
        "progress": {"done": 1, "total": 1},
        "manifest": None,
        "seam_marker_count": 5,
        "seam_hash_count": 4,
    }
    responses = {"/jobs/job788": StubResponse(snapshot)}
    _patch_client_ctx(monkeypatch, responses)
    monkeypatch.setattr(mdwb_cli, "_resolve_settings", lambda base: _fake_settings())

    result = runner.invoke(mdwb_cli.cli, ["show", "job788"])

    assert result.exit_code == 0
    assert "Seam markers: 5 (unique hashes: 4)" in result.output


def test_demo_snapshot_prints_links(monkeypatch):
    payload = {
        "id": "demo-job",
        "state": "DONE",
        "links": [
            {"text": "Docs", "href": "https://example.com/docs", "source": "dom", "delta": "match"},
        ],
    }
    responses = {"/jobs/demo": StubResponse(payload)}
    _patch_client_ctx(monkeypatch, responses)
    monkeypatch.setattr(mdwb_cli, "_resolve_settings", lambda base: _fake_settings())

    result = runner.invoke(mdwb_cli.cli, ["demo", "snapshot"])

    assert result.exit_code == 0
    assert "demo-job" in result.output
    assert "https://example.com/docs" in result.output


def test_demo_snapshot_json_output(monkeypatch):
    payload = {"id": "demo-json", "links": []}
    responses = {"/jobs/demo": StubResponse(payload)}
    _patch_client_ctx(monkeypatch, responses)
    monkeypatch.setattr(mdwb_cli, "_resolve_settings", lambda base: _fake_settings())

    result = runner.invoke(mdwb_cli.cli, ["demo", "snapshot", "--json"])

    assert result.exit_code == 0
    assert '"id": "demo-json"' in result.output
    assert '"links": []' in result.output


def test_demo_snapshot_toon_fallback(monkeypatch):
    payload = {"id": "demo-toon", "links": []}
    responses = {"/jobs/demo": StubResponse(payload)}
    _patch_client_ctx(monkeypatch, responses)
    monkeypatch.setattr(mdwb_cli, "_resolve_settings", lambda base: _fake_settings())
    monkeypatch.setattr(mdwb_cli, "_toon_available", lambda: False)

    result = runner.invoke(mdwb_cli.cli, ["demo", "snapshot", "--format", "toon"])

    assert result.exit_code == 0
    assert '"id": "demo-toon"' in result.output


def test_demo_links_prints_table(monkeypatch):
    links = [
        {"text": "Homepage", "href": "https://example.com", "source": "dom", "delta": "match"},
    ]
    responses = {"/jobs/demo/links.json": StubResponse(links)}
    _patch_client_ctx(monkeypatch, responses)
    monkeypatch.setattr(mdwb_cli, "_resolve_settings", lambda base: _fake_settings())

    result = runner.invoke(mdwb_cli.cli, ["demo", "links"])

    assert result.exit_code == 0
    assert "Links" in result.output
    assert "Homepage" in result.output
    assert "https://example.com" in result.output


def test_demo_links_json_output(monkeypatch):
    links = [{"text": "Archive", "href": "https://example.com/archive"}]
    responses = {"/jobs/demo/links.json": StubResponse(links)}
    _patch_client_ctx(monkeypatch, responses)
    monkeypatch.setattr(mdwb_cli, "_resolve_settings", lambda base: _fake_settings())

    result = runner.invoke(mdwb_cli.cli, ["demo", "links", "--json"])

    assert result.exit_code == 0
    assert '"Archive"' in result.output
    assert '"https://example.com/archive"' in result.output
