#!/usr/bin/env python3
"""Minimal mdwb CLI for interacting with the capture API (demo)."""

from __future__ import annotations

import json
import os
import subprocess
import time
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timedelta
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Optional, TextIO, Tuple

import httpx
import typer
from decouple import Config as DecoupleConfig, RepositoryEnv
from rich.console import Console
from rich.table import Table

_TyperOptionInfo: Any | None
_TyperOptionInfo: Any = None
try:  # pragma: no cover - typer internals
    from typer.models import OptionInfo as _TyperOptionInfo
except ImportError:  # pragma: no cover - fallback when typer internals move
    pass

console = Console()
cli = typer.Typer(help="Interact with the Markdown Web Browser API")
demo_cli = typer.Typer(help="Demo commands hitting the built-in /jobs/demo endpoints.")
cli.add_typer(demo_cli, name="demo")
jobs_cli = typer.Typer(help="Job utilities (events/watch/replay).")
cli.add_typer(jobs_cli, name="jobs")
jobs_replay_cli = typer.Typer(help="Replay helpers (manifest, future inputs).")
jobs_cli.add_typer(jobs_replay_cli, name="replay")
jobs_embeddings_cli = typer.Typer(help="Embeddings utilities (search, future helpers).")
jobs_cli.add_typer(jobs_embeddings_cli, name="embeddings")
jobs_artifacts_cli = typer.Typer(help="Download manifests/markdown/links for jobs.")
jobs_cli.add_typer(jobs_artifacts_cli, name="artifacts")
jobs_webhooks_cli = typer.Typer(help="Manage job webhooks.")
jobs_cli.add_typer(jobs_webhooks_cli, name="webhooks")
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


@contextmanager
def _client_ctx(settings: APISettings, *, http2: bool = True) -> Iterator[httpx.Client]:
    client = _client(settings, http2=http2)
    try:
        yield client
    finally:
        client.close()


def _print_job(job: dict) -> None:
    table = Table("Field", "Value", title=f"Job {job.get('id', 'unknown')}")
    for key in ("state", "url", "progress", "manifest", "warnings", "blocklist_hits"):
        value = job.get(key)
        if isinstance(value, (dict, list)):
            value = json.dumps(value, indent=2)
        table.add_row(key, str(value))
    console.print(table)


def _print_ocr_metrics(manifest: dict[str, Any], *, json_output: bool) -> None:
    batches = manifest.get("ocr_batches") or []
    quota = manifest.get("ocr_quota") or {}
    if json_output:
        console.print_json(data={"batches": batches, "quota": quota})
        return

    if quota:
        quota_table = Table("Limit", "Used", "Threshold", "Warning", title="OCR Quota")
        threshold = quota.get("threshold_ratio", 0)
        quota_table.add_row(
            str(quota.get("limit", "—")),
            str(quota.get("used", "—")),
            f"{float(threshold)*100:.0f}%",
            "⚠" if quota.get("warning_triggered") else "—",
        )
        console.print(quota_table)

    table = Table("Tile IDs", "Latency (ms)", "Status", "Attempts", "Request ID", "Payload (bytes)", title="OCR Batches")
    if not batches:
        table.add_row("—", "—", "—", "—", "—", "—")
    else:
        for batch in batches:
            tile_ids = ", ".join(batch.get("tile_ids", []))
            table.add_row(
                tile_ids or "—",
                str(batch.get("latency_ms", "—")),
                str(batch.get("status_code", "—")),
                str(batch.get("attempts", "—")),
                batch.get("request_id") or "—",
                str(batch.get("payload_bytes", "—")),
            )
    console.print(table)


def _print_links(links: Iterable[dict]) -> None:
    table = Table("Text", "Href", "Source", "Δ", title="Links")
    for row in links:
        table.add_row(row.get("text", ""), row.get("href", ""), row.get("source", ""), row.get("delta", ""))
    console.print(table)


def _print_webhooks(records: Iterable[dict[str, Any]]) -> None:
    rows = list(records)
    if not rows:
        console.print("[dim]No webhooks registered yet.[/]")
        return
    table = Table("URL", "Events", "Created", title="Webhooks")
    for row in rows:
        events = row.get("events") or []
        pretty_events = ", ".join(events) if events else "DONE, FAILED"
        created = row.get("created_at", "—")
        table.add_row(str(row.get("url", "—")), pretty_events, str(created))
    console.print(table)


def _parse_vector_input(vector: str | None, vector_file: Path | None) -> list[float]:
    """Parse embedding vector from inline JSON/whitespace or a file."""

    raw = None
    source = "--vector"
    if vector_file is not None:
        source = "--vector-file"
        raw = vector_file.read_text(encoding="utf-8")
    elif vector is not None:
        raw = vector
    if raw is None:
        raise typer.BadParameter("Provide --vector or --vector-file", param_hint="--vector")

    raw = raw.strip()
    if not raw:
        raise typer.BadParameter("Vector input is empty", param_hint=source)

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = None
    else:
        if isinstance(parsed, list):
            try:
                return [float(value) for value in parsed]
            except (TypeError, ValueError) as exc:  # pragma: no cover - defensive
                raise typer.BadParameter("Vector list must contain numbers", param_hint=source) from exc

    parts = raw.replace(",", " ").split()
    if not parts:
        raise typer.BadParameter("Vector input is empty", param_hint=source)
    try:
        return [float(part) for part in parts]
    except ValueError as exc:  # pragma: no cover - defensive
        raise typer.BadParameter("Vector input must be numeric", param_hint=source) from exc


def _print_embedding_matches(total_sections: int, matches: Iterable[dict[str, Any]]) -> None:
    table = Table("Section", "Tiles", "Similarity", "Distance", title="Embedding Matches")
    for match in matches:
        tile_start = match.get("tile_start")
        tile_end = match.get("tile_end")
        if tile_start is None and tile_end is None:
            tile_range = "—"
        else:
            start = tile_start if tile_start is not None else "?"
            end = tile_end if tile_end is not None else "?"
            tile_range = f"{start}–{end}"
        similarity = match.get("similarity")
        distance = match.get("distance")
        table.add_row(
            str(match.get("section_id", "—")),
            tile_range,
            f"{similarity:.3f}" if isinstance(similarity, (int, float)) else str(similarity),
            f"{distance:.3f}" if isinstance(distance, (int, float)) else str(distance),
        )
    console.print(table)
    console.print(f"[dim]Total sections indexed: {total_sections}[/]")


def _parse_event_hooks(values: Optional[list[str]]) -> dict[str, list[str]]:
    hooks: dict[str, list[str]] = {}
    if not values:
        return hooks
    for spec in values:
        if "=" not in spec:
            raise typer.BadParameter("Use EVENT=COMMAND syntax for --on", param_hint="--on")
        event, command = spec.split("=", 1)
        event = event.strip()
        command = command.strip()
        if not event or not command:
            raise typer.BadParameter("EVENT=COMMAND entries must be non-empty", param_hint="--on")
        hooks.setdefault(event, []).append(command)
    return hooks


def _trigger_event_hooks(entry: Mapping[str, Any], hooks: Optional[dict[str, list[str]]]) -> None:
    if not hooks:
        return
    event_name = entry.get("event")
    if not isinstance(event_name, str):
        if "snapshot" in entry:
            event_name = "snapshot"
        else:
            return

    commands: list[str] = []
    commands.extend(hooks.get(event_name, []))

    if event_name == "snapshot":
        snapshot = entry.get("snapshot")
        if isinstance(snapshot, dict):
            state = snapshot.get("state")
            if isinstance(state, str):
                commands.extend(hooks.get(f"state:{state}", []))

    commands.extend(hooks.get("*", []))

    if not commands:
        return

    for command in commands:
        _run_hook(command, event_name, entry)


def _run_hook(command: str, event_name: str, payload: Mapping[str, Any]) -> None:
    env = os.environ.copy()
    env["MDWB_EVENT_NAME"] = event_name
    try:
        env["MDWB_EVENT_PAYLOAD"] = json.dumps(payload)
    except Exception:  # pragma: no cover - defensive
        env["MDWB_EVENT_PAYLOAD"] = str(payload)
    try:
        subprocess.run(command, shell=True, check=False, env=env)
    except Exception as exc:  # pragma: no cover - defensive
        console.print(f"[yellow]Hook command '{command}' failed: {exc}[/]")


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


def _stream_job(
    job_id: str,
    settings: APISettings,
    *,
    raw: bool,
    hooks: Optional[dict[str, list[str]]] = None,
) -> None:
    with httpx.Client(base_url=settings.base_url, timeout=None, headers=_auth_headers(settings)) as client:
        with client.stream("GET", f"/jobs/{job_id}/stream") as response:
            response.raise_for_status()
            for event, payload in _iter_sse(response):
                if raw:
                    console.print(f"{event}\t{payload}")
                else:
                    _log_event(event, payload)
                if hooks:
                    entry_payload: Mapping[str, Any]
                    try:
                        entry_payload = json.loads(payload)
                    except json.JSONDecodeError:
                        entry_payload = {"raw": payload}
                    _trigger_event_hooks({"event": event, "payload": entry_payload}, hooks)


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
    hooks: Optional[dict[str, list[str]]] = None,
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
        _trigger_event_hooks(entry, hooks)
        snapshot = entry.get("snapshot")
        if isinstance(snapshot, dict):
            _render_snapshot(snapshot)
            state = snapshot.get("state")
            if follow and isinstance(state, str) and state.upper() in terminal_states:
                break
        else:
            console.print_json(data=entry)


def _watch_events_with_fallback(
    job_id: str,
    settings: APISettings,
    *,
    cursor: str | None,
    follow: bool,
    interval: float,
    raw: bool,
    hooks: Optional[dict[str, list[str]]] = None,
) -> None:
    """Stream `/jobs/{id}/events`, falling back to SSE when unavailable."""

    try:
        _watch_job_events_pretty(
            job_id,
            settings,
            cursor=cursor,
            follow=follow,
            interval=interval,
            raw=raw,
            hooks=hooks,
        )
    except httpx.HTTPError as exc:
        console.print(f"[yellow]Events feed unavailable ({exc}); falling back to SSE stream.[/]")
        _stream_job(job_id, settings, raw=raw, hooks=hooks)


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
        blocklist_hits = manifest.get("blocklist_hits")
        if blocklist_hits:
            blocklist_summary = _format_blocklist(blocklist_hits)
            if blocklist_summary != "-":
                _log_event("log", f"blocklist: {blocklist_summary}")
        sweep_summary = _format_sweep_summary(
            {
                "sweep_stats": manifest.get("sweep_stats"),
                "overlap_match_ratio": manifest.get("overlap_match_ratio"),
            }
        )
        if sweep_summary != "-":
            _log_event("log", f"sweep: {sweep_summary}")
        validation_summary = _format_validation_summary(manifest.get("validation_failures"))
        if validation_summary != "-":
            _log_event("log", f"validation: {validation_summary}")
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
    webhook_url: Optional[list[str]] = typer.Option(
        None,
        "--webhook-url",
        help="Register this webhook URL immediately after job creation (repeat to add multiple).",
    ),
    webhook_event: Optional[list[str]] = typer.Option(
        None,
        "--webhook-event",
        help="States that trigger the registered webhooks (defaults to DONE/FAILED).",
    ),
    on_event: Optional[list[str]] = typer.Option(
        None,
        "--on",
        help="When using --watch, run COMMAND whenever EVENT fires (format EVENT=COMMAND). Repeat flag for multiple hooks.",
    ),
) -> None:
    """Submit a new capture job and optionally stream progress."""

    if webhook_event and not webhook_url:
        raise typer.BadParameter("Use --webhook-event together with --webhook-url.", param_hint="--webhook-event")

    settings = _resolve_settings(api_base)
    client = _client(settings, http2=http2)
    payload: dict[str, object] = {"url": url}
    if profile:
        payload["profile_id"] = profile
    if ocr_policy:
        payload["ocr"] = {"policy": ocr_policy}

    hook_map = {}
    if on_event:
        hook_map = _parse_event_hooks(on_event)
        if not watch:
            raise typer.BadParameter("--on requires --watch so hooks have events to monitor.", param_hint="--on")

    response = client.post("/jobs", json=payload)
    response.raise_for_status()
    job = response.json()
    console.print(f"[green]Created job {job.get('id')}[/]")
    _print_job(job)

    job_id = job.get("id")
    if job_id and webhook_url:
        _register_webhooks_for_job(
            client,
            job_id,
            urls=webhook_url,
            events=webhook_event,
        )

    if watch and job_id:
        console.rule(f"Streaming {job_id}")
        _watch_events_with_fallback(
            job_id,
            settings,
            cursor=None,
            follow=True,
            interval=2.0,
            raw=raw,
            hooks=hook_map or None,
        )


@cli.command()
def show(
    job_id: str = typer.Argument(..., help="Job identifier"),
    api_base: Optional[str] = typer.Option(None, help="Override API base URL"),
    http2: bool = typer.Option(True, "--http2/--no-http2"),
    ocr_metrics: bool = typer.Option(False, "--ocr-metrics/--no-ocr-metrics", help="Print OCR batch telemetry when available."),
) -> None:
    """Display the latest snapshot for a real job."""

    settings = _resolve_settings(api_base)
    snapshot = _fetch_job_snapshot(job_id, settings, http2=http2)
    _print_job(snapshot)
    if ocr_metrics:
        manifest = snapshot.get("manifest")
        if not manifest:
            console.print("[yellow]Manifest not available yet; try again after the job completes.[/]")
        else:
            _print_ocr_metrics(manifest, json_output=False)


def _fetch_job_snapshot(job_id: str, settings: APISettings, *, http2: bool = True) -> dict[str, Any]:
    client = _client(settings, http2=http2)
    try:
        response = client.get(f"/jobs/{job_id}")
        response.raise_for_status()
        return response.json()
    finally:
        client.close()


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
def diag(
    job_id: str = typer.Argument(..., help="Job identifier"),
    api_base: Optional[str] = typer.Option(None, help="Override API base URL"),
    json_output: bool = typer.Option(False, "--json/--no-json", help="Emit JSON payload instead of tables."),
) -> None:
    """Print manifest/environment details for a job."""

    settings = _resolve_settings(api_base)
    client = _client(settings)
    response = client.get(f"/jobs/{job_id}")
    if response.status_code == 404:
        detail = _extract_detail(response) or f"Job {job_id} not found."
        console.print(f"[red]{detail}[/]")
        raise typer.Exit(1)
    response.raise_for_status()
    snapshot = response.json()

    manifest = snapshot.get("manifest")
    manifest_source = "snapshot"
    manifest_error: str | None = None
    if not manifest:
        manifest_response = client.get(f"/jobs/{job_id}/manifest.json")
        if manifest_response.status_code < 400:
            manifest = manifest_response.json()
            manifest_source = "manifest.json"
        else:
            manifest_error = _extract_detail(manifest_response) or "Manifest unavailable"

    payload = {
        "snapshot": snapshot,
        "manifest": manifest,
        "manifest_source": manifest_source,
        "manifest_error": manifest_error,
    }
    if json_output:
        console.print_json(data=payload)
        return
    _print_diag_report(snapshot, manifest, manifest_source, manifest_error)


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
    on_event: Optional[list[str]] = typer.Option(
        None,
        "--on",
        help="Run COMMAND when EVENT fires (format EVENT=COMMAND). Repeat flag to add multiple hooks.",
    ),
) -> None:
    """Stream `/jobs/{id}/events` with optional fallback to SSE."""

    settings = _resolve_settings(api_base)
    hook_map = _parse_event_hooks(on_event)
    _watch_events_with_fallback(
        job_id,
        settings,
        cursor=since,
        follow=follow,
        interval=interval,
        raw=raw,
        hooks=hook_map or None,
    )


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
        return
    if event == "progress":
        console.print(f"[magenta]{payload}[/]")
        return
    if event in {"warning", "warnings"}:
        console.print(f"[red]warning[/]: {payload}")
        return
    if event == "blocklist":
        data = _parse_json_payload(payload)
        if isinstance(data, dict):
            summary = ", ".join(f"{sel}:{count}" for sel, count in data.items()) or "no hits"
            console.print(f"[yellow]blocklist[/]: {summary}")
            return
    if event == "sweep":
        data = _parse_json_payload(payload) or {}
        stats = data.get("sweep_stats") or {}
        ratio = data.get("overlap_match_ratio")
        parts = []
        if ratio is not None:
            parts.append(f"ratio {float(ratio):.2f}")
        if stats.get("shrink_events"):
            parts.append(f"shrink {stats['shrink_events']}")
        if stats.get("retry_attempts"):
            parts.append(f"retries {stats['retry_attempts']}")
        summary = ", ".join(parts) or "no sweep data"
        console.print(f"[blue]sweep[/]: {summary}")
        return
    if event == "validation":
        data = _parse_json_payload(payload)
        if isinstance(data, list) and data:
            console.print(f"[red]validation[/]: {'; '.join(map(str, data))}")
            return
        console.print("[green]validation[/]: none")
        return
    console.print(f"[bold]{event}[/]: {payload}")


def _extract_detail(response) -> str | None:  # noqa: ANN001
    try:
        data = response.json()
    except Exception:  # pragma: no cover - defensive
        return getattr(response, "text", None)
    if isinstance(data, dict):
        return data.get("detail") or data.get("error")
    return getattr(response, "text", None)


def _option_value(value):  # noqa: ANN001
    if _TyperOptionInfo and isinstance(value, _TyperOptionInfo):
        return value.default
    return value


def _delete_job_webhooks(
    client: httpx.Client,
    job_id: str,
    *,
    webhook_id: int | None,
    url: str | None,
) -> tuple[httpx.Response, dict[str, Any]]:
    payload: dict[str, Any] = {}
    if webhook_id is not None:
        payload["id"] = webhook_id
    if url:
        payload["url"] = url
    response = client.request("DELETE", f"/jobs/{job_id}/webhooks", json=payload or None)
    return response, payload


def _parse_json_payload(payload: str) -> Any:
    try:
        return json.loads(payload)
    except Exception:  # pragma: no cover - best effort
        return None


def _write_text_output(content: str, path: str | None, *, description: str) -> None:
    if path:
        out_path = Path(path)
        out_path.write_text(content, encoding="utf-8")
        console.print(f"[green]Saved {description} to {out_path}[/]")
    else:
        console.print(content)


def _write_binary_output(content: bytes, path: str | None, *, description: str) -> None:
    if path:
        out_path = Path(path)
        out_path.write_bytes(content)
        console.print(f"[green]Saved {description} to {out_path}[/]")
    else:
        console.print(content.decode("utf-8", errors="ignore"))


def _download_bundle(job_id: str, api_base: Optional[str], out: Optional[str]) -> None:
    settings = _resolve_settings(api_base)
    client = _client(settings)
    response = client.get(f"/jobs/{job_id}/artifact/bundle.tar.zst")
    if response.status_code == 404:
        console.print(f"[red]Job {job_id} or bundle not found.[/]")
        raise typer.Exit(code=1)
    response.raise_for_status()
    target = out or f"{job_id}-bundle.tar.zst"
    _write_binary_output(response.content, target, description="bundle")


def _register_webhooks_for_job(
    client: httpx.Client,
    job_id: str,
    *,
    urls: list[str],
    events: Optional[list[str]],
) -> None:
    if not urls:
        return
    successes = 0
    for url in urls:
        payload: dict[str, Any] = {"url": url}
        if events:
            payload["events"] = events
        response = client.post(f"/jobs/{job_id}/webhooks", json=payload)
        if response.status_code >= 400:
            detail = _extract_detail(response) or response.text or "unknown error"
            console.print(f"[red]Failed to register webhook {url}: {detail}[/]")
            continue
        successes += 1
    if successes:
        console.print(f"[green]Registered {successes} webhook(s) for {job_id}.[/]")


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


def _format_progress_text(progress: Any) -> str:
    if not isinstance(progress, dict):
        return "-"
    done = progress.get("done")
    total = progress.get("total")
    if done is None and total is None:
        return "-"
    if done is None:
        return f"? / {total}"
    if total is None:
        return f"{done} / ?"
    return f"{done} / {total}"


def _print_diag_report(
    snapshot: dict[str, Any],
    manifest: dict[str, Any] | None,
    manifest_source: str,
    manifest_error: str | None,
) -> None:
    summary = Table("Field", "Value", title=f"Job {snapshot.get('id', 'unknown')}")
    summary.add_row("URL", snapshot.get("url", "—"))
    summary.add_row("State", str(snapshot.get("state", "—")))
    summary.add_row("Progress", _format_progress_text(snapshot.get("progress")))
    summary.add_row("Manifest Path", snapshot.get("manifest_path") or "—")
    summary.add_row("Manifest Source", manifest_source if manifest else "not available")
    if manifest_error:
        summary.add_row("Manifest Error", manifest_error)
    if snapshot.get("error"):
        summary.add_row("Error", str(snapshot.get("error")))
    console.print(summary)

    env = (manifest or {}).get("environment")
    if isinstance(env, dict):
        env_table = Table("Field", "Value", title="Environment")
        env_table.add_row("CfT", f"{env.get('cft_label', '—')} / {env.get('cft_version', '—')}")
        env_table.add_row("Playwright", f"{env.get('playwright_channel', '—')} / {env.get('playwright_version', '—')}")
        env_table.add_row("Browser Transport", env.get("browser_transport", "—"))
        viewport = env.get("viewport") or {}
        env_table.add_row(
            "Viewport",
            f"{viewport.get('width', '—')}×{viewport.get('height', '—')} @ {viewport.get('device_scale_factor', '—')}x",
        )
        env_table.add_row("Screenshot Style Hash", env.get("screenshot_style_hash", "—"))
        console.print(env_table)

    timings = (manifest or {}).get("timings")
    if isinstance(timings, dict):
        timings_table = Table("Stage", "Milliseconds", title="Timings")
        timings_table.add_row("Capture", str(timings.get("capture_ms", "—")))
        timings_table.add_row("OCR", str(timings.get("ocr_ms", "—")))
        timings_table.add_row("Stitch", str(timings.get("stitch_ms", "—")))
        timings_table.add_row("Total", str(timings.get("total_ms", "—")))
        console.print(timings_table)

    warnings = (manifest or {}).get("warnings") or []
    if warnings:
        warn_table = Table("Code", "Count", "Threshold", "Message", title="Warnings")
        for entry in warnings:
            if not isinstance(entry, dict):
                warn_table.add_row(str(entry), "-", "-", "-")
                continue
            warn_table.add_row(
                str(entry.get("code", "—")),
                str(entry.get("count", "—")),
                str(entry.get("threshold", "—")),
                entry.get("message", "—"),
            )
        console.print(warn_table)
    else:
        console.print("[dim]No manifest warnings recorded.[/]")

    blocklist_hits = (manifest or {}).get("blocklist_hits")
    console.print(f"Blocklist Hits: {_format_blocklist(blocklist_hits)}")


def _follow_warning_log(path: Path, *, json_output: bool, interval: float) -> None:
    handle: TextIO | None = None
    last_inode: int | None = None
    try:
        while True:
            if handle is None:
                try:
                    handle = path.open("r", encoding="utf-8")
                    handle.seek(0, os.SEEK_END)
                    last_inode = os.fstat(handle.fileno()).st_ino
                    console.print(f"[dim]Now tailing {path}…[/]")
                except FileNotFoundError:
                    console.print(f"[dim]{path} not found; waiting…[/]")
                    time.sleep(interval)
                    continue
            line = handle.readline()
            if not line:
                if _log_rotated_or_truncated(handle, path, last_inode):
                    handle.close()
                    handle = None
                    last_inode = None
                    continue
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
    finally:
        if handle:
            handle.close()


def _log_rotated_or_truncated(handle: TextIO, path: Path, last_inode: int | None) -> bool:
    try:
        stat = path.stat()
    except FileNotFoundError:
        console.print(f"[dim]{path} removed; waiting for recreation…[/]")
        return True
    try:
        current_pos = handle.tell()
    except (OSError, ValueError):
        return True
    if stat.st_size < current_pos:
        console.print(f"[dim]{path} truncated; reopening…[/]")
        return True
    if last_inode is not None and stat.st_ino != last_inode:
        console.print(f"[dim]{path} rotated; reopening…[/]")
        return True
    return False


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
    if target_path.exists():
        records = _load_warning_records(target_path, count)
        _print_warning_records(records, json_output=json_output)
    else:
        console.print(f"[yellow]Warning log not found at {target_path}[/]")

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
 


@jobs_webhooks_cli.command("list")
def jobs_webhooks_list(
    job_id: str = typer.Argument(..., help="Job identifier"),
    api_base: Optional[str] = typer.Option(None, help="Override API base URL"),
    json_output: bool = typer.Option(False, "--json", help="Emit structured JSON instead of a table."),
) -> None:
    """List registered webhooks for a job."""

    settings = _resolve_settings(api_base)
    client = _client(settings)
    response = client.get(f"/jobs/{job_id}/webhooks")
    if response.status_code == 404:
        detail = _extract_detail(response) or f"Job {job_id} not found."
        _print_webhook_list_error(detail, job_id, json_output)
    response.raise_for_status()
    data = response.json()
    if json_output:
        console.print_json(data={"status": "ok", "job_id": job_id, "webhooks": data})
        return
    _print_webhooks(data)


@jobs_webhooks_cli.command("add")
def jobs_webhooks_add(
    job_id: str = typer.Argument(..., help="Job identifier"),
    url: str = typer.Argument(..., help="Webhook endpoint to invoke on state changes."),
    api_base: Optional[str] = typer.Option(None, help="Override API base URL"),
    event: list[str] = typer.Option(
        None,
        "--event",
        "-e",
        help="Job state to trigger the webhook (repeat flag for multiple states). Defaults to DONE+FAILED.",
    ),
    json_output: bool = typer.Option(False, "--json/--no-json", help="Emit JSON payload instead of text."),
) -> None:
    """Register a webhook for a job."""

    settings = _resolve_settings(api_base)
    client = _client(settings)
    payload: dict[str, Any] = {"url": url}
    if event:
        payload["events"] = event
    response = client.post(f"/jobs/{job_id}/webhooks", json=payload)
    if response.status_code in {400, 404}:
        detail = _extract_detail(response) or (
            "Job not found." if response.status_code == 404 else "Webhook rejected."
        )
        _print_webhook_add_error(detail, job_id, json_output)
    response.raise_for_status()
    body = response.json()
    if json_output:
        console.print_json(data={"status": "ok", **body})
        return
    console.print(f"[green]Registered webhook for {job_id} ({url}).[/] Trigger states: {', '.join(event) if event else 'DONE, FAILED'}.")


@jobs_webhooks_cli.command("delete")
def jobs_webhooks_delete(
    job_id: str = typer.Argument(..., help="Job identifier"),
    api_base: Optional[str] = typer.Option(None, help="Override API base URL"),
    webhook_id: Optional[int] = typer.Option(None, "--id", help="Webhook record id"),
    url: Optional[str] = typer.Option(None, "--url", help="Webhook URL to delete"),
    json_output: bool = typer.Option(False, "--json/--no-json", help="Emit JSON payload instead of text."),
) -> None:
    """Remove a webhook subscription from a job."""

    webhook_id = _option_value(webhook_id)
    url = _option_value(url)
    if webhook_id is None and not url:
        raise typer.BadParameter("Provide --id or --url to delete a webhook")

    settings = _resolve_settings(api_base)
    client = _client(settings)
    response, payload = _delete_job_webhooks(client, job_id, webhook_id=webhook_id, url=url)
    if response.status_code == 404:
        detail = _extract_detail(response) or "Webhook or job not found."
        _print_delete_error(detail, job_id, json_output)
        return
    if response.status_code == 400:
        detail = _extract_detail(response) or "Webhook deletion rejected."
        _print_delete_error(detail, job_id, json_output)
        return
    response.raise_for_status()
    body = response.json()
    if json_output:
        console.print_json(data={"status": "ok", **body, "request": payload})
        return
    console.print(f"[green]Deleted {body.get('deleted', 0)} webhook(s) from {job_id}.[/]")


@jobs_artifacts_cli.command("manifest")
def jobs_manifest(
    job_id: str = typer.Argument(..., help="Job identifier"),
    api_base: Optional[str] = typer.Option(None, help="Override API base URL"),
    out: Optional[str] = typer.Option(None, "--out", help="Write manifest JSON to this path"),
    pretty: bool = typer.Option(True, "--pretty/--raw", help="Pretty-print JSON before writing."),
) -> None:
    settings = _resolve_settings(api_base)
    client = _client(settings)
    response = client.get(f"/jobs/{job_id}/manifest.json")
    if response.status_code == 404:
        console.print(f"[red]Job {job_id} not found.[/]")
        raise typer.Exit(code=1)
    response.raise_for_status()
    text = response.text
    if pretty:
        parsed = _parse_json_payload(text)
        if parsed is not None:
            text = json.dumps(parsed, indent=2)
    _write_text_output(text, out, description="manifest")


@jobs_artifacts_cli.command("markdown")
def jobs_markdown(
    job_id: str = typer.Argument(..., help="Job identifier"),
    api_base: Optional[str] = typer.Option(None, help="Override API base URL"),
    out: Optional[str] = typer.Option(None, "--out", help="Write markdown to this path"),
) -> None:
    settings = _resolve_settings(api_base)
    client = _client(settings)
    response = client.get(f"/jobs/{job_id}/result.md")
    if response.status_code == 404:
        console.print(f"[red]Job {job_id} not found.[/]")
        raise typer.Exit(code=1)
    response.raise_for_status()
    _write_text_output(response.text, out, description="markdown")


@jobs_artifacts_cli.command("links")
def jobs_links(
    job_id: str = typer.Argument(..., help="Job identifier"),
    api_base: Optional[str] = typer.Option(None, help="Override API base URL"),
    out: Optional[str] = typer.Option(None, "--out", help="Write links JSON to this path"),
    pretty: bool = typer.Option(True, "--pretty/--raw", help="Pretty-print JSON before writing."),
) -> None:
    settings = _resolve_settings(api_base)
    client = _client(settings)
    response = client.get(f"/jobs/{job_id}/links.json")
    if response.status_code == 404:
        console.print(f"[red]Job {job_id} not found.[/]")
        raise typer.Exit(code=1)
    response.raise_for_status()
    text = response.text
    if pretty:
        parsed = _parse_json_payload(text)
        if parsed is not None:
            text = json.dumps(parsed, indent=2)
    _write_text_output(text, out, description="links")


@jobs_artifacts_cli.command("bundle")
def jobs_bundle(
    job_id: str = typer.Argument(..., help="Job identifier"),
    api_base: Optional[str] = typer.Option(None, help="Override API base URL"),
    out: Optional[str] = typer.Option(
        None,
        "--out",
        help="Write tar bundle to this path (defaults to <job-id>-bundle.tar.zst).",
    ),
) -> None:
    _download_bundle(job_id, api_base, out)


@jobs_cli.command("bundle")
def jobs_bundle_alias(
    job_id: str = typer.Argument(..., help="Job identifier"),
    api_base: Optional[str] = typer.Option(None, help="Override API base URL"),
    out: Optional[str] = typer.Option(
        None,
        "--out",
        help="Write tar bundle to this path (defaults to <job-id>-bundle.tar.zst).",
    ),
) -> None:
    """Download the tar bundle (tiles, manifest, markdown, links) for a job."""

    _download_bundle(job_id, api_base, out)


@jobs_cli.command("ocr-metrics")
def jobs_ocr_metrics(
    job_id: str = typer.Argument(..., help="Job identifier"),
    api_base: Optional[str] = typer.Option(None, help="Override API base URL"),
    json_output: bool = typer.Option(False, "--json", help="Emit OCR telemetry as JSON"),
) -> None:
    """Show OCR batch latency + quota telemetry for a job."""

    settings = _resolve_settings(api_base)
    snapshot = _fetch_job_snapshot(job_id, settings)
    manifest = snapshot.get("manifest")
    if not manifest:
        raise typer.BadParameter("Manifest not available yet; rerun once the job completes.")
    _print_ocr_metrics(manifest, json_output=json_output)


@jobs_replay_cli.command("manifest")
def jobs_replay_manifest(
    manifest_path: Path = typer.Argument(
        ...,
        exists=True,
        dir_okay=False,
        readable=True,
        resolve_path=True,
        help="Path to manifest.json",
    ),
    api_base: Optional[str] = typer.Option(None, help="Override API base URL"),
    http2: bool = typer.Option(True, "--http2/--no-http2", help="Use HTTP/2 for the replay request."),
    json_output: bool = typer.Option(False, "--json/--no-json", help="Emit JSON instead of text output."),
) -> None:
    """Replay a capture manifest via POST /replay."""

    try:
        manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(f"Manifest is not valid JSON ({exc})", param_hint="manifest_path") from exc

    settings = _resolve_settings(api_base)
    client = _client(settings, http2=http2)
    response = client.post("/replay", json={"manifest": manifest_payload})
    if response.status_code >= 400:
        detail = _extract_detail(response) or response.text or f"HTTP {response.status_code}"
        if json_output:
            console.print_json(data={"status": "error", "detail": detail})
        else:
            console.print(f"[red]Replay failed:[/] {detail}")
        raise typer.Exit(1)

    job = response.json()
    if json_output:
        console.print_json(data={"status": "ok", "job": job})
        return
    console.print("[green]Replay submitted.[/]")
    _print_job(job)


@jobs_embeddings_cli.command("search")
def jobs_embeddings_search(
    job_id: str = typer.Argument(..., help="Job identifier"),
    vector: Optional[str] = typer.Option(
        None,
        "--vector",
        help="Inline JSON/whitespace-separated floats representing the embedding vector.",
    ),
    vector_file: Optional[Path] = typer.Option(
        None,
        "--vector-file",
        exists=True,
        dir_okay=False,
        readable=True,
        resolve_path=True,
        help="Path to a JSON/whitespace vector file.",
    ),
    top_k: int = typer.Option(5, "--top-k", "-k", help="Number of sections to return."),
    api_base: Optional[str] = typer.Option(None, help="Override API base URL"),
    json_output: bool = typer.Option(False, "--json/--no-json", help="Emit JSON instead of a table."),
) -> None:
    """Search section embeddings for a job."""

    vector_values = _parse_vector_input(vector, vector_file)
    if top_k < 1:
        raise typer.BadParameter("top-k must be at least 1", param_hint="--top-k")

    settings = _resolve_settings(api_base)
    client = _client(settings)
    response = client.post(
        f"/jobs/{job_id}/embeddings/search",
        json={"vector": vector_values, "top_k": top_k},
    )
    if response.status_code >= 400:
        detail = _extract_detail(response) or response.text or f"HTTP {response.status_code}"
        console.print(f"[red]Embeddings search failed:[/] {detail}")
        raise typer.Exit(1)
    data = response.json()
    if json_output:
        console.print_json(data=data)
        return
    _print_embedding_matches(data.get("total_sections", 0), data.get("matches", []))


def _print_delete_error(detail: str, job_id: str, json_output: bool) -> None:
    if json_output:
        console.print_json(data={"status": "error", "job_id": job_id, "detail": detail})
    else:
        console.print(f"[red]{detail}[/]")
    raise typer.Exit(1)


def _print_webhook_add_error(detail: str, job_id: str, json_output: bool) -> None:
    if json_output:
        console.print_json(data={"status": "error", "job_id": job_id, "detail": detail})
    else:
        console.print(f"[red]{detail}[/]")
    raise typer.Exit(1)


def _print_webhook_list_error(detail: str, job_id: str, json_output: bool) -> None:
    if json_output:
        console.print_json(data={"status": "error", "job_id": job_id, "detail": detail})
    else:
        console.print(f"[red]{detail}[/]")
    raise typer.Exit(1)
