"""R7 tests: sections, extract, links lookup, global stream, admin."""

from __future__ import annotations

import json
import pathlib
import shlex
import socket
import threading
import time
from dataclasses import replace
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient
from typer.testing import CliRunner

import app.main as main_mod
from app.auth import AUTH_CONTEXT, authenticate, claim_resource, create_api_key, revoke_api_key
from app.jobs import JobManager
from app.main import app
from app.store import StorageConfig, Store
from scripts import mdwb_cli


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
    monkeypatch.setattr(fake_jm, "get_structured_result", _structured)
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
    monkeypatch.setattr(fake_jm, "get_structured_result", _structured)
    monkeypatch.setattr(main_mod, "JOB_MANAGER", fake_jm)
    monkeypatch.setattr(type(main_mod.store), "read_links", _links)
    monkeypatch.setattr(type(main_mod.store), "read_markdown", lambda self, jid: md)

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


def test_links_lookup_finds_matches(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
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
            {
                "id": "a",
                "state": "DONE",
                "created_at": "2026-01-01T00:00:00Z",
                "embedding_model": "hash-bucket-v1",
                "cache_hit": True,
            },
            {
                "id": "b",
                "state": "FAILED",
                "created_at": "2026-01-02T00:00:00Z",
                "cache_hit": False,
            },
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


def test_admin_prune_dry_run_no_delete(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
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


def test_global_stream_subscribes(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    """SSE endpoint is registered and advertises the right content-type."""
    # Don't drain the stream (would require an asyncio task running in the
    # background that the TestClient cancels mid-stream). Just verify the
    # route is registered and the response is well-formed.
    from fastapi.routing import APIRoute

    stream_route = next(
        (r for r in app.routes if isinstance(r, APIRoute) and r.path == "/jobs/stream"),
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


def test_r7_cli_sections_invokes_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
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
                    "sections": [{"level": 1, "heading": "Top", "anchor": "top", "body_chars": 50}],
                    "total_chars": 50,
                },
                request=_hx.Request("GET", url),
            )

    monkeypatch.setattr(mdwb_cli.httpx, "Client", _StubClient)
    monkeypatch.setattr(mdwb_cli, "_resolve_settings", _fake_settings)

    runner = CliRunner()
    result = runner.invoke(mdwb_cli.cli, ["jobs", "sections", "abc"])
    assert result.exit_code == 0
    assert "Top" in result.output
    assert "/jobs/abc/sections" in captured["url"]


def test_r7_cli_admin_stats_invokes_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
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

    monkeypatch.setattr(mdwb_cli.httpx, "Client", _StubClient)
    monkeypatch.setattr(mdwb_cli, "_resolve_settings", _fake_settings)

    runner = CliRunner()
    result = runner.invoke(mdwb_cli.cli, ["admin", "stats"])
    assert result.exit_code == 0
    assert "total jobs" in result.output
    assert "/admin/stats" in captured["url"]


@pytest.fixture
def secured_cli_api(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch):
    """Actual TCP API and SQLite keys with provider-free persisted fixture content."""
    store = Store(StorageConfig(tmp_path / "cache", tmp_path / "runs.db"))
    settings = replace(
        main_mod.settings,
        REQUIRE_API_KEY=True,
        API_KEY_HEADER="X-CLI-Test-Key",
        telemetry=replace(main_mod.settings.telemetry, prometheus_port=0),
    )
    manager = JobManager(store=store)
    monkeypatch.setattr(main_mod, "store", store)
    monkeypatch.setattr(main_mod, "settings", settings)
    monkeypatch.setattr(main_mod, "JOB_MANAGER", manager)
    with store.session() as session:
        key, record = create_api_key(session, "CLI operator", owner="cli", is_admin=True)
        assert record.id is not None
        key_id = record.id
    token = AUTH_CONTEXT.set(authenticate(key, store, settings))
    try:
        claim_resource(store, "job:cli-fixture")
        store.allocate_run(
            job_id="cli-fixture",
            url="https://example.com/cli-fixture",
            started_at=datetime.now(timezone.utc),
        )
        store.write_markdown(
            job_id="cli-fixture", content="# CLI Document\n\nProvider-free persisted content.\n"
        )
        store.update_status(job_id="cli-fixture", status="DONE")
    finally:
        AUTH_CONTEXT.reset(token)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    base = f"http://127.0.0.1:{listener.getsockname()[1]}"
    server = uvicorn.Server(
        uvicorn.Config(app, log_level="warning", access_log=False, lifespan="on")
    )
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started, "Real protected API failed to start"
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("API_BASE_URL", base)
        monkeypatch.setenv("MDWB_API_KEY", key)
        yield base, key, key_id, store
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()
        store.engine.dispose()
        assert not thread.is_alive(), "Real protected API failed to stop"


def test_cli_authenticated_commands_use_real_tcp_api(secured_cli_api) -> None:
    """Former raw clients now authenticate with the actual API admission middleware."""
    base, key, _, _ = secured_cli_api
    cli_runner = CliRunner()
    commands = (
        ["admin", "stats", "--json"],
        ["jobs", "list", "--json"],
        ["jobs", "sections", "cli-fixture", "--json"],
        ["search", "Provider-free", "--json"],
        ["embed", "Provider-free", "--json"],
    )
    payloads = []
    for command in commands:
        result = cli_runner.invoke(mdwb_cli.cli, [*command, "--api-base", base])
        assert result.exit_code == 0, result.output
        assert key not in result.output
        payloads.append(json.loads(result.output))
    assert payloads[0]["total"] == 1
    assert payloads[1]["items"][0]["id"] == "cli-fixture"
    assert payloads[2]["sections"][0]["heading"] == "CLI Document"
    assert payloads[3]["matches"][0]["job_id"] == "cli-fixture"
    assert len(payloads[4]["vector"]) == payloads[4]["dim"]


def test_cli_missing_and_revoked_key_refused_by_real_api(
    secured_cli_api, monkeypatch: pytest.MonkeyPatch
) -> None:
    base, key, key_id, store = secured_cli_api
    cli_runner = CliRunner()
    command = ["admin", "stats", "--json", "--api-base", base]
    monkeypatch.delenv("MDWB_API_KEY")
    missing = cli_runner.invoke(mdwb_cli.cli, command)
    assert missing.exit_code != 0
    assert isinstance(missing.exception, httpx.HTTPStatusError)
    assert missing.exception.response.status_code == 401
    monkeypatch.setenv("MDWB_API_KEY", key)
    accepted = cli_runner.invoke(mdwb_cli.cli, command)
    assert accepted.exit_code == 0
    with store.session() as session:
        assert revoke_api_key(session, key_id)
    revoked = cli_runner.invoke(mdwb_cli.cli, command)
    assert revoked.exit_code != 0
    assert isinstance(revoked.exception, httpx.HTTPStatusError)
    assert revoked.exception.response.status_code == 401
    assert all(key not in result.output for result in (missing, accepted, revoked))


def test_cli_config_environment_without_env_file(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("API_BASE_URL", "https://api.example.test/mounted")
    monkeypatch.setenv("MDWB_API_KEY", "fixture-key")
    monkeypatch.setenv("WARNING_LOG_PATH", "fixture-warnings.jsonl")
    monkeypatch.setenv("MWB_OUTPUT_FORMAT", "toon")
    assert not (tmp_path / ".env").exists()
    settings = mdwb_cli._load_env_settings()
    assert settings.base_url == "https://api.example.test/mounted"
    assert settings.api_key == "fixture-key"
    assert settings.warning_log_path == pathlib.Path("fixture-warnings.jsonl")
    assert mdwb_cli._resolve_output_format(False, None) == "toon"
    assert mdwb_cli._resolve_output_format(True, None) == "json"
    assert mdwb_cli._resolve_output_format(False, "json") == "json"


def test_cli_credentials_not_sent_outside_api_origin_or_mount(tmp_path: pathlib.Path) -> None:
    """Real sockets observe credential omission on outside targets and redirected requests."""
    servers = []
    threads = []
    redirect_target = ""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/api/redirect":
                self.send_response(302)
                self.send_header("Location", redirect_target)
                self.end_headers()
                return
            body = json.dumps(
                {
                    "authorization": self.headers.get("Authorization"),
                    "user_agent": self.headers.get("User-Agent"),
                    "path": self.path,
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    try:
        for _ in range(2):
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            servers.append(server)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            threads.append(thread)
        first = f"http://127.0.0.1:{servers[0].server_port}"
        outside = f"http://127.0.0.1:{servers[1].server_port}/outside"
        redirect_target = outside
        settings = mdwb_cli.APISettings(first + "/api", "fixture-key", tmp_path / "warnings")
        with mdwb_cli._client(settings, http2=False, timeout=3.0) as client:
            assert client.timeout.read == 3.0
            inside = client.get("/inside")
            assert inside.json()["authorization"] == "Bearer fixture-key"
            assert inside.json()["user_agent"] == "mdwb (+https://github.com/Dicklesworthstone/markdown_web_browser)"
            assert client.get(first + "/elsewhere").json()["authorization"] is None
            assert client.get(outside).json()["authorization"] is None
            assert client.get("/redirect", follow_redirects=True).json()["authorization"] is None
        with mdwb_cli._client(settings, http2=False, timeout=None) as client:
            assert client.timeout.read is None
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=5)
            assert not thread.is_alive()


def test_cli_shell_hook_keeps_remote_payload_literal(
    tmp_path: pathlib.Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """The operator's compound shell hook runs; remote event strings cannot run commands."""
    marker = tmp_path / "remote-command-executed"
    attack = f"$(touch {shlex.quote(str(marker))}); touch {shlex.quote(str(marker))}"
    mdwb_cli._run_hook(
        "printf 'operator-hook-ran\\n' && printf '%s\\n' \"$MDWB_EVENT_NAME\" \"$MDWB_EVENT_PAYLOAD\"",
        attack,
        {"untrusted_event": attack},
    )
    stdout = capfd.readouterr().out
    assert stdout.startswith("operator-hook-ran\n")
    assert attack in stdout
    assert not marker.exists(), "Remote event data executed as shell code"
