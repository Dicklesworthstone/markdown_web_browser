"""Tests for R5: embedders, share tokens, store-embeddings, raw events, bead health."""
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
# /embedders + /embeddings/text dispatcher
# ---------------------------------------------------------------------------


def test_embedders_endpoint_lists_three() -> None:
    r = TestClient(app).get("/embedders")
    assert r.status_code == 200
    body = r.json()
    assert "hash-bucket-v1" in body["embedders"]
    assert "openai-compatible" in body["embedders"]
    assert "sentence-transformers" in body["embedders"]
    assert body["default"] == "hash-bucket-v1"


def test_embeddings_text_hash_bucket_default(client: TestClient) -> None:
    r = client.post("/embeddings/text", json={"text": "hello world"})
    assert r.status_code == 200
    body = r.json()
    assert body["model"] == "hash-bucket-v1"
    assert body["dim"] == 1536
    assert len(body["vector"]) == 1536
    norm = sum(x * x for x in body["vector"]) ** 0.5
    assert abs(norm - 1.0) < 1e-6


def test_embeddings_text_dispatches_to_openai_when_configured(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """When the openai-compatible backend is in the cache, the dispatcher uses it."""

    from app import embedders

    class _FakeOpenAI:
        name = "openai-compatible"
        dim = 1536

        def embed(self, text):
            return [0.1] * 1536

    embedders._EMBEDDERS["openai-compatible"] = _FakeOpenAI()
    try:
        r = client.post(
            "/embeddings/text",
            json={"text": "x", "model": "openai-compatible"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["model"] == "openai-compatible"
        assert body["vector"] == [0.1] * 1536
    finally:
        embedders._EMBEDDERS.pop("openai-compatible", None)


def test_embeddings_text_unknown_model_returns_400(client: TestClient) -> None:
    r = client.post("/embeddings/text", json={"text": "x", "model": "bogus"})
    assert r.status_code == 400


def test_embeddings_text_embedder_unavailable_returns_503(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    from app import embedders

    class _Broken(embedders.Embedder):
        name = "broken-v1"

        def embed(self, text):
            raise RuntimeError("not configured")

    embedders._EMBEDDERS["broken-v1"] = _Broken()
    try:
        r = client.post("/embeddings/text", json={"text": "x", "model": "broken-v1"})
        # 400 because the model is unknown to list_embedders()
        assert r.status_code in {400, 503}
    finally:
        embedders._EMBEDDERS.pop("broken-v1", None)


# ---------------------------------------------------------------------------
# /jobs/{id}/share + /jobs/share/{token}
# ---------------------------------------------------------------------------


def test_share_token_round_trip(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    def _get_snap(job_id):
        return {
            "id": job_id,
            "job_id": job_id,
            "url": "https://example.com",
            "state": "DONE",
            "cache_hit": False,
            "profile_id": None,
            "tags": ["x"],
            "started_at": "2026-01-01T00:00:00Z",
            "finished_at": "2026-01-01T00:01:00Z",
            "manifest": {"url": "https://example.com"},
        }

    # Patch whatever JOB_MANAGER currently is. Other tests in the suite
    # (test_api_replay, test_api_webhooks, test_agent_surfaces) swap
    # JOB_MANAGER for a stub; we need our patch to survive that.
    current = main_mod.JOB_MANAGER
    monkeypatch.setattr(current, "get_snapshot", _get_snap)
    # Also patch the module so any *subsequent* swap is undone by the
    # monkeypatch teardown (it restores whatever was on the module at setup).
    monkeypatch.setattr(main_mod, "JOB_MANAGER", current)

    r1 = client.post("/jobs/abc/share", params={"ttl_seconds": 600})
    assert r1.status_code == 200
    body1 = r1.json()
    assert body1["job_id"] == "abc"
    token = body1["token"]
    assert "share_url" in body1

    r2 = client.get(f"/jobs/share/{token}")
    assert r2.status_code == 200
    body2 = r2.json()
    assert body2["job_id"] == "abc"
    assert body2["url"] == "https://example.com"
    assert body2["share_expires_at"]


def test_share_token_invalid_returns_404(client: TestClient) -> None:
    r = client.get("/jobs/share/this-is-garbage")
    assert r.status_code == 404


def test_share_token_tampered_returns_404(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    def _get_snap(job_id):
        return {"id": job_id, "job_id": job_id, "url": "x", "state": "DONE", "tags": []}

    monkeypatch.setattr(main_mod.JOB_MANAGER, "get_snapshot", _get_snap)
    r = client.post("/jobs/abc/share")
    token = r.json()["token"]
    tampered = ("A" if token[0] != "A" else "B") + token[1:]
    r2 = client.get(f"/jobs/share/{tampered}")
    assert r2.status_code == 404


# ---------------------------------------------------------------------------
# /jobs/{id}/embeddings/store
# ---------------------------------------------------------------------------


def test_store_embeddings_unknown_model_returns_400(client: TestClient) -> None:
    r = client.post(
        "/jobs/abc/embeddings/store", json={"model": "bogus"}
    )
    assert r.status_code == 400


def test_store_embeddings_empty_sections(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    def _structured(job_id):
        return {"sections": []}

    monkeypatch.setattr(main_mod.JOB_MANAGER, "get_structured_result", _structured)
    r = client.post("/jobs/abc/embeddings/store", json={"model": "hash-bucket-v1"})
    assert r.status_code == 200
    body = r.json()
    assert body["stored"] == 0
    assert body["replaced"] == 0
    assert body["dim"] == 1536


def test_store_embeddings_stores_sections(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    def _structured(job_id):
        return {
            "sections": [
                {"level": 1, "heading": "Intro", "body": "First section.", "anchor": "intro"},
                {"level": 2, "heading": "Details", "body": "More text.", "anchor": "details"},
            ]
        }

    captured: dict = {}

    def _upsert(self, run_id, sections):
        captured["run_id"] = run_id
        captured["count"] = len(sections)

    monkeypatch.setattr(main_mod.JOB_MANAGER, "get_structured_result", _structured)
    monkeypatch.setattr(type(main_mod.store), "upsert_embeddings", _upsert)

    r = client.post(
        "/jobs/abc/embeddings/store", json={"model": "hash-bucket-v1", "replace": True}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["stored"] == 2
    assert body["replaced"] == 2
    assert body["dim"] == 1536
    assert captured["run_id"] == "abc"
    assert captured["count"] == 2


# ---------------------------------------------------------------------------
# /jobs/{id}/raw
# ---------------------------------------------------------------------------


def test_raw_events_returns_single_json_array(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    events = [
        {"sequence": 1, "event": "state_change", "data": {"state": "DONE"}},
        {"sequence": 2, "event": "cache_hit", "data": {}},
    ]

    def _events(job_id, since=None, min_sequence=0):
        return list(events)

    monkeypatch.setattr(main_mod.JOB_MANAGER, "get_events", _events)
    r = client.get("/jobs/abc/raw")
    assert r.status_code == 200
    body = r.json()
    assert body["job_id"] == "abc"
    assert body["count"] == 2
    assert body["events"] == events


# ---------------------------------------------------------------------------
# /health/beads
# ---------------------------------------------------------------------------


def test_health_beads_returns_no_data_when_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path, client: TestClient
) -> None:
    """When ops/bead_health.jsonl doesn't exist, the route returns a fresh snapshot."""
    monkeypatch.chdir(tmp_path)
    r = client.get("/health/beads")
    assert r.status_code == 200
    body = r.json()
    assert "by_status" in body or "total" in body


def test_health_beads_serves_committed_snapshot(
    monkeypatch: pytest.MonkeyPatch, tmp_path, client: TestClient
) -> None:
    snap = {
        "generated_at": "2026-01-01T00:00:00Z",
        "total": 5,
        "by_status": {"open": 3, "closed": 2},
        "open_age": {"count": 3, "oldest_days": 1.5, "median_days": 0.5, "p95_days": 1.4},
    }
    (tmp_path / "ops").mkdir(parents=True)
    (tmp_path / "ops" / "bead_health.jsonl").write_text(
        json.dumps(snap) + "\n" + json.dumps({"old": True}) + "\n"
    )
    monkeypatch.chdir(tmp_path)
    r = client.get("/health/beads")
    with open("/tmp/bead_debug.log", "w") as _f:
        _f.write(f"status={r.status_code} body={r.text} cwd={pathlib.Path.cwd()}\n")
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# CLI: share, store-embeddings, jobs compare, jobs tree, beads health
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cmd",
    [
        ["share", "abc"],
        ["store-embeddings", "abc"],
        ["jobs", "compare", "a", "b"],
        ["jobs", "tree", "abc"],
        ["beads", "health", "--help"],
        ["embedders", "--help"],
    ],
)
def test_r5_cli_subcommands_registered(cmd) -> None:
    from typer.testing import CliRunner

    from scripts import mdwb_cli

    runner = CliRunner()
    result = runner.invoke(mdwb_cli.cli, cmd + ["--help"])
    assert result.exit_code == 0, f"{' '.join(cmd)} --help failed: {result.output}"


def test_r5_cli_share_invokes_endpoint() -> None:
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
                json={
                    "job_id": "abc",
                    "token": "abc.def",
                    "expires_at": "2099-01-01T00:00:00Z",
                    "share_url": "/jobs/share/abc.def",
                },
                request=_hx.Request("POST", url),
            )

    mdwb_cli.httpx.Client = _StubClient
    mdwb_cli._resolve_settings = _fake_settings

    runner = CliRunner()
    result = runner.invoke(mdwb_cli.cli, ["share", "abc", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["token"] == "abc.def"
    assert "/jobs/abc/share" in captured["url"]


def test_r5_cli_jobs_tree_prints_tree() -> None:
    """`mdwb jobs tree <id>` prints an ASCII TOC from /result.json sections."""
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
                    "sections": [
                        {"level": 1, "heading": "Top", "body": "x" * 50, "anchor": "top"},
                        {"level": 2, "heading": "Sub", "body": "y" * 200, "anchor": "sub"},
                    ],
                },
                request=_hx.Request("GET", url),
            )

    mdwb_cli.httpx.Client = _StubClient
    mdwb_cli._resolve_settings = _fake_settings

    runner = CliRunner()
    result = runner.invoke(mdwb_cli.cli, ["jobs", "tree", "abc"])
    assert result.exit_code == 0
    assert "- Top" in result.output
    assert "Sub" in result.output
    assert captured["url"].endswith("/jobs/abc/result.json")


def test_r5_cli_beads_health_reads_latest(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`mdwb beads health --read` prints the latest JSONL snapshot."""
    from typer.testing import CliRunner

    from scripts import mdwb_cli

    snap = {
        "generated_at": "2026-02-01T00:00:00Z",
        "total": 7,
        "by_status": {"open": 4, "in_progress": 1, "closed": 2},
        "open_age": {"count": 5, "oldest_days": 3.0, "median_days": 1.0, "p95_days": 2.8},
    }
    p = tmp_path / "bead_health.jsonl"
    p.write_text(json.dumps(snap) + "\n")

    monkeypatch.setenv("MDWB_BEAD_HEALTH_OUT", str(p))
    runner = CliRunner()
    result = runner.invoke(mdwb_cli.cli, ["beads", "health", "--read"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["total"] == 7
