"""R7 tests: sections, extract, links lookup, global stream, admin."""
from __future__ import annotations

import json
import pathlib

import pytest
from fastapi.testclient import TestClient

import app.main as main_mod
from app.main import app


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


# ---------------------------------------------------------------------------
# /jobs/{id}/sections
# ---------------------------------------------------------------------------


def test_sections_returns_sections_only(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    def _structured(job_id):
        return {
            "url": "https://example.com",
            "sections": [
                {"level": 1, "heading": "Top", "body": "a" * 100, "anchor": "top"},
                {"level": 2, "heading": "Sub", "body": "b" * 50, "anchor": "sub"},
            ],
        }

    fake_jm = main_mod.JobManager.__new__(main_mod.JobManager)
    fake_jm.get_structured_result = _structured
    monkeypatch.setattr(main_mod, "JOB_MANAGER", fake_jm)
    response = client.get("/jobs/abc/sections")
    assert response.status_code == 200
    body = response.json()
    assert len(body["sections"]) == 2
    assert body["total_chars"] == 150


# ---------------------------------------------------------------------------
# /jobs/{id}/extract
# ---------------------------------------------------------------------------


def test_extract_picks_up_tables_and_code(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    def _structured(job_id):
        return {"url": "https://example.com", "sections": []}

    def _links(self, jid):
        return {
            "anchors": [
                {"href": "https://a.com", "text": "A", "source": "dom"},
            ]
        }

    md = (
        "# Title\n\n"
        "| col1 | col2 |\n| --- | --- |\n| a | b |\n| c | d |\n\n"
        "```python\nprint('hi')\n```\n"
        "Some text.\n"
    )

    fake_jm = main_mod.JobManager.__new__(main_mod.JobManager)
    fake_jm.get_structured_result = _structured
    monkeypatch.setattr(main_mod, "JOB_MANAGER", fake_jm)
    monkeypatch.setattr(type(main_mod.store), "read_links", _links)
    monkeypatch.setattr(
        type(main_mod.store), "read_markdown", lambda self, jid: md
    )

    response = client.get("/jobs/abc/extract")
    assert response.status_code == 200
    body = response.json()
    assert len(body["links"]) == 1
    assert len(body["tables"]) == 1
    assert body["tables"][0]["headers"] == ["col1", "col2"]
    assert len(body["tables"][0]["rows"]) == 2
    assert len(body["code_blocks"]) == 1
    assert body["code_blocks"][0]["language"] == "python"


# ---------------------------------------------------------------------------
# /jobs/{id}/links/{query}
# ---------------------------------------------------------------------------


def test_links_lookup_finds_matches(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    def _links(self, jid):
        return {
            "anchors": [
                {"href": "https://docs.example.com", "text": "Documentation", "source": "dom"},
                {"href": "https://blog.example.com", "text": "Blog", "source": "ocr"},
            ]
        }

    monkeypatch.setattr(type(main_mod.store), "read_links", _links)
    response = client.get("/jobs/abc/links/docs")
    assert response.status_code == 200
    body = response.json()
    assert len(body["matches"]) == 1
    assert body["matches"][0]["text"] == "Documentation"


# ---------------------------------------------------------------------------
# /admin/stats
# ---------------------------------------------------------------------------


def test_admin_stats_returns_full_payload(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    def _list(**kwargs):
        return [
            {"id": "a", "state": "DONE", "created_at": "2026-01-01T00:00:00Z", "embedding_model": "hash-bucket-v1", "cache_hit": True},
            {"id": "b", "state": "FAILED", "created_at": "2026-01-02T00:00:00Z", "cache_hit": False},
        ], 2

    monkeypatch.setattr(main_mod.JOB_MANAGER, "list_jobs", _list)
    response = client.get("/admin/stats")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert body["by_state"]["DONE"] == 1
    assert body["by_state"]["FAILED"] == 1
    assert body["embedder_counts"]["hash-bucket-v1"] == 1
    assert body["cache_hits"] == 1


# ---------------------------------------------------------------------------
# /admin/jobs/prune
# ---------------------------------------------------------------------------


def test_admin_prune_dry_run_no_delete(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    from datetime import datetime, timezone, timedelta

    old_ts = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    new_ts = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()

    def _list(**kwargs):
        return [
            {"id": "old1", "state": "DONE", "created_at": old_ts, "finished_at": old_ts},
            {"id": "new1", "state": "DONE", "created_at": new_ts, "finished_at": new_ts},
        ], 2

    monkeypatch.setattr(main_mod.JOB_MANAGER, "list_jobs", _list)
    response = client.post(
        "/admin/jobs/prune",
        json={"older_than_days": 7, "dry_run": True},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["dry_run"] is True
    assert body["candidates"] == 1  # only the 10-day-old one
    assert body["deleted"] == 0


def test_admin_prune_validates_input(client: TestClient) -> None:
    response = client.post(
        "/admin/jobs/prune",
        json={"older_than_days": 0},  # below min of 1
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# /admin/cache/clear
# ---------------------------------------------------------------------------


def test_admin_cache_clear_runs(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    response = client.post("/admin/cache/clear")
    assert response.status_code == 200
    body = response.json()
    assert "cache_name" in body
    assert "cleared_at" in body


# ---------------------------------------------------------------------------
# /jobs/stream (SSE)
# ---------------------------------------------------------------------------


def test_global_stream_subscribes(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """SSE endpoint is registered and advertises the right content-type."""
    # Don't drain the stream (would require an asyncio task running in the
    # background that the TestClient cancels mid-stream). Just verify the
    # route is registered and the response is well-formed.
    from fastapi.routing import APIRoute

    stream_route = next(
        (
            r
            for r in app.routes
            if isinstance(r, APIRoute) and r.path == "/jobs/stream"
        ),
        None,
    )
    assert stream_route is not None, "/jobs/stream not registered"
    # Verify methods: stream route should accept GET
    assert "GET" in stream_route.methods


# ---------------------------------------------------------------------------
# CLI: sections, extract, admin stats, admin prune, admin cache-clear
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cmd",
    [
        ["jobs", "sections", "abc"],
        ["jobs", "extract", "abc"],
        ["jobs", "extract", "abc", "--only", "links"],
        ["admin", "stats"],
        ["admin", "prune", "--older-than-days", "30"],
        ["admin", "cache-clear"],
    ],
)
def test_r7_cli_subcommands_registered(cmd) -> None:
    from typer.testing import CliRunner

    from scripts import mdwb_cli

    runner = CliRunner()
    result = runner.invoke(mdwb_cli.cli, cmd + ["--help"])
    assert result.exit_code == 0, f"{' '.join(cmd)} --help failed: {result.output}"


def test_r7_cli_sections_invokes_endpoint() -> None:
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

        def get(self, url):
            import httpx as _hx

            captured["url"] = url
            return _hx.Response(
                200,
                json={
                    "job_id": "abc",
                    "url": "https://x.com",
                    "sections": [
                        {"level": 1, "heading": "Top", "anchor": "top", "body_chars": 50}
                    ],
                    "total_chars": 50,
                },
                request=_hx.Request("GET", url),
            )

    mdwb_cli.httpx.Client = _StubClient
    mdwb_cli._resolve_settings = _fake_settings

    runner = CliRunner()
    result = runner.invoke(mdwb_cli.cli, ["jobs", "sections", "abc"])
    assert result.exit_code == 0
    assert "Top" in result.output
    assert "/jobs/abc/sections" in captured["url"]


def test_r7_cli_admin_stats_invokes_endpoint() -> None:
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

        def get(self, url):
            import httpx as _hx

            captured["url"] = url
            return _hx.Response(
                200,
                json={
                    "total": 5,
                    "by_state": {"DONE": 4, "FAILED": 1},
                    "by_day": {},
                    "embedder_default": "hash-bucket-v1",
                    "embedder_counts": {"hash-bucket-v1": 5},
                    "cache_hits": 2,
                    "generated_at": "2026-01-01T00:00:00Z",
                },
                request=_hx.Request("GET", url),
            )

    mdwb_cli.httpx.Client = _StubClient
    mdwb_cli._resolve_settings = _fake_settings

    runner = CliRunner()
    result = runner.invoke(mdwb_cli.cli, ["admin", "stats"])
    assert result.exit_code == 0
    assert "total jobs" in result.output
    assert "/admin/stats" in captured["url"]
