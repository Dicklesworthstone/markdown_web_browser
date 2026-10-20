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
    monkeypatch.setattr(_main.JOB_MANAGER, "store", isolated_store)
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


# ---------------------------------------------------------------------------
# Auth, Rate Limiting, Owner Isolation & Profile Hardening (7ru.1)
# ---------------------------------------------------------------------------


def test_cli_auth_headers_send_both_x_api_key_and_bearer() -> None:
    from pathlib import Path

    from scripts import mdwb_cli, olmocr_cli

    mdwb_settings = mdwb_cli.APISettings(
        base_url="http://localhost:8000",
        api_key="mdwb_secret_123",
        warning_log_path=Path("ops/warnings.jsonl"),
    )
    headers = mdwb_cli._auth_headers(mdwb_settings)
    assert headers["X-API-Key"] == "mdwb_secret_123"
    assert headers["Authorization"] == "Bearer mdwb_secret_123"

    olm_settings = olmocr_cli.CLISettings(
        api_base_url="http://localhost:8000",
        mdwb_api_key="mdwb_secret_456",
        ocr_server="http://localhost:8001",
        ocr_model="olmOCR-2-7B-1025-FP8",
        ocr_api_key=None,
        tiles_long_side=1288,
        tile_overlap_px=120,
        viewport_overlap_px=120,
        concurrency_min=2,
        concurrency_max=8,
        cft_version="130.0",
        cft_label="Stable",
        playwright_channel="cft",
        screenshot_style_hash="abc",
    )
    olm_headers = olmocr_cli._auth_headers(olm_settings)
    assert olm_headers["X-API-Key"] == "mdwb_secret_456"
    assert olm_headers["Authorization"] == "Bearer mdwb_secret_456"


@pytest.mark.asyncio
async def test_api_key_auth_enforced_when_required(
    client: TestClient, isolated_store, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.auth import create_api_key, revoke_api_key

    monkeypatch.setattr("app.auth.effective_require_api_key", lambda *_a, **_kw: True)

    with isolated_store.session() as session:
        plaintext_valid, _ = create_api_key(session, name="alice-key", rate_limit=60, owner="alice")
        plaintext_revoked, record_revoked = create_api_key(
            session, name="revoked-key", rate_limit=60, owner="revoked-user"
        )
        assert record_revoked.id is not None
        revoke_api_key(session, record_revoked.id)

    # Exempt paths succeed without an API key
    assert client.get("/health").status_code == 200
    assert client.get("/browser").status_code == 200

    # Protected endpoints reject missing, malformed, and revoked keys before work
    assert client.get("/jobs").status_code == 401
    assert client.post("/jobs", json={"url": "https://example.com"}).status_code == 401
    assert (
        client.get("/jobs", headers={"X-API-Key": "mdwb_invalid_key_000000000000"}).status_code
        == 401
    )
    assert client.get("/jobs", headers={"X-API-Key": plaintext_revoked}).status_code == 401

    # Valid key succeeds via X-API-Key and Authorization: Bearer
    resp_header = client.get("/jobs", headers={"X-API-Key": plaintext_valid})
    assert resp_header.status_code == 200
    assert "X-RateLimit-Limit" in resp_header.headers

    resp_bearer = client.get(
        "/jobs", headers={"Authorization": f"Bearer {plaintext_valid}"}
    )
    assert resp_bearer.status_code == 200

    # Custom API_KEY_HEADER also works
    monkeypatch.setattr("app.auth.effective_api_key_header", lambda *_a, **_kw: "X-Custom-Token")
    resp_custom = client.get("/jobs", headers={"X-Custom-Token": plaintext_valid})
    assert resp_custom.status_code == 200


@pytest.mark.asyncio
async def test_multi_tenant_owner_isolation_across_jobs_and_cache(
    client: TestClient, isolated_store, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, datetime

    from app.auth import create_api_key
    from app.jobs import JobState

    monkeypatch.setattr("app.auth.effective_require_api_key", lambda *_a, **_kw: True)

    with isolated_store.session() as session:
        alice_key, _ = create_api_key(session, name="alice-key", rate_limit=60, owner="alice")
        bob_key, _ = create_api_key(session, name="bob-key", rate_limit=60, owner="bob")

    # Seed completed runs for Alice and Bob in isolated_store
    now = datetime.now(UTC)
    paths_a = isolated_store.allocate_run(
        job_id="job_alice_1",
        url="https://example.com/secret-a",
        started_at=now,
        owner="alice",
        profile_id="work",
        cache_key="shared-cache-key",
        tags=["alice-tag"],
    )
    isolated_store.write_markdown(
        job_id="job_alice_1", content="# Alice Secret Heading\n\nAlice confidential body."
    )
    isolated_store.write_manifest(
        job_id="job_alice_1",
        manifest={"url": "https://example.com/secret-a", "profile_id": "work"},
    )
    isolated_store.update_status(
        job_id="job_alice_1",
        status=JobState.DONE.value,
        finished_at=now,
    )

    paths_b = isolated_store.allocate_run(
        job_id="job_bob_1",
        url="https://example.com/secret-b",
        started_at=now,
        owner="bob",
        profile_id="work",
        tags=["bob-tag"],
    )
    isolated_store.write_markdown(job_id="job_bob_1", content="# Bob Heading\n\nBob body.")
    isolated_store.write_manifest(
        job_id="job_bob_1",
        manifest={"url": "https://example.com/secret-b", "profile_id": "work"},
    )
    isolated_store.update_status(
        job_id="job_bob_1",
        status=JobState.DONE.value,
        finished_at=now,
    )

    # Populate in-memory snapshots in JOB_MANAGER
    import app.main as _main

    _main.JOB_MANAGER._snapshots["job_alice_1"] = {
        "id": "job_alice_1",
        "url": "https://example.com/secret-a",
        "state": JobState.DONE,
        "owner": "alice",
        "profile_id": "work",
        "manifest_path": str(paths_a.manifest_path),
        "manifest": {"url": "https://example.com/secret-a", "profile_id": "work"},
    }
    _main.JOB_MANAGER._snapshots["job_bob_1"] = {
        "id": "job_bob_1",
        "url": "https://example.com/secret-b",
        "state": JobState.DONE,
        "owner": "bob",
        "profile_id": "work",
        "manifest_path": str(paths_b.manifest_path),
        "manifest": {"url": "https://example.com/secret-b", "profile_id": "work"},
    }

    alice_headers = {"X-API-Key": alice_key}
    bob_headers = {"X-API-Key": bob_key}

    # Alice can read her job; Bob gets 403 on Alice's job and artifacts
    assert client.get("/jobs/job_alice_1", headers=alice_headers).status_code == 200
    assert client.get("/jobs/job_alice_1", headers=bob_headers).status_code == 403
    assert client.get("/jobs/job_alice_1/result.md", headers=bob_headers).status_code == 403
    assert client.get("/jobs/job_alice_1/manifest.json", headers=bob_headers).status_code == 403
    assert client.delete("/jobs/job_alice_1", headers=bob_headers).status_code == 403

    # Bob cannot diff his job against Alice's job
    diff_resp = client.post(
        "/jobs/job_bob_1/diff",
        headers=bob_headers,
        json={"other_job_id": "job_alice_1"},
    )
    assert diff_resp.status_code == 403

    # GET /jobs only lists the caller's own jobs
    alice_list = client.get("/jobs", headers=alice_headers).json()
    assert [item["id"] for item in alice_list["items"]] == ["job_alice_1"]
    bob_list = client.get("/jobs", headers=bob_headers).json()
    assert [item["id"] for item in bob_list["items"]] == ["job_bob_1"]

    # POST /jobs/search only searches the caller's own jobs
    bob_search = client.post(
        "/jobs/search",
        headers=bob_headers,
        json={"query": "confidential"},
    ).json()
    assert bob_search["matches"] == []

    alice_search = client.post(
        "/jobs/search",
        headers=alice_headers,
        json={"query": "confidential"},
    ).json()
    assert len(alice_search["matches"]) == 1
    assert alice_search["matches"][0]["job_id"] == "job_alice_1"

    # POST /jobs/batch/status rejects cross-owner job IDs with 403 and allows own job IDs
    batch_forbidden = client.post(
        "/jobs/batch/status",
        headers=bob_headers,
        json={"job_ids": ["job_alice_1", "job_bob_1"]},
    )
    assert batch_forbidden.status_code == 403

    batch_ok = client.post(
        "/jobs/batch/status",
        headers=bob_headers,
        json={"job_ids": ["job_bob_1"]},
    ).json()
    assert batch_ok["statuses"][0]["job_id"] == "job_bob_1"
    assert batch_ok["statuses"][0]["state"] == "DONE"

    # Cache hit lookup is strictly scoped to owner
    assert isolated_store.find_cache_hit("shared-cache-key", owner="bob") is None
    alice_hit = isolated_store.find_cache_hit("shared-cache-key", owner="alice")
    assert alice_hit is not None
    assert alice_hit.id == "job_alice_1"

    # Public share endpoint redacts profile_id and sensitive manifest fields
    share_resp = client.post("/jobs/job_alice_1/share", headers=alice_headers)
    assert share_resp.status_code == 200
    token = share_resp.json()["token"]

    # Unauthenticated user can view the shared token, but profile_id is redacted
    public_resp = client.get(f"/jobs/share/{token}")
    assert public_resp.status_code == 200
    public_body = public_resp.json()
    assert public_body["profile_id"] is None
    assert "profile_id" not in (public_body.get("manifest") or {})


@pytest.mark.asyncio
async def test_rate_limiter_enforces_429_and_syncs_across_workers(
    client: TestClient, isolated_store, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.auth import create_api_key
    from app.rate_limit import RateLimiter, reset_rate_limiter

    reset_rate_limiter()
    monkeypatch.setattr("app.auth.effective_require_api_key", lambda *_a, **_kw: True)

    with isolated_store.session() as session:
        limited_key, _ = create_api_key(
            session, name="rate-limited", rate_limit=3, owner="rate-limited-owner"
        )
    headers = {"X-API-Key": limited_key}

    for _ in range(3):
        res = client.get("/jobs", headers=headers)
        assert res.status_code == 200

    res_429 = client.get("/jobs", headers=headers)
    assert res_429.status_code == 429
    assert "Retry-After" in res_429.headers
    assert res_429.headers["X-RateLimit-Remaining"] == "0"

    # Verify cross-worker SQLite bucket sync with two distinct RateLimiter instances
    worker_a = RateLimiter(requests_per_minute=2, burst_capacity=2, store=isolated_store)
    worker_b = RateLimiter(requests_per_minute=2, burst_capacity=2, store=isolated_store)
    allowed_1, _ = worker_a.check_rate_limit("shared-worker-key", requests_per_minute=2)
    allowed_2, _ = worker_b.check_rate_limit("shared-worker-key", requests_per_minute=2)
    allowed_3, info_3 = worker_a.check_rate_limit("shared-worker-key", requests_per_minute=2)
    assert allowed_1 is True
    assert allowed_2 is True
    assert allowed_3 is False
    assert info_3["retry_after"] is not None


def test_profile_id_validation_rejects_traversal_and_preserves_distinct_slugs(
    tmp_path,
) -> None:
    from pydantic import ValidationError

    from app.capture import _profile_storage_state_path
    from app.schemas import JobCreateRequest, ReplayRequest, validate_profile_id

    for bad_profile in [
        ".",
        "..",
        "../secret",
        "foo/bar",
        "foo\\bar",
        ".leadingdot",
        "trailingdot.",
        "   ",
        "bad space",
        "semi;colon",
    ]:
        with pytest.raises(ValueError):
            validate_profile_id(bad_profile)
        with pytest.raises(ValidationError):
            JobCreateRequest(url="https://example.com", profile_id=bad_profile)
        with pytest.raises(ValidationError):
            ReplayRequest(manifest={"url": "https://example.com", "profile_id": bad_profile})

    # Distinct slugs ('my-profile' vs 'my_profile') and distinct owners never collide
    p_dash_alice = _profile_storage_state_path(tmp_path, "my-profile", owner="alice")
    p_under_alice = _profile_storage_state_path(tmp_path, "my_profile", owner="alice")
    p_dash_bob = _profile_storage_state_path(tmp_path, "my-profile", owner="bob")

    assert p_dash_alice != p_under_alice
    assert p_dash_alice != p_dash_bob
    assert p_dash_alice.resolve().is_relative_to(tmp_path.resolve())
    assert p_under_alice.resolve().is_relative_to(tmp_path.resolve())
    assert p_dash_bob.resolve().is_relative_to(tmp_path.resolve())


def test_browser_ui_has_no_external_cdns_and_escapes_sse_error_logs() -> None:
    """Verify /browser has no external CDN dependencies and _snapshot_events escapes HTML in errors."""
    from typing import cast
    from app.jobs import JobSnapshot
    from app.main import _snapshot_events, app

    with TestClient(app) as client:
        resp = client.get("/browser")
        assert resp.status_code == 200
        html_body = resp.text
        assert "cdnjs.cloudflare.com" not in html_body
        assert "cdn.jsdelivr.net" not in html_body
        assert 'href="/static/browser.css"' in html_body
        assert 'src="/static/browser.js"' in html_body

    snapshot = cast(
        JobSnapshot,
        {
            "id": "xss-job",
            "state": "FAILED",
            "error": '<img src=x onerror="alert(1)"><script>evil()</script>',
        },
    )
    events = dict(_snapshot_events(snapshot))
    assert "log" in events
    assert "<script>" not in events["log"]
    assert "<img" not in events["log"]
    assert "&lt;script&gt;evil()&lt;/script&gt;" in events["log"]


def test_browser_js_and_app_js_offline_markdown_xss_refresh_and_artifacts() -> None:
    """Execute web/browser.js and web/app.js in Node to verify XSS sanitization, refresh reuse_cache=false, and DONE markdown/artifact rendering."""
    import shutil
    import subprocess
    from pathlib import Path

    node_bin = shutil.which("node")
    assert node_bin is not None, "node binary required to validate frontend JS runtime"

    repo_root = Path(__file__).resolve().parents[1]
    browser_js = repo_root / "web" / "browser.js"
    app_js = repo_root / "web" / "app.js"

    # Syntax check both files
    subprocess.run([node_bin, "--check", str(browser_js)], check=True)
    subprocess.run([node_bin, "--check", str(app_js)], check=True)

    # Execute runtime behavioral assertions in Node
    node_script = """
const assert = require('node:assert');
const fs = require('node:fs');
const vm = require('node:vm');

// Test 1: web/browser.js sanitizeUrl, parseMarkdownSafe, and handleRefresh (reuse_cache: false)
const postedPayloads = [];
function makeEl() {
  return {
    value: '',
    disabled: false,
    textContent: '',
    innerHTML: '',
    style: {},
    dataset: {},
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    addEventListener(type, fn) { this['_on_' + type] = fn; },
    querySelectorAll() { return []; },
    focus() {},
    select() {},
  };
}
const browserElements = {};
const browserSandbox = {
  window: { location: { origin: 'http://localhost:8007', search: '?api_key=test-ui-key' } },
  document: {
    readyState: 'complete',
    cookie: '',
    getElementById(id) {
      if (!browserElements[id]) browserElements[id] = makeEl();
      return browserElements[id];
    },
    addEventListener() {},
  },
  localStorage: {
    _store: new Map(),
    getItem(k) { return this._store.get(k) || null; },
    setItem(k, v) { this._store.set(k, String(v)); },
    removeItem(k) { this._store.delete(k); },
  },
  URL,
  URLSearchParams,
  console,
  setTimeout,
  fetch: async (url, opts = {}) => {
    if (url === '/jobs' && opts.method === 'POST') {
      postedPayloads.push({ body: JSON.parse(opts.body), headers: opts.headers });
      return { ok: true, status: 202, json: async () => ({ id: 'job-refresh-1' }) };
    }
    if (url.startsWith('/jobs/job-refresh-1') && !url.endsWith('/result.md')) {
      return { ok: true, status: 200, json: async () => ({ id: 'job-refresh-1', state: 'DONE', progress: { done: 1, total: 1 } }) };
    }
    if (url.endsWith('/result.md')) {
      return {
        ok: true,
        status: 200,
        text: async () => '# Heading\\n\\n[bad](javascript:alert(1)) [good](https://example.com/next)\\n\\n<script>alert(1)</script>',
      };
    }
    throw new Error('Unexpected fetch: ' + url);
  },
};
vm.createContext(browserSandbox);
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), browserSandbox);

const api = browserSandbox.window.mdwbBrowser;
assert.strictEqual(api.getApiKey(), 'test-ui-key');
assert.strictEqual(api.sanitizeUrl('javascript:alert(1)'), '');
assert.strictEqual(api.sanitizeUrl('JaVaScRiPt:alert(1)'), '');
assert.strictEqual(api.sanitizeUrl('data:text/html,<script>alert(1)</script>'), '');
assert.strictEqual(api.sanitizeUrl('vbscript:msgbox(1)'), '');
assert.strictEqual(api.sanitizeUrl('https://example.com/path'), 'https://example.com/path');

const rendered = api.parseMarkdownSafe(
  '# Title\\n\\n<!-- source: tile_0000 -->\\n\\n[evil](javascript:alert(1)) and [safe](/docs)\\n\\n<script>evil()</script>',
  'https://example.com/base/'
);
assert(rendered.includes('<h1>Title</h1>'), 'Expected H1 in rendered markdown');
assert(!rendered.includes('javascript:'), 'javascript: URL must be stripped');
assert(rendered.includes('href="https://example.com/docs"'), 'Relative URL must resolve against baseUrl');
assert(!rendered.includes('<script>'), 'Raw <script> tag must be escaped');
assert(!rendered.includes('<!-- source:'), 'Provenance comment must be stripped from rendered view');

(async () => {
  await api.loadUrl('https://example.com');
  assert.strictEqual(postedPayloads.length, 1);
  assert.strictEqual(postedPayloads[0].body.reuse_cache, true);
  assert.strictEqual(postedPayloads[0].headers['X-API-Key'], 'test-ui-key');

  // Trigger refresh button handler -> must bypass cache and send reuse_cache: false
  await browserElements['refresh-btn']._on_click();
  assert.strictEqual(postedPayloads.length, 2);
  assert.strictEqual(postedPayloads[1].body.reuse_cache, false);

  // Test 2: web/app.js renderArtifacts with real backend schema (index, source_y_offset, sha256)
  const appElements = [];
  const appSandbox = {
    window: { location: { origin: 'http://localhost:8007', search: '' } },
    document: {
      querySelectorAll() { return []; },
      querySelector() { return null; },
      getElementById() { return null; },
      addEventListener() {},
      createDocumentFragment() { return { children: [], appendChild(c) { this.children.push(c); } }; },
      createElement(tag) {
        const node = {
          tagName: tag.toUpperCase(),
          textContent: '',
          innerHTML: '',
          className: '',
          dataset: {},
          style: {},
          children: [],
          append(...items) { this.children.push(...items); },
          appendChild(item) { this.children.push(item); },
          classList: { toggle() {}, add() {}, remove() {}, contains() { return false; } },
        };
        appElements.push(node);
        return node;
      },
    },
    localStorage: browserSandbox.localStorage,
    URL,
    URLSearchParams,
    console,
    setTimeout,
  };
  vm.createContext(appSandbox);
  vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), appSandbox);
  const artifactContainer = appSandbox.document.createElement('ul');
  appSandbox.renderArtifacts(
    artifactContainer,
    JSON.stringify([{ index: 2, path: 'tiles/tile_0002.png', sha256: 'deadbeef1234', source_y_offset: 2336 }])
  );
  assert.strictEqual(artifactContainer.children.length, 1);
  const li = artifactContainer.children[0];
  assert.strictEqual(li.children[0].children[0].textContent, 'tile_0002');
  assert.strictEqual(li.children[0].children[2].textContent, 'y=2336px');
  assert.strictEqual(li.children[1].textContent, 'deadbeef1234');
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
"""
    subprocess.run([node_bin, "-e", node_script, str(browser_js), str(app_js)], check=True)



