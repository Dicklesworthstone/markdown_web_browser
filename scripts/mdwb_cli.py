#!/usr/bin/env python3
"""Minimal mdwb CLI for interacting with the capture API (demo)."""

from __future__ import annotations

import json
from typing import Iterable, Tuple

import httpx
import typer
from rich.console import Console
from rich.table import Table

console = Console()
cli = typer.Typer(help="Interact with the Markdown Web Browser API")
demo_cli = typer.Typer(help="Demo commands hitting the built-in /jobs/demo endpoints.")
cli.add_typer(demo_cli, name="demo")


def _client(api_base: str, http2: bool = True) -> httpx.Client:
    timeout = httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=10.0)
    return httpx.Client(base_url=api_base, timeout=timeout, http2=http2)


def _print_job(job: dict) -> None:
    table = Table("Field", "Value", title=f"Job {job.get('id', 'unknown')}")
    for key in ("state", "url", "progress", "manifest"):
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


@cli.command()
def fetch(url: str = typer.Argument(..., help="URL to capture")) -> None:
    """Placeholder until the real /jobs endpoint lands."""

    console.print(
        "[yellow]POST /jobs is not available yet. Use `mdwb demo stream`/`links` to exercise the API until bd-3px ships.[/]"
    )


@demo_cli.command("snapshot")
def demo_snapshot(
    api_base: str = typer.Option("http://localhost:8000", help="API base URL"),
    json_output: bool = typer.Option(False, "--json", help="Print raw JSON instead of tables."),
) -> None:
    """Fetch the demo job snapshot from /jobs/demo."""

    client = _client(api_base)
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
    api_base: str = typer.Option("http://localhost:8000", help="API base URL"),
    json_output: bool = typer.Option(False, "--json", help="Print raw JSON."),
) -> None:
    """Fetch the demo links JSON."""

    client = _client(api_base)
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
    else:
        console.print(f"[bold]{event}[/]: {payload}")


@demo_cli.command("stream")
def demo_stream(
    api_base: str = typer.Option("http://localhost:8000", help="API base URL"),
    raw: bool = typer.Option(False, "--raw", help="Print raw event payloads instead of colored labels."),
) -> None:
    """Tail the demo SSE stream."""

    with httpx.Client(base_url=api_base, timeout=None) as client:
        with client.stream("GET", "/jobs/demo/stream") as response:
            response.raise_for_status()
            for event, payload in _iter_sse(response):
                if raw:
                    console.print(f"{event}\t{payload}")
                else:
                    _log_event(event, payload)


@demo_cli.command("watch")
def demo_watch(api_base: str = typer.Option("http://localhost:8000", help="API base URL")) -> None:
    """Convenience alias for `demo stream`."""

    demo_stream(api_base=api_base)


@demo_cli.command("events")
def demo_events(
    api_base: str = typer.Option("http://localhost:8000", help="API base URL"),
    output: typer.FileTextWrite = typer.Option(
        "-", "--output", "-o", help="File to append JSON events to (default stdout)."
    ),
) -> None:
    """Emit demo SSE events as JSON lines (automation-friendly)."""

    import json as jsonlib

    with httpx.Client(base_url=api_base, timeout=None) as client:
        with client.stream("GET", "/jobs/demo/stream") as response:
            response.raise_for_status()
            for event, payload in _iter_sse(response):
                jsonlib.dump({"event": event, "data": payload}, output)
                output.write("\n")
                output.flush()


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
