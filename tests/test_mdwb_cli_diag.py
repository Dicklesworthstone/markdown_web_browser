from __future__ import annotations

from pathlib import Path
from typing import Any

from contextlib import contextmanager

from typer.testing import CliRunner

from scripts import mdwb_cli

runner = CliRunner()


class StubResponse:
    def __init__(self, status_code: int, payload: dict[str, Any] | None = None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):  # noqa: ANN001
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(self.text or f"HTTP {self.status_code}")


class StubClient:
    def __init__(self, responses: dict[str, StubResponse]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    def get(self, path: str):  # noqa: ANN001
        self.calls.append(path)
        response = self.responses.get(path)
        if response is None:
            raise KeyError(f"unexpected path {path}")
        return response

    def close(self) -> None:  # pragma: no cover - simple stub
        return None


def _fake_settings():
    return mdwb_cli.APISettings(base_url="http://localhost", api_key=None, warning_log_path=Path("ops/warnings.jsonl"))


def _patch_client_ctx(monkeypatch, stub):
    @contextmanager
    def fake_ctx(settings, http2=True, **kwargs):  # noqa: ANN001
        yield stub

    monkeypatch.setattr(mdwb_cli, "_client_ctx", fake_ctx)


def test_diag_uses_snapshot_manifest(monkeypatch):
    manifest = {
        "environment": {
            "cft_label": "Stable-1",
            "cft_version": "chrome-130",
            "playwright_channel": "cft",
            "playwright_version": "1.50.0",
            "browser_transport": "cdp",
            "viewport": {"width": 1280, "height": 2000, "device_scale_factor": 2},
            "screenshot_style_hash": "hash",
        },
        "timings": {"capture_ms": 10, "ocr_ms": 20, "stitch_ms": 5, "total_ms": 35},
        "warnings": [{"code": "canvas-heavy", "count": 5, "threshold": 3, "message": "canvas"}],
        "blocklist_hits": {"#banner": 2},
        "seam_markers": [
            {"tile_index": 0, "position": "top", "hash": "abc111"},
            {"tile_index": 1, "position": "bottom", "hash": "def222"},
        ],
    }
    snapshot = {"id": "job123", "url": "https://example.com", "state": "DONE", "progress": {"done": 5, "total": 5}, "manifest": manifest}
    stub = StubClient({"/jobs/job123": StubResponse(200, payload=snapshot)})
    _patch_client_ctx(monkeypatch, stub)
    monkeypatch.setattr(mdwb_cli, "_resolve_settings", lambda base: _fake_settings())

    result = runner.invoke(mdwb_cli.cli, ["diag", "job123"])

    assert result.exit_code == 0, result.output
    assert "/jobs/job123/manifest.json" not in stub.calls
    assert "CfT" in result.output
    assert "Stable-1" in result.output
    assert "Seam Markers" in result.output
    assert "abc111" in result.output


def test_diag_fetches_manifest_when_missing(monkeypatch):
    snapshot = {"id": "job456", "url": "https://example.com", "state": "DONE", "progress": {"done": 1, "total": 1}}
    manifest = {"environment": {}, "warnings": [], "blocklist_hits": {}}
    stub = StubClient(
        {
            "/jobs/job456": StubResponse(200, payload=snapshot),
            "/jobs/job456/manifest.json": StubResponse(200, payload=manifest),
        }
    )
    _patch_client_ctx(monkeypatch, stub)
    monkeypatch.setattr(mdwb_cli, "_resolve_settings", lambda base: _fake_settings())

    result = runner.invoke(mdwb_cli.cli, ["diag", "job456", "--json"])

    assert result.exit_code == 0
    assert "/jobs/job456/manifest.json" in stub.calls
    assert '"manifest_source": "manifest.json"' in result.output


def test_diag_handles_missing_job(monkeypatch):
    stub = StubClient({"/jobs/missing": StubResponse(404, text="not found")})
    _patch_client_ctx(monkeypatch, stub)
    monkeypatch.setattr(mdwb_cli, "_resolve_settings", lambda base: _fake_settings())

    result = runner.invoke(mdwb_cli.cli, ["diag", "missing"])

    assert result.exit_code == 1
    assert "not found" in result.output.lower()


def test_diag_reports_manifest_error(monkeypatch):
    snapshot = {"id": "job789", "url": "https://example.com/error", "state": "DONE", "progress": {"done": 1, "total": 1}}
    stub = StubClient(
        {
            "/jobs/job789": StubResponse(200, payload=snapshot),
            "/jobs/job789/manifest.json": StubResponse(500, payload={"detail": "manifest-deleted"}),
        }
    )
    _patch_client_ctx(monkeypatch, stub)
    monkeypatch.setattr(mdwb_cli, "_resolve_settings", lambda base: _fake_settings())

    with mdwb_cli.console.capture() as capture:
        result = runner.invoke(mdwb_cli.cli, ["diag", "job789"])

    output = capture.get()

    assert result.exit_code == 0
    assert "/jobs/job789/manifest.json" in stub.calls
    assert "Manifest Error" in output
    assert "manifest-deleted" in output


def test_diag_uses_snapshot_seam_summary(monkeypatch):
    snapshot = {
        "id": "job999",
        "url": "https://example.com/seams",
        "state": "DONE",
        "progress": {"done": 1, "total": 1},
        "seam_markers": {
            "count": 3,
            "unique_tiles": 2,
            "unique_hashes": 2,
            "sample": [
                {"tile_index": 0, "position": "top", "hash": "abc111"},
                {"tile_index": 1, "position": "bottom", "hash": "def222"},
            ],
        },
        "seam_marker_count": 3,
        "seam_hash_count": 2,
    }
    stub = StubClient(
        {
            "/jobs/job999": StubResponse(200, payload=snapshot),
            "/jobs/job999/manifest.json": StubResponse(404, payload={"detail": "missing"}),
        }
    )
    _patch_client_ctx(monkeypatch, stub)
    monkeypatch.setattr(mdwb_cli, "_resolve_settings", lambda base: _fake_settings())

    with mdwb_cli.console.capture() as capture:
        result = runner.invoke(mdwb_cli.cli, ["diag", "job999"])

    output = capture.get()
    assert result.exit_code == 0
    assert "Seam Markers" in output
    assert "abc111" in output


def test_diag_prints_seam_counts_when_summary_missing(monkeypatch):
    snapshot = {
        "id": "job998",
        "url": "https://example.com/fallback",
        "state": "DONE",
        "progress": {"done": 1, "total": 1},
        "seam_marker_count": 4,
        "seam_hash_count": 3,
    }
    stub = StubClient(
        {
            "/jobs/job998": StubResponse(200, payload=snapshot),
            "/jobs/job998/manifest.json": StubResponse(404, payload={"detail": "missing"}),
        }
    )
    _patch_client_ctx(monkeypatch, stub)
    monkeypatch.setattr(mdwb_cli, "_resolve_settings", lambda base: _fake_settings())

    with mdwb_cli.console.capture() as capture:
        result = runner.invoke(mdwb_cli.cli, ["diag", "job998"])

    output = capture.get()
    assert result.exit_code == 0
    assert "Seam markers: 4 (unique hashes: 3)" in output
