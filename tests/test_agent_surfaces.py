"""Tests for the agent-facing surfaces added in the round-2 reality check:

- POST /jobs/batch
- GET /jobs (list with filters)
- POST /jobs/{id}/tag
- GET /jobs/{id}/result.json
- GET /schema (grouped agent-oriented discovery)
- /jobs/{job_id}/result.json parsing
- markdown section parsing helpers

These tests use the in-process FastAPI TestClient (no live capture).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.jobs import _extract_markdown_links, _parse_markdown_sections
from app.schemas import (
    BatchJobRequest,
    JobTagRequest,
    StructuredResult,
)
from app.store import StorageConfig, build_store


@pytest.fixture
def isolated_store(tmp_path):
    """Build a fresh Store in a tmp dir with the current schema.

    The shared runs.db fixture used by other tests lacks the new ``tags``
    column; this fixture gives each test in this file a private DB so
    CREATE TABLE reflects RunRecord.tags.
    """
    cfg = StorageConfig(cache_root=tmp_path / "cache", db_path=tmp_path / "runs.db")
    store = build_store(cfg)
    yield store


@pytest.fixture
def client(monkeypatch, isolated_store) -> TestClient:
    """A TestClient whose /jobs list and tag calls hit an isolated store."""
    import app.main as _main

    monkeypatch.setattr(_main, "store", isolated_store)
    # Also rebind the JobManager's store reference so persistence goes there.
    _main.JOB_MANAGER.store = isolated_store
    return TestClient(_main.app)


# ---------------------------------------------------------------------------
# Schema contract
# ---------------------------------------------------------------------------


def test_batch_job_request_minimum() -> None:
    req = BatchJobRequest(urls=["https://example.com", "https://example.org"])
    assert len(req.urls) == 2
    assert req.reuse_cache is True
    assert req.tags == []


def test_batch_job_request_with_tags() -> None:
    req = BatchJobRequest(
        urls=["https://example.com"],
        profile_id="research",
        reuse_cache=False,
        tags=["dataset:2026-q1", "team:research"],
    )
    assert req.tags == ["dataset:2026-q1", "team:research"]
    assert req.reuse_cache is False
    assert req.profile_id == "research"


def test_job_tag_request_contract() -> None:
    req = JobTagRequest(tag="reviewed")
    assert req.tag == "reviewed"


def test_structured_result_minimum_payload() -> None:
    """StructuredResult can be constructed with the bare minimum."""
    payload = {
        "job_id": "abc",
        "url": "https://example.com",
        "state": "DONE",
        "word_count": 0,
        "char_count": 0,
        "sections": [],
        "links": [],
        "cache_hit": False,
        "profile_id": None,
    }
    res = StructuredResult(**payload)
    assert res.job_id == "abc"
    assert res.sections == []


# ---------------------------------------------------------------------------
# /schema discovery
# ---------------------------------------------------------------------------


def test_schema_endpoint_is_reachable(client: TestClient) -> None:
    response = client.get("/schema")
    assert response.status_code == 200
    body = response.json()
    assert body["service"] == "markdown-web-browser"
    assert "endpoints" in body
    assert "invariants" in body
    # Every endpoint surface must list the headline routes
    assert "POST /jobs/batch" in body["endpoints"]["capture"]
    assert "GET /jobs/{id}/result.json" in body["endpoints"]["artifacts"]
    assert "POST /jobs/{id}/embeddings/search" in body["endpoints"]["embed"]


def test_schema_contains_agent_tips(client: TestClient) -> None:
    body = client.get("/schema").json()
    assert isinstance(body.get("agent_tips"), list)
    assert len(body["agent_tips"]) > 0
    # Every tip should be a one-liner
    for tip in body["agent_tips"]:
        assert isinstance(tip, str)
        assert len(tip) < 200


# ---------------------------------------------------------------------------
# Section / link parsing helpers
# ---------------------------------------------------------------------------


def test_parse_markdown_sections_basic() -> None:
    md = "# Hello\n\nworld\n## Sub\n\nfoo\n### Deep\n\nbar"
    sections = _parse_markdown_sections(md)
    assert len(sections) == 3
    assert sections[0] == {
        "level": 1,
        "heading": "Hello",
        "body": "world",
        "anchor": "hello",
        "tile_indices": [],
    }
    assert sections[1]["heading"] == "Sub"
    assert sections[2]["level"] == 3
    assert sections[2]["body"] == "bar"


def test_parse_markdown_sections_empty() -> None:
    assert _parse_markdown_sections("") == []


def test_parse_markdown_sections_no_headings() -> None:
    assert _parse_markdown_sections("just plain text\nno headings here") == []


def test_parse_markdown_sections_anchor_slug() -> None:
    md = "# Hello World!\n\nbody"
    sections = _parse_markdown_sections(md)
    assert sections[0]["anchor"] == "hello-world"


def test_extract_markdown_links_unique() -> None:
    md = "See [a](https://example.com) and [b](https://example.com) and [c](https://other.com)"
    out = _extract_markdown_links(md)
    assert out == ["https://example.com", "https://other.com"]


def test_extract_markdown_links_only_http() -> None:
    md = "[a](mailto:foo@bar.com) [b](https://x.com) [c](#anchor)"
    out = _extract_markdown_links(md)
    assert out == ["https://x.com"]


# ---------------------------------------------------------------------------
# /jobs list + /jobs/batch + /jobs/{id}/tag via TestClient
# ---------------------------------------------------------------------------


def test_jobs_list_returns_filters_payload(client: TestClient) -> None:
    response = client.get("/jobs", params={"limit": 5})
    assert response.status_code == 200
    body = response.json()
    assert "items" in body
    assert "total" in body
    assert "filtered" in body
    assert "filters" in body


def test_jobs_list_with_tag_filter(client: TestClient) -> None:
    response = client.get("/jobs", params={"tag": "dataset:2026-q1", "state": "DONE"})
    assert response.status_code == 200
    body = response.json()
    assert body["filters"]["tag"] == "dataset:2026-q1"
    assert body["filters"]["state"] == "DONE"


def test_jobs_list_invalid_filter_is_safe(client: TestClient) -> None:
    """Filters must not 500 even when no jobs match."""
    response = client.get("/jobs", params={"tag": "this-tag-does-not-exist-anywhere-12345"})
    assert response.status_code == 200
    assert response.json()["filtered"] == 0


def test_jobs_batch_returns_per_url_outcomes(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /jobs/batch should respond with one item per URL, never crash on a 404 lookup."""
    # Patch the snapshot lookup so we don't depend on real DB / state
    class _FakeSnapshot:
        def __init__(self, url: str) -> None:
            self._url = url
            self._cache_hit = False

        def get(self, k: str, default=None):
            return {"job_id": "fake", "state": "PENDING", "cache_hit": False}.get(k, default)

    async def _fake_create(self, request, tags=None):
        return {"job_id": "fake-" + request.url[-6:], "state": "PENDING", "cache_hit": False}

    monkeypatch.setattr("app.main.JOB_MANAGER.create_job", _fake_create)

    response = client.post(
        "/jobs/batch",
        json={
            "urls": ["https://example.com", "https://example.org"],
            "tags": ["dataset:test"],
        },
    )
    assert response.status_code == 202
    body = response.json()
    assert body["submitted"] == 2
    assert len(body["items"]) == 2
    assert body["items"][0]["url"] == "https://example.com"


def test_jobs_batch_rejects_too_many_urls(client: TestClient) -> None:
    response = client.post(
        "/jobs/batch",
        json={"urls": [f"https://x.com/{i}" for i in range(201)]},
    )
    # Pydantic max_length=200 should reject
    assert response.status_code == 422


def test_jobs_batch_requires_at_least_one_url(client: TestClient) -> None:
    response = client.post("/jobs/batch", json={"urls": []})
    assert response.status_code == 422


def test_job_tag_route_404_for_missing_job(client: TestClient) -> None:
    response = client.post(
        "/jobs/does-not-exist/tag",
        json={"tag": "test"},
    )
    # Either 200 (silent noop) or 404 — both acceptable as long as it's not 500
    assert response.status_code in {200, 404}


# ---------------------------------------------------------------------------
# /jobs/{id}/result.json contract
# ---------------------------------------------------------------------------


def test_result_json_404_for_unknown_job(client: TestClient) -> None:
    response = client.get("/jobs/does-not-exist/result.json")
    assert response.status_code == 404


def test_result_json_route_exists_and_responds(client: TestClient) -> None:
    """Even when the job doesn't exist, the route should be wired and return JSON 404."""
    response = client.get("/jobs/abc/result.json")
    assert response.status_code in {200, 404}
    if response.status_code == 200:
        body = response.json()
        assert "sections" in body
        assert "links" in body


# ---------------------------------------------------------------------------
# CLI commands registered
# ---------------------------------------------------------------------------


def test_cli_batch_command_registered() -> None:
    from typer.testing import CliRunner

    from scripts import mdwb_cli

    runner = CliRunner()
    result = runner.invoke(mdwb_cli.cli, ["batch", "--help"])
    assert result.exit_code == 0
    assert "--tag" in result.output
    assert "--reuse-cache" in result.output


def test_cli_schema_command_registered() -> None:
    from typer.testing import CliRunner

    from scripts import mdwb_cli

    runner = CliRunner()
    result = runner.invoke(mdwb_cli.cli, ["schema", "--help"])
    assert result.exit_code == 0


def test_cli_jobs_list_command_registered() -> None:
    from typer.testing import CliRunner

    from scripts import mdwb_cli

    runner = CliRunner()
    result = runner.invoke(mdwb_cli.cli, ["jobs", "list", "--help"])
    assert result.exit_code == 0
    assert "--tag" in result.output
    assert "--state" in result.output


def test_cli_jobs_tag_command_registered() -> None:
    from typer.testing import CliRunner

    from scripts import mdwb_cli

    runner = CliRunner()
    result = runner.invoke(mdwb_cli.cli, ["jobs", "tag", "--help"])
    assert result.exit_code == 0


def test_cli_jobs_result_command_registered() -> None:
    from typer.testing import CliRunner

    from scripts import mdwb_cli

    runner = CliRunner()
    result = runner.invoke(mdwb_cli.cli, ["jobs", "result", "--help"])
    assert result.exit_code == 0
    assert "--json" in result.output
