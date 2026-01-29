#!/usr/bin/env python3
"""Minimal mdwb CLI for interacting with the capture API (demo)."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import shutil
import shlex
import subprocess
import time
from collections import Counter, deque
from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import (
    Any,
    Callable,
    ContextManager,
    Iterable,
    Iterator,
    Mapping,
    Literal,
    Optional,
    Sequence,
    TextIO,
    Tuple,
)

import httpx
import sys
import typer
from decouple import Config as DecoupleConfig, RepositoryEnv
from rich.console import Console
from rich.table import Table
import zstandard as zstd

_DEFAULT_TIMEOUT = object()

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
resume_cli = typer.Typer(help="Inspect resume state (done_flags/work_index).")
cli.add_typer(resume_cli, name="resume")
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

OutputFormat = Literal["json", "toon"]


def _resolve_output_format(
    json_output: bool, format_option: Optional[str]
) -> Optional[OutputFormat]:
    """Resolve output format with precedence: CLI flag > MWB_OUTPUT_FORMAT > TOON_DEFAULT_FORMAT > default.

    Args:
        json_output: If True, treat as --json flag (returns "json")
        format_option: Explicit --format value from CLI

    Returns:
        Output format ("json" or "toon") or None if no machine format requested
    """
    # CLI flag has highest precedence
    if format_option is not None:
        normalized = format_option.strip().lower()
        if normalized in {"json", "toon"}:
            return normalized  # type: ignore[return-value]
        raise typer.BadParameter("format must be 'json' or 'toon'")

    # --json flag
    if json_output:
        return "json"

    # Check environment variables in order of precedence
    env_format = os.environ.get("MWB_OUTPUT_FORMAT", "").strip().lower()
    if env_format in {"json", "toon"}:
        return env_format  # type: ignore[return-value]

    env_format = os.environ.get("TOON_DEFAULT_FORMAT", "").strip().lower()
    if env_format in {"json", "toon"}:
        return env_format  # type: ignore[return-value]

    return None


def _toon_available() -> bool:
    return shutil.which("tru") is not None


def _encode_toon(payload: str) -> str:
    result = subprocess.run(
        ["tru", "--encode"], input=payload, text=True, capture_output=True, check=True
    )
    return result.stdout


def _emit_machine_payload(
    data: Any, *, output_format: OutputFormat, show_stats: bool = False
) -> None:
    """Emit data in the specified machine-readable format.

    Args:
        data: Data to serialize
        output_format: "json" or "toon"
        show_stats: If True, print token savings comparison to stderr
    """
    json_payload = json.dumps(data, indent=2)
    json_bytes = len(json_payload.encode("utf-8"))

    if output_format == "json":
        # Show potential TOON savings if stats requested
        if show_stats and _toon_available():
            try:
                toon_payload = _encode_toon(json_payload)
                toon_bytes = len(toon_payload.encode("utf-8"))
                savings = 100 - (toon_bytes * 100 // json_bytes) if json_bytes > 0 else 0
                typer.echo(
                    f"[mdwb-toon] JSON: {json_bytes} bytes, TOON would be: {toon_bytes} bytes ({savings}% potential savings)",
                    err=True,
                )
            except subprocess.CalledProcessError:
                typer.echo(f"[mdwb-toon] JSON: {json_bytes} bytes (TOON unavailable for comparison)", err=True)
        elif show_stats:
            typer.echo(f"[mdwb-toon] JSON: {json_bytes} bytes (TOON unavailable for comparison)", err=True)
        console.print_json(data=data)
        return

    # TOON format
    if not _toon_available():
        typer.echo("warning: tru not available, falling back to JSON", err=True)
        if show_stats:
            typer.echo(f"[mdwb-toon] JSON: {json_bytes} bytes (TOON unavailable)", err=True)
        console.print_json(data=data)
        return
    try:
        toon_payload = _encode_toon(json_payload)
    except subprocess.CalledProcessError:
        typer.echo("warning: tru --encode failed, falling back to JSON", err=True)
        if show_stats:
            typer.echo(f"[mdwb-toon] JSON: {json_bytes} bytes (TOON encoding failed)", err=True)
        console.print_json(data=data)
        return

    # Show stats for TOON mode
    if show_stats:
        toon_bytes = len(toon_payload.encode("utf-8"))
        savings = 100 - (toon_bytes * 100 // json_bytes) if json_bytes > 0 else 0
        typer.echo(f"[mdwb-toon] JSON: {json_bytes} bytes, TOON: {toon_bytes} bytes ({savings}% savings)", err=True)

    if not toon_payload.endswith("\n"):
        toon_payload += "\n"
    sys.stdout.write(toon_payload)


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
    return APISettings(
        base_url="http://localhost:8000", api_key=None, warning_log_path=Path("ops/warnings.jsonl")
    )


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


def _client(
    settings: APISettings,
    http2: bool = True,
    *,
    timeout: httpx.Timeout | float | None | object = _DEFAULT_TIMEOUT,
) -> httpx.Client:
    if timeout is _DEFAULT_TIMEOUT:
        effective_timeout: httpx.Timeout | float | None = httpx.Timeout(
            connect=10.0,
            read=60.0,
            write=30.0,
            pool=10.0,
        )
    else:
        effective_timeout = timeout
    return httpx.Client(
        base_url=settings.base_url,
        timeout=effective_timeout,
        http2=http2,
        headers=_auth_headers(settings),
    )


def _client_ctx(
    settings: APISettings,
    *,
    http2: bool = True,
    timeout: httpx.Timeout | float | None | object = _DEFAULT_TIMEOUT,
) -> ContextManager[httpx.Client]:
    @contextmanager
    def _ctx() -> Iterator[httpx.Client]:
        client = _client(settings, http2=http2, timeout=timeout)
        try:
            yield client
        finally:
            client.close()

    return _ctx()


def _client_ctx_or_shared(
    shared: httpx.Client | None,
    settings: APISettings,
    *,
    http2: bool = True,
    timeout: httpx.Timeout | float | None | object = _DEFAULT_TIMEOUT,
) -> ContextManager[httpx.Client]:
    if shared is not None:
        return nullcontext(shared)
    return _client_ctx(settings, http2=http2, timeout=timeout)


def _resume_hash(identifier: str) -> str:
    return hashlib.sha256(identifier.encode("utf-8")).hexdigest()[:32]


class ResumeManager:
    """Tracks completed inputs via done_flags + optional work_index CSV."""

    def __init__(
        self,
        root: Path,
        *,
        index_path: Optional[Path] = None,
        done_dir: Optional[Path] = None,
    ) -> None:
        self.root = root
        self.index_path = index_path or (root / "work_index_list.csv.zst")
        self.done_dir = done_dir or (root / "done_flags")
        self._index_cache: dict[str, set[str]] | None = None
        self._done_cache: set[str] | None = None
        self._done_entries_cache: dict[str, str | None] | None = None

    def status(self) -> tuple[int, Optional[int]]:
        mapping = self._load_index()
        done_hashes = self._done_hashes()
        if mapping is None:
            return len(done_hashes), None
        total = sum(len(entries) for entries in mapping.values())
        done = sum(len(mapping.get(h, ())) for h in done_hashes if h in mapping)
        missing = len([h for h in done_hashes if h not in mapping])
        done += missing
        total += missing
        return done, total

    def list_entries(self, limit: Optional[int] = None) -> list[str]:
        return self.list_completed_entries(limit)

    def list_completed_entries(self, limit: Optional[int] = None) -> list[str]:
        mapping = self._load_index()
        done_hashes = self._done_hashes()
        done_entries = self._done_entries()
        if mapping:
            completed: list[str] = []
            for group_hash, entries in mapping.items():
                if group_hash not in done_hashes:
                    continue
                completed.extend(sorted(entries))
            placeholders: list[str] = []
            for hash_value in sorted(done_hashes):
                if hash_value in mapping:
                    continue
                entry_value = done_entries.get(hash_value)
                placeholders.append(entry_value or f"hash:{hash_value}")
            all_entries = completed + placeholders
        else:

            def _sort_key(item: tuple[str, str | None]) -> tuple[int, str]:
                hash_value, entry_value = item
                if entry_value:
                    return (0, entry_value)
                return (1, hash_value)

            ordered = sorted(done_entries.items(), key=_sort_key)
            all_entries = [entry or f"hash:{hash_value}" for hash_value, entry in ordered]
        if limit is not None and limit > 0:
            return all_entries[:limit]
        return all_entries

    def list_pending_entries(self, limit: Optional[int] = None) -> list[str]:
        mapping = self._load_index()
        if not mapping:
            return []
        done_hashes = self._done_hashes()
        pending: list[str] = []
        for group_hash in sorted(mapping.keys()):
            if group_hash in done_hashes:
                continue
            pending.extend(sorted(mapping[group_hash]))
            if limit is not None and limit > 0 and len(pending) >= limit:
                return pending[:limit]
        if limit is not None and limit > 0:
            return pending[:limit]
        return pending

    def is_complete(self, entry: str) -> bool:
        group_hash = _resume_hash(entry)
        if group_hash not in self._done_hashes():
            return False
        mapping = self._load_index()
        if mapping is None:
            return True
        entries = mapping.get(group_hash)
        if not entries:
            return True
        return entry in entries

    def mark_complete(self, entry: str) -> None:
        group_hash = _resume_hash(entry)
        self.done_dir.mkdir(parents=True, exist_ok=True)
        flag = self.done_dir / f"done_{group_hash}.flag"
        if not flag.exists():
            payload = {"timestamp": datetime.now(timezone.utc).isoformat(), "entry": entry}
            flag.write_text(json.dumps(payload), encoding="utf-8")
            self._done_cache = None
            self._done_entries_cache = None

    def _done_hashes(self) -> set[str]:
        if self._done_cache is not None:
            return self._done_cache
        hashes: set[str] = set()
        entries: dict[str, str | None] = {}
        if self.done_dir.exists():
            for child in self.done_dir.iterdir():
                if not child.is_file():
                    continue
                name = child.name
                if name.startswith("done_") and name.endswith(".flag"):
                    hash_value = name[len("done_") : -len(".flag")]
                    hashes.add(hash_value)
                    entries.setdefault(hash_value, self._read_flag_entry(child))
        self._done_cache = hashes
        self._done_entries_cache = entries
        return hashes

    def _done_entries(self) -> dict[str, str | None]:
        if self._done_entries_cache is None:
            self._done_hashes()
        return self._done_entries_cache or {}

    def _read_flag_entry(self, flag_path: Path) -> str | None:
        try:
            raw = flag_path.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if not raw:
            return None
        if raw.startswith("{"):
            try:
                payload = json.loads(raw)
                entry = payload.get("entry")
                if isinstance(entry, str) and entry.strip():
                    return entry.strip()
            except json.JSONDecodeError:
                return None
            return None
        lines = raw.splitlines()
        if len(lines) >= 2:
            candidate = lines[1].strip()
            return candidate or None
        return None

    def _load_index(self) -> dict[str, set[str]] | None:
        if self._index_cache is not None:
            return self._index_cache
        if not self.index_path.exists():
            return None
        mapping: dict[str, set[str]] = {}
        try:
            dctx = zstd.ZstdDecompressor()
            with self.index_path.open("rb") as compressed:
                with dctx.stream_reader(compressed) as reader:
                    with io.TextIOWrapper(
                        reader, encoding="utf-8", errors="replace"
                    ) as text_stream:
                        csv_reader = csv.reader(text_stream)
                        for row in csv_reader:
                            if not row:
                                continue
                            group_hash, *entries = row
                            cleaned = {value for value in entries if value}
                            if cleaned:
                                mapping[group_hash] = cleaned
        except Exception as exc:  # pragma: no cover - best effort guard
            console.print(f"[yellow]Resume index unreadable[/]: {exc}")
            return None
        self._index_cache = mapping
        return mapping


class _ProgressMeter:
    def __init__(self) -> None:
        self._start = time.monotonic()
        self._last_print: tuple[int | None, int | None] | None = None

    def describe(self, done: int | None, total: int | None) -> str | None:
        if done is None and total is None:
            return None
        key = (done, total)
        if self._last_print == key:
            return None
        self._last_print = key
        parts: list[str] = []
        if total and total > 0 and done is not None:
            pct = min(max(done / total * 100, 0.0), 100.0)
            parts.append(f"{pct:5.1f}%")
        rate = None
        elapsed = max(time.monotonic() - self._start, 1e-6)
        if done is not None and done > 0:
            rate = done / elapsed
        if rate is not None and done is not None and total is not None and total > done:
            remaining = (total - done) / rate
            parts.append(f"ETA {_format_duration_short(remaining)}")
        elif rate is not None and total is not None and done is not None and total == done:
            parts.append("ETA 0s")
        if not parts:
            return None
        return "(" + ", ".join(parts) + ")"


def _format_duration_short(seconds: float) -> str:
    seconds = max(0, int(seconds))
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    parts: list[str] = []
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    parts.append(f"{secs}s")
    return " ".join(parts)


@resume_cli.command("status")
def resume_status(
    root: Path = typer.Option(
        Path("."), "--root", "-r", help="Resume root containing done_flags/work_index."
    ),
    limit: int = typer.Option(10, min=0, help="Maximum entries to display (0 = unlimited)."),
    pending: bool = typer.Option(
        False, "--pending/--no-pending", help="Also list pending entries (from work_index)."
    ),
    json_output: bool = typer.Option(
        False, "--json/--no-json", help="Emit JSON instead of tables."
    ),
    output_format: Optional[str] = typer.Option(None, "--format", help="Emit json or toon output (env: MWB_OUTPUT_FORMAT, TOON_DEFAULT_FORMAT)."),
    stats: bool = typer.Option(False, "--stats", help="Show token savings comparison (JSON vs TOON bytes)."),
) -> None:
    """Inspect the completion state tracked by --resume."""

    root = root.resolve()
    manager = ResumeManager(root)
    review_dir = root / "done_flags_review"
    index_mapping = manager._load_index() or {}
    done_entries = manager._done_entries()
    if index_mapping:
        orphan_flags = sorted(
            hash_value for hash_value in manager._done_hashes() if hash_value not in index_mapping
        )
    else:
        orphan_flags = sorted(hash_value for hash_value, entry in done_entries.items() if not entry)
    if orphan_flags:
        review_dir.mkdir(parents=True, exist_ok=True)
        for hash_value in orphan_flags:
            flag_path = manager.done_dir / f"done_{hash_value}.flag"
            marker = review_dir / f"{hash_value}.flag"
            if flag_path.exists() and not marker.exists():
                marker.write_text(flag_path.read_text(), encoding="utf-8")
    orphan_count = len(orphan_flags)
    done, total = manager.status()
    entry_limit = None if limit == 0 else limit
    completed_entries = manager.list_completed_entries(entry_limit)
    resolved_format = _resolve_output_format(json_output, output_format)
    pending_entries = (
        manager.list_pending_entries(entry_limit) if pending or resolved_format else []
    )
    data = {
        "root": str(manager.root),
        "done_dir": str(manager.done_dir),
        "index_path": str(manager.index_path),
        "done": done,
        "total": total,
        "entries": completed_entries,
        "completed_entries": completed_entries,
        "pending_entries": pending_entries,
        "orphan_flag_count": orphan_count,
        "orphan_flag_hashes": orphan_flags,
        "orphan_flag_review_dir": str(review_dir),
    }
    if resolved_format:
        _emit_machine_payload(data, output_format=resolved_format, show_stats=stats)
        return

    table = Table("Field", "Value", title="Resume Status")
    table.add_row("Root", data["root"])
    table.add_row("done_flags", data["done_dir"])
    table.add_row("index", data["index_path"])
    table.add_row("Completed", str(done))
    table.add_row("Total", "?" if total is None else str(total))
    console.print(table)
    if completed_entries:
        console.print(
            f"Completed entries ({len(completed_entries)})"
            f" (limit={'all' if entry_limit is None else entry_limit}):"
        )
        for entry in completed_entries:
            console.print(f"- {entry}")
    else:
        console.print("[dim]No resume entries recorded yet.[/]")
    if orphan_count:
        console.print(
            f"[yellow]{orphan_count} orphan done flag(s) copied to {review_dir} for audit.[/]"
        )
    if pending:
        if pending_entries:
            console.print(
                f"Pending entries ({len(pending_entries)})"
                f" (limit={'all' if entry_limit is None else entry_limit}):"
            )
            for entry in pending_entries:
                console.print(f"- {entry}")
        else:
            console.print("[dim]No pending entries recorded (index required).[/]")


def _print_job(job: dict) -> None:
    manifest = job.get("manifest")
    sweep_row = "-"
    validation_row = "-"
    if isinstance(manifest, dict):
        sweep_row = _format_sweep_summary(
            {
                "sweep_stats": manifest.get("sweep_stats"),
                "overlap_match_ratio": manifest.get("overlap_match_ratio"),
            }
        )
        validation_row = _format_validation_summary(manifest.get("validation_failures"))
    table = Table("Field", "Value", title=f"Job {job.get('id', 'unknown')}")
    for key in ("state", "url", "progress", "manifest", "warnings", "blocklist_hits"):
        value = job.get(key)
        if isinstance(value, (dict, list)):
            value = json.dumps(value, indent=2)
        table.add_row(key, str(value))
    if sweep_row != "-":
        table.add_row("sweep", sweep_row)
    if validation_row != "-":
        table.add_row("validation", validation_row)
    console.print(table)
    seam_payload = _resolve_seam_data(manifest if isinstance(manifest, dict) else None, job)
    if seam_payload:
        _print_seam_markers(seam_payload)
    else:
        _print_seam_marker_counts(job.get("seam_marker_count"), job.get("seam_hash_count"))


def _print_ocr_metrics(manifest: dict[str, Any], *, output_format: Optional[OutputFormat]) -> None:
    batches = manifest.get("ocr_batches") or []
    quota = manifest.get("ocr_quota") or {}
    autotune = manifest.get("ocr_autotune") or {}
    seam_markers = manifest.get("seam_markers") or []
    seam_marker_events = manifest.get("seam_marker_events") or []
    if output_format:
        _emit_machine_payload(
            {
                "batches": batches,
                "quota": quota,
                "autotune": autotune,
                "seam_markers": seam_markers,
                "seam_marker_events": seam_marker_events,
            },
            output_format=output_format,
        )
        return

    if quota:
        quota_table = Table("Limit", "Used", "Threshold", "Warning", title="OCR Quota")
        threshold = quota.get("threshold_ratio", 0)
        quota_table.add_row(
            str(quota.get("limit", "—")),
            str(quota.get("used", "—")),
            f"{float(threshold) * 100:.0f}%",
            "⚠" if quota.get("warning_triggered") else "—",
        )
        console.print(quota_table)

    _print_ocr_autotune(autotune)

    table = Table(
        "Tile IDs",
        "Latency (ms)",
        "Status",
        "Attempts",
        "Request ID",
        "Payload (bytes)",
        title="OCR Batches",
    )
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
    _print_seam_markers(seam_markers)


def _print_links(links: Iterable[dict]) -> None:
    table = Table("Text", "Href", "Domain", "Source", "Δ", "Target", "Rel", title="Links")
    for row in links:
        rel_tokens = row.get("rel")
        if isinstance(rel_tokens, (list, tuple)):
            rel_value = ", ".join(str(token) for token in rel_tokens if token)
        else:
            rel_value = row.get("rel", "") or ""
        table.add_row(
            row.get("text", ""),
            row.get("href", ""),
            row.get("domain", ""),
            row.get("source", ""),
            row.get("delta", ""),
            row.get("target", ""),
            rel_value,
        )
    console.print(table)


def _print_ocr_autotune(autotune: Mapping[str, Any] | None) -> None:
    if not autotune:
        console.print("[dim]No OCR concurrency telemetry recorded yet.[/]")
        return
    summary = Table("Metric", "Value", title="OCR Concurrency Autotune")
    summary.add_row("Initial", str(autotune.get("initial_limit", "—")))
    summary.add_row("Peak", str(autotune.get("peak_limit", "—")))
    summary.add_row("Final", str(autotune.get("final_limit", "—")))
    summary.add_row(
        "Events", str(len(autotune.get("events") or []) or autotune.get("event_count", 0))
    )
    console.print(summary)
    events = autotune.get("events") or []
    if not isinstance(events, list) or not events:
        return
    event_table = Table("Δ", "Reason", "Status", "Latency", title="Recent Autotune Events")
    for entry in events[-5:][::-1]:
        if not isinstance(entry, Mapping):
            continue
        event_table.add_row(
            f"{entry.get('previous_limit', '—')}→{entry.get('new_limit', '—')}",
            str(entry.get("reason", "—")),
            str(entry.get("status_code", "—")),
            f"{entry.get('latency_ms', '—')} ms",
        )
    console.print(event_table)


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
                raise typer.BadParameter(
                    "Vector list must contain numbers", param_hint=source
                ) from exc

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
    elif event_name == "state":
        payload = entry.get("payload")
        if isinstance(payload, str) and payload:
            commands.extend(hooks.get(f"state:{payload}", []))
        elif isinstance(payload, Mapping):
            state = payload.get("state")
            if isinstance(state, str):
                commands.extend(hooks.get(f"state:{state}", []))

    commands.extend(hooks.get("*", []))

    if not commands:
        return

    for command in commands:
        _run_hook(command, event_name, entry)


def _run_hook(command: str, event_name: str, payload: Mapping[str, Any]) -> None:
    env = os.environ.copy()
    env["MDWB_EVENT_NAME"] = shlex.quote(event_name)
    try:
        payload_str = json.dumps(payload)
    except Exception:  # pragma: no cover - defensive
        payload_str = str(payload)
    env["MDWB_EVENT_PAYLOAD"] = shlex.quote(payload_str)

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
    on_terminal: Optional[Callable[[str, dict[str, Any] | None], None]] = None,
    progress_meter: _ProgressMeter | None = None,
    client: httpx.Client | None = None,
) -> None:
    with _client_ctx_or_shared(client, settings, timeout=None) as active_client:
        with active_client.stream("GET", f"/jobs/{job_id}/stream") as response:
            response.raise_for_status()
            for event, payload in _iter_sse(response):
                if raw:
                    console.print(f"{event}\t{payload}")
                else:
                    formatted = payload
                    if event == "progress" and progress_meter:
                        data = _parse_json_payload(payload)
                        if isinstance(data, dict):
                            text = _format_progress_text(data, meter=progress_meter)
                            if text:
                                formatted = text
                    _log_event(event, formatted)
                if hooks:
                    entry_payload: Mapping[str, Any]
                    try:
                        entry_payload = json.loads(payload)
                    except json.JSONDecodeError:
                        entry_payload = {"raw": payload}
                    _trigger_event_hooks({"event": event, "payload": entry_payload}, hooks)
                if on_terminal and event == "state":
                    state_value = payload.strip().upper()
                    if state_value in {"DONE", "FAILED"}:
                        on_terminal(state_value, None)
                        return
                if on_terminal and event == "state":
                    state_value = payload.strip().upper()
                    if state_value in {"DONE", "FAILED"}:
                        on_terminal(state_value, None)


def _iter_event_lines(
    job_id: str,
    settings: APISettings,
    *,
    cursor: str | None,
    follow: bool,
    interval: float,
    client: httpx.Client | None = None,
):
    with nullcontext(client) if client is not None else _client_ctx(settings) as active_client:
        while True:
            params: dict[str, str] = {}
            if cursor:
                params["since"] = cursor
            with active_client.stream("GET", f"/jobs/{job_id}/events", params=params) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    if not line:
                        continue
                    yield line
                    cursor = _cursor_from_line(line, cursor)
            if not follow:
                break
            time.sleep(interval)


def _watch_job_events(
    job_id: str,
    settings: APISettings,
    *,
    cursor: str | None,
    follow: bool,
    interval: float,
    output: TextIO,
    client: httpx.Client | None = None,
) -> None:
    for line in _iter_event_lines(
        job_id,
        settings,
        cursor=cursor,
        follow=follow,
        interval=interval,
        client=client,
    ):
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
    client: httpx.Client | None = None,
    progress_meter: _ProgressMeter | None = None,
    on_terminal: Optional[Callable[[str, dict[str, Any] | None], None]] = None,
) -> None:
    terminal_states = {"DONE", "FAILED"}
    for line in _iter_event_lines(
        job_id,
        settings,
        cursor=cursor,
        follow=follow,
        interval=interval,
        client=client,
    ):
        entry: dict[str, Any] | None = None
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            entry = None

        if entry is not None:
            _trigger_event_hooks(entry, hooks)

        if raw:
            console.print(line)
            continue

        if entry is None:
            console.print(line)
            continue
        event_name = entry.get("event")
        if isinstance(event_name, str) and event_name == "dom_assist":
            _print_dom_assist_event(entry)
            continue
        if isinstance(event_name, str) and event_name == "seams":
            _print_seam_event(entry)
            continue
        snapshot = entry.get("snapshot")
        if isinstance(snapshot, dict):
            _render_snapshot(snapshot, meter=progress_meter)
            state = snapshot.get("state")
            if follow and isinstance(state, str) and state.upper() in terminal_states:
                if on_terminal:
                    on_terminal(state.upper(), snapshot)
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
    on_terminal: Optional[Callable[[str, dict[str, Any] | None], None]] = None,
    progress_meter: _ProgressMeter | None = None,
    client: httpx.Client | None = None,
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
            client=client,
            progress_meter=progress_meter,
            on_terminal=on_terminal,
        )
    except httpx.HTTPError as exc:
        console.print(f"[yellow]Events feed unavailable ({exc}); falling back to SSE stream.[/]")
        _stream_job(
            job_id,
            settings,
            raw=raw,
            hooks=hooks,
            on_terminal=on_terminal,
            progress_meter=progress_meter,
            client=client,
        )


def _render_snapshot(snapshot: dict[str, Any], *, meter: _ProgressMeter | None = None) -> None:
    state = snapshot.get("state")
    if state:
        _log_event("state", str(state))
    profile_id = snapshot.get("profile_id")
    if profile_id:
        _log_event("log", f"profile: {profile_id}")
    if snapshot.get("cache_hit"):
        _log_event("log", "cache: hit")
    progress = snapshot.get("progress")
    if isinstance(progress, dict):
        text = _format_progress_text(progress, meter=meter)
        if text:
            _log_event("progress", text)
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
    seam_data = _resolve_seam_data(manifest if isinstance(manifest, dict) else None, snapshot)
    seam_summary = _format_seam_log_summary(seam_data)
    if seam_summary != "-":
        _log_event("log", f"seams: {seam_summary}")
    error = snapshot.get("error")
    if error:
        _log_event("log", json.dumps({"error": error}))


@cli.command()
def fetch(
    url: str = typer.Argument(..., help="URL to capture"),
    api_base: Optional[str] = typer.Option(None, help="Override API base URL"),
    profile: Optional[str] = typer.Option(None, "--profile", help="Browser profile identifier"),
    ocr_policy: Optional[str] = typer.Option(None, "--ocr-policy", help="OCR policy/model id"),
    watch: bool = typer.Option(
        False, "--watch/--no-watch", help="Stream job progress after submission"
    ),
    raw: bool = typer.Option(False, "--raw", help="When watching, print raw NDJSON lines"),
    http2: bool = typer.Option(True, "--http2/--no-http2"),
    progress_eta: bool = typer.Option(
        True, "--progress/--no-progress", help="Show percent/ETA while streaming events."
    ),
    resume: bool = typer.Option(
        False,
        "--resume/--no-resume",
        help="Skip submission when the URL already appears in done_flags/work_index (auto-enables --watch to record completion).",
    ),
    resume_root: Path = typer.Option(
        Path("."),
        "--resume-root",
        help="Directory containing work_index_list.csv.zst and done_flags/ when using --resume.",
    ),
    resume_index: Optional[Path] = typer.Option(
        None,
        "--resume-index",
        help="Override the default work_index_list.csv.zst path (defaults to RESUME_ROOT/work_index_list.csv.zst).",
    ),
    resume_done_dir: Optional[Path] = typer.Option(
        None,
        "--resume-done-dir",
        help="Override the default done_flags directory (defaults to RESUME_ROOT/done_flags).",
    ),
    cache: bool = typer.Option(
        True,
        "--cache/--no-cache",
        help="Reuse cached captures when an identical configuration already exists.",
    ),
    reuse_session: bool = typer.Option(
        False,
        "--reuse-session/--no-reuse-session",
        help="Reuse a single HTTP/2 client for job submission and streaming (reduces TLS/H2 churn).",
    ),
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
        raise typer.BadParameter(
            "Use --webhook-event together with --webhook-url.", param_hint="--webhook-event"
        )

    hook_map = {}
    if on_event:
        hook_map = _parse_event_hooks(on_event)
        if not watch:
            raise typer.BadParameter(
                "--on requires --watch so hooks have events to monitor.", param_hint="--on"
            )

    settings = _resolve_settings(api_base)

    resume_manager: ResumeManager | None = None
    if resume:
        resolved_root = resume_root.resolve()
        index_path = (
            resume_index.resolve() if resume_index else (resolved_root / "work_index_list.csv.zst")
        )
        done_dir = resume_done_dir.resolve() if resume_done_dir else (resolved_root / "done_flags")
        resume_manager = ResumeManager(resolved_root, index_path=index_path, done_dir=done_dir)
        done_count, total_entries = resume_manager.status()
        if total_entries is not None:
            percent = (done_count / total_entries * 100) if total_entries else 0.0
            console.print(
                f"[dim]Resume progress[/]: {done_count}/{total_entries} entries ({percent:.1f}%)."
            )
        elif done_count:
            console.print(f"[dim]Resume progress[/]: {done_count} entries marked complete.")
        if resume_manager.is_complete(url):
            console.print(
                f"[green]Resume[/]: {url} already marked complete under {resume_manager.done_dir}; skipping submission."
            )
            return
        if not watch:
            watch = True
            console.print(
                "[dim]Resume requires watching job completion; enabling --watch automatically.[/]"
            )

    shared_client: httpx.Client | None = _client(settings, http2=http2) if reuse_session else None
    try:
        with _client_ctx_or_shared(shared_client, settings, http2=http2) as client:
            payload: dict[str, object] = {"url": url}
            if profile:
                payload["profile_id"] = profile
            payload["reuse_cache"] = cache
            if ocr_policy:
                payload["ocr"] = {"policy": ocr_policy}

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

        resume_marked = False

        def _handle_terminal(state: str, snapshot: dict[str, Any] | None) -> None:
            nonlocal resume_marked
            if resume_manager and state.upper() == "DONE" and not resume_marked:
                resume_manager.mark_complete(url)
                resume_marked = True

        progress_meter = _ProgressMeter() if progress_eta else None

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
                on_terminal=_handle_terminal if resume_manager else None,
                progress_meter=progress_meter,
                client=shared_client,
            )
    finally:
        if shared_client is not None:
            shared_client.close()


@cli.command()
def show(
    job_id: str = typer.Argument(..., help="Job identifier"),
    api_base: Optional[str] = typer.Option(None, help="Override API base URL"),
    http2: bool = typer.Option(True, "--http2/--no-http2"),
    ocr_metrics: bool = typer.Option(
        False, "--ocr-metrics/--no-ocr-metrics", help="Print OCR batch telemetry when available."
    ),
) -> None:
    """Display the latest snapshot for a real job."""

    settings = _resolve_settings(api_base)
    snapshot = _fetch_job_snapshot(job_id, settings, http2=http2)
    _print_job(snapshot)
    if ocr_metrics:
        manifest = snapshot.get("manifest")
        if not manifest:
            console.print(
                "[yellow]Manifest not available yet; try again after the job completes.[/]"
            )
        else:
            _print_ocr_metrics(manifest, output_format=None)


def _fetch_job_snapshot(
    job_id: str, settings: APISettings, *, http2: bool = True
) -> dict[str, Any]:
    with _client_ctx(settings, http2=http2) as client:
        response = client.get(f"/jobs/{job_id}")
        response.raise_for_status()
        return response.json()


@cli.command()
def stream(
    job_id: str = typer.Argument(..., help="Job identifier"),
    api_base: Optional[str] = typer.Option(None, help="Override API base URL"),
    raw: bool = typer.Option(
        False, "--raw", help="Print raw event payloads instead of colored labels."
    ),
) -> None:
    """Tail the live SSE stream for a job."""

    settings = _resolve_settings(api_base)
    _stream_job(job_id, settings, raw=raw)


@cli.command()
def diag(
    job_id: str = typer.Argument(..., help="Job identifier"),
    api_base: Optional[str] = typer.Option(None, help="Override API base URL"),
    json_output: bool = typer.Option(
        False, "--json/--no-json", help="Emit JSON payload instead of tables."
    ),
    output_format: Optional[str] = typer.Option(None, "--format", help="Emit json or toon output."),
) -> None:
    """Print manifest/environment details for a job."""

    settings = _resolve_settings(api_base)
    with _client_ctx(settings) as client:
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
    resolved_format = _resolve_output_format(json_output, output_format)
    if resolved_format:
        _emit_machine_payload(payload, output_format=resolved_format)
        return
    _print_diag_report(snapshot, manifest, manifest_source, manifest_error)


def _open_output_stream(path_value: str) -> tuple[TextIO, bool]:
    if path_value == "-":
        return sys.stdout, False
    handle = open(path_value, "a", encoding="utf-8")
    return handle, True


@cli.command()
def events(
    job_id: str = typer.Argument(..., help="Job identifier"),
    api_base: Optional[str] = typer.Option(None, help="Override API base URL"),
    since: Optional[str] = typer.Option(None, help="ISO timestamp cursor for incremental polling."),
    follow: bool = typer.Option(
        False, "--follow/--no-follow", help="Continue polling for new events."
    ),
    interval: float = typer.Option(
        2.0, "--interval", help="Polling interval in seconds when following."
    ),
    output_path: str = typer.Option(
        "-", "--output", "-o", help="File to append NDJSON events to (default stdout)."
    ),
) -> None:
    """Fetch newline-delimited job events (JSONL)."""

    settings = _resolve_settings(api_base)
    output, should_close = _open_output_stream(output_path)
    try:
        _watch_job_events(
            job_id, settings, cursor=since, follow=follow, interval=interval, output=output
        )
    finally:
        if should_close:
            output.close()


@cli.command()
def watch(
    job_id: str = typer.Argument(..., help="Job identifier"),
    api_base: Optional[str] = typer.Option(None, help="Override API base URL"),
    since: Optional[str] = typer.Option(None, help="ISO timestamp cursor for incremental polling."),
    follow: bool = typer.Option(
        True, "--follow/--once", help="Keep polling for new events instead of exiting."
    ),
    interval: float = typer.Option(
        2.0, "--interval", help="Polling interval in seconds when following."
    ),
    raw: bool = typer.Option(
        False, "--raw", help="Print raw NDJSON events instead of formatted output."
    ),
    progress_eta: bool = typer.Option(
        True, "--progress/--no-progress", help="Show percent/ETA while streaming events."
    ),
    reuse_session: bool = typer.Option(
        False,
        "--reuse-session/--no-reuse-session",
        help="Reuse a single HTTP client for the event stream.",
    ),
    on_event: Optional[list[str]] = typer.Option(
        None,
        "--on",
        help="Run COMMAND when EVENT fires (format EVENT=COMMAND). Repeat flag to add multiple hooks.",
    ),
) -> None:
    """Stream `/jobs/{id}/events` with optional fallback to SSE."""

    settings = _resolve_settings(api_base)
    hook_map = _parse_event_hooks(on_event)
    shared_client: httpx.Client | None = _client(settings) if reuse_session else None
    try:
        _watch_events_with_fallback(
            job_id,
            settings,
            cursor=since,
            follow=follow,
            interval=interval,
            raw=raw,
            hooks=hook_map or None,
            progress_meter=_ProgressMeter() if progress_eta else None,
            client=shared_client,
        )
    finally:
        if shared_client is not None:
            shared_client.close()


@demo_cli.command("snapshot")
def demo_snapshot(
    api_base: Optional[str] = typer.Option(None, help="Override API base URL"),
    json_output: bool = typer.Option(False, "--json", help="Print raw JSON instead of tables."),
    output_format: Optional[str] = typer.Option(None, "--format", help="Emit json or toon output (env: MWB_OUTPUT_FORMAT, TOON_DEFAULT_FORMAT)."),
    stats: bool = typer.Option(False, "--stats", help="Show token savings comparison (JSON vs TOON bytes)."),
) -> None:
    """Fetch the demo job snapshot from /jobs/demo."""

    settings = _resolve_settings(api_base)
    with _client_ctx(settings) as client:
        response = client.get("/jobs/demo")
        response.raise_for_status()
        data = response.json()
    resolved_format = _resolve_output_format(json_output, output_format)
    if resolved_format:
        _emit_machine_payload(data, output_format=resolved_format, show_stats=stats)
    else:
        _print_job(data)
        if links := data.get("links"):
            _print_links(links)


@demo_cli.command("links")
def demo_links(
    api_base: Optional[str] = typer.Option(None, help="Override API base URL"),
    json_output: bool = typer.Option(False, "--json", help="Print raw JSON."),
    output_format: Optional[str] = typer.Option(None, "--format", help="Emit json or toon output (env: MWB_OUTPUT_FORMAT, TOON_DEFAULT_FORMAT)."),
    stats: bool = typer.Option(False, "--stats", help="Show token savings comparison (JSON vs TOON bytes)."),
) -> None:
    """Fetch the demo links JSON."""

    settings = _resolve_settings(api_base)
    with _client_ctx(settings) as client:
        response = client.get("/jobs/demo/links.json")
        response.raise_for_status()
        data = response.json()
    resolved_format = _resolve_output_format(json_output, output_format)
    if resolved_format:
        _emit_machine_payload(data, output_format=resolved_format, show_stats=stats)
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
    if event == "seams":
        data = _parse_json_payload(payload)
        summary = _format_seam_log_summary(data)
        console.print(f"[blue]seams[/]: {summary}")
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
    with _client_ctx(settings) as client:
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


def _print_warning_records(
    records: list[dict[str, Any]], *, output_format: Optional[OutputFormat]
) -> None:
    if not records:
        console.print("[dim]No warning entries found.[/]")
        return
    if output_format == "json":
        for record in records:
            console.print(json.dumps(_augment_warning_record(record)))
        return
    if output_format == "toon":
        if not _toon_available():
            typer.echo("warning: tru not available, falling back to JSON", err=True)
            for record in records:
                console.print(json.dumps(_augment_warning_record(record)))
            return
        for record in records:
            payload = json.dumps(_augment_warning_record(record), indent=2)
            try:
                toon_payload = _encode_toon(payload)
            except subprocess.CalledProcessError:
                typer.echo("warning: tru --encode failed, falling back to JSON", err=True)
                console.print(json.dumps(_augment_warning_record(record)))
                continue
            if not toon_payload.endswith("\n"):
                toon_payload += "\n"
            sys.stdout.write(toon_payload)
        return
    table = Table(title="Warning Log")
    table.add_column("timestamp")
    table.add_column("job")
    table.add_column("warnings", overflow="fold")
    table.add_column("blocklist")
    table.add_column("sweep")
    table.add_column("validation")
    table.add_column("dom")
    table.add_column("seams", overflow="fold")
    for row in _warning_rows(records):
        table.add_row(*row)
    console.print(table)


def _warning_rows(
    records: Iterable[dict[str, Any]],
) -> Iterable[tuple[str, str, str, str, str, str, str, str]]:
    for record in records:
        timestamp = record.get("timestamp", "-")
        job = record.get("job_id", "-")
        warnings = _format_warning_summary(record.get("warnings"))
        blocklist = _format_blocklist(record.get("blocklist_hits"))
        sweep = _format_sweep_summary(record)
        validation = _format_validation_summary(record.get("validation_failures"))
        dom_summary = _format_dom_assist_summary(record.get("dom_assist_summary"))
        seams = _format_seam_log_summary(record.get("seam_markers"))
        yield (str(timestamp), str(job), warnings, blocklist, sweep, validation, dom_summary, seams)


def _augment_warning_record(record: dict[str, Any]) -> dict[str, Any]:
    enriched = dict(record)
    validations = record.get("validation_failures")
    if isinstance(validations, list):
        enriched.setdefault("validation_failure_count", len(validations))
    stats = record.get("sweep_stats")
    ratio = record.get("overlap_match_ratio")
    if ratio is None and isinstance(stats, dict):
        ratio = stats.get("overlap_match_ratio")
    enriched["sweep_summary"] = _format_sweep_summary(
        {"sweep_stats": stats if isinstance(stats, dict) else {}, "overlap_match_ratio": ratio}
    )
    if ratio is not None:
        enriched["overlap_match_ratio"] = ratio
    seam_summary = record.get("seam_markers")
    if isinstance(seam_summary, Mapping):
        enriched["seam_summary_text"] = _format_seam_log_summary(seam_summary)
    dom_summary = record.get("dom_assist_summary")
    if isinstance(dom_summary, Mapping):
        enriched["dom_assist_summary_text"] = _format_dom_assist_summary(dom_summary)
    return enriched


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


def _format_seam_log_summary(data: Any) -> str:
    if not isinstance(data, Mapping) or not data:
        return "-"
    parts: list[str] = []
    count = data.get("count")
    tiles = data.get("unique_tiles")
    hashes = data.get("unique_hashes")
    if isinstance(count, int):
        suffix = "marker" if count == 1 else "markers"
        parts.append(f"{count} {suffix}")
    if isinstance(tiles, int) and tiles > 0:
        parts.append(f"{tiles} tiles")
    if isinstance(hashes, int) and hashes > 0:
        parts.append(f"{hashes} hashes")
    sample = data.get("sample")
    if isinstance(sample, Sequence) and sample:
        preview = [
            entry.get("hash")
            for entry in sample
            if isinstance(entry, Mapping) and isinstance(entry.get("hash"), str)
        ]
        if preview:
            parts.append("hashes " + ", ".join(preview))
    usage = data.get("usage")
    if isinstance(usage, Mapping):
        fallback_count = usage.get("count")
        if isinstance(fallback_count, int) and fallback_count > 0:
            parts.append(f"fallbacks={fallback_count}")
    return " | ".join(parts) if parts else "-"


def _format_blocklist(values: Any) -> str:
    if not isinstance(values, dict) or not values:
        return "-"
    parts = [f"{selector}:{count}" for selector, count in values.items()]
    return ", ".join(parts)


def _format_dom_assist_summary(summary: Any) -> str:
    if not isinstance(summary, Mapping) or not summary:
        return "-"
    count = summary.get("count")
    reasons = summary.get("reasons") or []
    reason_counts = summary.get("reason_counts") or []
    if isinstance(reason_counts, list) and reason_counts:
        formatted_counts = ", ".join(_format_reason_count(entry) for entry in reason_counts)
    elif reasons:
        formatted_counts = ", ".join(str(reason) for reason in reasons)
    else:
        formatted_counts = "-"
    sample = summary.get("sample") or {}
    sample_reason = sample.get("reason")
    density = summary.get("assist_density")
    density_text = None
    if isinstance(density, (int, float)):
        density_text = f"density={density:.3f}"
    parts = [f"{count} assist(s)" if count is not None else "assists", formatted_counts]
    if density_text:
        parts.append(density_text)
    if sample_reason:
        parts.append(f"sample={sample_reason}")
    return " | ".join(part for part in parts if part and part != "-")


def _format_reason_count(entry: Mapping[str, Any]) -> str:
    reason = entry.get("reason", "unknown")
    count = entry.get("count")
    ratio = entry.get("ratio")
    ratio_text = ""
    if isinstance(ratio, (int, float)):
        ratio_text = f", {ratio * 100:.1f}%"
    return f"{reason}({count}{ratio_text})"


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
        precise = f"{ratio:.2f}"
        parts.append(f"ratio={precise}")
        truncated = math.floor(ratio * 10) / 10
        truncated_str = f"{truncated:.1f}"
        if truncated_str != precise:
            parts.append(f"ratio={truncated_str}")
    if not parts:
        return "-"
    if len(parts) == 1:
        return parts[0]
    return "\n".join(parts)


def _format_validation_summary(values: Any) -> str:
    if not isinstance(values, list) or not values:
        return "-"
    return "; ".join(str(entry) for entry in values)


def _format_progress_text(progress: Any, *, meter: _ProgressMeter | None = None) -> str:
    if not isinstance(progress, dict):
        return "-"
    done = progress.get("done")
    total = progress.get("total")
    if done is None and total is None:
        return "-"
    base: str
    if done is None:
        base = f"? / {total}"
    elif total is None:
        base = f"{done} / ?"
    else:
        base = f"{done} / {total}"
    extra = None
    if meter is not None:
        extra = meter.describe(
            done if isinstance(done, int) else None, total if isinstance(total, int) else None
        )
    if extra:
        return f"{base} {extra}"
    return base


def _print_diag_report(
    snapshot: dict[str, Any],
    manifest: dict[str, Any] | None,
    manifest_source: str,
    manifest_error: str | None,
) -> None:
    summary = Table("Field", "Value", title=f"Job {snapshot.get('id', 'unknown')}")
    summary.add_row("URL", snapshot.get("url", "—"))
    summary.add_row("State", str(snapshot.get("state", "—")))
    summary.add_row("Profile", snapshot.get("profile_id") or "—")
    summary.add_row("Cache", "hit" if snapshot.get("cache_hit") else "miss")
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
        env_table.add_row(
            "Playwright",
            f"{env.get('playwright_channel', '—')} / {env.get('playwright_version', '—')}",
        )
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

    dom_assists = (manifest or {}).get("dom_assists") or []
    if dom_assists:
        counter = _dom_assist_counter(dom_assists)
        if counter:
            summary_table = Table("Reason", "Count", title="DOM Assist Reasons")
            for reason, count in counter.most_common():
                summary_table.add_row(reason, str(count))
            console.print(summary_table)
        assist_table = Table("Tile", "Line", "Reason", "Replacement", title="DOM Assists")
        for entry in dom_assists[:10]:
            if not isinstance(entry, dict):
                continue
            assist_table.add_row(
                str(entry.get("tile_index", "—")),
                str(entry.get("line", "—")),
                str(entry.get("reason", "—")),
                entry.get("dom_text", "—"),
            )
        if len(dom_assists) > 10:
            assist_table.caption = f"Showing 10 of {len(dom_assists)} entries"
        console.print(assist_table)
    else:
        console.print("[dim]No DOM assists recorded.[/]")

    seam_payload = _resolve_seam_data(manifest if isinstance(manifest, dict) else None, snapshot)
    if seam_payload:
        _print_seam_markers(seam_payload)
    else:
        _print_seam_marker_counts(
            snapshot.get("seam_marker_count"),
            snapshot.get("seam_hash_count"),
        )

    autotune_data = (manifest or {}).get("ocr_autotune")
    _print_ocr_autotune(autotune_data)


def _print_dom_assist_event(entry: Mapping[str, Any]) -> None:
    data = entry.get("data")
    if not isinstance(data, Mapping):
        console.print_json(data=entry)
        return
    count = data.get("count")
    reasons = data.get("reasons") or []
    sample = data.get("sample") or {}
    table = Table("Metric", "Value", title="DOM Assist Summary")
    table.add_row("Total", str(count or 0))
    if reasons:
        table.add_row("Reasons", ", ".join(str(reason) for reason in reasons))
    tile_info = sample.get("tile_index")
    if tile_info is not None:
        table.add_row("Sample Tile", str(tile_info))
    if sample.get("reason"):
        table.add_row("Sample Reason", str(sample.get("reason")))
    if sample.get("dom_text"):
        table.add_row("Sample Text", str(sample.get("dom_text")))
    console.print(table)
    counts = data.get("reason_counts")
    if isinstance(counts, list) and counts:
        reason_table = Table("Reason", "Count")
        for entry in counts:
            reason_table.add_row(str(entry.get("reason", "—")), str(entry.get("count", "—")))
        console.print(reason_table)


def _print_seam_event(entry: Mapping[str, Any]) -> None:
    payload = entry.get("data") or entry.get("payload")
    if payload is None:
        console.print_json(data=entry)
        return
    _print_seam_markers(payload)


def _dom_assist_counter(entries: Sequence[Mapping[str, Any]]) -> Counter[str]:
    counter: Counter[str] = Counter()
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        reason = entry.get("reason")
        if isinstance(reason, str) and reason:
            counter[reason] += 1
    return counter


def _normalize_seam_rows(entries: Sequence[Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        tile = entry.get("tile_index", entry.get("tile"))
        rows.append(
            {
                "tile": tile,
                "position": entry.get("position"),
                "hash": entry.get("hash"),
            }
        )
    return rows


def _resolve_seam_data(
    manifest: Mapping[str, Any] | None,
    snapshot: Mapping[str, Any] | None,
) -> Any:
    manifest_data: Mapping[str, Any] | None = manifest if isinstance(manifest, Mapping) else None
    if manifest_data:
        markers = manifest_data.get("seam_markers")
        if isinstance(markers, list):
            payload: dict[str, Any] = {"markers": markers}
            events = manifest_data.get("seam_marker_events")
            if isinstance(events, list):
                payload["events"] = events
            return payload
        if markers:
            return markers
    snapshot_data: Mapping[str, Any] | None = snapshot if isinstance(snapshot, Mapping) else None
    if snapshot_data:
        seam_field = snapshot_data.get("seam_markers")
        if seam_field:
            return seam_field
        count = snapshot_data.get("seam_marker_count")
        hash_count = snapshot_data.get("seam_hash_count")
        if isinstance(count, int):
            summary: dict[str, Any] = {"count": count}
            if isinstance(hash_count, int):
                summary["unique_hashes"] = hash_count
            return summary
    return None


def _summarize_seam_data(entries: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if isinstance(entries, Mapping) and "markers" not in entries:
        summary = dict(entries)
        sample_entries = summary.get("sample")
        rows = _normalize_seam_rows(sample_entries) if isinstance(sample_entries, Sequence) else []
        return summary, rows

    markers: list[Mapping[str, Any]] = []
    events: list[Mapping[str, Any]] = []
    if isinstance(entries, Mapping):
        raw_markers = entries.get("markers")
        if isinstance(raw_markers, Sequence):
            markers = [entry for entry in raw_markers if isinstance(entry, Mapping)]
        raw_events = entries.get("events")
        if isinstance(raw_events, Sequence):
            events = [entry for entry in raw_events if isinstance(entry, Mapping)]
    elif isinstance(entries, Sequence):
        markers = [entry for entry in entries if isinstance(entry, Mapping)]

    if not markers:
        return ({}, [])

    tile_ids = {
        entry.get("tile_index") for entry in markers if isinstance(entry.get("tile_index"), int)
    }
    hashes = {entry.get("hash") for entry in markers if entry.get("hash")}
    summary: dict[str, Any] = {
        "count": len(markers),
        "unique_tiles": len(tile_ids) or None,
        "unique_hashes": len(hashes) or None,
    }
    if events:
        summary["usage"] = {
            "count": len(events),
            "sample": [
                {
                    "prev_tile_index": evt.get("prev_tile_index"),
                    "curr_tile_index": evt.get("curr_tile_index"),
                    "seam_hash": evt.get("seam_hash"),
                }
                for evt in events[:3]
            ],
        }

    rows = [
        {
            "tile": entry.get("tile_index"),
            "position": entry.get("position"),
            "hash": entry.get("hash"),
        }
        for entry in markers
    ]
    return summary, rows


def _print_seam_marker_counts(count: Any, hash_count: Any) -> None:
    if not isinstance(count, int):
        console.print("[dim]No seam markers recorded yet.[/]")
        return
    message = f"Seam markers: {count}"
    if isinstance(hash_count, int):
        message += f" (unique hashes: {hash_count})"
    console.print(message)


def _print_seam_markers(entries: Any) -> None:
    summary, rows = _summarize_seam_data(entries)
    if not summary and not rows:
        console.print("[dim]No seam markers recorded yet.[/]")
        return

    total = summary.get("count") if summary else len(rows)
    if total is None and rows:
        total = len(rows)
    tile_count = (
        summary.get("unique_tiles")
        if summary
        else len({row["tile"] for row in rows if row.get("tile")})
    )
    hash_count = (
        summary.get("unique_hashes")
        if summary
        else len({row["hash"] for row in rows if row.get("hash")})
    )

    table = Table("Metric", "Value", title="Seam Markers")
    table.add_row("Markers", str(total if total is not None else "—"))
    table.add_row("Tiles", str(tile_count if tile_count is not None else "—"))
    table.add_row("Distinct hashes", str(hash_count if hash_count is not None else "—"))
    console.print(table)

    usage = summary.get("usage") if summary else None
    if isinstance(usage, Mapping):
        usage_count = usage.get("count")
        if isinstance(usage_count, int) and usage_count > 0:
            console.print(f"Seam fallback events: {usage_count}")
            samples = usage.get("sample")
            if isinstance(samples, Sequence) and samples:
                usage_table = Table("Prev Tile", "Curr Tile", "Seam Hash", title="Fallback Samples")
                sample_total = min(len(samples), 5)
                for entry in samples[:sample_total]:
                    usage_table.add_row(
                        str(entry.get("prev_tile_index", "—")),
                        str(entry.get("curr_tile_index", "—")),
                        str(entry.get("seam_hash", "—")),
                    )
                console.print(usage_table)

    if rows:
        position_order = {"top": 0, "bottom": 1}

        def _tile_key(row: Mapping[str, Any]) -> tuple[int, Any]:
            tile = row.get("tile")
            if isinstance(tile, (int, float)):
                return (0, float(tile))
            return (1, str(tile or ""))

        def _position_key(value: Any) -> int:
            if isinstance(value, str):
                return position_order.get(value.lower(), 2)
            return 2

        sorted_rows = sorted(
            rows, key=lambda row: (_tile_key(row), _position_key(row.get("position")))
        )
        detail = Table("Tile", "Position", "Hash")
        sample_count = min(len(sorted_rows), 10)
        for entry in sorted_rows[:sample_count]:
            tile_value = entry.get("tile", "—")
            position = entry.get("position", "—")
            seam_hash = entry.get("hash", "—")
            detail.add_row(
                str(tile_value if tile_value is not None else "—"),
                str(position or "—"),
                str(seam_hash or "—"),
            )
        if len(sorted_rows) > sample_count:
            detail.caption = f"Showing {sample_count} of {len(sorted_rows)} markers"
        console.print(detail)
    else:
        _print_seam_marker_counts(
            summary.get("count") if summary else None,
            summary.get("unique_hashes") if summary else None,
        )


def _follow_warning_log(
    path: Path, *, output_format: Optional[OutputFormat], interval: float
) -> None:
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
            _print_warning_records([record], output_format=output_format)
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
    raw: bool = typer.Option(
        False, "--raw", help="Print raw event payloads instead of colored labels."
    ),
) -> None:
    """Tail the demo SSE stream."""

    settings = _resolve_settings(api_base)
    with _client_ctx(settings, timeout=None) as client:
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
    follow: bool = typer.Option(
        False, "--follow/--no-follow", help="Stream new entries as they arrive."
    ),
    interval: float = typer.Option(
        1.0, "--interval", help="Polling interval in seconds when following."
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Emit raw JSON lines instead of a table."
    ),
    output_format: Optional[str] = typer.Option(None, "--format", help="Emit json or toon output."),
    log_path: Optional[Path] = typer.Option(None, "--log-path", help="Override WARNING_LOG_PATH."),
) -> None:
    """Tail the structured warning/blocklist log."""

    settings = _resolve_settings(None)
    target_path = log_path or settings.warning_log_path
    resolved_format = _resolve_output_format(json_output, output_format)
    if target_path.exists():
        records = _load_warning_records(target_path, count)
        _print_warning_records(records, output_format=resolved_format)
    else:
        console.print(f"[yellow]Warning log not found at {target_path}[/]")

    if follow:
        console.print(f"[dim]Following {target_path} (Ctrl+C to stop)...[/]")
        try:
            _follow_warning_log(target_path, output_format=resolved_format, interval=interval)
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
    with _client_ctx(settings, timeout=None) as client:
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
    snapshot: Optional[Path] = typer.Argument(
        None, exists=True, dir_okay=False, help="Path to DOM snapshot HTML file."
    ),
    job_id: Optional[str] = typer.Option(
        None, "--job-id", help="Lookup DOM snapshot for an existing job."
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Print raw JSON list instead of a table."
    ),
    output_format: Optional[str] = typer.Option(None, "--format", help="Emit json or toon output (env: MWB_OUTPUT_FORMAT, TOON_DEFAULT_FORMAT)."),
    stats: bool = typer.Option(False, "--stats", help="Show token savings comparison (JSON vs TOON bytes)."),
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
    resolved_format = _resolve_output_format(json_output, output_format)
    if resolved_format:
        _emit_machine_payload(data, output_format=resolved_format, show_stats=stats)
        return
    _print_links(data)


@jobs_webhooks_cli.command("list")
def jobs_webhooks_list(
    job_id: str = typer.Argument(..., help="Job identifier"),
    api_base: Optional[str] = typer.Option(None, help="Override API base URL"),
    json_output: bool = typer.Option(
        False, "--json", help="Emit structured JSON instead of a table."
    ),
    output_format: Optional[str] = typer.Option(None, "--format", help="Emit json or toon output (env: MWB_OUTPUT_FORMAT, TOON_DEFAULT_FORMAT)."),
    stats: bool = typer.Option(False, "--stats", help="Show token savings comparison (JSON vs TOON bytes)."),
) -> None:
    """List registered webhooks for a job."""

    settings = _resolve_settings(api_base)
    with _client_ctx(settings) as client:
        response = client.get(f"/jobs/{job_id}/webhooks")
        if response.status_code == 404:
            detail = _extract_detail(response) or f"Job {job_id} not found."
            resolved_format = _resolve_output_format(json_output, output_format)
            _print_webhook_list_error(detail, job_id, resolved_format)
        response.raise_for_status()
        data = response.json()
    resolved_format = _resolve_output_format(json_output, output_format)
    if resolved_format:
        _emit_machine_payload(
            {"status": "ok", "job_id": job_id, "webhooks": data},
            output_format=resolved_format,
            show_stats=stats,
        )
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
    json_output: bool = typer.Option(
        False, "--json/--no-json", help="Emit JSON payload instead of text."
    ),
    output_format: Optional[str] = typer.Option(None, "--format", help="Emit json or toon output (env: MWB_OUTPUT_FORMAT, TOON_DEFAULT_FORMAT)."),
    stats: bool = typer.Option(False, "--stats", help="Show token savings comparison (JSON vs TOON bytes)."),
) -> None:
    """Register a webhook for a job."""

    settings = _resolve_settings(api_base)
    with _client_ctx(settings) as client:
        payload: dict[str, Any] = {"url": url}
        if event:
            payload["events"] = event
        response = client.post(f"/jobs/{job_id}/webhooks", json=payload)
        if response.status_code in {400, 404}:
            detail = _extract_detail(response) or (
                "Job not found." if response.status_code == 404 else "Webhook rejected."
            )
            resolved_format = _resolve_output_format(json_output, output_format)
            _print_webhook_add_error(detail, job_id, resolved_format)
        response.raise_for_status()
        body = response.json()
    resolved_format = _resolve_output_format(json_output, output_format)
    if resolved_format:
        _emit_machine_payload({"status": "ok", **body}, output_format=resolved_format, show_stats=stats)
        return
    console.print(
        f"[green]Registered webhook for {job_id} ({url}).[/] Trigger states: {', '.join(event) if event else 'DONE, FAILED'}."
    )


@jobs_webhooks_cli.command("delete")
def jobs_webhooks_delete(
    job_id: str = typer.Argument(..., help="Job identifier"),
    api_base: Optional[str] = typer.Option(None, help="Override API base URL"),
    webhook_id: Optional[int] = typer.Option(None, "--id", help="Webhook record id"),
    url: Optional[str] = typer.Option(None, "--url", help="Webhook URL to delete"),
    json_output: bool = typer.Option(
        False, "--json/--no-json", help="Emit JSON payload instead of text."
    ),
    output_format: Optional[str] = typer.Option(None, "--format", help="Emit json or toon output (env: MWB_OUTPUT_FORMAT, TOON_DEFAULT_FORMAT)."),
    stats: bool = typer.Option(False, "--stats", help="Show token savings comparison (JSON vs TOON bytes)."),
) -> None:
    """Remove a webhook subscription from a job."""

    webhook_id = _option_value(webhook_id)
    url = _option_value(url)
    if webhook_id is None and not url:
        raise typer.BadParameter("Provide --id or --url to delete a webhook")

    settings = _resolve_settings(api_base)
    with _client_ctx(settings) as client:
        response, payload = _delete_job_webhooks(client, job_id, webhook_id=webhook_id, url=url)
        if response.status_code == 404:
            detail = _extract_detail(response) or "Webhook or job not found."
            resolved_format = _resolve_output_format(json_output, output_format)
            _print_delete_error(detail, job_id, resolved_format)
            return
        if response.status_code == 400:
            detail = _extract_detail(response) or "Webhook deletion rejected."
            resolved_format = _resolve_output_format(json_output, output_format)
            _print_delete_error(detail, job_id, resolved_format)
            return
        response.raise_for_status()
        body = response.json()
    resolved_format = _resolve_output_format(json_output, output_format)
    if resolved_format:
        _emit_machine_payload(
            {"status": "ok", **body, "request": payload},
            output_format=resolved_format,
            show_stats=stats,
        )
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
    with _client_ctx(settings) as client:
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
    with _client_ctx(settings) as client:
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
    with _client_ctx(settings) as client:
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
    output_format: Optional[str] = typer.Option(None, "--format", help="Emit json or toon output."),
) -> None:
    """Show OCR batch latency + quota telemetry for a job."""

    settings = _resolve_settings(api_base)
    snapshot = _fetch_job_snapshot(job_id, settings)
    manifest = snapshot.get("manifest")
    if not manifest:
        raise typer.BadParameter("Manifest not available yet; rerun once the job completes.")
    resolved_format = _resolve_output_format(json_output, output_format)
    _print_ocr_metrics(manifest, output_format=resolved_format)


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
    http2: bool = typer.Option(
        True, "--http2/--no-http2", help="Use HTTP/2 for the replay request."
    ),
    json_output: bool = typer.Option(
        False, "--json/--no-json", help="Emit JSON instead of text output."
    ),
    output_format: Optional[str] = typer.Option(None, "--format", help="Emit json or toon output."),
) -> None:
    """Replay a capture manifest via POST /replay."""

    try:
        manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(
            f"Manifest is not valid JSON ({exc})", param_hint="manifest_path"
        ) from exc

    settings = _resolve_settings(api_base)
    with _client_ctx(settings, http2=http2) as client:
        response = client.post("/replay", json={"manifest": manifest_payload})
        if response.status_code >= 400:
            detail = _extract_detail(response) or response.text or f"HTTP {response.status_code}"
            resolved_format = _resolve_output_format(json_output, output_format)
            if resolved_format:
                _emit_machine_payload(
                    {"status": "error", "detail": detail},
                    output_format=resolved_format,
                )
            else:
                console.print(f"[red]Replay failed:[/] {detail}")
            raise typer.Exit(1)

        job = response.json()
    resolved_format = _resolve_output_format(json_output, output_format)
    if resolved_format:
        _emit_machine_payload({"status": "ok", "job": job}, output_format=resolved_format)
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
    json_output: bool = typer.Option(
        False, "--json/--no-json", help="Emit JSON instead of a table."
    ),
    output_format: Optional[str] = typer.Option(None, "--format", help="Emit json or toon output."),
) -> None:
    """Search section embeddings for a job."""

    vector_values = _parse_vector_input(vector, vector_file)
    if top_k < 1:
        raise typer.BadParameter("top-k must be at least 1", param_hint="--top-k")

    settings = _resolve_settings(api_base)
    with _client_ctx(settings) as client:
        response = client.post(
            f"/jobs/{job_id}/embeddings/search",
            json={"vector": vector_values, "top_k": top_k},
        )
        if response.status_code >= 400:
            detail = _extract_detail(response) or response.text or f"HTTP {response.status_code}"
            console.print(f"[red]Embeddings search failed:[/] {detail}")
            raise typer.Exit(1)
        data = response.json()
    resolved_format = _resolve_output_format(json_output, output_format)
    if resolved_format:
        _emit_machine_payload(data, output_format=resolved_format)
        return
    _print_embedding_matches(data.get("total_sections", 0), data.get("matches", []))


def _print_delete_error(detail: str, job_id: str, output_format: Optional[OutputFormat]) -> None:
    if output_format:
        _emit_machine_payload(
            {"status": "error", "job_id": job_id, "detail": detail},
            output_format=output_format,
        )
    else:
        console.print(f"[red]{detail}[/]")
    raise typer.Exit(1)


def _print_webhook_add_error(
    detail: str, job_id: str, output_format: Optional[OutputFormat]
) -> None:
    if output_format:
        _emit_machine_payload(
            {"status": "error", "job_id": job_id, "detail": detail},
            output_format=output_format,
        )
    else:
        console.print(f"[red]{detail}[/]")
    raise typer.Exit(1)


def _print_webhook_list_error(
    detail: str, job_id: str, output_format: Optional[OutputFormat]
) -> None:
    if output_format:
        _emit_machine_payload(
            {"status": "error", "job_id": job_id, "detail": detail},
            output_format=output_format,
        )
    else:
        console.print(f"[red]{detail}[/]")
    raise typer.Exit(1)
