#!/usr/bin/env python3
"""Minimal mdwb CLI for interacting with the capture API (demo)."""

from __future__ import annotations

import json
import os
import time
from collections import deque
from datetime import datetime, timedelta
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional, TextIO, Tuple

import httpx
import typer
from decouple import Config as DecoupleConfig, RepositoryEnv
from rich.console import Console
from rich.table import Table

console = Console()
cli = typer.Typer(help="Interact with the Markdown Web Browser API")
demo_cli = typer.Typer(help="Demo commands hitting the built-in /jobs/demo endpoints.")
cli.add_typer(demo_cli, name="demo")
jobs_cli = typer.Typer(help="Job utilities (events/watch).")
cli.add_typer(jobs_cli, name="jobs")
warnings_cli = typer.Typer(help="Warning/blocklist log helpers.")
cli.add_typer(warnings_cli, name="warnings")


@dataclass
class APISettings:
    base_url: str
    api_key: Optional[str]
    warning_log_path: Path


def _load_env_settings() -> APISettings:
    env_path = Path(".env")
    if env_path.exists():
        config = DecoupleConfig(RepositoryEnv(str(env_path)))
        base_url = config("API_BASE_URL", default="http://localhost:8000")
        api_key = config("MDWB_API_KEY", default=None)
        warning_log = Path(config("WARNING_LOG_PATH", default="ops/warnings.jsonl"))
        return APISettings(base_url=base_url, api_key=api_key, warning_log_path=warning_log)
    return APISettings(base_url="http://localhost:8000", api_key=None, warning_log_path=Path("ops/warnings.jsonl"))


def _resolve_settings(override_base: Optional[str]) -> APISettings:
    settings = _load_env_settings()
    if override_base:
        settings.base_url = override_base
    return settings


def _auth_headers(settings: APISettings) -> dict[str, str]:
    headers: dict[str, str] = {}
    if settings.api_key:
        headers["Authorization"] = f"Bearer {settings.api_key}"
    return headers


def _client(settings: APISettings, http2: bool = True) -> httpx.Client:
    timeout = httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=10.0)
    return httpx.Client(
        base_url=settings.base_url,
        timeout=timeout,
        http2=http2,
        headers=_auth_headers(settings),
    )


def _print_job(job: dict) -> None:
    table = Table("Field", "Value", title=f"Job {job.get('id', 'unknown')}")
    for key in ("state", "url", "progress", "manifest", "warnings", "blocklist_hits"):
        value = job.get(key)
        if isinstance(value, (dict, list)):
            value = json.dumps(value, indent=2)
        table.add_row(key, str(value))
    console.print(table)


def _print_links(links: Iterable[dict]) -> None:
    table = Table("Text", "Href", "Source", "Δ", title="Links")
    for row in links:
        table.add_row(row.get("text", ""), row.get("href", ""), row.get("source", ""), row.get("delta", ""))
    console.print(table)


def _iter_sse(response: httpx.Response) -> Iterable[Tuple[str, str]]:
    event = "message"
    data_lines: list[str] = []
    for line in response.iter_lines():
        if not line:
            if data_lines:
                yield event, "\n".join(data_lines)
            event = "message"
            data_lines = []
            continue
        if line.startswith("event:"):
            event = line.split(":", 1)[1].strip()
        elif line.startswith("data:"):
            data_lines.append(line.split(":", 1)[1].strip())
    if data_lines:
        yield event, "\n".join(data_lines)


def _stream_job(job_id: str, settings: APISettings, *, raw: bool) -> None:
    with httpx.Client(base_url=settings.base_url, timeout=None, headers=_auth_headers(settings)) as client:
        with client.stream("GET", f"/jobs/{job_id}/stream") as response:
            response.raise_for_status()
            for event, payload in _iter_sse(response):
                if raw:
                    console.print(f"{event}\t{payload}")
                else:
                    _log_event(event, payload)


def _iter_event_lines(
    job_id: str,
    settings: APISettings,
    *,
    cursor: str | None,
    follow: bool,
    interval: float,
):
    client = _client(settings)
    try:
        while True:
            params: dict[str, str] = {}
            if cursor:
                params["since"] = cursor
            with client.stream("GET", f"/jobs/{job_id}/events", params=params) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    if not line:
                        continue
                    yield line
                    cursor = _cursor_from_line(line, cursor)
            if not follow:
                break
            time.sleep(interval)
    finally:
        client.close()


def _watch_job_events(
    job_id: str,
    settings: APISettings,
    *,
    cursor: str | None,
    follow: bool,
    interval: float,
    output: TextIO,
) -> None:
    for line in _iter_event_lines(job_id, settings, cursor=cursor, follow=follow, interval=interval):
        output.write(line + "\n")
        output.flush()


def _watch_job_events_pretty(
    job_id: str,
    settings: APISettings,
    *,
    cursor: str | None,
    follow: bool,
    interval: float,
    raw: bool,
) -> None:
    terminal_states = {"DONE", "FAILED"}
    for line in _iter_event_lines(job_id, settings, cursor=cursor, follow=follow, interval=interval):
        if raw:
            console.print(line)
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            console.print(line)
            continue
        snapshot = entry.get("snapshot")
        if isinstance(snapshot, dict):
            _render_snapshot(snapshot)
            state = snapshot.get("state")
            if follow and isinstance(state, str) and state.upper() in terminal_states:
                break
        else:
            console.print_json(data=entry)


def _render_snapshot(snapshot: dict[str, Any]) -> None:
    state = snapshot.get("state")
    if state:
        _log_event("state", str(state))
    progress = snapshot.get("progress")
    if isinstance(progress, dict):
        done = progress.get("done", 0)
        total = progress.get("total", 0)
        _log_event("progress", f"{done} / {total} tiles")
    manifest_path = snapshot.get("manifest_path")
    if manifest_path:
        _log_event("log", f"manifest: {manifest_path}")
    manifest = snapshot.get("manifest")
    if isinstance(manifest, dict):
        warnings = manifest.get("warnings")
        if warnings:
            _log_event("warnings", json.dumps(warnings))
    error = snapshot.get("error")
    if error:
        _log_event("log", json.dumps({"error": error}))

@cli.command()
def fetch(
    url: str = typer.Argument(..., help="URL to capture"),
    api_base: Optional[str] = typer.Option(None, help="Override API base URL"),
    profile: Optional[str] = typer.Option(None, "--profile", help="Browser profile identifier"),
    ocr_policy: Optional[str] = typer.Option(None, "--ocr-policy", help="OCR policy/model id"),
    watch: bool = typer.Option(False, "--watch/--no-watch", help="Stream job progress after submission"),
    raw: bool = typer.Option(False, "--raw", help="When watching, print raw NDJSON lines"),
    http2: bool = typer.Option(True, "--http2/--no-http2"),
) -> None:
    """Submit a new capture job and optionally stream progress."""

    settings = _resolve_settings(api_base)
    client = _client(settings, http2=http2)
    payload: dict[str, object] = {"url": url}
    if profile:
        payload["profile_id"] = profile
    if ocr_policy:
        payload["ocr_policy"] = ocr_policy

    response = client.post("/jobs", json=payload)
    response.raise_for_status()
    job = response.json()
    console.print(f"[green]Created job {job.get('id')}[/]")
    _print_job(job)

    if watch and job.get("id"):
        console.rule(f"Streaming {job['id']}")
        try:
            _watch_job_events_pretty(
                job["id"],
                settings,
                cursor=None,
                follow=True,
                interval=2.0,
                raw=raw,
            )
        except httpx.HTTPError as exc:
            console.print(
                f"[yellow]Events feed unavailable ({exc}); falling back to SSE stream.[/]"
            )
            _stream_job(job["id"], settings, raw=raw)


@cli.command()
def show(
    job_id: str = typer.Argument(..., help="Job identifier"),
    api_base: Optional[str] = typer.Option(None, help="Override API base URL"),
    http2: bool = typer.Option(True, "--http2/--no-http2"),
) -> None:
    """Display the latest snapshot for a real job."""

    settings = _resolve_settings(api_base)
    client = _client(settings, http2=http2)
    response = client.get(f"/jobs/{job_id}")
    response.raise_for_status()
    _print_job(response.json())


@cli.command()
def stream(
    job_id: str = typer.Argument(..., help="Job identifier"),
    api_base: Optional[str] = typer.Option(None, help="Override API base URL"),
    raw: bool = typer.Option(False, "--raw", help="Print raw event payloads instead of colored labels."),
) -> None:
    """Tail the live SSE stream for a job."""

    settings = _resolve_settings(api_base)
    _stream_job(job_id, settings, raw=raw)


@cli.command()
def events(
    job_id: str = typer.Argument(..., help="Job identifier"),
    api_base: Optional[str] = typer.Option(None, help="Override API base URL"),
    since: Optional[str] = typer.Option(None, help="ISO timestamp cursor for incremental polling."),
    follow: bool = typer.Option(False, "--follow/--no-follow", help="Continue polling for new events."),
    interval: float = typer.Option(2.0, "--interval", help="Polling interval in seconds when following."),
    output: typer.FileTextWrite = typer.Option(
        "-", "--output", "-o", help="File to append NDJSON events to (default stdout)."
    ),
) -> None:
    """Fetch newline-delimited job events (JSONL)."""

    settings = _resolve_settings(api_base)
    _watch_job_events(job_id, settings, cursor=since, follow=follow, interval=interval, output=output)


@cli.command()
def watch(
    job_id: str = typer.Argument(..., help="Job identifier"),
    api_base: Optional[str] = typer.Option(None, help="Override API base URL"),
    since: Optional[str] = typer.Option(None, help="ISO timestamp cursor for incremental polling."),
    follow: bool = typer.Option(True, "--follow/--once", help="Keep polling for new events instead of exiting."),
    interval: float = typer.Option(2.0, "--interval", help="Polling interval in seconds when following."),
    raw: bool = typer.Option(False, "--raw", help="Print raw NDJSON events instead of formatted output."),
) -> None:
    """Stream `/jobs/{id}/events` with optional fallback to SSE."""

    settings = _resolve_settings(api_base)
    try:
        _watch_job_events_pretty(
            job_id,
            settings,
            cursor=since,
            follow=follow,
            interval=interval,
            raw=raw,
        )
    except httpx.HTTPError as exc:
        console.print(f"[yellow]Events feed unavailable ({exc}); falling back to SSE stream.[/]")
        _stream_job(job_id, settings, raw=raw)


@demo_cli.command("snapshot")
def demo_snapshot(
    api_base: Optional[str] = typer.Option(None, help="Override API base URL"),
    json_output: bool = typer.Option(False, "--json", help="Print raw JSON instead of tables."),
) -> None:
    """Fetch the demo job snapshot from /jobs/demo."""

    settings = _resolve_settings(api_base)
    client = _client(settings)
    response = client.get("/jobs/demo")
    response.raise_for_status()
    data = response.json()
    if json_output:
        console.print_json(data=data)
    else:
        _print_job(data)
        if links := data.get("links"):
            _print_links(links)


@demo_cli.command("links")
def demo_links(
    api_base: Optional[str] = typer.Option(None, help="Override API base URL"),
    json_output: bool = typer.Option(False, "--json", help="Print raw JSON."),
) -> None:
    """Fetch the demo links JSON."""

    settings = _resolve_settings(api_base)
    client = _client(settings)
    response = client.get("/jobs/demo/links.json")
    response.raise_for_status()
    data = response.json()
    if json_output:
        console.print_json(data=data)
    else:
        _print_links(data)


def _log_event(event: str, payload: str) -> None:
    if event == "state":
        console.print(f"[cyan]{payload}[/]")
    elif event == "progress":
        console.print(f"[magenta]{payload}[/]")
    elif event in {"warning", "warnings"}:
        console.print(f"[red]warning[/]: {payload}")
    else:
        console.print(f"[bold]{event}[/]: {payload}")


def _cursor_from_line(line: str, fallback: str | None) -> str | None:
    try:
        entry = json.loads(line)
    except json.JSONDecodeError:
        return fallback
    timestamp = entry.get("timestamp")
    snapshot = entry.get("snapshot")
    if timestamp:
        return _bump_timestamp(timestamp)
    if isinstance(snapshot, dict):
        ts = snapshot.get("timestamp")
        if isinstance(ts, str):
            return _bump_timestamp(ts)
    return fallback


def _bump_timestamp(value: str) -> str:
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return value
    return (dt + timedelta(microseconds=1)).isoformat()


def _load_warning_records(path: Path, limit: int) -> list[dict[str, Any]]:
    if limit <= 0 or not path.exists():
        return []
    records: deque[dict[str, Any]] = deque(maxlen=limit)
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            records.append(payload)
    return list(records)


def _print_warning_records(records: list[dict[str, Any]], *, json_output: bool) -> None:
    if not records:
        console.print("[dim]No warning entries found.[/]")
        return
    if json_output:
        for record in records:
            console.print(json.dumps(record))
        return
    table = Table("timestamp", "job", "warnings", "blocklist", "sweep", "validation", title="Warning Log")
    for row in _warning_rows(records):
        table.add_row(*row)
    console.print(table)


def _warning_rows(records: Iterable[dict[str, Any]]) -> Iterable[tuple[str, str, str, str, str, str]]:
    for record in records:
        timestamp = record.get("timestamp", "-")
        job = record.get("job_id", "-")
        warnings = _format_warning_summary(record.get("warnings"))
        blocklist = _format_blocklist(record.get("blocklist_hits"))
        sweep = _format_sweep_summary(record)
        validation = _format_validation_summary(record.get("validation_failures"))
        yield (str(timestamp), str(job), warnings, blocklist, sweep, validation)


def _format_warning_summary(values: Any) -> str:
    if not isinstance(values, list) or not values:
        return "-"
    formatted: list[str] = []
    for entry in values:
        if not isinstance(entry, dict):
            formatted.append(str(entry))
            continue
        code = entry.get("code", "?")
        count = entry.get("count")
        threshold = entry.get("threshold")
        if count is not None and threshold is not None:
            formatted.append(f"{code} ({count}/{threshold})")
        elif count is not None:
            formatted.append(f"{code} ({count})")
        else:
            formatted.append(str(code))
    return "; ".join(formatted)


def _format_blocklist(values: Any) -> str:
    if not isinstance(values, dict) or not values:
        return "-"
    parts = [f"{selector}:{count}" for selector, count in values.items()]
    return ", ".join(parts)


def _format_sweep_summary(record: dict[str, Any]) -> str:
    stats = record.get("sweep_stats")
    if not isinstance(stats, dict):
        stats = {}
    parts: list[str] = []
    shrink = stats.get("shrink_events")
    retry = stats.get("retry_attempts")
    overlap_pairs = stats.get("overlap_pairs")
    if shrink:
        parts.append(f"shrink={shrink}")
    if retry:
        parts.append(f"retry={retry}")
    if overlap_pairs:
        parts.append(f"pairs={overlap_pairs}")
    ratio = record.get("overlap_match_ratio", stats.get("overlap_match_ratio"))
    if isinstance(ratio, (int, float)):
        parts.append(f"ratio={ratio:.2f}")
    return ", ".join(parts) if parts else "-"


def _format_validation_summary(values: Any) -> str:
    if not isinstance(values, list) or not values:
        return "-"
    return "; ".join(str(entry) for entry in values)


def _follow_warning_log(path: Path, *, json_output: bool, interval: float) -> None:
    with path.open("r", encoding="utf-8") as handle:
        handle.seek(0, os.SEEK_END)
        while True:
            line = handle.readline()
            if not line:
                time.sleep(interval)
                continue
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            _print_warning_records([record], json_output=json_output)


@demo_cli.command("stream")
def demo_stream(
    api_base: Optional[str] = typer.Option(None, help="Override API base URL"),
    raw: bool = typer.Option(False, "--raw", help="Print raw event payloads instead of colored labels."),
) -> None:
    """Tail the demo SSE stream."""

    settings = _resolve_settings(api_base)
    with httpx.Client(base_url=settings.base_url, timeout=None, headers=_auth_headers(settings)) as client:
        with client.stream("GET", "/jobs/demo/stream") as response:
            response.raise_for_status()
            for event, payload in _iter_sse(response):
                if raw:
                    console.print(f"{event}\t{payload}")
                else:
                    _log_event(event, payload)


@demo_cli.command("watch")
def demo_watch(api_base: Optional[str] = typer.Option(None, help="Override API base URL")) -> None:
    """Convenience alias for `demo stream`."""

    demo_stream(api_base=api_base)


@warnings_cli.command("tail")
def warnings_tail(
    count: int = typer.Option(20, "--count", "-n", help="Number of entries to display."),
    follow: bool = typer.Option(False, "--follow/--no-follow", help="Stream new entries as they arrive."),
    interval: float = typer.Option(1.0, "--interval", help="Polling interval in seconds when following."),
    json_output: bool = typer.Option(False, "--json", help="Emit raw JSON lines instead of a table."),
    log_path: Optional[Path] = typer.Option(None, "--log-path", help="Override WARNING_LOG_PATH."),
) -> None:
    """Tail the structured warning/blocklist log."""

    settings = _resolve_settings(None)
    target_path = log_path or settings.warning_log_path
    if not target_path.exists():
        console.print(f"[yellow]Warning log not found at {target_path}[/]")
        return

    records = _load_warning_records(target_path, count)
    _print_warning_records(records, json_output=json_output)

    if follow:
        console.print(f"[dim]Following {target_path} (Ctrl+C to stop)...[/]")
        try:
            _follow_warning_log(target_path, json_output=json_output, interval=interval)
        except KeyboardInterrupt:  # pragma: no cover - manual interaction
            console.print("[dim]Stopped tailing warning log.[/]")


@demo_cli.command("events")
def demo_events(
    api_base: Optional[str] = typer.Option(None, help="Override API base URL"),
    output: typer.FileTextWrite = typer.Option(
        "-", "--output", "-o", help="File to append JSON events to (default stdout)."
    ),
) -> None:
    """Emit demo SSE events as JSON lines (automation-friendly)."""

    import json as jsonlib

    settings = _resolve_settings(api_base)
    with httpx.Client(base_url=settings.base_url, timeout=None, headers=_auth_headers(settings)) as client:
        with client.stream("GET", "/jobs/demo/stream") as response:
            response.raise_for_status()
            for event, payload in _iter_sse(response):
                jsonlib.dump({"event": event, "data": payload}, output)
                output.write("\n")
                output.flush()


dom_cli = typer.Typer(help="DOM snapshot utilities.")
cli.add_typer(dom_cli, name="dom")


@dom_cli.command("links")
def dom_links(
    snapshot: Optional[Path] = typer.Argument(None, exists=True, dir_okay=False, help="Path to DOM snapshot HTML file."),
    job_id: Optional[str] = typer.Option(None, "--job-id", help="Lookup DOM snapshot for an existing job."),
    json_output: bool = typer.Option(False, "--json", help="Print raw JSON list instead of a table."),
) -> None:
    """Extract links from a DOM snapshot using the ogf helper."""

    from app.dom_links import extract_links_from_dom, serialize_links
    from app.store import Store

    path = snapshot
    if job_id:
        store = Store()
        path = store.dom_snapshot_path(job_id=job_id)
    if not path:
        raise typer.BadParameter("Provide either a snapshot path or --job-id")
    if not path.exists():
        raise typer.BadParameter(f"DOM snapshot not found: {path}")

    records = extract_links_from_dom(path)
    data = serialize_links(records)
    if json_output:
        console.print_json(data=data)
        return
    _print_links(data)
 
@jobs_cli.command("watch")
def jobs_watch(
    job_id: str = typer.Argument(..., help="Job identifier"),
    api_base: Optional[str] = typer.Option(None, help="Override API base URL"),
    since: Optional[str] = typer.Option(None, help="Resume from this ISO timestamp."),
    interval: float = typer.Option(2.0, "--interval", help="Polling interval (seconds) when following."),
    follow: bool = typer.Option(True, "--follow/--once", help="Stream continuously (default) or exit after one batch."),
    output: typer.FileTextWrite = typer.Option(
        "-", "--output", "-o", help="File to append NDJSON events to (default stdout)."
    ),
) -> None:
    """Continuously tail the `/jobs/{id}/events` NDJSON feed."""

    settings = _resolve_settings(api_base)
    _watch_job_events(job_id, settings, cursor=since, follow=follow, interval=interval, output=output)
