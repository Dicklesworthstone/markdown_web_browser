from __future__ import annotations

import json
import asyncio
import os
import re
import shlex
import subprocess
import sys
from dataclasses import replace
from io import StringIO
from pathlib import Path

import httpx
import pytest
import typer
from rich.console import Console
from scripts import mdwb_cli
from scripts import manage_api_keys, test_integration_advanced, test_integration_full
from scripts.agents import shared
from scripts.agents import summarize_article as summarize_article_module
from scripts.agents import generate_todos as generate_todos_module
from app.auth import verify_api_key
from app.settings import get_settings
from app.store import StorageConfig, Store


SAMPLE_MD = """
# Example Article

Welcome to the **Markdown Web Browser** demo. We focus on deterministic captures.
Second sentence explains why reproducibility matters. Third sentence adds more context!

## Next Steps

- [ ] Wire nightly smoke results into dashboards.
- [x] Add SKIP_LIBVIPS_CHECK flag to run_checks.
- Prioritize TODO: add agent starter scripts.
- Note: Validate CfT label/build in manifests.

### Action Items

1. Update docs/ops.
2. Share sample scripts with other agents.
"""


def test_summarize_markdown_truncates_to_sentences():
    summary = shared.summarize_markdown(SAMPLE_MD, sentences=2)
    assert "Markdown Web Browser demo." in summary
    assert "We focus on deterministic captures." in summary
    assert "Second sentence explains why reproducibility matters." not in summary
    assert "Third sentence adds more context" not in summary


def test_extract_todos_prefers_checkboxes_and_heading_context():
    todos = shared.extract_todos(SAMPLE_MD, max_tasks=8)
    assert todos[0].startswith("Wire nightly smoke")
    assert any(task.startswith("Prioritize TODO") for task in todos)
    assert any("Validate CfT" in task for task in todos)
    assert any("Update docs/ops" in task for task in todos)


def _mock_capture(monkeypatch, markdown=SAMPLE_MD):
    capture = shared.CaptureResult(job_id="job-xyz", snapshot={}, markdown=markdown)

    def fake_capture(**kwargs):  # noqa: ANN001
        return capture

    for module in (shared, summarize_article_module.shared, generate_todos_module.shared):
        monkeypatch.setattr(module, "capture_markdown", fake_capture)
        monkeypatch.setattr(module, "resolve_settings", lambda api_base: object())


def test_summarize_article_cli_writes_out(monkeypatch, tmp_path: Path):
    _mock_capture(monkeypatch)
    out_path = tmp_path / "summary.txt"

    summarize_article_module.summarize(
        url="https://example.com",
        job_id="",
        api_base=None,
        profile=None,
        ocr_policy=None,
        sentences=2,
        http2=True,
        poll_interval=2.0,
        timeout=300.0,
        out=out_path,
    )
    expected = shared.summarize_markdown(SAMPLE_MD, sentences=2)
    assert out_path.read_text(encoding="utf-8") == expected


def test_generate_todos_cli_writes_text(monkeypatch, tmp_path: Path):
    _mock_capture(monkeypatch)
    out_path = tmp_path / "todos.txt"

    generate_todos_module.todos(
        url="https://example.com",
        job_id="",
        api_base=None,
        profile=None,
        ocr_policy=None,
        limit=5,
        json_output=False,
        http2=True,
        poll_interval=2.0,
        timeout=300.0,
        out=out_path,
    )
    expected = "\n".join(shared.extract_todos(SAMPLE_MD, max_tasks=5))
    assert out_path.read_text(encoding="utf-8") == expected


def test_generate_todos_cli_writes_json(monkeypatch, tmp_path: Path):
    _mock_capture(monkeypatch)
    out_path = tmp_path / "todos.json"

    generate_todos_module.todos(
        url="https://example.com",
        job_id="",
        api_base=None,
        profile=None,
        ocr_policy=None,
        limit=8,
        json_output=True,
        http2=True,
        poll_interval=2.0,
        timeout=300.0,
        out=out_path,
    )
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert payload["job_id"] == "job-xyz"
    assert payload["todos"] == shared.extract_todos(SAMPLE_MD)


def test_capture_markdown_validates_missing_job_id(monkeypatch):
    settings = mdwb_cli.APISettings(
        base_url="http://localhost", api_key=None, warning_log_path=Path("ops/warnings.jsonl")
    )

    def fake_submit_job(**kwargs):  # noqa: ANN001
        return {"state": "BROWSER_STARTING"}

    monkeypatch.setattr(shared, "submit_job", fake_submit_job)
    with pytest.raises(RuntimeError, match="job id"):
        shared.capture_markdown(
            url="https://example.com",
            job_id=None,
            settings=settings,
            http2=True,
            profile=None,
            ocr_policy=None,
        )


def test_capture_markdown_requires_url_or_job_id():
    settings = mdwb_cli.APISettings(
        base_url="http://localhost", api_key=None, warning_log_path=Path("ops/warnings.jsonl")
    )
    with pytest.raises(typer.BadParameter):
        shared.capture_markdown(
            url=None,
            job_id=None,
            settings=settings,
            http2=True,
            profile=None,
            ocr_policy=None,
        )


def test_capture_markdown_raises_on_non_done_state(monkeypatch):
    settings = mdwb_cli.APISettings(
        base_url="http://localhost", api_key=None, warning_log_path=Path("ops/warnings.jsonl")
    )

    monkeypatch.setattr(shared, "submit_job", lambda **kwargs: {"id": "job-1"})
    monkeypatch.setattr(
        shared,
        "wait_for_completion",
        lambda *args, **kwargs: {"id": "job-1", "state": "FAILED", "manifest": {"error": "boom"}},
    )
    monkeypatch.setattr(shared, "fetch_markdown", lambda *args, **kwargs: "")

    with pytest.raises(RuntimeError, match="FAILED"):
        shared.capture_markdown(
            url="https://example.com",
            job_id=None,
            settings=settings,
            http2=True,
            profile=None,
            ocr_policy=None,
        )


def test_generate_todos_cli_handles_empty_output(monkeypatch, tmp_path: Path):
    _mock_capture(monkeypatch, markdown="")
    out_path = tmp_path / "todos.txt"

    generate_todos_module.todos(
        url="https://example.com",
        job_id="",
        api_base=None,
        profile=None,
        ocr_policy=None,
        limit=3,
        json_output=False,
        http2=True,
        poll_interval=2.0,
        timeout=300.0,
        out=out_path,
    )

    assert out_path.read_text(encoding="utf-8") == ""


def _configure_integration_fixture(monkeypatch, handler):
    """Exercise HTTP harness contracts against fixture responses, without an OCR claim."""
    settings = replace(get_settings(), API_KEY_HEADER="X-Research-Key")
    for module in (test_integration_full, test_integration_advanced):
        monkeypatch.setattr(module, "get_settings", lambda: settings)
        monkeypatch.setattr(
            module,
            "load_config",
            lambda env_path: lambda key, default=None: "fixture-secret"
            if key == "MDWB_API_KEY"
            else default,
        )
    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: real_client(transport=transport, **kwargs)
    )


@pytest.mark.parametrize(
    "missing,empty_markdown,invalid_manifest,invalid_links,expected",
    [
        (None, False, False, False, True),
        ("result.md", False, False, False, False),
        ("links.json", False, False, False, False),
        ("manifest.json", False, False, False, False),
        (None, True, False, False, False),
        (None, False, True, False, False),
        (None, False, "empty", False, False),
        (None, False, False, True, False),
        (None, False, False, "empty", False),
    ],
)
async def test_full_integration_artifact_gate(
    monkeypatch, missing, empty_markdown, invalid_manifest, invalid_links, expected
):
    requested: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        assert request.headers["X-Research-Key"] == "fixture-secret"
        assert request.headers["User-Agent"] == "OpenAI File Downloader, XaiImageApiFetch/1.0"
        name = request.url.path.rsplit("/", 1)[-1]
        if name == missing:
            return httpx.Response(404)
        if name == "result.md":
            return httpx.Response(200, text="" if empty_markdown else "# Fixture result")
        if name == "links.json":
            return httpx.Response(
                200,
                json={}
                if invalid_links == "empty"
                else "invalid"
                if invalid_links
                else [{"href": "https://example.com", "text": "Guide"}],
            )
        return httpx.Response(
            200,
            json={}
            if invalid_manifest == "empty"
            else []
            if invalid_manifest
            else {"tiles_total": 1},
        )

    _configure_integration_fixture(monkeypatch, handle)
    runner = test_integration_full.IntegrationTestRunner(api_url="http://fixture")
    success, details = await runner.test_artifact_retrieval("job-fixture")
    assert success is expected
    assert requested == [
        "/jobs/job-fixture/result.md",
        "/jobs/job-fixture/links.json",
        "/jobs/job-fixture/manifest.json",
    ]
    if expected:
        assert set(details["artifacts_retrieved"]) == {"Markdown", "Links JSON", "Manifest"}


@pytest.mark.parametrize(
    "status,body,expected",
    [
        (200, "mdwb_jobs_total 1\n", True),
        (200, "", False),
        (200, "not metrics", False),
        (503, "down", False),
    ],
)
async def test_full_integration_metrics_gate(monkeypatch, status, body, expected):
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL("http://fixture/metrics")
        assert request.headers["X-Research-Key"] == "fixture-secret"
        return httpx.Response(status, text=body)

    _configure_integration_fixture(monkeypatch, handle)
    runner = test_integration_full.IntegrationTestRunner(api_url="http://fixture")
    success, _ = await runner.test_performance_metrics()
    assert success is expected


async def test_full_integration_metrics_connection_error_fails(monkeypatch):
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("fixture server unavailable", request=request)

    _configure_integration_fixture(monkeypatch, handle)
    runner = test_integration_full.IntegrationTestRunner(api_url="http://fixture")
    success, details = await runner.test_performance_metrics()
    assert success is False
    assert "fixture server unavailable" in details["error"]


@pytest.mark.parametrize("missing", [None, "result.md", "links.json", "manifest.json"])
async def test_advanced_integration_artifact_gate(monkeypatch, missing):
    requested: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        name = request.url.path.rsplit("/", 1)[-1]
        if name == missing:
            return httpx.Response(404)
        if name == "result.md":
            return httpx.Response(200, text="# Fixture result")
        if name == "links.json":
            return httpx.Response(200, json=[{"href": "https://example.com"}])
        return httpx.Response(200, json={"tiles_total": 1})

    _configure_integration_fixture(monkeypatch, handle)
    runner = test_integration_advanced.AdvancedIntegrationTester(api_url="http://fixture")
    case = test_integration_advanced.TestCase("Fixture", "https://example.com", "Harness fixture")
    monitor = test_integration_advanced.PipelineMonitor(runner.console)
    result = await runner.validate_results(
        "fixture", case, {"manifest": {"tiles_total": 1}}, monitor
    )
    assert result["success"] is (missing is None)
    assert requested == [
        "/jobs/fixture/result.md",
        "/jobs/fixture/links.json",
        "/jobs/fixture/manifest.json",
    ]


@pytest.mark.parametrize(
    "actual_tiles,expected_tiles,actual_links,expected_links,expected",
    [
        (1, 1, 1, 1, True),
        (3, 1, 1, 1, True),
        (4, 1, 1, 1, False),
        (None, 1, 1, 1, False),
        (None, None, 1, None, False),
        (0, 1, 1, 1, False),
        (True, 1, 1, 1, False),
        ("1", 1, 1, 1, False),
        (1, 1, 0, 1, False),
        (1, 1, 2, 1, False),
        (1, None, 0, None, True),
    ],
)
async def test_advanced_integration_enforces_tile_and_link_expectations(
    monkeypatch, actual_tiles, expected_tiles, actual_links, expected_links, expected
):
    def handle(request: httpx.Request) -> httpx.Response:
        name = request.url.path.rsplit("/", 1)[-1]
        if name == "result.md":
            return httpx.Response(200, text="# Fixture result")
        if name == "links.json":
            return httpx.Response(200, json=[{"href": "https://example.com"}] * actual_links)
        return httpx.Response(200, json={"tiles_total": actual_tiles})

    _configure_integration_fixture(monkeypatch, handle)
    runner = test_integration_advanced.AdvancedIntegrationTester(api_url="http://fixture")
    case = test_integration_advanced.TestCase(
        "Fixture",
        "https://example.com",
        "Harness fixture",
        expected_tiles=expected_tiles,
        expected_links=expected_links,
    )
    monitor = test_integration_advanced.PipelineMonitor(runner.console)
    result = await runner.validate_results(
        "fixture", case, {"manifest": {"tiles_total": actual_tiles}}, monitor
    )
    assert result["success"] is expected


@pytest.mark.parametrize("empty", ["result.md", "links.json", "manifest.json"])
async def test_advanced_integration_rejects_empty_artifacts(monkeypatch, empty):
    def handle(request: httpx.Request) -> httpx.Response:
        name = request.url.path.rsplit("/", 1)[-1]
        if name == empty:
            return httpx.Response(200, text="   ")
        if name == "result.md":
            return httpx.Response(200, text="# Fixture result")
        if name == "links.json":
            return httpx.Response(200, json=[])
        return httpx.Response(200, json={"tiles_total": 1})

    _configure_integration_fixture(monkeypatch, handle)
    runner = test_integration_advanced.AdvancedIntegrationTester(api_url="http://fixture")
    case = test_integration_advanced.TestCase("Fixture", "https://example.com", "Harness fixture")
    monitor = test_integration_advanced.PipelineMonitor(runner.console)
    result = await runner.validate_results(
        "fixture", case, {"manifest": {"tiles_total": 1}}, monitor
    )
    assert result["success"] is False
    assert result["checks"][empty]["passed"] is False


@pytest.mark.parametrize("phase", ["submit", "poll", "validate"])
async def test_advanced_integration_case_timeout_bounds_all_phases(monkeypatch, phase):
    cancelled: list[str] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        current = (
            "submit"
            if request.method == "POST"
            else "poll"
            if request.url.path.endswith("/fixture")
            else "validate"
        )
        if current == phase:
            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                cancelled.append(current)
                raise
        if current == "submit":
            return httpx.Response(202, json={"id": "fixture"})
        if current == "poll":
            return httpx.Response(200, json={"state": "DONE", "manifest": {"tiles_total": 1}})
        return httpx.Response(200, text="# Fixture result")

    _configure_integration_fixture(monkeypatch, handle)
    runner = test_integration_advanced.AdvancedIntegrationTester(api_url="http://fixture")
    case = test_integration_advanced.TestCase(
        "Fixture", "https://example.com", "Harness fixture", timeout=0.7
    )
    result = await asyncio.wait_for(runner.run_test_case(case), timeout=2)
    assert result["success"] is False
    assert "Timeout" in result["data"]["error"]
    assert cancelled == [phase]


@pytest.mark.parametrize("flags", [[], [True], [False], [True, False]])
async def test_advanced_integration_exit_status(monkeypatch, flags):
    _configure_integration_fixture(monkeypatch, lambda request: httpx.Response(200))

    async def run_fixture(self):
        self.results = [{"success": flag} for flag in flags]

    monkeypatch.setattr(
        test_integration_advanced.AdvancedIntegrationTester, "run_all_tests", run_fixture
    )
    monkeypatch.setattr(sys, "argv", ["test_integration_advanced.py"])
    if flags and all(flags):
        await test_integration_advanced.main()
    else:
        with pytest.raises(SystemExit) as raised:
            await test_integration_advanced.main()
        assert raised.value.code == 1


def test_integration_header_and_timeline_use_current_settings(monkeypatch):
    _configure_integration_fixture(monkeypatch, lambda request: httpx.Response(200))
    output = StringIO()
    runner = test_integration_full.IntegrationTestRunner()
    runner.console = Console(file=output, width=140)
    runner.print_header()
    assert "System Configuration" in output.getvalue()
    monitor = test_integration_advanced.PipelineMonitor(runner.console)
    monitor.add_event(test_integration_advanced.TestStage.INIT, "Starting fixture")
    monitor.add_event(
        test_integration_advanced.TestStage.CAPTURE,
        "Fixture event",
        {"first": 1, "second": 2, "third": 3, "fourth": 4},
    )
    table = monitor.get_timeline_table()
    assert table.row_count == 2
    runner.console.print(table)
    assert "first=1" in output.getvalue()
    assert "fourth=4" not in output.getvalue()


def test_manage_keys_admin_flag_and_custom_header(monkeypatch, tmp_path):
    store = Store(StorageConfig(tmp_path / "cache", tmp_path / "runs.db"))
    output = StringIO()
    monkeypatch.setattr(manage_api_keys, "build_store", lambda: store)
    settings = replace(get_settings(), API_KEY_HEADER="X-Research-Key")
    monkeypatch.setattr(manage_api_keys, "get_settings", lambda: settings)
    monkeypatch.setattr(manage_api_keys, "console", Console(file=output, width=140))
    monkeypatch.setattr(sys, "argv", ["manage_api_keys.py", "create", "Ops", "--admin"])
    manage_api_keys.main()
    rendered = output.getvalue()
    match = re.search(r"mdwb_[0-9a-f]{32}", rendered)
    assert match is not None
    plain_key = match.group()
    assert rendered.count(plain_key) == 1
    assert "X-Research-Key" in rendered
    with store.session() as session:
        record = verify_api_key(session, plain_key)
        assert record is not None and record.is_admin
        assert record.id is not None
        key_id = record.id
    output.seek(0)
    output.truncate()
    manage_api_keys.cmd_show(key_id)
    manage_api_keys.cmd_list()
    assert plain_key not in output.getvalue()
    assert "Admin" in output.getvalue()
    store.engine.dispose()


def test_uv_bootstrap_rejects_unverified_download_before_execution(tmp_path):
    """Download-boundary control; this does not execute or qualify installation."""
    installer = Path(__file__).resolve().parents[1] / "install.sh"
    candidate = tmp_path / "unverified-installer.sh"
    marker = tmp_path / "unverified-code-executed"
    candidate.write_text(f"touch {shlex.quote(str(marker))}\n")
    script = f"""
set --
source {shlex.quote(str(installer))}
command_exists() {{
    if [ "$1" = uv ]; then return 1; fi
    command -v "$1" >/dev/null
}}
curl() {{
    while [ "$#" -gt 0 ]; do
        if [ "$1" = --output ]; then
            shift
            cp -- {shlex.quote(str(candidate))} "$1"
            return
        fi
        shift
    done
    return 1
}}
install_uv
"""
    environment = {**os.environ, "TMPDIR": str(tmp_path)}
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=environment)
    assert result.returncode != 0
    assert "checksum mismatch" in result.stdout
    assert "not executed" in result.stdout
    assert not marker.exists()
    downloads = list(tmp_path.glob("mdwb-uv.*/install.sh"))
    assert len(downloads) == 1
    assert downloads[0].read_bytes() == candidate.read_bytes()
