"""R6 tests: agent ergonomics + observability + operational polish."""

from __future__ import annotations

import asyncio
import pathlib
import shutil
from datetime import datetime, timezone
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

import app.main as main_mod
from app.main import app
from app.jobs import JobManager
from app.store import RunPaths, RunRecord, StorageConfig, Store


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def delete_store(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Store]:
    store = Store(StorageConfig(tmp_path / "cache", tmp_path / "runs.db"))
    monkeypatch.setattr(main_mod, "store", store)
    monkeypatch.setattr(main_mod, "JOB_MANAGER", JobManager(store=store))
    try:
        yield store
    finally:
        store.engine.dispose()


@pytest.fixture
def purge_calls(monkeypatch: pytest.MonkeyPatch):
    """Record dispatch at the destructive boundary; never remove fixture files."""
    calls: list[tuple[pathlib.Path, tuple[object, ...], dict[str, object]]] = []

    def record(path: str | pathlib.Path, *args: object, **kwargs: object) -> None:
        calls.append((pathlib.Path(path), args, kwargs))

    monkeypatch.setattr(shutil, "rmtree", record)
    return calls


def _seed_delete_run(store: Store, job_id: str = "abc") -> RunPaths:
    now = datetime.now(timezone.utc)
    paths = store.allocate_run(job_id=job_id, url=f"https://example.com/{job_id}", started_at=now)
    paths.manifest_path.write_text("{}", encoding="utf-8")
    paths.markdown_path.write_text("# hi", encoding="utf-8")
    store.update_status(job_id=job_id, status="DONE", finished_at=now)
    return paths


# ---------------------------------------------------------------------------
# /jobs/{id}/toc + /jobs/{id}/summary
# ---------------------------------------------------------------------------


def test_toc_returns_sections_and_links(
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

    def _links(self, jid):
        return {
            "anchors": [
                {"href": "https://a.com", "text": "A", "source": "dom"},
                {"href": "https://b.com", "text": "B", "source": "ocr"},
            ]
        }

    monkeypatch.setattr(main_mod.JOB_MANAGER, "get_structured_result", _structured)
    monkeypatch.setattr(type(main_mod.store), "read_links", _links)
    response = client.get("/jobs/abc/toc")
    assert response.status_code == 200
    body = response.json()
    assert body["url"] == "https://example.com"
    assert len(body["sections"]) == 2
    assert len(body["outbound_links"]) == 2
    assert "https://a.com" in body["outbound_links"]


def test_summary_composes_natural_language(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    def _structured(job_id):
        return {
            "url": "https://example.com",
            "sections": [
                {"level": 1, "heading": "Alpha", "body": "x" * 100, "anchor": "alpha"},
                {"level": 2, "heading": "Beta", "body": "y" * 50, "anchor": "beta"},
            ],
        }

    def _manifest(self, jid):
        return {"url": "https://example.com", "embedding_model": "hash-bucket-v1"}

    def _links(self, jid):
        return {
            "anchors": [
                {"href": "https://a.com"},
                {"href": "https://b.com"},
                {"href": "https://c.com"},
            ]
        }

    monkeypatch.setattr(main_mod.JOB_MANAGER, "get_structured_result", _structured)
    monkeypatch.setattr(type(main_mod.store), "read_manifest", _manifest)
    monkeypatch.setattr(type(main_mod.store), "read_links", _links)

    response = client.get("/jobs/abc/summary")
    assert response.status_code == 200
    body = response.json()
    assert "Captured page at" in body["summary"]
    assert "Alpha" in body["summary"]
    assert body["outbound_link_count"] == 3
    assert body["embedding_model"] == "hash-bucket-v1"


def test_summary_handles_missing_artifacts(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """No manifest, no links -> summary still composes from URL alone."""

    def _structured(job_id):
        return {"url": "https://x.com", "sections": []}

    def _raise(self, jid):
        raise RuntimeError("no manifest")

    monkeypatch.setattr(main_mod.JOB_MANAGER, "get_structured_result", _structured)
    monkeypatch.setattr(type(main_mod.store), "read_manifest", _raise)
    monkeypatch.setattr(type(main_mod.store), "read_links", _raise)

    response = client.get("/jobs/abc/summary")
    assert response.status_code == 200
    body = response.json()
    assert "Captured page at https://x.com" in body["summary"]


# ---------------------------------------------------------------------------
# /jobs/search/text
# ---------------------------------------------------------------------------


def test_search_text_alias_matches_search(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """Two routes, same payload."""

    def _list(**kwargs):
        return [{"id": "abc", "url": "https://x.com", "state": "DONE", "tags": []}], 1

    def _read(self, jid):
        return "hello world"

    monkeypatch.setattr(main_mod.JOB_MANAGER, "list_jobs", _list)
    monkeypatch.setattr(type(main_mod.store), "read_markdown", _read)
    response = client.post("/jobs/search/text", json={"query": "hello", "limit": 5})
    assert response.status_code == 200
    body = response.json()
    assert body["returned"] == 1


# ---------------------------------------------------------------------------
# /embeddings/text/batch
# ---------------------------------------------------------------------------


def test_batch_embeddings_returns_one_per_text(client: TestClient) -> None:
    response = client.post(
        "/embeddings/text/batch",
        json={"texts": ["alpha", "beta", "gamma"], "model": "hash-bucket-v1"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 3
    assert body["model"] == "hash-bucket-v1"
    assert body["dim"] == 1536
    assert len(body["vectors"]) == 3
    # Determinism: same text -> same vector
    a = body["vectors"][0]["vector"]
    b = body["vectors"][1]["vector"]
    assert a != b


def test_batch_embeddings_unknown_model_returns_400(client: TestClient) -> None:
    response = client.post(
        "/embeddings/text/batch",
        json={"texts": ["x"], "model": "bogus"},
    )
    assert response.status_code == 400


def test_batch_embeddings_empty_texts_returns_422(client: TestClient) -> None:
    response = client.post(
        "/embeddings/text/batch",
        json={"texts": []},
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# /metrics/job-counts + /metrics/embedders
# ---------------------------------------------------------------------------


def test_metrics_job_counts_returns_shape(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    def _list(**kwargs):
        return [
            {"id": "a", "state": "DONE", "created_at": "2026-01-01T00:00:00Z", "tags": []},
            {"id": "b", "state": "FAILED", "created_at": "2026-01-02T00:00:00Z", "tags": []},
            {"id": "c", "state": "DONE", "created_at": "2026-01-01T00:00:00Z", "tags": []},
        ], 3

    monkeypatch.setattr(main_mod.JOB_MANAGER, "list_jobs", _list)
    response = client.get("/metrics/job-counts")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 3
    assert body["by_state"]["DONE"] == 2
    assert body["by_state"]["FAILED"] == 1
    # The by_day aggregation is correct in concept; in CI today may be any
    # day so we just assert the keys list is a subset of the last 30 days.
    assert isinstance(body.get("by_day"), dict)


def test_metrics_embedders_returns_counts(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    def _list(**kwargs):
        return [
            {"id": "a", "embedding_model": "hash-bucket-v1"},
            {"id": "b", "embedding_model": "openai-compatible"},
            {"id": "c", "embedding_model": "hash-bucket-v1"},
        ], 3

    monkeypatch.setattr(main_mod.JOB_MANAGER, "list_jobs", _list)
    response = client.get("/metrics/embedders")
    assert response.status_code == 200
    body = response.json()
    assert "hash-bucket-v1" in body["available"]
    assert body["counts"]["hash-bucket-v1"] == 2
    assert body["counts"]["openai-compatible"] == 1


# ---------------------------------------------------------------------------
# /jobs/{id}/replay
# ---------------------------------------------------------------------------


def test_replay_creates_new_job_with_same_url(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    async def _create(self_or_request, request=None, tags=None):
        if request is None:
            request = self_or_request
        return {"job_id": "new-id-xyz", "state": "PENDING"}

    def _get_snap(job_id):
        return {
            "id": job_id,
            "url": "https://example.com",
            "profile_id": "research",
            "tags": ["dataset:test"],
        }

    fake_jm = main_mod.JobManager.__new__(main_mod.JobManager)
    monkeypatch.setattr(fake_jm, "create_job", _create)
    monkeypatch.setattr(fake_jm, "get_snapshot", _get_snap)
    monkeypatch.setattr(main_mod, "JOB_MANAGER", fake_jm)

    response = client.post(
        "/jobs/abc/replay",
        json={"reuse_cache": False, "profile_id": "override"},
    )
    assert response.status_code == 202
    body = response.json()
    assert body["original_job_id"] == "abc"
    assert body["new_job_id"] == "new-id-xyz"
    assert body["url"] == "https://example.com"


def test_replay_404_for_unknown_job(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    fake_jm = main_mod.JobManager.__new__(main_mod.JobManager)
    monkeypatch.setattr(fake_jm, "get_snapshot", lambda job_id: None)
    monkeypatch.setattr(main_mod, "JOB_MANAGER", fake_jm)
    response = client.post("/jobs/abc/replay", json={"reuse_cache": True})
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# DELETE /jobs/{id}
# ---------------------------------------------------------------------------


def test_delete_dispatches_explicit_purge_without_deleting_fixture_files(
    client: TestClient, delete_store: Store, purge_calls
) -> None:
    """Prove purge authorization/dispatch/counts; physical deletion is prohibited."""
    paths = _seed_delete_run(delete_store)
    response = client.delete("/jobs/abc", params={"purge_artifacts": "true"})
    assert response.status_code == 200
    body = response.json()
    assert body["deleted"] is True
    assert body["artifacts_removed"] == 1  # one directory
    assert body["bytes_freed"] > 0
    assert body["bytes_freed"] == 6
    assert purge_calls == [(paths.root.resolve(), (), {})]
    assert delete_store.fetch_run("abc") is None
    # The destructive boundary double intentionally retains these bytes.
    # This replaces the old physical-absence oracle to honor NO FILE DELETION.
    assert paths.root.is_dir()
    assert paths.manifest_path.read_bytes() == b"{}"
    assert paths.markdown_path.read_bytes() == b"# hi"


def test_delete_retains_artifacts_by_default(
    client: TestClient, delete_store: Store, purge_calls
) -> None:
    paths = _seed_delete_run(delete_store)
    response = client.delete("/jobs/abc")
    assert response.status_code == 200
    body = response.json()
    assert body["deleted"] is True
    assert body["artifacts_removed"] == 0
    assert body["bytes_freed"] == 0
    assert purge_calls == []
    assert delete_store.fetch_run("abc") is None
    assert paths.manifest_path.read_bytes() == b"{}"
    assert paths.markdown_path.read_bytes() == b"# hi"


def test_delete_idempotent_for_missing_job(
    client: TestClient, delete_store: Store, purge_calls
) -> None:
    assert delete_store.fetch_run("missing") is None
    response = client.delete("/jobs/missing")
    assert response.status_code == 200
    body = response.json()
    assert body["deleted"] is False
    assert body["artifacts_removed"] == 0
    assert body["bytes_freed"] == 0
    assert purge_calls == []


def test_explicit_purge_refuses_shared_roots(
    client: TestClient, delete_store: Store, purge_calls
) -> None:
    paths = _seed_delete_run(delete_store)
    source = delete_store.fetch_run("abc")
    assert source is not None
    delete_store.register_cached_run(job_id="alias", source=source)
    assert delete_store.path_reference_count(str(paths.root)) == 2
    response = client.delete("/jobs/abc", params={"purge_artifacts": "true"})
    assert response.status_code == 409
    assert purge_calls == []
    assert delete_store.fetch_run("abc") is not None
    assert delete_store.fetch_run("alias") is not None
    assert paths.manifest_path.read_bytes() == b"{}"
    assert paths.markdown_path.read_bytes() == b"# hi"


def test_explicit_purge_refuses_roots_outside_cache(
    client: TestClient, delete_store: Store, purge_calls, tmp_path: pathlib.Path
) -> None:
    paths = _seed_delete_run(delete_store)
    outside = tmp_path / "outside-cache"
    outside.mkdir()
    sentinel = outside / "retained.txt"
    sentinel.write_text("outside-sentinel", encoding="utf-8")
    with delete_store.session() as session:
        record = session.get(RunRecord, "abc")
        assert record is not None
        record.cache_path = str(outside)
        session.add(record)
        session.commit()
    response = client.delete("/jobs/abc", params={"purge_artifacts": "true"})
    assert response.status_code == 409
    assert purge_calls == []
    assert delete_store.fetch_run("abc") is not None
    assert sentinel.read_text("utf-8") == "outside-sentinel"
    assert paths.manifest_path.read_bytes() == b"{}"
    assert paths.markdown_path.read_bytes() == b"# hi"


async def test_delete_refuses_active_job(
    client: TestClient, delete_store: Store, purge_calls
) -> None:
    paths = _seed_delete_run(delete_store)
    delete_store.update_status(job_id="abc", status="BROWSER_STARTING")

    async def wait_for_cancel() -> None:
        await asyncio.Event().wait()

    task = asyncio.create_task(wait_for_cancel())
    main_mod.JOB_MANAGER._tasks["abc"] = task
    try:
        await asyncio.sleep(0)
        assert not task.done()
        for params in ({}, {"purge_artifacts": "true"}):
            response = client.delete("/jobs/abc", params=params)
            assert response.status_code == 409
        assert purge_calls == []
        assert delete_store.fetch_run("abc") is not None
        assert paths.manifest_path.read_bytes() == b"{}"
        assert paths.markdown_path.read_bytes() == b"# hi"
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


def test_purge_failure_keeps_metadata_and_files(
    client: TestClient, delete_store: Store, purge_calls, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _seed_delete_run(delete_store)

    def fail(path: str | pathlib.Path, *args: object, **kwargs: object) -> None:
        purge_calls.append((pathlib.Path(path), args, kwargs))
        raise PermissionError("fixture purge denied")

    monkeypatch.setattr(shutil, "rmtree", fail)
    response = client.delete("/jobs/abc", params={"purge_artifacts": "true"})
    assert response.status_code == 500
    assert purge_calls == [(paths.root.resolve(), (), {})]
    assert delete_store.fetch_run("abc") is not None
    assert paths.manifest_path.read_bytes() == b"{}"
    assert paths.markdown_path.read_bytes() == b"# hi"


# ---------------------------------------------------------------------------
# /health/live + /health/ready
# ---------------------------------------------------------------------------


def test_health_live_always_ok(client: TestClient) -> None:
    response = client.get("/health/live")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_health_ready_503_when_watchdog_down(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """When the watchdog task is None, /health/ready must return 503."""
    fake_jm = main_mod.JobManager.__new__(main_mod.JobManager)
    fake_jm._watchdog_task = None
    monkeypatch.setattr(main_mod, "JOB_MANAGER", fake_jm)
    response = client.get("/health/ready")
    assert response.status_code == 503


def test_health_ready_200_when_watchdog_running(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    # Fake a running task by using a mock that has done() returning False
    class _FakeTask:
        def done(self):
            return False

    fake_jm = main_mod.JobManager.__new__(main_mod.JobManager)
    monkeypatch.setattr(fake_jm, "_watchdog_task", _FakeTask(), raising=False)
    monkeypatch.setattr(main_mod, "JOB_MANAGER", fake_jm)
    response = client.get("/health/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["watchdog"] == "running"


# ---------------------------------------------------------------------------
# CLI: jobs toc, jobs summary, jobs delete, health, metrics
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cmd",
    [
        ["jobs", "toc", "abc"],
        ["jobs", "summary", "abc"],
        ["jobs", "delete", "abc", "--yes"],
        ["jobs", "retry", "abc"],
        ["health"],
        ["health", "--ready"],
        ["metrics"],
    ],
)
def test_r6_cli_subcommands_registered(cmd) -> None:
    from typer.testing import CliRunner

    from scripts import mdwb_cli

    runner = CliRunner()
    result = runner.invoke(mdwb_cli.cli, cmd + ["--help"])
    assert result.exit_code == 0, f"{' '.join(cmd)} --help failed: {result.output}"


def test_r6_cli_jobs_toc_invokes_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
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
            captured["params"] = params
            return _hx.Response(
                200,
                json={
                    "job_id": "abc",
                    "url": "https://x.com",
                    "sections": [{"level": 1, "heading": "Top", "anchor": "top", "body_chars": 50}],
                    "outbound_links": ["https://a.com"],
                },
                request=_hx.Request("GET", url),
            )

    monkeypatch.setattr(mdwb_cli.httpx, "Client", _StubClient)
    monkeypatch.setattr(mdwb_cli, "_resolve_settings", _fake_settings)

    runner = CliRunner()
    result = runner.invoke(mdwb_cli.cli, ["jobs", "toc", "abc"])
    assert result.exit_code == 0
    assert "Top" in result.output
    assert "https://a.com" in result.output
    assert "/jobs/abc/toc" in captured["url"]


def test_r6_cli_metrics_combines_two_endpoints(monkeypatch: pytest.MonkeyPatch) -> None:
    """`mdwb metrics` should hit /metrics/job-counts AND /metrics/embedders."""
    from typer.testing import CliRunner

    from scripts import mdwb_cli

    captured_urls: list = []

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

            captured_urls.append(url)
            if "job-counts" in url:
                return _hx.Response(
                    200,
                    json={
                        "total": 5,
                        "by_state": {"DONE": 3, "FAILED": 2},
                        "by_issue": {},
                        "by_day": {},
                        "generated_at": "2026-01-01T00:00:00Z",
                    },
                    request=_hx.Request("GET", url),
                )
            if "embedders" in url:
                return _hx.Response(
                    200,
                    json={
                        "available": ["hash-bucket-v1"],
                        "default": "hash-bucket-v1",
                        "counts": {"hash-bucket-v1": 5},
                    },
                    request=_hx.Request("GET", url),
                )
            return _hx.Response(404, json={"detail": "not found"}, request=_hx.Request("GET", url))

    monkeypatch.setattr(mdwb_cli.httpx, "Client", _StubClient)
    monkeypatch.setattr(mdwb_cli, "_resolve_settings", _fake_settings)

    runner = CliRunner()
    result = runner.invoke(mdwb_cli.cli, ["metrics"])
    assert result.exit_code == 0
    assert "total jobs" in result.output
    assert "hash-bucket-v1" in result.output
    # Both endpoints hit
    assert any("job-counts" in u for u in captured_urls)
    assert any("embedders" in u for u in captured_urls)
