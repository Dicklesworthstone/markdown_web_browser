"""R8 tests: headings, follow, export, batch queries, metrics, admin cache-invalidate."""
from __future__ import annotations

import pathlib

import pytest
from fastapi.testclient import TestClient

import app.main as main_mod
from app.main import app


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_headings_returns_body_for_exact_match(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    def _structured(job_id):
        return {
            "url": "https://x.com",
            "sections": [
                {"level": 1, "heading": "Top", "body": "intro", "anchor": "top"},
                {"level": 2, "heading": "Pricing", "body": "It costs $5.", "anchor": "pricing"},
                {"level": 2, "heading": "Features", "body": "Many things.", "anchor": "features"},
            ],
        }

    fake_jm = main_mod.JobManager.__new__(main_mod.JobManager)
    fake_jm.get_structured_result = _structured
    monkeypatch.setattr(main_mod, "JOB_MANAGER", fake_jm)

    response = client.get("/jobs/abc/headings/Pricing")
    assert response.status_code == 200
    body = response.json()
    assert body["heading"] == "Pricing"
    assert body["body"] == "It costs $5."
    assert len(body["subsections"]) == 0


def test_headings_404_for_missing(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    fake_jm = main_mod.JobManager.__new__(main_mod.JobManager)
    fake_jm.get_structured_result = lambda self, jid: {"url": "", "sections": []}
    monkeypatch.setattr(main_mod, "JOB_MANAGER", fake_jm)
    response = client.get("/jobs/abc/headings/Missing")
    assert response.status_code == 404


def test_follow_resolves_anchor(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    def _read_md(self, jid):
        return "See [our pricing](https://example.com/pricing#pricing-table)."

    monkeypatch.setattr(type(main_mod.store), "read_markdown", _read_md)
    response = client.get("/jobs/abc/follow", params={"anchor": "pricing-table"})
    assert response.status_code == 200
    body = response.json()
    assert body["resolved_href"] == "https://example.com/pricing#pricing-table"
    assert body["resolved_text"] == "our pricing"


def test_export_strips_provenance(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    md = "# Title\n\nBody text.\n<!-- source: tile_0000, y=0, sha256=abc -->\nMore body.\n"

    def _read_md(self, jid):
        return md

    monkeypatch.setattr(type(main_mod.store), "read_markdown", _read_md)
    response = client.get("/jobs/abc/export.md")
    assert response.status_code == 200
    body = response.json()
    assert "source: tile_0000" not in body["markdown"]
    assert "More body." in body["markdown"]
    assert body["truncated"] is False


def test_queries_returns_sections_and_links(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    def _structured(job_id):
        return {
            "url": "https://x.com",
            "sections": [
                {"level": 1, "heading": "Top", "body": "x" * 100, "anchor": "top"},
            ],
        }

    def _read_links(self, jid):
        return {"anchors": [{"href": "https://a.com", "text": "A", "source": "dom"}]}

    fake_jm = main_mod.JobManager.__new__(main_mod.JobManager)
    fake_jm.get_structured_result = _structured
    monkeypatch.setattr(main_mod, "JOB_MANAGER", fake_jm)
    monkeypatch.setattr(type(main_mod.store), "read_links", _read_links)

    response = client.post(
        "/jobs/abc/queries", json={"queries": ["sections", "links"]}
    )
    assert response.status_code == 200
    body = response.json()
    assert "sections" in body["results"]
    assert "links" in body["results"]


def test_metrics_cache_returns_shape(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    if hasattr(main_mod.store, "cache_stats"):
        monkeypatch.delattr(type(main_mod.store), "cache_stats", raising=False)
    response = client.get("/metrics/cache")
    assert response.status_code == 200
    body = response.json()
    assert "enabled" in body
    assert body["cache_name"] == "capture"


def test_metrics_queue_returns_shape(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    def _list(self, **kwargs):
        return [
            {"id": "a", "state": "DONE", "created_at": "2026-01-01T00:00:00Z"},
        ], 1

    fake_jm = main_mod.JobManager.__new__(main_mod.JobManager)
    fake_jm.list_jobs = _list
    fake_jm._watchdog_task = None
    fake_jm._started_at = None
    fake_jm._tasks = {}
    monkeypatch.setattr(main_mod, "JOB_MANAGER", fake_jm)
    response = client.get("/metrics/queue")
    assert response.status_code == 200
    body = response.json()
    assert "by_state" in body
    assert isinstance(body["total_completed"], int)  # 0 or 1 depending on unpacking


def test_admin_cache_invalidate_returns_shape(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    response = client.post(
        "/admin/cache/invalidate", params={"url": "https://example.com"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["url"] == "https://example.com"
    assert "invalidated_at" in body


@pytest.mark.parametrize(
    "cmd",
    [
        ["jobs", "headings", "abc", "Pricing"],
        ["jobs", "follow", "abc", "section-1"],
        ["jobs", "export", "abc"],
        ["admin", "cache-invalidate", "https://example.com"],
    ],
)
def test_r8_cli_subcommands_registered(cmd) -> None:
    from typer.testing import CliRunner

    from scripts import mdwb_cli

    runner = CliRunner()
    result = runner.invoke(mdwb_cli.cli, cmd + ["--help"])
    assert result.exit_code == 0, f"{' '.join(cmd)} --help failed: {result.output}"


def test_r8_cli_jobs_headings_invokes_endpoint() -> None:
    from typer.testing import CliRunner

    from scripts import mdwb_cli

    captured: dict = {}

    def _fake_settings(base=None):
        return type(
            "S",
            (),
            {"base_url": "http://x", "api_key": None, "warning_log_path": pathlib.Path(".")},
        )()

    class _StubClient:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

        def get(self, url, params=None):
            import httpx as _hx

            captured["url"] = url
            return _hx.Response(
                200,
                json={
                    "job_id": "abc", "url": "https://x.com",
                    "heading": "Pricing", "anchor": "pricing",
                    "body": "It costs $5.", "body_chars": 10,
                    "subsections": [], "outbound_links": [],
                },
                request=_hx.Request("GET", url),
            )

    mdwb_cli.httpx.Client = _StubClient
    mdwb_cli._resolve_settings = _fake_settings

    runner = CliRunner()
    result = runner.invoke(mdwb_cli.cli, ["jobs", "headings", "abc", "Pricing"])
    assert result.exit_code == 0
    assert "Pricing" in result.output
    assert "/jobs/abc/headings/Pricing" in captured["url"]


def test_r8_cli_admin_cache_invalidate_invokes_endpoint() -> None:
    from typer.testing import CliRunner

    from scripts import mdwb_cli

    captured: dict = {}

    def _fake_settings(base=None):
        return type(
            "S",
            (),
            {"base_url": "http://x", "api_key": None, "warning_log_path": pathlib.Path(".")},
        )()

    class _StubClient:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

        def post(self, url, params=None, json=None):
            import httpx as _hx

            captured["url"] = url
            captured["params"] = params
            return _hx.Response(
                200,
                json={"url": params.get("url", ""), "removed": False, "invalidated_at": "2026-01-01T00:00:00Z"},
                request=_hx.Request("POST", url),
            )

    mdwb_cli.httpx.Client = _StubClient
    mdwb_cli._resolve_settings = _fake_settings

    runner = CliRunner()
    result = runner.invoke(
        mdwb_cli.cli, ["admin", "cache-invalidate", "https://example.com"]
    )
    assert result.exit_code == 0
    assert captured["params"]["url"] == "https://example.com"
    assert "/admin/cache/invalidate" in captured["url"]
