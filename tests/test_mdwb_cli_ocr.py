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

    def close(self) -> None:  # pragma: no cover - simple stub
        return None


class StubResponse:
    def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self):  # noqa: ANN001
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _fake_settings():
    return mdwb_cli.APISettings(base_url="http://localhost", api_key=None, warning_log_path=Path("ops/warnings.jsonl"))


def test_jobs_ocr_metrics_prints_table(monkeypatch):
    manifest = {
        "ocr_batches": [
            {
                "tile_ids": ["job-tile-0001", "job-tile-0002"],
                "latency_ms": 1200,
                "status_code": 200,
                "attempts": 1,
                "request_id": "req-1",
                "payload_bytes": 1024,
            }
        ],
        "ocr_quota": {"limit": 10, "used": 7, "threshold_ratio": 0.7, "warning_triggered": True},
    }
    response = StubResponse({"id": "job", "manifest": manifest})
    stub = StubClient({"/jobs/job": response})
    monkeypatch.setattr(mdwb_cli, "_client", lambda settings, http2=True: stub)
    monkeypatch.setattr(mdwb_cli, "_resolve_settings", lambda base: _fake_settings())

    result = runner.invoke(mdwb_cli.cli, ["jobs", "ocr-metrics", "job"])

    assert result.exit_code == 0
    assert "OCR Batches" in result.output
    assert "req-1" in result.output
    assert "70%" in result.output


def test_jobs_ocr_metrics_json_output(monkeypatch):
    manifest = {"ocr_batches": [], "ocr_quota": {"limit": None, "used": None, "threshold_ratio": 0.7}}
    response = StubResponse({"id": "job", "manifest": manifest})
    stub = StubClient({"/jobs/job": response})
    monkeypatch.setattr(mdwb_cli, "_client", lambda settings, http2=True: stub)
    monkeypatch.setattr(mdwb_cli, "_resolve_settings", lambda base: _fake_settings())

    result = runner.invoke(mdwb_cli.cli, ["jobs", "ocr-metrics", "job", "--json"])

    assert result.exit_code == 0
    assert "\"batches\": []" in result.output


def test_jobs_ocr_metrics_errors_when_manifest_missing(monkeypatch):
    response = StubResponse({"id": "job", "manifest": None})
    stub = StubClient({"/jobs/job": response})
    monkeypatch.setattr(mdwb_cli, "_client", lambda settings, http2=True: stub)
    monkeypatch.setattr(mdwb_cli, "_resolve_settings", lambda base: _fake_settings())

    result = runner.invoke(mdwb_cli.cli, ["jobs", "ocr-metrics", "job"])

    assert result.exit_code != 0
    assert "Manifest not available" in result.output


def test_show_command_prints_ocr_metrics(monkeypatch):
    manifest = {
        "ocr_batches": [
            {
                "tile_ids": ["job-tile-0001"],
                "latency_ms": 900,
                "status_code": 200,
                "attempts": 1,
                "payload_bytes": 2048,
            }
        ],
        "ocr_quota": {"limit": None, "used": None, "threshold_ratio": 0.7},
    }
    snapshot = {"id": "job", "manifest": manifest}
    monkeypatch.setattr(mdwb_cli, "_fetch_job_snapshot", lambda job_id, settings, http2=True: snapshot)
    monkeypatch.setattr(mdwb_cli, "_resolve_settings", lambda base: _fake_settings())

    result = runner.invoke(mdwb_cli.cli, ["show", "job", "--ocr-metrics"])

    assert result.exit_code == 0
    assert "OCR Batches" in result.output
