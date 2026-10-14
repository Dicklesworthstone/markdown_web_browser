"""Actual browser/renderer contracts with a deterministic HTTP API fixture.

The fixture controls OCR output and job timing; it proves UI behavior, not OCR
quality or production capture readiness. All shipped renderer assets are loaded.
"""

from __future__ import annotations

import asyncio
import json
import threading
import socket
import time
from dataclasses import replace
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TypedDict
from urllib.parse import parse_qs, urlparse

import pytest
import uvicorn
from playwright.async_api import async_playwright

from app import main, jobs
from app.auth import AUTH_CONTEXT, authenticate, claim_resource, create_api_key
from app.jobs import JobManager
from app.schemas import JobCreateRequest
from app.store import StorageConfig, Store

WEB_ROOT = Path(__file__).resolve().parents[1] / "web"
PAYLOAD = """# Safe content

- [x] Done task
- [ ] Pending task

[safe](https://example.org/guide) [script](javascript:window.auditMarker=1)
[vb](vbscript:window.auditMarker=2) [data](data:text/html;base64,PHNjcmlwdD4=)

<img src="bad" onerror="window.auditMarker=3">
<svg><a xlink:href="javascript:window.auditMarker=4">svg link</a></svg>
<iframe srcdoc="<script>window.auditMarker=5</script>"></iframe>
<p onclick="window.auditMarker=6">event handler</p>
<math><mtext><table><mglyph><style><!--</style><img title="--><img src=bad onerror=window.auditMarker=7>"></table></mtext></math>
<table><tr><th>Invoice</th></tr><tr><td>42</td></tr></table>
<math><mi>x</mi><mo>=</mo><mn>42</mn></math>

```html
<script>window.auditMarker=8</script>
```
"""


class UIState(TypedDict):
    requests: list[dict]
    jobs: dict[str, str]
    markdown: str
    fail_next: bool
    slow_started: threading.Event
    release_slow: threading.Event
    event_requests: list[str]
    event_responses: list[tuple[int, list[dict]]]


@pytest.fixture
def ui_server():
    state: UIState = {
        "requests": [],
        "jobs": {},
        "markdown": PAYLOAD,
        "fail_next": False,
        "slow_started": threading.Event(),
        "release_slow": threading.Event(),
        "event_requests": [],
        "event_responses": [],
    }

    class Handler(BaseHTTPRequestHandler):
        def send(self, body, kind="application/json", status=200):
            data = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                # Superseding navigation intentionally aborts the old request.
                pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            state["requests"].append(body)
            job_id = str(len(state["requests"]))
            state["jobs"][job_id] = body["url"]
            if state["fail_next"]:
                state["fail_next"] = False
                self.send('{"detail":"fixture failure"}', status=503)
            else:
                self.send(json.dumps({"id": job_id}))

        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/auth/config":
                self.send('{"required":false,"header":"X-API-Key"}')
            elif path in {"/", "/browser"}:
                page = "index.html" if path == "/" else "browser.html"
                self.send((WEB_ROOT / page).read_text(), "text/html")
            elif path.startswith("/static/"):
                file = (WEB_ROOT / path.removeprefix("/static/")).resolve()
                if not file.is_relative_to(WEB_ROOT) or not file.is_file():
                    self.send("missing", status=404)
                    return
                kind = "text/javascript" if file.suffix == ".js" else "text/css"
                self.send(file.read_text(), kind)
            elif path == "/jobs/demo/stream":
                lines = "\n".join(f"data: {line}" for line in PAYLOAD.split("\n"))
                self.send(f"event: raw\n{lines}\n\n", "text/event-stream")
            elif path.endswith("/links.json") or path.endswith("/manifest.json"):
                self.send("[]" if path.endswith("/links.json") else "{}")
            elif path == "/jobs/events-reconnect/events":
                index = len(state["event_requests"])
                state["event_requests"].append(self.path)
                status, entries = (
                    state["event_responses"][index]
                    if index < len(state["event_responses"])
                    else (200, [])
                )
                self.send(
                    "".join(json.dumps(entry) + "\n" for entry in entries),
                    "application/x-ndjson",
                    status,
                )
            elif path.endswith("/events"):
                self.send("", "application/x-ndjson")
            elif path.endswith("/result.md"):
                job_id = path.split("/")[2]
                url = state["jobs"][job_id]
                if "slow" in url:
                    state["slow_started"].set()
                    assert state["release_slow"].wait(10), "test never released old navigation"
                self.send(state["markdown"] + f"\n\nURL: {url}\nJob: {job_id}", "text/markdown")
            elif path.startswith("/jobs/"):
                self.send('{"state":"DONE","progress":{"done":3,"total":3}}')
            else:
                self.send("missing", status=404)

        def log_message(self, format, *args):
            # HTTP access is asserted in state[requests]; pytest prints failures.
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", state
    state["release_slow"].set()
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


@pytest.fixture
async def browser_page(ui_server):
    base, _ = ui_server
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(channel="chrome", headless=True)
        context = await browser.new_context(
            user_agent="OpenAI File Downloader, XaiImageApiFetch/1.0"
        )
        page = await context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))

        # CSS/fonts on external CDNs are decorative; all runtime JS is local.
        async def local_only(route):
            if route.request.url.startswith(base):
                await route.continue_()
            else:
                await route.abort()

        await page.route("**/*", local_only)
        await page.goto(base + "/browser")
        await page.wait_for_function("!!window.mdwbBrowser?.elements.goBtn")
        print(
            f"UI contract browser build={browser.version}; OCR=fixture; renderer=marked11+DOMPurify3.4.16"
        )
        yield page, errors
        await context.close()
        await browser.close()


async def assert_safe_render(page, selector):
    content = page.locator(selector)
    await content.locator("table").filter(has_text="Invoice").wait_for()
    assert await content.locator("table").filter(has_text="Invoice").inner_text() == "Invoice\n42"
    assert await content.locator("math mn").last.text_content() == "42"
    assert await content.locator('a[href="https://example.org/guide"]').count() == 1
    checkboxes = content.locator('input[type="checkbox"]')
    assert await checkboxes.count() == 2
    assert await checkboxes.first.is_checked()
    assert await checkboxes.evaluate_all("items => items.every(input => input.disabled)")
    assert await content.locator("script,iframe,form,style").count() == 0
    assert await content.evaluate(
        "el => [...el.querySelectorAll('*')].every(e => [...e.attributes].every(a => !a.name.startsWith('on'))) "
    )
    assert await content.evaluate(
        "el => [...el.querySelectorAll('*')].every(e => [...e.attributes].filter(a => ['href','src','xlink:href'].includes(a.name)).every(a => ['http:','https:','mailto:','tel:'].includes(new URL(a.value, location.href).protocol)))"
    )
    for label in ("script", "vb", "data"):
        link = content.get_by_text(label, exact=True)
        if await link.count():
            await link.first.click()
    assert await page.evaluate("window.auditMarker || 0") == 0


async def test_rendered_and_raw_views_reject_xss(browser_page, ui_server):
    page, errors = browser_page
    await page.evaluate("window.mdwbBrowser.loadUrl('https://example.com/first')")
    await assert_safe_render(page, "#rendered-content")
    await page.locator("#raw-btn").click()
    raw = page.locator("#raw-content")
    assert "<script>window.auditMarker=8</script>" in await raw.inner_text()
    assert await raw.locator("span.token").count() > 0
    assert await page.evaluate("window.auditMarker || 0") == 0
    assert errors == []


async def test_navigation_cache_refresh_retry_and_keyboard(browser_page, ui_server):
    page, errors = browser_page
    _, state = ui_server
    url_input = page.locator("#url-input")
    await url_input.fill("example.com/first")
    await url_input.press("Enter")
    await page.wait_for_function(
        "window.mdwbBrowser.state.currentJobId === '1' && document.querySelector('#status-text').textContent.includes('Successfully')"
    )
    assert state["requests"] == [{"url": "https://example.com/first", "reuse_cache": True}]
    await page.evaluate("window.mdwbBrowser.loadUrl('https://example.com/second')")
    await page.keyboard.press("Alt+ArrowLeft")
    assert await url_input.input_value() == "https://example.com/first"
    assert await page.evaluate("window.mdwbBrowser.state.currentJobId") == "1"
    await page.keyboard.press("Alt+ArrowRight")
    assert await url_input.input_value() == "https://example.com/second"
    assert len(state["requests"]) == 2
    await page.locator("#refresh-btn").click()
    await page.wait_for_function(
        "document.querySelector('#rendered-content').textContent.includes('Job: 3')"
    )
    assert state["requests"][-1] == {"url": "https://example.com/second", "reuse_cache": False}
    await page.keyboard.press("Control+r")
    await page.wait_for_function(
        "document.querySelector('#rendered-content').textContent.includes('Job: 4')"
    )
    assert state["requests"][-1]["reuse_cache"] is False
    state["fail_next"] = True
    await page.evaluate("window.mdwbBrowser.loadUrl('https://example.com/failure')")
    assert await page.locator("#error-message").inner_text() == '{"detail":"fixture failure"}'
    await page.locator("#retry-btn").click()
    await page.wait_for_function(
        "document.querySelector('#rendered-content').textContent.includes('Job: 6')"
    )
    assert state["requests"][-1]["reuse_cache"] is False
    assert await page.locator("#loading-spinner").is_hidden()
    assert errors == []


async def test_superseded_navigation_cannot_overwrite_current_page(browser_page, ui_server):
    page, errors = browser_page
    _, state = ui_server
    await page.evaluate("void window.mdwbBrowser.loadUrl('https://example.com/slow')")
    await page.wait_for_function("window.mdwbBrowser.state.currentJobId === '1'")
    assert await asyncio.to_thread(state["slow_started"].wait, 5)
    await page.evaluate("window.mdwbBrowser.loadUrl('https://example.com/current')")
    state["release_slow"].set()
    await page.wait_for_function(
        "document.querySelector('#rendered-content').textContent.includes('URL: https://example.com/current')"
    )
    assert await page.locator("#url-input").input_value() == "https://example.com/current"
    assert (
        "URL: https://example.com/slow" not in await page.locator("#rendered-content").inner_text()
    )
    assert await page.evaluate("window.mdwbBrowser.state.currentJobId") == "2"
    assert await page.locator("#loading-spinner").is_hidden()
    assert errors == []


async def test_dashboard_sse_uses_the_same_safe_renderer(browser_page, ui_server):
    page, errors = browser_page
    base, _ = ui_server
    await page.goto(base + "/")
    await assert_safe_render(page, '[data-sse-field="rendered"]')
    assert (
        "<script>window.auditMarker=8</script>"
        in await page.locator('[data-sse-field="raw"]').inner_text()
    )
    assert errors == []


async def test_dashboard_events_resume_by_sequence_and_recover_both_gap_forms(
    browser_page, ui_server
):
    page, errors = browser_page
    base, state = ui_server

    # Identical timestamps distinguish sequence replay from the old timestamp
    # filter. A heartbeat's later timestamp must never become a replay cursor.
    def entry(sequence):
        return {
            "event": "snapshot",
            "sequence": sequence,
            "timestamp": "2026-10-13T00:00:00Z",
            "snapshot": {"state": "OCR_WAITING", "progress": {"done": sequence, "total": 20}},
        }

    state["event_responses"] = [
        (200, [entry(0), entry(1), {"event": "heartbeat", "timestamp": "2099-01-01T00:00:00Z"}]),
        (409, []),
        (200, [entry(10), entry(11), {"event": "gap", "code": "event-history-gap"}]),
        (200, [entry(12)]),
        (200, [entry(12), entry(13)]),
    ]
    await page.goto(base + "/")
    await page.locator("#job-url").fill("")
    await page.locator("#job-id").fill("events-reconnect")
    await page.locator("#run-job").click()
    await page.wait_for_function(
        "[...document.querySelectorAll('[data-events-log] .event-feed__meta')].some(node => node.textContent.includes('#13'))",
        timeout=20000,
    )
    await page.get_by_role("tab", name="Events", exact=True).click()
    await page.locator("[data-events-pause]").click()
    assert [parse_qs(urlparse(path).query) for path in state["event_requests"][:5]] == [
        {},
        {"after": ["1"]},
        {},
        {},
        {"after": ["12"]},
    ]
    rows = await page.locator("[data-events-log] .event-feed__meta").all_text_contents()
    sequences = [int(row.rsplit("#", 1)[1]) for row in rows]
    assert sequences == [13, 12, 11, 10, 1, 0]
    assert errors == []


async def test_xss_negative_control_confirms_browser_oracle(browser_page):
    page, _ = browser_page
    # Reproduce the former unsanitized boundary with the actual shipped parser.
    await page.evaluate(
        "document.querySelector('#rendered-content').innerHTML = marked.parse('[unsafe](javascript:window.auditMarker=9)')"
    )
    await page.get_by_text("unsafe", exact=True).click()
    assert await page.evaluate("window.auditMarker") == 9


async def test_authenticated_browser_uses_real_api_cookie_and_private_cache(tmp_path, monkeypatch):
    """Actual TCP API, SQLite and browser; cached fixture text, no hosted OCR claim."""
    store = Store(StorageConfig(tmp_path / "cache", tmp_path / "runs.db"))
    settings = replace(
        main.settings,
        REQUIRE_API_KEY=True,
        API_KEY_HEADER="X-Research-Key",
        telemetry=replace(main.settings.telemetry, prometheus_port=0),
    )
    manager = JobManager(store=store)
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(main, "settings", settings)
    monkeypatch.setattr(main, "JOB_MANAGER", manager)
    with store.session() as session:
        alice, _ = create_api_key(session, "Alice", owner="alice", rate_limit=1000)
        bob, _ = create_api_key(session, "Bob", owner="bob", rate_limit=1000)
    target_url = "https://example.com/private-ui"
    token = AUTH_CONTEXT.set(authenticate(alice, store, settings))
    try:
        request = JobCreateRequest(url=target_url)
        config = jobs._build_capture_config(request, jobs.global_settings)
        cache_key = jobs._build_cache_key(config=config, settings=jobs.global_settings)
        claim_resource(store, "job:cached-source")
        store.allocate_run(
            job_id="cached-source",
            url=target_url,
            started_at=datetime.now(timezone.utc),
            cache_key=cache_key,
        )
        store.write_manifest(
            job_id="cached-source",
            manifest={"url": target_url, "tiles_total": 1, "cache_key": cache_key},
        )
        store.write_markdown(job_id="cached-source", content=PAYLOAD)
        store.write_links(job_id="cached-source", links=[])
        store.update_status(
            job_id="cached-source", status="DONE", finished_at=datetime.now(timezone.utc)
        )
    finally:
        AUTH_CONTEXT.reset(token)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(main.app, log_level="warning", lifespan="on"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        assert server.started, "actual API server failed to start"
        base = f"http://127.0.0.1:{port}"
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel="chrome", headless=True)
            context = await browser.new_context(
                user_agent="OpenAI File Downloader, XaiImageApiFetch/1.0"
            )
            page = await context.new_page()

            async def local_only(route):
                if route.request.url.startswith(base):
                    await route.continue_()
                else:
                    await route.abort()

            await page.route("**/*", local_only)
            try:
                await page.goto(base + "/browser")
                key_input = page.get_by_role("textbox", name="API key")
                await key_input.wait_for()
                assert await page.evaluate("fetch('/jobs').then(r => r.status)") == 401
                await key_input.fill("mdwb_" + "z" * 32)
                await page.get_by_role("button", name="Sign in", exact=True).click()
                await page.get_by_role("status").filter(has_text="Sign-in failed").wait_for()
                assert await key_input.input_value() == ""
                assert not await context.cookies()
                await key_input.fill(alice)
                await page.get_by_role("button", name="Sign in", exact=True).click()
                await page.get_by_role("status").filter(has_text="Signed in").wait_for()
                cookie = next(c for c in await context.cookies() if c["name"] == "mdwb_session")
                assert cookie["httpOnly"] and cookie["sameSite"] == "Strict"
                assert "mdwb_session" not in await page.evaluate("document.cookie")
                assert await key_input.input_value() == ""
                await page.evaluate("""() => {
                    window.mdwbGlobalProbe = [];
                    window.mdwbGlobalSource = new EventSource('/jobs/stream');
                    window.mdwbGlobalSource.onmessage = event => {
                        window.mdwbGlobalProbe.push({id: event.lastEventId, entry: JSON.parse(event.data)});
                    };
                }""")
                await page.wait_for_function(
                    "window.mdwbGlobalProbe.some(item => item.entry.event === 'subscribed')"
                )
                await page.evaluate("url => window.mdwbBrowser.loadUrl(url)", target_url)
                await assert_safe_render(page, "#rendered-content")
                job_id = await page.evaluate("window.mdwbBrowser.state.currentJobId")
                assert isinstance(job_id, str) and job_id != "cached-source"
                assert manager._tasks == {}, "cached path must not admit capture/OCR"
                await page.wait_for_function(
                    "id => window.mdwbGlobalProbe.some(item => item.entry.job_id === id && item.entry.snapshot?.state === 'DONE')",
                    arg=job_id,
                )
                feeds = await page.evaluate(
                    """async id => {
                    const read = (url) => new Promise((resolve, reject) => {
                        const entries = [];
                        let raw = null;
                        const source = new EventSource(url);
                        const timer = setTimeout(() => { source.close(); reject(new Error('SSE did not reach DONE')); }, 5000);
                        source.addEventListener('raw', event => { raw = event.data; });
                        source.addEventListener('job-event', event => {
                            const entry = JSON.parse(event.data);
                            entries.push(entry);
                            if (entry.snapshot?.state === 'DONE') {
                                source.close(); clearTimeout(timer); resolve({entries, raw});
                            }
                        });
                    });
                    const url = `/jobs/${id}/stream?after=-1`;
                    const [first, second, historyResponse] = await Promise.all([
                        read(url), read(url), fetch(`/jobs/${id}/events.json`)
                    ]);
                    const history = (await historyResponse.json()).events;
                    const global = window.mdwbGlobalProbe.filter(item => item.entry.job_id === id);
                    window.mdwbGlobalSource.close();
                    return {first, second, history, global};
                }""",
                    job_id,
                )
                assert feeds["first"]["entries"] == feeds["second"]["entries"] == feeds["history"]
                assert feeds["first"]["raw"] == feeds["second"]["raw"] == PAYLOAD
                assert [item["entry"] for item in feeds["global"]] == feeds["history"]
                assert [item["id"] for item in feeds["global"]] == [
                    entry["cursor"] for entry in feeds["history"]
                ]
                assert [entry["sequence"] for entry in feeds["history"]] == list(
                    range(len(feeds["history"]))
                )
                assert (
                    await page.evaluate("fetch('/jobs').then(r => r.json()).then(p => p.total)")
                    == 2
                )
                assert alice not in await page.content()
                assert (
                    await page.evaluate(
                        "Object.keys(localStorage).length + Object.keys(sessionStorage).length"
                    )
                    == 0
                )
                other = await browser.new_context(
                    user_agent="OpenAI File Downloader, XaiImageApiFetch/1.0",
                    extra_http_headers={settings.API_KEY_HEADER: bob},
                )
                try:
                    response = await other.request.get(base + f"/jobs/{job_id}/result.md")
                    assert response.status == 404
                    response = await other.request.get(base + "/jobs")
                    assert response.status == 200 and (await response.json())["total"] == 0
                    cursor = feeds["global"][-1]["id"]
                    response = await other.request.get(
                        base + "/jobs/stream", params={"after": cursor}
                    )
                    assert response.status == 409
                    assert (await response.json())["detail"]["first_available"] is None
                finally:
                    await other.close()
                await page.get_by_role("button", name="Sign out", exact=True).click()
                await page.get_by_role("status").filter(has_text="Signed out").wait_for()
                assert await page.evaluate("fetch('/jobs').then(r => r.status)") == 401
                assert "Invoice" not in await page.locator("#rendered-content").inner_text()
                assert not await context.cookies()
            finally:
                await context.close()
                await browser.close()
    finally:
        server.should_exit = True
        await asyncio.to_thread(thread.join, 5)
        listener.close()
        store.engine.dispose()
        assert not thread.is_alive(), "actual API server did not shut down"
