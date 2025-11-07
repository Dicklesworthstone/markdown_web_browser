#!/usr/bin/env python3
"""Minimal mdwb CLI for interacting with the capture API (demo)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Tuple

import httpx
import typer
from decouple import Config as DecoupleConfig, RepositoryEnv
from rich.console import Console
from rich.table import Table

console = Console()
cli = typer.Typer(help="Interact with the Markdown Web Browser API")
demo_cli = typer.Typer(help="Demo commands hitting the built-in /jobs/demo endpoints.")
cli.add_typer(demo_cli, name="demo")


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


@cli.command()
def fetch(
    url: str = typer.Argument(..., help="URL to capture"),
    api_base: Optional[str] = typer.Option(None, help="Override API base URL"),
    profile: Optional[str] = typer.Option(None, "--profile", help="Browser profile identifier"),
    ocr_policy: Optional[str] = typer.Option(None, "--ocr-policy", help="OCR policy/model id"),
    watch: bool = typer.Option(False, "--watch/--no-watch", help="Stream job progress after submission"),
    raw: bool = typer.Option(False, "--raw", help="When watching, print raw SSE lines"),
    http2: bool = typer.Option(True, "--http2/--no-http2"),
) -> None:
    """Submit a new capture job and optionally stream progress."""

    settings = _resolve_settings(api_base)
    client = _client(settings, http2=http2)
    payload: dict[str, object] = {"url": url}
    if profile:
        payload["profile_id"] = profile
    if ocr_policy:
        payload["ocr"] = {"policy": ocr_policy}

    response = client.post("/jobs", json=payload)
    response.raise_for_status()
    job = response.json()
    console.print(f"[green]Created job {job.get('id')}[/]")
    _print_job(job)

    if watch and job.get("id"):
        console.rule(f"Streaming {job['id']}")
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
def watch(
    job_id: str = typer.Argument(..., help="Job identifier"),
    api_base: Optional[str] = typer.Option(None, help="Override API base URL"),
    interval: float = typer.Option(1.5, "--interval", help="Seconds between polls"),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON snapshots"),
    stop_on_done: bool = typer.Option(True, "--stop/--no-stop", help="Exit when terminal state reached"),
    http2: bool = typer.Option(True, "--http2/--no-http2"),
) -> None:
    """Poll `/jobs/{id}` until completion, printing snapshots."""

    settings = _resolve_settings(api_base)
    client = _client(settings, http2=http2)
    terminal_states = {"DONE", "FAILED", "CANCELLED"}

    while True:
        response = client.get(f"/jobs/{job_id}")
        response.raise_for_status()
        snapshot = response.json()
        if json_output:
            console.print_json(data=snapshot)
        else:
            _print_job(snapshot)
        state = str(snapshot.get("state", ""))
        if stop_on_done and state.upper() in terminal_states:
            break
        try:
            typer.sleep(interval)
        except KeyboardInterrupt:  # pragma: no cover
            break


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
 
