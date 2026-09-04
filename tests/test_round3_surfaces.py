"""Tests for round-3 agent surfaces: rerun, diff, search, embed, slo.json, schema.json."""

from __future__ import annotations

import pathlib

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


# ---------------------------------------------------------------------------
# Schema discovery (round 3 additions)
# ---------------------------------------------------------------------------


def test_schema_json_returns_same_as_schema(client: TestClient) -> None:
    """GET /schema and GET /schema.json must return the same payload."""
    a = client.get("/schema").json()
    b = client.get("/schema.json").json()
    assert a == b


def test_metrics_slo_json_returns_same_as_slo(client: TestClient) -> None:
    """GET /metrics/slo and GET /metrics/slo.json must return the same payload.

    We exclude ``generated_at`` from the comparison because the timestamp is
    generated per request (microseconds apart).
    """
    a = client.get("/metrics/slo").json()
    b = client.get("/metrics/slo.json").json()
    a.pop("generated_at", None)
    b.pop("generated_at", None)
    assert a == b


# ---------------------------------------------------------------------------
# /embeddings/text (deterministic, model-free)
# ---------------------------------------------------------------------------


def test_embeddings_text_returns_1536_dim_unit_vector(client: TestClient) -> None:
    response = client.post("/embeddings/text", json={"text": "hello world"})
    assert response.status_code == 200
    body = response.json()
    assert body["model"] == "hash-bucket-v1"
    assert body["dim"] == 1536
    assert len(body["vector"]) == 1536
    assert body["text_chars"] == 11
    norm = sum(x * x for x in body["vector"]) ** 0.5
    assert abs(norm - 1.0) < 1e-6


def test_embeddings_text_is_deterministic(client: TestClient) -> None:
    """Same text twice -> same vector."""
    a = client.post("/embeddings/text", json={"text": "the quick brown fox"}).json()
    b = client.post("/embeddings/text", json={"text": "the quick brown fox"}).json()
    assert a["vector"] == b["vector"]


def test_embeddings_text_differs_for_different_text(client: TestClient) -> None:
    a = client.post("/embeddings/text", json={"text": "alpha"}).json()
    b = client.post("/embeddings/text", json={"text": "beta"}).json()
    assert a["vector"] != b["vector"]


def test_embeddings_text_rejects_unknown_model(client: TestClient) -> None:
    response = client.post(
        "/embeddings/text", json={"text": "hi", "model": "openai-ada-002"}
    )
    assert response.status_code == 400


def test_embeddings_text_rejects_empty(client: TestClient) -> None:
    response = client.post("/embeddings/text", json={"text": ""})
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# /jobs/{id}/rerun
# ---------------------------------------------------------------------------


def test_rerun_404_for_missing_job(client: TestClient) -> None:
    response = client.post(
        "/jobs/does-not-exist/rerun", params={"reuse_cache": "false"}
    )
    assert response.status_code == 404


def test_rerun_uses_existing_job_config(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rerun should submit a new job with the original URL + profile + tags."""

    async def _fake_create(self_or_request, request=None, tags=None):
        if request is None:
            # Called as bound method: (self, request, tags)
            request = self_or_request
        return {
            "job_id": "new-id-xyz",
            "state": "PENDING",
            "cache_hit": False,
            "url": request.url,
        }

    monkeypatch.setattr("app.main.JOB_MANAGER.create_job", _fake_create)
    monkeypatch.setattr(
        "app.main.JOB_MANAGER.get_snapshot",
        lambda _id: {
            "job_id": _id,
            "url": "https://example.com",
            "profile_id": "research",
            "tags": ["dataset:test"],
        },
    )

    response = client.post("/jobs/abc/rerun")
    assert response.status_code == 202
    body = response.json()
    assert body["original_job_id"] == "abc"
    assert body["new_job_id"] == "new-id-xyz"
    assert body["url"] == "https://example.com"


# ---------------------------------------------------------------------------
# /jobs/{id}/diff
# ---------------------------------------------------------------------------


def test_diff_404_for_missing_job_a(client: TestClient) -> None:
    response = client.post(
        "/jobs/does-not-exist/diff", json={"other_job_id": "x"}
    )
    assert response.status_code == 404


def test_diff_404_for_missing_job_b(client: TestClient) -> None:
    response = client.post(
        "/jobs/abc/diff", json={"other_job_id": "does-not-exist"}
    )
    assert response.status_code == 404


def test_diff_works_against_two_snapshots(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshots = {
        "a1": {
            "job_id": "a1",
            "url": "https://example.com",
            "state": "DONE",
        },
        "b1": {
            "job_id": "b1",
            "url": "https://example.com",
            "state": "DONE",
        },
    }
    monkeypatch.setattr(
        "app.main.JOB_MANAGER.get_snapshot", lambda jid: snapshots.get(jid)
    )

    md_a = "# Intro\n\nOld body\n## Pricing\n\n$5"
    md_b = "# Intro\n\nNew body\n## Pricing\n\n$5\n## New section\n\nAdded"
    read_mds = {"a1": md_a, "b1": md_b}

    import app.main

    def _fake_read_md(self, jid):
        return read_mds.get(jid, "")

    def _fake_read_links(self, jid):
        return {"anchors": []}

    monkeypatch.setattr(type(app.main.store), "read_markdown", _fake_read_md)
    monkeypatch.setattr(type(app.main.store), "read_links", _fake_read_links)

    response = client.post(
        "/jobs/a1/diff",
        json={"other_job_id": "b1", "include_links": False, "max_chars_per_section": 100},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["a_job_id"] == "a1"
    assert body["b_job_id"] == "b1"
    states = {s["anchor"]: s["state"] for s in body["sections"]}
    assert states["intro"] == "changed"
    assert states["pricing"] == "unchanged"
    assert states["new-section"] == "added"


# ---------------------------------------------------------------------------
# /jobs/search
# ---------------------------------------------------------------------------


def test_search_returns_empty_for_whitespace_query(client: TestClient) -> None:
    response = client.post("/jobs/search", json={"query": "   "})
    assert response.status_code == 200
    body = response.json()
    assert body["matches"] == []


def test_search_quoted_phrase_match(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = [
        {"id": "job1", "url": "https://x.com", "state": "DONE", "tags": []},
    ]
    monkeypatch.setattr(
        "app.main.JOB_MANAGER.list_jobs", lambda **_kwargs: (rows, 1)
    )
    import app.main
    monkeypatch.setattr(
        type(app.main.store), "read_markdown", lambda self, jid: "Hello world hello again"
    )

    response = client.post("/jobs/search", json={"query": '"hello world"'})
    assert response.status_code == 200
    body = response.json()
    assert body["returned"] == 1
    assert body["matches"][0]["job_id"] == "job1"


def test_search_filters_by_state_and_tag(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict = {}

    def _fake_list(**kwargs):
        captured.update(kwargs)
        return [], 0

    monkeypatch.setattr("app.main.JOB_MANAGER.list_jobs", _fake_list)
    response = client.post(
        "/jobs/search", json={"query": "alpha", "state": "DONE", "tag": "x"}
    )
    assert response.status_code == 200
    assert captured.get("state") == "DONE"
    assert captured.get("tag") == "x"


def test_search_skips_runs_with_unreadable_markdown(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "app.main.JOB_MANAGER.list_jobs",
        lambda **_kw: ([{"id": "broken", "url": "", "state": "DONE", "tags": []}], 1),
    )

    import app.main

    def _raise(self, jid):
        raise RuntimeError("disk gone")

    monkeypatch.setattr(type(app.main.store), "read_markdown", _raise)
    response = client.post("/jobs/search", json={"query": "anything"})
    assert response.status_code == 200
    body = response.json()
    assert body["matches"] == []
    assert body["total_scanned"] == 1


# ---------------------------------------------------------------------------
# /jobs/{id}/slo
# ---------------------------------------------------------------------------


def test_per_job_slo_404_for_missing_job(client: TestClient) -> None:
    response = client.get("/jobs/does-not-exist/slo")
    assert response.status_code == 404


def test_per_job_slo_with_known_timings(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.main
    monkeypatch.setattr(
        type(app.main.store),
        "read_manifest",
        lambda self, jid: {
            "timings": {
                "capture_ms": 1500,
                "ocr_ms": 4000,
                "stitch_ms": 200,
                "total_ms": 5700,
            }
        },
    )
    response = client.get("/jobs/abc/slo")
    assert response.status_code == 200
    body = response.json()
    assert body["p50_total_ms"] == 5700
    assert body["count"] == 1
    assert body["status"] in {"within_budget", "breach", "unknown"}


# ---------------------------------------------------------------------------
# CLI subcommands (round 3)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cmd",
    ["rerun", "diff", "search", "embed"],
)
def test_round3_cli_subcommand_registered(cmd: str) -> None:
    from typer.testing import CliRunner

    from scripts import mdwb_cli

    runner = CliRunner()
    result = runner.invoke(mdwb_cli.cli, [cmd, "--help"])
    assert result.exit_code == 0, f"{cmd} --help failed: {result.output}"


def test_round3_cli_embed_passes_text_through() -> None:
    from typer.testing import CliRunner

    from scripts import mdwb_cli

    captured: dict = {}

    def _fake_settings(base=None):
        return type(
            "S",
            (),
            {
                "base_url": "http://x",
                "api_key": None,
                "warning_log_path": pathlib.Path("."),
            },
        )()

    class _StubClient:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

        def post(self, url, json=None):
            captured["url"] = url
            captured["json"] = json
            # Build a real Response with a Request so raise_for_status works.
            request = httpx.Request("POST", url, json=json)
            return httpx.Response(
                200,
                json={
                    "model": "hash-bucket-v1",
                    "dim": 1536,
                    "vector": [0.0] * 1536,
                    "text_chars": len(json.get("text", "")),
                },
                request=request,
            )

    mdwb_cli.httpx.Client = _StubClient
    mdwb_cli._resolve_settings = _fake_settings

    runner = CliRunner()
    result = runner.invoke(mdwb_cli.cli, ["embed", "hello agent", "--json"])
    assert result.exit_code == 0
    assert captured["json"]["text"] == "hello agent"
    assert "/embeddings/text" in captured["url"]
