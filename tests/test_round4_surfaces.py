"""Tests for R4 surfaces: multi-tag, cancel, slo.json, events.json, links, artifacts, batch/status, raw markdown, CANCELLED state."""

from __future__ import annotations

import json
import pathlib
from datetime import datetime, timezone
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

import app.main as main_mod
from app.jobs import JobManager, JobState
from app.main import app
from app.store import RunRecord, StorageConfig, Store


class _FakeJobManager:
    """Stand-in for app.main.JOB_MANAGER.

    Each instance attribute holds a function (not a bound method). When the
    route calls ``JOB_MANAGER.method(arg)`` Python looks up ``method`` on the
    instance and invokes it as a plain function — so signatures are
    ``(arg)``, not ``(self, arg)``.
    """

    def __init__(self, **attrs):
        for k, v in attrs.items():
            setattr(self, k, v)


def _patch_jm(monkeypatch, **attrs):
    """Replace main_mod.JOB_MANAGER with a fake that exposes the given methods."""
    fake = _FakeJobManager(**attrs)
    monkeypatch.setattr(main_mod, "JOB_MANAGER", fake)
    return fake


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def artifact_store(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Store]:
    store = Store(StorageConfig(tmp_path / "cache", tmp_path / "runs.db"))
    monkeypatch.setattr(main_mod, "store", store)
    try:
        yield store
    finally:
        store.engine.dispose()


# ---------------------------------------------------------------------------
# CANCELLED enum
# ---------------------------------------------------------------------------


def test_cancelled_state_is_defined() -> None:
    assert JobState.CANCELLED.value == "CANCELLED"
    assert "CANCELLED" in {s.value for s in JobState}


# ---------------------------------------------------------------------------
# /jobs/{id}/tags (multi-add) + DELETE /jobs/{id}/tag/{tag}
# ---------------------------------------------------------------------------


def test_multi_tag_add_is_idempotent(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def _add_tags(job_id, tags):
        return ["a", "b", "c"]

    def _get_snap(job_id):
        return {"id": job_id, "job_id": job_id, "tags": ["a", "b", "c"]}

    _patch_jm(monkeypatch, add_tags=_add_tags, get_snapshot=_get_snap)
    response = client.post("/jobs/abc/tags", json={"tags": ["a", "a", "b", "c"]})
    assert response.status_code == 200
    assert response.json()["tags"] == ["a", "b", "c"]


def test_remove_tag_returns_empty_when_not_present(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_jm(monkeypatch, remove_tag=lambda job_id, tag: [])
    response = client.delete("/jobs/abc/tag/anything")
    assert response.status_code == 200
    assert response.json()["tags"] == []


# ---------------------------------------------------------------------------
# /jobs/{id}/cancel
# ---------------------------------------------------------------------------


def test_cancel_sets_cancelled_state(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def _cancel(job_id, *, reason):
        return True

    def _get_snap(job_id):
        return {
            "id": job_id,
            "job_id": job_id,
            "state": JobState.CANCELLED.value,
            "url": "https://example.com",
        }

    _patch_jm(monkeypatch, cancel_job=_cancel, get_snapshot=_get_snap)
    response = client.post("/jobs/abc/cancel", params={"reason": "user hit stop"})
    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "CANCELLED"


def test_cancel_404_for_unknown_job(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def _cancel(job_id, *, reason):
        return False

    def _get_snap(job_id):
        return None

    _patch_jm(monkeypatch, cancel_job=_cancel, get_snapshot=_get_snap)
    response = client.post("/jobs/does-not-exist/cancel")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# /jobs/{id}/slo.json alias
# ---------------------------------------------------------------------------


def test_slo_json_alias_matches_slo(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def _manifest(self, jid):
        return {"timings": {"capture_ms": 100, "ocr_ms": 200, "stitch_ms": 50, "total_ms": 350}}

    monkeypatch.setattr(type(main_mod.store), "read_manifest", _manifest)
    a = client.get("/jobs/abc/slo").json()
    b = client.get("/jobs/abc/slo.json").json()
    assert a == b


# ---------------------------------------------------------------------------
# /jobs/{id}/events.json
# ---------------------------------------------------------------------------


def test_events_json_404_for_unknown_job(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, artifact_store: Store
) -> None:
    monkeypatch.setattr(main_mod, "JOB_MANAGER", JobManager(store=artifact_store))
    response = client.get("/jobs/abc/events.json")
    assert response.status_code == 404


def test_events_json_empty_for_known_job_without_history(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, artifact_store: Store
) -> None:
    artifact_store.allocate_run(
        job_id="abc", url="https://example.com", started_at=datetime.now(timezone.utc)
    )
    monkeypatch.setattr(main_mod, "JOB_MANAGER", JobManager(store=artifact_store))
    response = client.get("/jobs/abc/events.json")
    assert response.status_code == 200
    body = response.json()
    assert body["job_id"] == "abc"
    assert body["events"] == []
    assert body["count"] == 0


def test_events_json_returns_recorded_events(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, artifact_store: Store
) -> None:
    artifact_store.allocate_run(
        job_id="abc", url="https://example.com", started_at=datetime.now(timezone.utc)
    )
    events = [
        artifact_store.append_job_event(job_id="abc", owner=None, entry=entry)
        for entry in (
            {"event": "state_change", "data": {"state": "DONE"}},
            {"event": "cache_hit", "data": {"source_job_id": "old"}},
        )
    ]
    monkeypatch.setattr(main_mod, "JOB_MANAGER", JobManager(store=artifact_store))
    response = client.get("/jobs/abc/events.json")
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 2
    assert body["events"] == events


# ---------------------------------------------------------------------------
# /jobs/{id}/links (per-source breakdown)
# ---------------------------------------------------------------------------


def test_links_structured_groups_by_source(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _links(self, jid):
        return {
            "anchors": [
                {"href": "https://a.com", "text": "A", "source": "dom", "delta": "match"},
                {"href": "https://b.com", "text": "B", "source": "ocr", "delta": None},
                {"href": "https://c.com", "text": "C", "source": "dom", "delta": "mismatch"},
                {"href": "https://d.com", "text": "D", "source": "both", "delta": "match"},
                {"href": "https://e.com", "text": "E"},
            ]
        }

    monkeypatch.setattr(type(main_mod.store), "read_links", _links)
    response = client.get("/jobs/abc/links")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 5
    assert body["counts"]["dom"] == 2
    assert body["counts"]["ocr"] == 1
    assert body["counts"]["both"] == 1
    assert body["counts"]["other"] == 1
    assert len(body["by_source"]["dom"]) == 2


def test_links_structured_empty(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def _links(self, jid):
        return None

    monkeypatch.setattr(type(main_mod.store), "read_links", _links)
    response = client.get("/jobs/abc/links")
    body = response.json()
    assert body["total"] == 0
    assert body["by_source"] == {}


# ---------------------------------------------------------------------------
# /jobs/{id}/artifacts
# ---------------------------------------------------------------------------


def test_artifacts_404_when_run_missing(client: TestClient, artifact_store: Store) -> None:
    assert artifact_store.fetch_run("abc") is None
    response = client.get("/jobs/abc/artifacts")
    assert response.status_code == 404


def test_artifacts_lists_files_in_root(client: TestClient, artifact_store: Store) -> None:
    run_paths = artifact_store.allocate_run(
        job_id="abc", url="https://example.com", started_at=datetime.now(timezone.utc)
    )
    root = run_paths.root
    run_paths.manifest_path.write_text("{}", encoding="utf-8")
    run_paths.markdown_path.write_text("# hi", encoding="utf-8")
    (root / "tile_0000.png").write_bytes(b"fake-png")
    sub = root / "tiles"
    sub.mkdir()
    (sub / "tile_0001.png").write_bytes(b"more")
    record = artifact_store.fetch_run("abc")
    assert record is not None
    assert pathlib.Path(record.cache_path) == root
    assert "artifact_root" not in json.loads(run_paths.manifest_path.read_text("utf-8"))
    response = client.get("/jobs/abc/artifacts")
    assert response.status_code == 200
    body = response.json()
    assert body["file_count"] == 4
    paths = {f["path"] for f in body["files"]}
    assert "manifest.json" in paths
    assert "out.md" in paths
    assert "tile_0000.png" in paths
    assert "tiles/tile_0001.png" in paths
    assert paths == {"manifest.json", "out.md", "tile_0000.png", "tiles/tile_0001.png"}
    assert body["artifact_root"] == str(root.resolve())
    assert {file["path"]: file["size"] for file in body["files"]} == {
        "manifest.json": 2,
        "out.md": 4,
        "tile_0000.png": 8,
        "tiles/tile_0001.png": 4,
    }
    assert body["synthetic"]["structured_result"].endswith("/result.json")
    assert body["total_bytes"] == sum(f["size"] for f in body["files"])
    assert body["total_bytes"] == 18


def test_artifacts_404_when_persisted_root_is_absent(
    client: TestClient, artifact_store: Store
) -> None:
    root = artifact_store.config.cache_root / "absent-run"
    with artifact_store.session() as session:
        session.add(
            RunRecord(
                id="absent-root",
                url="https://example.com",
                started_at=datetime.now(timezone.utc),
                cache_path=str(root),
                manifest_path=str(root / "manifest.json"),
            )
        )
        session.commit()
    assert artifact_store.fetch_run("absent-root") is not None
    assert not root.exists()
    response = client.get("/jobs/absent-root/artifacts")
    assert response.status_code == 404


def test_artifacts_refuse_persisted_root_outside_cache(
    client: TestClient, artifact_store: Store, tmp_path: pathlib.Path
) -> None:
    outside = tmp_path / "outside-cache"
    outside.mkdir()
    (outside / "secret.txt").write_text("outside-secret-sentinel", encoding="utf-8")
    with artifact_store.session() as session:
        session.add(
            RunRecord(
                id="escaped-root",
                url="https://example.com",
                started_at=datetime.now(timezone.utc),
                cache_path=str(outside),
                manifest_path=str(outside / "manifest.json"),
            )
        )
        session.commit()
    response = client.get("/jobs/escaped-root/artifacts")
    assert response.status_code == 409
    assert "secret.txt" not in response.text
    assert "outside-secret-sentinel" not in response.text
    assert str(outside) not in response.text


def test_artifacts_do_not_list_symlinked_files_outside_root(
    client: TestClient, artifact_store: Store, tmp_path: pathlib.Path
) -> None:
    run_paths = artifact_store.allocate_run(
        job_id="symlink-run", url="https://example.com", started_at=datetime.now(timezone.utc)
    )
    run_paths.manifest_path.write_text("{}", encoding="utf-8")
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("outside-secret-sentinel", encoding="utf-8")
    (run_paths.root / "escaped-secret.txt").symlink_to(outside)
    response = client.get("/jobs/symlink-run/artifacts")
    assert response.status_code == 200
    body = response.json()
    assert [file["path"] for file in body["files"]] == ["manifest.json"]
    assert body["file_count"] == 1
    assert body["total_bytes"] == 2
    assert "escaped-secret.txt" not in response.text
    assert "outside-secret-sentinel" not in response.text
    assert str(outside) not in response.text


# ---------------------------------------------------------------------------
# /jobs/batch/status
# ---------------------------------------------------------------------------


def test_batch_status_rejects_empty_ids(client: TestClient) -> None:
    response = client.post("/jobs/batch/status", json={"job_ids": []})
    assert response.status_code == 422


def test_batch_status_rejects_too_many_ids(client: TestClient) -> None:
    response = client.post("/jobs/batch/status", json={"job_ids": [f"x{i}" for i in range(501)]})
    assert response.status_code == 422


def test_batch_status_returns_compact_records(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _batch(ids):
        return [
            {
                "job_id": i,
                "state": "DONE",
                "cache_hit": False,
                "url": f"https://x.com/{i}",
                "progress": {"done": 1, "total": 1},
                "error": None,
                "tags": [],
            }
            for i in ids
        ]

    _patch_jm(monkeypatch, batch_status=_batch)
    response = client.post("/jobs/batch/status", json={"job_ids": ["a", "b"]})
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 2
    assert [s["job_id"] for s in body["statuses"]] == ["a", "b"]


# ---------------------------------------------------------------------------
# /jobs/{id}/result.md?raw=true
# ---------------------------------------------------------------------------


def test_result_md_raw_strips_provenance(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    md = (
        "# Heading\n\n"
        "Body text.\n"
        "<!-- source: tile_0000, y=0, height=1288, sha256=abc, scale=0.5 -->\n"
        "More body.\n"
        "<!-- another comment that spans\nmultiple lines -->\n"
        "Last paragraph.\n"
    )

    def _read_md(self, jid):
        return md

    monkeypatch.setattr(type(main_mod.store), "read_markdown", _read_md)

    response = client.get("/jobs/abc/result.md")
    assert "source: tile_0000" in response.text
    assert "More body." in response.text

    response = client.get("/jobs/abc/result.md?raw=true")
    assert "source: tile_0000" not in response.text
    assert "More body." in response.text
    assert "Last paragraph." in response.text


def test_result_md_raw_alias_route(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    md = "# X\n<!-- comment -->\nY"

    def _read_md(self, jid):
        return md

    monkeypatch.setattr(type(main_mod.store), "read_markdown", _read_md)
    response = client.get("/jobs/abc/result.md/raw")
    assert "comment" not in response.text
    assert "Y" in response.text


# ---------------------------------------------------------------------------
# CLI: events-json, links, artifacts, cancel, tags, batch-status, schema.json
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cmd",
    [
        ["events-json", "abc"],
        ["links", "abc"],
        ["artifacts", "abc"],
        ["cancel", "abc"],
        ["tags", "abc", "list"],
        ["tags", "abc", "add", "tag1"],
        ["tags", "abc", "rm", "tag1"],
        ["batch-status", "abc", "def"],
        ["schema.json"],
    ],
)
def test_r4_cli_subcommands_registered(cmd) -> None:
    from typer.testing import CliRunner

    from scripts import mdwb_cli

    runner = CliRunner()
    result = runner.invoke(mdwb_cli.cli, cmd + ["--help"])
    assert result.exit_code == 0, f"{' '.join(cmd)} --help failed: {result.output}"


def test_r4_cli_tags_add_invokes_correct_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
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

        def post(self, url, json=None):
            import httpx as _hx

            captured["url"] = url
            captured["json"] = json
            return _hx.Response(
                200,
                json={"job_id": "abc", "tags": ["foo"]},
                request=_hx.Request("POST", url, json=json),
            )

    monkeypatch.setattr(mdwb_cli.httpx, "Client", _StubClient)
    monkeypatch.setattr(mdwb_cli, "_resolve_settings", _fake_settings)

    runner = CliRunner()
    result = runner.invoke(mdwb_cli.cli, ["tags", "abc", "add", "foo"])
    assert result.exit_code == 0
    assert captured["json"] == {"tags": ["foo"]}
    assert "/jobs/abc/tags" in captured["url"]


def test_r4_cli_batch_status_invokes_correct_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
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

        def post(self, url, json=None):
            import httpx as _hx

            captured["url"] = url
            captured["json"] = json
            return _hx.Response(
                200,
                json={"count": 2, "statuses": [{"job_id": "a", "state": "DONE"}]},
                request=_hx.Request("POST", url, json=json),
            )

    monkeypatch.setattr(mdwb_cli.httpx, "Client", _StubClient)
    monkeypatch.setattr(mdwb_cli, "_resolve_settings", _fake_settings)

    runner = CliRunner()
    result = runner.invoke(mdwb_cli.cli, ["batch-status", "a", "b"])
    assert result.exit_code == 0
    assert captured["json"] == {"job_ids": ["a", "b"]}
    assert "/jobs/batch/status" in captured["url"]


def test_r4_cli_schema_json_writes_to_file(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
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
                json={"service": "mdwb", "endpoints": {}},
                request=_hx.Request("GET", url),
            )

    monkeypatch.setattr(mdwb_cli.httpx, "Client", _StubClient)
    monkeypatch.setattr(mdwb_cli, "_resolve_settings", _fake_settings)

    out = tmp_path / "schema.json"
    runner = CliRunner()
    result = runner.invoke(mdwb_cli.cli, ["schema.json", "--out", str(out)])
    assert result.exit_code == 0
    assert out.exists()
    data = json.loads(out.read_text())
    assert data["service"] == "mdwb"
    assert "/schema.json" in captured["url"]
