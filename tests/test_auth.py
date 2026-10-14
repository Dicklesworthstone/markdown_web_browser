"""Real SQLite/API admission and controlled browser/network boundary tests.

No OCR or production-network qualification is claimed by these fixtures.
"""

from __future__ import annotations

import asyncio
import hashlib
import base64
import socket
import secrets
from contextlib import asynccontextmanager, suppress
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from typing import AsyncGenerator

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from app import main
from app.auth import (
    APIKey,
    AUTH_CONTEXT,
    AuthContext,
    authenticate,
    claim_resource,
    create_api_key,
    owns_resource,
    revoke_api_key,
    verify_api_key,
)
from app.capture import _profile_storage_state_path
from app.jobs import JobManager
from app.rate_limit import consume_shared
from app.schemas import BatchJobRequest, CrawlRequest, JobCreateRequest
from app.store import StorageConfig, Store
from app.egress import (
    EgressDenied,
    EgressProxy,
    NetworkPolicy,
    network_policy,
    validate_network_url,
)


@pytest.fixture
def secured_api(tmp_path, monkeypatch):
    store = Store(StorageConfig(tmp_path / "cache", tmp_path / "runs.db"))
    settings = replace(
        main.settings,
        REQUIRE_API_KEY=True,
        API_KEY_HEADER="X-Research-Key",
        webhook_secret=secrets.token_urlsafe(32),
        telemetry=replace(main.settings.telemetry, prometheus_port=0),
    )
    manager = JobManager(store=store)

    async def wait_for_cancel(**kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(manager, "_run_job", wait_for_cancel)
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(main, "JOB_MANAGER", manager)
    monkeypatch.setattr(main, "settings", settings)
    with store.session() as session:
        alice, _ = create_api_key(session, "Alice", owner="alice", rate_limit=1000)
        bob, _ = create_api_key(session, "Bob", owner="bob", rate_limit=1000)
        admin, _ = create_api_key(session, "Ops", is_admin=True, rate_limit=1000)
    with TestClient(main.app) as client:
        yield client, store, manager, settings, alice, bob, admin
    store.engine.dispose()


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("GET", "/jobs", None),
        ("POST", "/jobs", {"url": "https://example.com"}),
        ("POST", "/jobs/crawl", {"url": "https://example.com"}),
        ("GET", "/jobs/private/artifacts", None),
        ("GET", "/jobs/private/result.md", None),
        ("POST", "/jobs/private/embeddings/store", {}),
        ("POST", "/jobs/private/webhooks", {}),
        ("GET", "/admin/stats", None),
        ("GET", "/metrics", None),
        ("POST", "/replay", {"manifest": {}}),
    ],
)
def test_missing_key_fails_before_admission(secured_api, method, path, body):
    client, store, manager, *_ = secured_api
    for headers in ({}, {"X-Research-Key": "mdwb_" + "z" * 32}):
        response = client.request(method, path, json=body, headers=headers)
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "ApiKey"
    assert manager._snapshots == {}
    assert store.list_runs()[1] == 0
    with store.session() as session:
        assert all(key.last_used_at is None for key in session.exec(select(APIKey)))


def test_key_roundtrip_revocation_and_admin_boundary(secured_api):
    client, store, _, settings, alice, _, admin = secured_api
    assert client.get("/jobs", headers={settings.API_KEY_HEADER: alice}).status_code == 200
    assert client.get("/jobs", headers={"Authorization": f"Bearer {alice}"}).status_code == 200
    assert client.get("/admin/stats", headers={settings.API_KEY_HEADER: alice}).status_code == 403
    assert client.get("/admin/stats", headers={settings.API_KEY_HEADER: admin}).status_code == 200
    with store.session() as session:
        record = verify_api_key(session, alice)
        assert record is not None and record.id is not None
        assert record.key_hash == hashlib.sha256(alice.encode()).hexdigest()
        assert alice not in record.model_dump_json()
        assert revoke_api_key(session, record.id)
        assert verify_api_key(session, alice) is None
    response = client.post(
        "/jobs", json={"url": "https://example.com"}, headers={settings.API_KEY_HEADER: alice}
    )
    assert response.status_code == 401
    assert alice not in response.text


def test_valid_job_admission_and_owner_isolation(secured_api):
    client, store, manager, settings, alice, bob, _ = secured_api
    headers = {settings.API_KEY_HEADER: alice}
    response = client.post(
        "/jobs", json={"url": "https://example.com", "profile_id": "research"}, headers=headers
    )
    assert response.status_code == 202
    job_id = response.json()["id"]
    assert job_id in manager._snapshots
    token = AUTH_CONTEXT.set(authenticate(alice, store, settings))
    try:
        paths = store.allocate_run(
            job_id=job_id,
            url="https://example.com",
            started_at=datetime.now(timezone.utc),
            profile_id="research",
        )
        paths.markdown_path.write_text("# Alice private result", encoding="utf-8")
        paths.dom_snapshot_path.write_text("<script>window.marker=1</script>", encoding="utf-8")
    finally:
        AUTH_CONTEXT.reset(token)
    assert client.get(f"/jobs/{job_id}/result.md", headers=headers).text == "# Alice private result"
    artifact = client.get(f"/jobs/{job_id}/artifact/artifact/dom.html", headers=headers)
    assert artifact.status_code == 200
    assert "sandbox" in artifact.headers["content-security-policy"]
    assert [row["id"] for row in client.get("/jobs", headers=headers).json()["items"]] == [job_id]
    bob_headers = {settings.API_KEY_HEADER: bob}
    assert client.get("/jobs", headers=bob_headers).json()["total"] == 0
    for suffix in ("", "/result.md", "/manifest.json", "/artifacts", "/stream", "/webhooks"):
        assert client.get(f"/jobs/{job_id}{suffix}", headers=bob_headers).status_code == 404
    assert client.post(f"/jobs/{job_id}/cancel", headers=bob_headers).status_code == 404
    assert (
        client.post("/jobs/search", headers=bob_headers, json={"query": "Alice"}).json()["matches"]
        == []
    )
    assert client.post(f"/jobs/{job_id}/cancel", headers=headers).status_code == 200


def test_browser_cookie_custom_header_csrf_and_revocation(secured_api):
    client, store, _, settings, alice, *_ = secured_api
    config = client.get("/auth/config").json()
    assert config == {"required": True, "header": "X-Research-Key"}
    response = client.post("/auth/session", headers={settings.API_KEY_HEADER: alice})
    assert response.status_code == 204
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=strict" in response.headers["set-cookie"]
    assert client.get("/jobs").status_code == 200
    assert client.post("/jobs", json={"url": "https://example.com"}).status_code == 403
    assert (
        client.post(
            "/jobs",
            json={"url": "https://example.com"},
            headers={"Origin": "https://attacker.example"},
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/jobs", json={"url": "https://example.com"}, headers={"Origin": "http://testserver"}
        ).status_code
        == 202
    )
    with store.session() as session:
        record = verify_api_key(session, alice)
        assert record is not None and record.id is not None
        revoke_api_key(session, record.id)
    assert client.get("/jobs").status_code == 401
    assert client.delete("/auth/session").status_code == 204
    assert "mdwb_session" not in client.cookies


def test_api_quota_returns_retry_after(secured_api):
    client, store, _, settings, *_ = secured_api
    with store.session() as session:
        key, _ = create_api_key(session, "Small quota", rate_limit=1)
    headers = {settings.API_KEY_HEADER: key}
    assert client.get("/jobs", headers=headers).status_code == 200
    response = client.get("/jobs", headers=headers)
    assert response.status_code == 429
    assert int(response.headers["retry-after"]) >= 1
    assert response.headers["x-ratelimit-remaining"] == "0"


def test_quota_atomic_across_connections(tmp_path):
    config = StorageConfig(tmp_path / "cache", tmp_path / "runs.db")
    first, second = Store(config), Store(config)
    with ThreadPoolExecutor(max_workers=8) as executor:
        outcomes = list(
            executor.map(
                lambda index: consume_shared(
                    (first if index % 2 else second).engine, "same-key", 5
                )[0],
                range(24),
            )
        )
    assert sum(outcomes) == 5
    assert consume_shared(second.engine, "other-key", 5)[0]
    first.engine.dispose()
    second.engine.dispose()


@pytest.mark.parametrize("profile", ["", ".", "..", "../other", "a/b", "a b", "a\\b", "x" * 129])
def test_profile_refusal_at_all_entry_points(tmp_path, profile):
    for model, payload in (
        (JobCreateRequest, {"url": "https://example.com"}),
        (BatchJobRequest, {"urls": ["https://example.com"]}),
        (CrawlRequest, {"url": "https://example.com"}),
    ):
        with pytest.raises(ValueError):
            model.model_validate({**payload, "profile_id": profile})
    with pytest.raises(ValueError):
        _profile_storage_state_path(tmp_path, profile)


def test_profile_namespaces_and_symlink_escape(secured_api, tmp_path):
    _, store, _, settings, alice, bob, _ = secured_api
    paths = []
    for key in (alice, bob):
        token = AUTH_CONTEXT.set(authenticate(key, store, settings))
        try:
            paths.append(_profile_storage_state_path(tmp_path / "profiles", "research"))
        finally:
            AUTH_CONTEXT.reset(token)
    assert paths[0] != paths[1]
    assert all(path.is_relative_to(tmp_path / "profiles") for path in paths)
    paths[0].parent.parent.mkdir(parents=True)
    paths[0].parent.symlink_to(tmp_path, target_is_directory=True)
    token = AUTH_CONTEXT.set(authenticate(alice, store, settings))
    try:
        with pytest.raises(ValueError, match="escapes"):
            _profile_storage_state_path(tmp_path / "profiles", "research")
    finally:
        AUTH_CONTEXT.reset(token)


def test_crawl_owner_grant_is_durable(secured_api):
    client, store, _, settings, alice, bob, _ = secured_api
    token = AUTH_CONTEXT.set(authenticate(alice, store, settings))
    try:
        claim_resource(store, "crawl:private-crawl")
    finally:
        AUTH_CONTEXT.reset(token)
    reopened = Store(store.config)
    try:
        alice_owner = authenticate(alice, reopened, settings).owner_key
        bob_owner = authenticate(bob, reopened, settings).owner_key
        assert alice_owner is not None and bob_owner is not None
        assert owns_resource(reopened, "crawl:private-crawl", alice_owner)
        assert not owns_resource(reopened, "crawl:private-crawl", bob_owner)
        assert not owns_resource(reopened, "crawl:missing", alice_owner)
    finally:
        reopened.engine.dispose()
    assert (
        client.get("/crawl/private-crawl", headers={settings.API_KEY_HEADER: bob}).status_code
        == 404
    )


async def test_event_replay_survives_two_cold_api_processes_and_observes_another_writer(tmp_path):
    """Actual cold TCP APIs and SQLite; capture/provider text is an explicit fixture."""
    import json
    import os
    import subprocess
    import sys
    import time
    import httpx
    from app.schemas import JobCreateRequest
    from tests.test_job_manager import _fake_runner

    store = Store(StorageConfig(tmp_path / "cache", tmp_path / "runs.db"))
    cfg = replace(main.settings, REQUIRE_API_KEY=True, API_KEY_HEADER="X-Research-Key")
    with store.session() as session:
        alice, _ = create_api_key(session, "Alice", owner="alice", rate_limit=1000)
        bob, _ = create_api_key(session, "Bob", owner="bob", rate_limit=1000)
    context = authenticate(alice, store, cfg)
    manager = JobManager(store=store, runner=_fake_runner)
    token = AUTH_CONTEXT.set(context)
    try:
        job = await manager.create_job(
            JobCreateRequest(url="https://example.com/cold-replay", reuse_cache=False)
        )
        await manager._tasks[job["id"]]
        store.write_markdown(job_id=job["id"], content="Invoice 7319: 42.")
        original = manager.get_events(job["id"])
    finally:
        AUTH_CONTEXT.reset(token)
    script = """import json, socket, sys, uvicorn
from pathlib import Path
from app import main
listener = socket.socket()
listener.bind(('127.0.0.1', 0))
listener.listen()
with Path(sys.argv[1]).open('x') as handle:
    json.dump({'port': listener.getsockname()[1]}, handle)
uvicorn.Server(uvicorn.Config(main.app, log_level='warning', lifespan='on')).run(sockets=[listener])
"""
    child_env = {
        **os.environ,
        "RUNS_DB_PATH": str(store.config.db_path),
        "CACHE_ROOT": str(store.config.cache_root),
        "REQUIRE_API_KEY": "true",
        "API_KEY_HEADER": "X-Research-Key",
        "PROMETHEUS_PORT": "0",
    }
    headers = {
        "X-Research-Key": alice,
        "User-Agent": "OpenAI File Downloader, XaiImageApiFetch/1.0",
    }
    cursor = original[1]["sequence"]
    expected = original[2:]
    try:
        for launch in range(2):
            receipt = tmp_path / f"api-port-{launch}.json"
            log = tmp_path / f"api-process-{launch}.log"
            with log.open("x") as output:
                process = subprocess.Popen(
                    [sys.executable, "-u", "-c", script, str(receipt)],
                    env=child_env,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                )
                try:
                    async with httpx.AsyncClient(
                        headers=headers, trust_env=False, timeout=5
                    ) as client:
                        deadline = time.monotonic() + 20
                        base = None
                        while time.monotonic() < deadline and process.poll() is None:
                            if receipt.exists():
                                base = f"http://127.0.0.1:{json.loads(receipt.read_text())['port']}"
                                try:
                                    response = await client.get(base + "/health")
                                    if response.status_code == 200:
                                        break
                                except httpx.HTTPError:
                                    pass
                            await asyncio.sleep(0.05)
                        else:
                            raise AssertionError("cold API failed to start: " + log.read_text())
                        assert base is not None
                        response = await client.get(
                            base + f"/jobs/{job['id']}/events.json", params={"after": cursor}
                        )
                        assert response.status_code == 200, response.text
                        assert response.json()["events"] == expected
                        foreign = await client.get(
                            base + f"/jobs/{job['id']}/events.json", headers={"X-Research-Key": bob}
                        )
                        assert foreign.status_code == 404
                        async with client.stream(
                            "GET",
                            base + "/jobs/stream",
                            headers={"Last-Event-ID": original[1 if launch == 0 else -1]["cursor"]},
                        ) as response:
                            assert response.status_code == 200
                            replay = []
                            async for line in response.aiter_lines():
                                if line.startswith("data: "):
                                    replay.append(json.loads(line[6:]))
                                    if len(replay) == len(expected):
                                        break
                            assert replay == expected
                        if launch == 0:
                            async with client.stream("GET", base + "/jobs/stream") as response:
                                lines = response.aiter_lines()
                                async for line in lines:
                                    if line.startswith("data: "):
                                        assert json.loads(line[6:])["event"] == "subscribed"
                                        break
                                manager._record_custom_event(job["id"], "probe", {"invoice": 8831})
                                await manager.flush_events()
                                async for line in lines:
                                    if line.startswith("data: "):
                                        update = json.loads(line[6:])
                                        break
                                else:
                                    raise AssertionError(
                                        "cold API missed the other writer's committed event"
                                    )
                                assert update == manager.get_events(job["id"])[-1]
                                assert update["data"] == {"invoice": 8831}
                            cursor = original[-1]["sequence"]
                            expected = [update]
                finally:
                    process.terminate()
                    await asyncio.to_thread(process.wait, 10)
                    assert process.poll() is not None, "owned API process leaked"
    finally:
        await manager.stop_watchdog()
        store.engine.dispose()


@pytest.mark.parametrize(
    "reader,endpoint",
    [
        ("read_job_events", "events.json"),
        ("recover_terminal_event", "events"),
        ("read_global_events", None),
    ],
)
def test_event_journal_read_failure_is_a_typed_503(secured_api, monkeypatch, reader, endpoint):
    client, store, manager, settings, alice, _, _ = secured_api
    headers = {settings.API_KEY_HEADER: alice}
    job = client.post(
        "/jobs",
        headers=headers,
        json={"url": "https://example.com/journal-failure", "reuse_cache": False},
    )
    assert job.status_code == 202
    if reader == "recover_terminal_event":

        async def retire_admission_fixture():
            task = manager._tasks.pop(job.json()["id"])
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        assert client.portal is not None
        client.portal.call(retire_admission_fixture)

    def fails(*args, **kwargs):
        raise OSError("controlled journal read failure")

    monkeypatch.setattr(store, reader, fails)
    url = f"/jobs/{job.json()['id']}/{endpoint}" if endpoint else "/jobs/stream"
    response = client.get(url, headers=headers)
    assert response.status_code == 503
    assert response.json() == {"detail": {"code": "event-journal-unavailable"}}


def test_unknown_event_routes_are_404_in_anonymous_operator_mode(secured_api, monkeypatch):
    client, _, _, cfg, _, _, _ = secured_api
    monkeypatch.setattr(main, "settings", replace(cfg, REQUIRE_API_KEY=False))
    for suffix in ("", "/raw", "/events.json", "/events", "/stream"):
        response = client.get("/jobs/missing-operator-job" + suffix)
        assert response.status_code == 404


def test_authenticated_event_cursors_preserve_first_entry_and_refuse_gaps(secured_api):
    client, _, manager, settings, alice, bob, _ = secured_api
    headers = {settings.API_KEY_HEADER: alice}
    result = client.post(
        "/jobs", headers=headers, json={"url": "https://example.com/events", "reuse_cache": False}
    )
    assert result.status_code == 202
    job_id = result.json()["id"]
    all_events = client.get(f"/jobs/{job_id}/events.json", headers=headers)
    assert all_events.status_code == 200
    original = all_events.json()["events"]
    assert original and original[0]["sequence"] == 0
    cursor = original[-1]["sequence"]

    async def emit(count):
        for index in range(count):
            manager._record_custom_event(
                job_id, "probe", {"value": "next" if count == 1 else index}
            )
        await manager.flush_events()

    assert client.portal is not None
    client.portal.call(emit, 1)
    resumed = client.get(f"/jobs/{job_id}/events.json?after={cursor}", headers=headers)
    assert resumed.status_code == 200
    assert resumed.json()["count"] == 1
    entry = resumed.json()["events"][0]
    assert entry["sequence"] == cursor + 1 and entry["data"] == {"value": "next"}
    assert (
        client.get(
            f"/jobs/{job_id}/events.json", headers={settings.API_KEY_HEADER: bob}
        ).status_code
        == 404
    )
    assert client.get("/jobs/missing/events.json", headers=headers).status_code == 404
    assert client.get(f"/jobs/{job_id}/events?after=-2", headers=headers).status_code == 400
    assert client.get(f"/jobs/{job_id}/stream?after=-2", headers=headers).status_code == 400
    assert (
        client.get(
            f"/jobs/{job_id}/stream", headers={**headers, "Last-Event-ID": "invalid"}
        ).status_code
        == 400
    )
    assert client.get("/jobs/missing/stream", headers=headers).status_code == 404
    assert (
        client.get(
            f"/jobs/{job_id}/stream", headers={**headers, "Last-Event-ID": "9" * 5000}
        ).status_code
        == 400
    )
    assert client.get("/jobs/stream?after=invalid", headers=headers).status_code == 400
    client.portal.call(emit, 650)
    for endpoint in ("events", "events.json", "stream"):
        lost = client.get(f"/jobs/{job_id}/{endpoint}?after={cursor}", headers=headers)
        assert lost.status_code == 409
        detail = lost.json()["detail"]
        assert detail["code"] == "event-history-gap" and detail["after"] == cursor
        assert detail["first_available"] == cursor + 152
        assert detail["last_available"] == cursor + 651
        assert detail["snapshot_url"] == f"/jobs/{job_id}"
    assert manager._event_subscribers == {}


def test_public_shell_and_health_require_no_key(secured_api):
    client, *_ = secured_api
    for path in ("/", "/browser", "/health", "/health/live", "/static/client.js"):
        assert client.get(path).status_code == 200


def test_share_is_redacted_and_binary_signature_is_unambiguous(secured_api):
    client, _, manager, settings, alice, bob, _ = secured_api
    headers = {settings.API_KEY_HEADER: alice}
    result = client.post(
        "/jobs",
        headers=headers,
        json={
            "url": "https://name:secret@example.com/page?token=private#private",
            "profile_id": "private-profile",
            "tags": ["private-tag"],
        },
    )
    assert result.status_code == 202
    job_id = result.json()["id"]
    manager._snapshots[job_id]["manifest"] = {
        "api_key": alice,
        "profile_id": "private-profile",
        "artifact_root": "/private",
        "tiles_total": 3,
        "selected_ocr_model": "olmOCR-2-7B-1025-FP8",
    }
    assert (
        client.post(f"/jobs/{job_id}/share", headers={settings.API_KEY_HEADER: bob}).status_code
        == 404
    )
    found_binary_dot = False
    for ttl in range(60, 572):
        shared = client.post(f"/jobs/{job_id}/share", headers=headers, params={"ttl_seconds": ttl})
        assert shared.status_code == 200
        token = shared.json()["token"]
        decoded = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        if b"." not in decoded[-32:]:
            continue
        found_binary_dot = True
        public = client.get(shared.json()["share_url"])
        assert public.status_code == 200
        body = public.json()
        assert body["url"] == "https://example.com/page"
        assert body["profile_id"] is None and body["tags"] == []
        assert body["manifest"] == {"tiles_total": 3, "selected_ocr_model": "olmOCR-2-7B-1025-FP8"}
        for secret in (
            alice,
            "private-profile",
            "private-tag",
            "token=private",
            "name:secret",
            "/private",
        ):
            assert secret not in public.text
        tampered = decoded[:-1] + bytes([decoded[-1] ^ 1])
        tampered_token = base64.urlsafe_b64encode(tampered).decode().rstrip("=")
        assert (
            base64.urlsafe_b64decode(tampered_token + "=" * (-len(tampered_token) % 4)) != decoded
        )
        assert client.get(f"/jobs/share/{tampered_token}").status_code == 404
        break
    assert found_binary_dot, "No binary-delimiter regression control was generated"


@asynccontextmanager
async def _controlled_http(
    *,
    host: str = "127.0.0.1",
    body: bytes = b"Known controlled response7319",
    content_type: str = "text/plain",
    redirect: str | None = None,
    port: int = 0,
) -> AsyncGenerator[tuple[str, list[dict]], None]:
    received: list[dict] = []

    async def handle(reader, writer):
        try:
            async with asyncio.timeout(5):
                head = (await reader.readuntil(b"\r\n\r\n")).decode("ascii")
                lines = head.split("\r\n")
                headers = dict(line.split(":", 1) for line in lines[1:] if line)
                headers = {name.lower(): value.strip() for name, value in headers.items()}
                payload = await reader.readexactly(int(headers.get("content-length", "0")))
                received.append({"request": lines[0], "headers": headers, "body": payload})
                if headers.get("upgrade", "").lower() == "websocket":
                    accept = base64.b64encode(
                        hashlib.sha1(
                            (
                                headers["sec-websocket-key"]
                                + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
                            ).encode()
                        ).digest()
                    )
                    writer.write(
                        b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: "
                        + accept
                        + b"\r\n\r\n\x81\x06WS7319"
                    )
                    await writer.drain()
                    await reader.read(256)
                else:
                    status = "302 Found" if redirect else "200 OK"
                    location = f"Location: {redirect}\r\n" if redirect else ""
                    writer.write(
                        (
                            f"HTTP/1.1 {status}\r\n{location}Content-Type: {content_type}\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n"
                        ).encode()
                        + body
                    )
                    await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handle, host, port)
    port = server.sockets[0].getsockname()[1]
    address = f"[{host}]" if ":" in host else host
    async with server:
        yield f"http://{address}:{port}", received


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.1",
        "169.254.169.254",
        "168.63.129.16",
        "100.100.100.200",
        "::1",
        "fd00:ec2::254",
        "fe80::1",
        "::ffff:127.0.0.1",
        "64:ff9b::7f00:1",
        "2002:7f00:1::",
        "0.0.0.0",
        "224.0.0.1",
        "fe80::1%eth0",
    ],
)
def test_public_policy_refuses_nonpublic_and_metadata(address):
    assert not NetworkPolicy().allows(address)
    if address not in {"127.0.0.1", "10.0.0.1", "::1"}:
        assert not NetworkPolicy(("127.0.0.0/8", "10.0.0.0/8", "169.254.0.0/16", "::/0")).allows(
            address
        )


def test_server_grants_are_bound_to_owners_and_cache_identity(operator_network_settings):
    from app import jobs

    settings = replace(
        operator_network_settings, REQUIRE_API_KEY=True, NETWORK_TRUSTED_OWNERS=("owner:alice",)
    )
    assert NetworkPolicy().allows("1.1.1.1") and NetworkPolicy().allows("2606:4700:4700::1111")
    assert not network_policy(settings, owner=None).allows("127.0.0.1")
    assert not network_policy(settings, owner="owner:bob").allows("127.0.0.1")
    assert network_policy(settings, owner="owner:alice").allows("127.0.0.1")
    assert not network_policy(settings, owner="owner:alice").allows("127.0.0.2")
    config = jobs._build_capture_config(JobCreateRequest(url="https://example.com"), settings)
    token = AUTH_CONTEXT.set(AuthContext(1, "Alice", "prefix", None, "alice"))
    try:
        approved = jobs._build_cache_seed(config=config, settings=settings)
        withdrawn = jobs._build_cache_seed(
            config=config, settings=replace(settings, NETWORK_TRUSTED_OWNERS=())
        )
        assert approved != withdrawn
    finally:
        AUTH_CONTEXT.reset(token)


@pytest.mark.parametrize(
    "url",
    [
        "https://name:secret@example.com/",
        "file:///private",
        "http://[fe80::1%eth0]/",
        "https://example.com\n/",
    ],
)
def test_network_url_rejects_credentials_and_non_http_inputs(url):
    with pytest.raises(EgressDenied):
        validate_network_url(url)


async def test_proxy_denies_actual_private_http_and_connect_before_connection():
    import httpx

    async with _controlled_http() as (url, received), EgressProxy(NetworkPolicy()) as proxy:
        async with httpx.AsyncClient(proxy=proxy.url, trust_env=False, timeout=5) as client:
            assert (await client.get(url)).status_code == 403
            with pytest.raises(httpx.ProxyError):
                await client.get(url.replace("http:", "https:"))
        assert received == [] and proxy.opened == 0
        assert sum(proxy.denials.values()) == 2
    assert proxy.tasks == set()


async def test_proxy_uses_one_dns_answer_set_and_pins_actual_peer(monkeypatch):
    import httpx

    loop = asyncio.get_running_loop()
    original = loop.getaddrinfo
    resolutions = []
    async with _controlled_http() as (url, received):
        port = int(url.rsplit(":", 1)[1])
        async with _controlled_http(
            host="127.0.0.2", port=port, body=b"Rebound destination8831"
        ) as (_, wrong):

            async def rebound(host, target_port, **options):
                if host == "fixture.test":
                    resolutions.append(host)
                    selected = "127.0.0.1" if len(resolutions) == 1 else "127.0.0.2"
                    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (selected, target_port))]
                return await original(host, target_port, **options)

            monkeypatch.setattr(loop, "getaddrinfo", rebound)
            async with EgressProxy(NetworkPolicy(("127.0.0.1/32",))) as proxy:
                async with httpx.AsyncClient(proxy=proxy.url, trust_env=False, timeout=5) as client:
                    response = await client.get(f"http://fixture.test:{port}/")
                    assert response.content == b"Known controlled response7319"
                assert proxy.opened == 1 and resolutions == ["fixture.test"]
            assert len(received) == 1 and wrong == []


async def test_proxy_refuses_mixed_dns_answers_before_admission(monkeypatch):
    import httpx

    loop = asyncio.get_running_loop()
    original = loop.getaddrinfo
    async with _controlled_http() as (url, received):
        port = int(url.rsplit(":", 1)[1])

        async def mixed(host, target_port, **options):
            if host == "fixture.test":
                return [
                    (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, target_port))
                    for ip in ("127.0.0.1", "1.1.1.1", "127.0.0.2")
                ]
            return await original(host, target_port, **options)

        monkeypatch.setattr(loop, "getaddrinfo", mixed)
        async with EgressProxy(NetworkPolicy(("127.0.0.1/32",))) as proxy:
            async with httpx.AsyncClient(proxy=proxy.url, trust_env=False, timeout=5) as client:
                assert (await client.get(f"http://fixture.test:{port}/")).status_code == 403
            assert proxy.opened == 0 and received == []


async def test_browser_proxy_controls_redirect_subresources_and_websockets():
    from playwright.async_api import async_playwright
    from app.capture import _launch_browser
    from app.settings import get_settings

    async with _controlled_http(host="127.0.0.2") as (blocked, received):
        html = f"""<body>Known browser network fixture7319<script>
        window.denied = [];
        fetch('{blocked}/fetch').then(r => denied.push(r.ok ? 'escaped' : 'blocked')).catch(() => denied.push('blocked'));
        const socket = new WebSocket('{blocked.replace("http:", "ws:")}/socket');
        socket.onopen = () => denied.push('escaped'); socket.onerror = () => denied.push('blocked');
        const image = new Image(); image.src = '{blocked}/image'; document.body.append(image);
        </script></body>""".encode()
        async with (
            _controlled_http(body=html, content_type="text/html") as (allowed, positive),
            EgressProxy(NetworkPolicy(("127.0.0.1/32",))) as proxy,
            async_playwright() as playwright,
        ):
            settings = get_settings().browser
            browser, channel, transport = await _launch_browser(
                playwright,
                "cft",
                expected_build=settings.cft_version,
                executable=settings.cft_executable,
                fontconfig_file=settings.fontconfig_file,
                proxy_url=proxy.url,
            )
            try:
                page = await browser.new_page()
                await page.goto(allowed)
                content = await page.text_content("body")
                assert content is not None and "fixture7319" in content
                await page.wait_for_function("denied.length === 2")
                assert await page.evaluate("denied") == ["blocked", "blocked"]
                ws = allowed.replace("http:", "ws:") + "/positive-socket"
                assert (
                    await page.evaluate(
                        """url => new Promise(resolve => {const s = new WebSocket(url); s.onmessage = e => {s.close(); resolve(e.data)}; s.onerror = () => resolve('failed')})""",
                        ws,
                    )
                    == "WS7319"
                )
                async with _controlled_http(redirect=blocked) as (redirect, hits):
                    result = await page.goto(redirect)
                    assert result is not None and result.status == 403
                    assert len(hits) == 1
                assert len(positive) >= 2 and received == []
                assert sum(proxy.denials.values()) >= 4
                assert channel == "cft" and transport == "cdp"
            finally:
                await browser.close()


async def test_actual_signed_webhook_obeys_job_owner_and_preserves_signature(
    monkeypatch, operator_network_settings
):
    import hmac
    from app import jobs

    settings = replace(
        operator_network_settings, REQUIRE_API_KEY=True, NETWORK_TRUSTED_OWNERS=("owner:alice",)
    )
    monkeypatch.setattr(jobs, "global_settings", settings)
    sender = jobs.build_signed_webhook_sender("controlled-signing-secret")
    async with _controlled_http() as (url, received):
        for owner in ("bob", "alice"):
            token = AUTH_CONTEXT.set(AuthContext(1, owner, "prefix", None, owner))
            try:
                await sender(url, {"marker": 7319})
            finally:
                AUTH_CONTEXT.reset(token)
        assert len(received) == 1
        request = received[0]
        assert request["body"] == b'{"marker":7319}'
        assert hmac.compare_digest(
            request["headers"]["x-mdwb-signature"],
            "v1="
            + hmac.new(b"controlled-signing-secret", request["body"], hashlib.sha256).hexdigest(),
        )
        assert "authorization" not in request["headers"]
        assert "x-api-key" not in request["headers"]


async def test_proxy_controls_actual_ipv6_and_mapped_destinations():
    from urllib.parse import urlsplit

    async def get(proxy, url):
        address = urlsplit(proxy.url)
        reader, writer = await asyncio.open_connection(address.hostname, address.port)
        try:
            authority = urlsplit(url).netloc
            writer.write(
                f"GET {url}/ HTTP/1.1\r\nHost: {authority}\r\nConnection: close\r\n\r\n".encode()
            )
            await writer.drain()
            return await asyncio.wait_for(reader.read(), 5)
        finally:
            writer.close()
            await writer.wait_closed()

    async with _controlled_http(host="::1") as (url, received):
        for policy, status in ((NetworkPolicy(), 403), (NetworkPolicy(("::1/128",)), 200)):
            async with EgressProxy(policy) as proxy:
                response = await get(proxy, url)
                assert response.startswith(f"HTTP/1.1 {status}".encode())
                if status == 200:
                    assert b"Known controlled response7319" in response
                assert proxy.opened == (1 if status == 200 else 0)
        assert len(received) == 1
    async with (
        _controlled_http() as (url, received),
        EgressProxy(NetworkPolicy(("127.0.0.1/32", "::/0"))) as proxy,
    ):
        port = url.rsplit(":", 1)[1]
        assert (await get(proxy, f"http://[::ffff:127.0.0.1]:{port}")).startswith(b"HTTP/1.1 403")
        assert received == [] and proxy.opened == 0


@pytest.mark.parametrize("connections", [1, 10])
async def test_proxy_shutdown_closes_pending_connections_before_wait_closed(connections):
    from urllib.parse import urlsplit

    writers = []
    readers = []
    try:
        async with asyncio.timeout(2):
            async with EgressProxy(NetworkPolicy()) as proxy:
                address = urlsplit(proxy.url)
                for _ in range(connections):
                    reader, writer = await asyncio.open_connection(address.hostname, address.port)
                    writers.append(writer)
                    readers.append(reader)
                    writer.write(b"GET ")  # Keep each header read pending.
                    await writer.drain()
                assert len(proxy.tasks) == connections
            assert proxy.tasks == set()
            for reader in readers:
                # Closing an unread partial request may produce TCP RST or
                # EOF. Both terminate the transport; neither permits a timeout.
                with suppress(ConnectionResetError):
                    assert await reader.read() == b""
    finally:
        for writer in writers:
            writer.close()
            with suppress(ConnectionResetError):
                await writer.wait_closed()


async def test_proxy_closes_an_accept_callback_delivered_during_exit(monkeypatch):
    from urllib.parse import urlsplit

    proxy = EgressProxy(NetworkPolicy())
    accepted = []
    normal_accept = proxy._accept
    monkeypatch.setattr(proxy, "_accept", lambda reader, writer: accepted.append((reader, writer)))
    writer = None
    try:
        async with asyncio.timeout(2):
            async with proxy:
                address = urlsplit(proxy.url)
                reader, writer = await asyncio.open_connection(address.hostname, address.port)
                assert len(accepted) == 1
                # Model an already accepted transport whose connection callback
                # becomes runnable after close() and the handler snapshot.
                asyncio.get_running_loop().call_soon(normal_accept, *accepted[0])
            assert proxy.tasks == set()
            assert await reader.read() == b""
    finally:
        if writer is not None:
            writer.close()
            await writer.wait_closed()


async def test_webhook_uses_recorded_owner_and_refuses_redirect_credentials(
    tmp_path, monkeypatch, operator_network_settings
):
    from app import jobs

    settings = replace(
        operator_network_settings, REQUIRE_API_KEY=True, NETWORK_TRUSTED_OWNERS=("owner:alice",)
    )
    monkeypatch.setattr(jobs, "global_settings", settings)
    store = Store(StorageConfig(tmp_path / "cache", tmp_path / "runs.db"))
    manager = JobManager(store=store)
    manager._job_owners["controlled-job"] = "owner:alice"
    from app.auth import ResourceOwner

    with store.session() as session:
        session.add(ResourceOwner(resource="job:controlled-job", owner_key="owner:alice"))
        session.commit()
    # The delivery runs without an API request context, as a watchdog would.
    token = AUTH_CONTEXT.set(None)
    try:
        async with (
            _controlled_http(host="127.0.0.2") as (blocked, wrong),
            _controlled_http(redirect=blocked) as (url, received),
        ):
            sender = jobs.build_signed_webhook_sender(secrets.token_urlsafe(32))
            await manager._deliver_webhook(sender, "controlled-job", url, {"marker": 7319})
            assert len(received) == 1 and wrong == []
            event = manager._event_logs["controlled-job"][-1]
            assert event["event"] == "webhook_network_policy"
            assert event["data"]["mode"] == "public-plus-server-grants"
            assert "127.0.0.1" not in str(event)
        async with _controlled_http() as (url, received):
            await manager._deliver_webhook(
                jobs._default_webhook_sender, "controlled-job", url, {"marker": 7319}
            )
            assert len(received) == 1 and "x-mdwb-signature" not in received[0]["headers"]
            secret_url = url.replace("http://", "http://name:secret@")
            await manager._deliver_webhook(sender, "controlled-job", secret_url, {"marker": 7319})
            assert len(received) == 1
    finally:
        AUTH_CONTEXT.reset(token)
        store.engine.dispose()


async def test_capture_refuses_private_navigation_before_tiling(monkeypatch):
    from app import capture
    from app.settings import get_settings

    monkeypatch.setattr(
        capture,
        "get_settings",
        lambda: replace(
            get_settings(), NETWORK_PRIVATE_CIDRS=(), NETWORK_TRUST_LOCAL_OPERATOR=False
        ),
    )
    async with _controlled_http() as (url, received):
        with pytest.raises(EgressDenied, match="network policy"):
            await capture.capture_tiles(capture.CaptureConfig(url=url))
        assert received == []


async def test_pinned_webhook_supports_actual_ipv6_without_proxy_authority_rewriting(
    monkeypatch, operator_network_settings
):
    from app import jobs

    settings = replace(
        operator_network_settings,
        REQUIRE_API_KEY=True,
        NETWORK_PRIVATE_CIDRS=("::1/128",),
        NETWORK_TRUSTED_OWNERS=("owner:alice",),
    )
    monkeypatch.setattr(jobs, "global_settings", settings)
    async with _controlled_http(host="::1") as (url, received):
        for owner, count in (("bob", 0), ("alice", 1)):
            token = AUTH_CONTEXT.set(AuthContext(1, owner, "prefix", None, owner))
            try:
                await jobs._default_webhook_sender(url, {"marker": 7319})
            finally:
                AUTH_CONTEXT.reset(token)
            assert len(received) == count
        assert received[0]["body"] == b'{"marker":7319}'
        assert received[0]["headers"]["host"].startswith("[::1]:")


@pytest.mark.parametrize("mixed", [False, True])
async def test_webhook_backend_pins_dns_and_refuses_mixed_answers(monkeypatch, mixed):
    import httpcore
    from app.egress import PinnedNetworkBackend

    loop = asyncio.get_running_loop()
    original = loop.getaddrinfo
    resolutions = []
    async with _controlled_http() as (url, received):
        port = int(url.rsplit(":", 1)[1])
        async with _controlled_http(
            host="127.0.0.2", port=port, body=b"Rebound destination8831"
        ) as (_, wrong):

            async def controlled_dns(host, target_port, **options):
                if host == "fixture.test":
                    resolutions.append(host)
                    ips = (
                        ["127.0.0.1", "1.1.1.1", "127.0.0.2"]
                        if mixed
                        else ["127.0.0.1" if len(resolutions) == 1 else "127.0.0.2"]
                    )
                    return [
                        (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, target_port)) for ip in ips
                    ]
                return await original(host, target_port, **options)

            monkeypatch.setattr(loop, "getaddrinfo", controlled_dns)
            backend = PinnedNetworkBackend(NetworkPolicy(("127.0.0.1/32",)))
            async with httpcore.AsyncConnectionPool(network_backend=backend) as client:
                if mixed:
                    with pytest.raises(EgressDenied, match="without-server-grant"):
                        await client.request("GET", f"http://fixture.test:{port}/")
                    assert received == [] and backend.opened == 0
                else:
                    response = await client.request("GET", f"http://fixture.test:{port}/")
                    assert response.content == b"Known controlled response7319"
                    assert len(received) == 1 and backend.opened == 1
                    assert received[0]["headers"]["host"] == f"fixture.test:{port}"
            assert resolutions == ["fixture.test"] and wrong == []
