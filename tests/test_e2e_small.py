"""Bounded API/manager/SQLite integration; cached fixtures do not prove OCR quality."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import jobs, main
from app.capture import CaptureResult
from app.jobs import JobManager
from app.schemas import JobCreateRequest
from app.store import StorageConfig, Store


def test_cached_capture_is_served_through_real_api_and_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Store(StorageConfig(tmp_path / "cache", tmp_path / "runs.db"))
    capture_calls: list[str] = []

    async def forbidden_capture(**kwargs) -> tuple[CaptureResult, list[dict[str, object]]]:
        capture_calls.append(kwargs["url"])
        pytest.fail("A complete cached run must not invoke browser capture or OCR")

    manager = JobManager(store=store, runner=forbidden_capture)
    request = JobCreateRequest(url="https://example.com/invoice")
    config = jobs._build_capture_config(request, jobs.global_settings)
    cache_key = jobs._build_cache_key(config=config, settings=jobs.global_settings)
    now = datetime.now(timezone.utc)
    paths = store.allocate_run(
        job_id="source-run", url=request.url, started_at=now, cache_key=cache_key
    )
    markdown = (
        "# Invoice\n\n| Item | Amount |\n| --- | --- |\n| Widget | 42 |\n\n"
        "<!-- source: tile_0000, y=0, sha256=fixture -->\n"
        "[Guide](https://example.com/guide)\n"
    )
    links = {"anchors": [{"href": "https://example.com/guide", "text": "Guide", "source": "dom"}]}
    store.write_markdown(job_id="source-run", content=markdown)
    store.write_links(job_id="source-run", links=links)
    store.write_manifest(
        job_id="source-run",
        manifest={"url": request.url, "tiles_total": 1, "cache_key": cache_key},
    )
    store.update_status(job_id="source-run", status="DONE", finished_at=now)
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(main, "JOB_MANAGER", manager)
    monkeypatch.setattr(main, "settings", replace(main.settings, REQUIRE_API_KEY=False))

    try:
        with TestClient(main.app) as client:
            submitted = client.post("/jobs", json=request.model_dump())
            assert submitted.status_code == 202
            payload = submitted.json()
            assert payload["id"] != "source-run"
            assert payload["state"] == "DONE"
            assert payload["cache_hit"] is True
            job_id = payload["id"]
            assert client.get(f"/jobs/{job_id}/result.md").text == markdown
            link_response = client.get(f"/jobs/{job_id}/links.json")
            assert link_response.status_code == 200
            assert link_response.json() == links["anchors"]
            structured = client.get(f"/jobs/{job_id}/result.json")
            assert structured.status_code == 200
            assert structured.json()["sections"][0]["heading"] == "Invoice"
            assert "| Widget | 42 |" in structured.json()["sections"][0]["body"]
            raw = client.get(f"/jobs/{job_id}/result.md?raw=true")
            assert raw.status_code == 200
            assert "<!-- source:" not in raw.text
            assert "[Guide](https://example.com/guide)" in raw.text
            events = client.get(f"/jobs/{job_id}/events.json").json()["events"]
            cache_events = [entry for entry in events if entry["event"] == "cache_hit"]
            assert len(cache_events) == 1
            assert cache_events[0]["data"]["source_job_id"] == "source-run"
            assert capture_calls == []
            assert manager._tasks == {}
            record = store.fetch_run(job_id)
            assert record is not None
            assert record.status == "DONE"
            assert Path(record.cache_path) == paths.root
        reopened = Store(store.config)
        try:
            assert reopened.read_markdown(job_id) == markdown
            assert reopened.read_links(job_id) == links
        finally:
            reopened.engine.dispose()
    finally:
        store.engine.dispose()


@pytest.mark.parametrize(
    "state,write_manifest,age_hours",
    [("FAILED", True, 0), ("DONE", False, 0), ("DONE", True, 48)],
)
def test_cache_refuses_failed_missing_or_expired_artifacts(
    tmp_path: Path, state: str, write_manifest: bool, age_hours: int
) -> None:
    store = Store(StorageConfig(tmp_path / "cache", tmp_path / "runs.db"), cache_ttl_hours=24)
    finished = datetime.now(timezone.utc) - timedelta(hours=age_hours)
    try:
        store.allocate_run(
            job_id="incomplete", url="https://example.com", started_at=finished, cache_key="abc123"
        )
        if write_manifest:
            store.write_manifest(job_id="incomplete", manifest={"url": "https://example.com"})
        store.update_status(job_id="incomplete", status=state, finished_at=finished)
        assert store.find_cache_hit("abc123") is None
        assert store.fetch_run("incomplete") is not None
    finally:
        store.engine.dispose()
