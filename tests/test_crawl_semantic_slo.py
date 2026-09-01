"""Tests for the depth-1 crawler, semantic post opt-in, and SLO rollup endpoint."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import _compute_live_slo_summary, app
from app.schemas import JobCreateRequest


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


# ---------------------------------------------------------------------------
# Crawl endpoint surface
# ---------------------------------------------------------------------------


def test_crawl_request_schema_round_trip() -> None:
    from app.schemas import CrawlRequest

    req = CrawlRequest(
        url="https://example.com",
        max_pages=5,
        max_depth=1,
        domain_allowlist=["example.com", "docs.example.com"],
        respect_robots_txt=False,
        crawl_delay_ms=250,
        reuse_cache=False,
    )
    payload = req.model_dump()
    assert payload["max_pages"] == 5
    assert payload["domain_allowlist"] == ["example.com", "docs.example.com"]
    # Round-trip via JSON
    again = CrawlRequest(**payload)
    assert str(again.url).startswith("https://example.com")


def test_crawl_endpoint_returns_crawl_id(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    """POST /jobs/crawl should return a crawl_id without actually launching."""
    fake_state: dict[str[str, object]] = {}

    class _FakeOrchestrator:
        async def start_crawl(self, config, capture_fn=None):  # noqa: ANN001
            fake_state["seed"] = config.seed_url
            return "crawl_test123"

        def get_crawl_status(self, crawl_id: str):  # noqa: ANN001
            return {
                "crawl_id": crawl_id,
                "seed_url": fake_state.get("seed", ""),
                "status": "running",
                "started_at": "2026-01-01T00:00:00Z",
                "finished_at": None,
                "max_pages": 10,
                "max_depth": 1,
                "visited": 0,
                "completed": 0,
                "failed": 0,
                "pending": 1,
                "queued_urls": [fake_state.get("seed", "")],
                "results": [],
            }

    monkeypatch.setattr("app.main._crawler", _FakeOrchestrator())
    response = client.post(
        "/jobs/crawl",
        json={
            "url": "https://example.com",
            "max_pages": 5,
            "domain_allowlist": ["example.com"],
            "respect_robots_txt": True,
            "crawl_delay_ms": 500,
            "reuse_cache": True,
        },
    )
    assert response.status_code == 202
    body = response.json()
    assert body["crawl_id"] == "crawl_test123"
    assert body["seed_url"].rstrip("/") == "https://example.com"


def test_crawl_status_endpoint_returns_not_found(client: TestClient) -> None:
    response = client.get("/crawl/does-not-exist")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Semantic post opt-in plumbing
# ---------------------------------------------------------------------------


def test_job_create_request_accepts_semantic_post_fields() -> None:
    req = JobCreateRequest(
        url="https://example.com",
        semantic_post_enabled=True,
        semantic_post_max_chars=12345,
    )
    assert req.semantic_post_enabled is True
    assert req.semantic_post_max_chars == 12345


def test_capture_manifest_exposes_semantic_post_fields() -> None:
    """CaptureManifest must carry the new fields so /jobs/{id}/manifest.json surfaces them."""
    from app.capture import CaptureManifest

    fields = CaptureManifest.__dataclass_fields__  # type: ignore[attr-defined]
    assert "semantic_post_summary" in fields
    assert "semantic_post_ms" in fields


def test_semantic_post_disabled_is_noop_when_request_unset() -> None:
    req = JobCreateRequest(url="https://example.com")
    assert req.semantic_post_enabled is False
    assert req.semantic_post_max_chars is None


# ---------------------------------------------------------------------------
# SLO rollup endpoint
# ---------------------------------------------------------------------------


def test_metrics_slo_no_data_when_no_manifests(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    summary = _compute_live_slo_summary()
    assert summary["status"] == "no-data"
    assert summary["categories"] == {}


def test_metrics_slo_returns_rollup_when_manifest_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    smoke = tmp_path / "benchmarks" / "production"
    smoke.mkdir(parents=True)
    fake_entries = [
        {"category": "news", "timings": {"capture_ms": 1000, "ocr_ms": 4000, "stitch_ms": 200, "total_ms": 5200}},
        {"category": "news", "timings": {"capture_ms": 1500, "ocr_ms": 5000, "stitch_ms": 250, "total_ms": 6750}},
        {"category": "dashboards", "timings": {"capture_ms": 2000, "ocr_ms": 6000, "stitch_ms": 300, "total_ms": 8300}},
    ]
    (smoke / "latest_manifest_index.json").write_text(json.dumps(fake_entries))
    monkeypatch.chdir(tmp_path)

    summary = _compute_live_slo_summary()
    assert summary["status"] == "ok"
    assert summary["entry_count"] == 3
    # The compute_slo_summary returns {"categories": {...}, "aggregate": {...}, "status": ...}
    # _compute_live_slo_summary wraps that into {"categories": <rollup>, ...}
    inner = summary["categories"]
    assert "categories" in inner  # nested by compute_slo_summary
    assert "news" in inner["categories"]
    news = inner["categories"]["news"]
    assert news["count"] == 2
    assert news["p50_total_ms"] == 5975
    assert news["p95_total_ms"] >= news["p50_total_ms"]


def test_metrics_slo_endpoint_is_reachable(client: TestClient) -> None:
    response = client.get("/metrics/slo")
    assert response.status_code == 200
    assert "status" in response.json()
    assert response.json()["status"] in {"ok", "no-data"}


# ---------------------------------------------------------------------------
# mdwb CLI discover command
# ---------------------------------------------------------------------------


def test_cli_crawl_command_registered() -> None:
    from typer.testing import CliRunner
    from scripts import mdwb_cli

    runner = CliRunner()
    result = runner.invoke(mdwb_cli.cli, ["--help"])
    assert result.exit_code == 0
    output = result.output
    # Typer flattens subcommands into the top-level help for root commands
    for cmd in ("crawl", "discover", "slo"):
        assert cmd in output, f"{cmd} not in CLI help"


def test_cli_discover_no_live_runs_offline() -> None:
    """`mdwb discover --no-live` should run without hitting any HTTP endpoint."""
    from typer.testing import CliRunner

    from scripts import mdwb_cli

    runner = CliRunner()
    result = runner.invoke(mdwb_cli.cli, ["discover", "--no-live"])
    assert result.exit_code == 0
    # The static catalog should always mention the headline endpoints + CLI commands
    assert "POST /jobs" in result.output
    assert "mdwb fetch" in result.output
    assert "mdwb crawl" in result.output
    assert "mdwb discover" in result.output


def test_cli_crawl_help_works() -> None:
    """Verify `mdwb crawl --help` runs without hitting the API."""
    from typer.testing import CliRunner

    from scripts import mdwb_cli

    runner = CliRunner()
    result = runner.invoke(mdwb_cli.cli, ["crawl", "--help"])
    assert result.exit_code == 0
    assert "--max-pages" in result.output
    assert "--no-watch" in result.output


def test_cli_discover_help_works() -> None:
    from typer.testing import CliRunner

    from scripts import mdwb_cli

    runner = CliRunner()
    result = runner.invoke(mdwb_cli.cli, ["discover", "--help"])
    assert result.exit_code == 0
    assert "--live" in result.output


def test_cli_slo_help_works() -> None:
    from typer.testing import CliRunner

    from scripts import mdwb_cli

    runner = CliRunner()
    result = runner.invoke(mdwb_cli.cli, ["slo", "--help"])
    assert result.exit_code == 0
    assert "--json" in result.output