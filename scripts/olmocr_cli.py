#!/usr/bin/env python3
"""Hosted olmOCR CLI for capture/ocr reproducibility and ops automation."""

from __future__ import annotations

import random
import statistics
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

import httpx
import typer
from decouple import Config as DecoupleConfig, RepositoryEnv
from decouple import UndefinedValueError
from rich import box
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table
from rich.traceback import install as install_rich_traceback

install_rich_traceback()

console = Console()
app = typer.Typer(help="Run hosted/local olmOCR workflows in a reproducible way.")


class ExitCode(int):
    SUCCESS = 0
    PARTIAL = 10
    UPSTREAM_UNAVAILABLE = 20


@dataclass
class CLISettings:
    api_base_url: str
    mdwb_api_key: Optional[str]
    ocr_server: str
    ocr_model: str
    ocr_api_key: Optional[str]
    tiles_long_side: int
    tile_overlap_px: int
    viewport_overlap_px: int
    concurrency_min: int
    concurrency_max: int
    cft_version: str
    cft_label: str
    playwright_channel: str
    screenshot_style_hash: Optional[str]


@dataclass
class JobRunResult:
    url: str
    job_id: str
    duration_s: float
    output_dir: Path
    tiles_total: Optional[int]
    tiles_failed: Optional[int]
    timings: dict | None


class CLIError(RuntimeError):
    """Recoverable CLI error."""


def _load_decouple() -> DecoupleConfig:
    env_path = Path(".env")
    if not env_path.exists():
        raise CLIError(".env not found; copy .env.example and fill in OCR/API settings.")
    return DecoupleConfig(RepositoryEnv(str(env_path)))


def _config_value(
    config: DecoupleConfig,
    name: str,
    *,
    cast=None,
    default: Optional[str] = None,
) -> Optional[str]:
    try:
        return config(name, cast=cast)
    except UndefinedValueError:
        return default


def _required_config(
    config: DecoupleConfig | Any,
    name: str,
    *,
    default: Optional[str] = None,
) -> str:
    value = _config_value(config, name, default=default)
    if value is None:
        raise CLIError(f"{name} must be set in .env or provided with a default")
    return value


def load_settings() -> CLISettings:
    config = _load_decouple()
    return CLISettings(
        api_base_url=_required_config(config, "API_BASE_URL", default="http://localhost:8000"),
        mdwb_api_key=_config_value(config, "MDWB_API_KEY"),
        ocr_server=_required_config(
            config, "OLMOCR_SERVER", default="https://ai2endpoints.cirrascale.ai/api"
        ),
        ocr_model=_required_config(config, "OLMOCR_MODEL", default="olmOCR-2-7B-1025-FP8"),
        ocr_api_key=_config_value(config, "OLMOCR_API_KEY"),
        tiles_long_side=int(_required_config(config, "TILE_LONG_SIDE_PX", default="1288")),
        tile_overlap_px=int(_required_config(config, "TILE_OVERLAP_PX", default="120")),
        viewport_overlap_px=int(_required_config(config, "VIEWPORT_OVERLAP_PX", default="120")),
        concurrency_min=int(_required_config(config, "OCR_MIN_CONCURRENCY", default="2")),
        concurrency_max=int(_required_config(config, "OCR_MAX_CONCURRENCY", default="8")),
        cft_version=_required_config(config, "CFT_VERSION", default="unknown"),
        cft_label=_required_config(config, "CFT_LABEL", default=""),
        playwright_channel=_required_config(config, "PLAYWRIGHT_CHANNEL", default="cft"),
        screenshot_style_hash=_config_value(config, "SCREENSHOT_STYLE_HASH", default=None),
    )


def _auth_headers(settings: CLISettings) -> dict[str, str]:
    headers: dict[str, str] = {}
    if settings.mdwb_api_key:
        headers["Authorization"] = f"Bearer {settings.mdwb_api_key}"
        headers["X-API-Key"] = settings.mdwb_api_key
    return headers


def _http_client(settings: CLISettings, http2: bool) -> httpx.Client:
    timeout = httpx.Timeout(connect=10.0, read=60.0, write=60.0, pool=30.0)
    return httpx.Client(
        base_url=settings.api_base_url,
        headers=_auth_headers(settings),
        timeout=timeout,
        http2=http2,
    )


def _slugify(url: str) -> str:
    safe = [c if c.isalnum() else "-" for c in url]
    slug = "".join(safe).strip("-")
    return slug[:48] or "job"


def _start_job(client: httpx.Client, payload: dict) -> str:
    response = client.post("/jobs", json=payload)
    response.raise_for_status()
    data = response.json()
    if "id" not in data:
        raise CLIError("/jobs response missing 'id'")
    return str(data["id"])


def _poll_job(client: httpx.Client, job_id: str, poll_interval: float, timeout_s: float) -> dict:
    started = time.perf_counter()
    with Progress(
        SpinnerColumn(),
        TextColumn("{task.description}"),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    ) as progress:
        task_id = progress.add_task(f"Polling job {job_id}…", total=None)
        while True:
            response = client.get(f"/jobs/{job_id}")
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:  # pragma: no cover - pass through
                raise CLIError(f"Failed to fetch job {job_id}: {exc.response.text}") from exc

            snapshot = response.json()
            state = snapshot.get("state") or snapshot.get("status")
            progress.update(task_id, description=f"Job {job_id}: {state}")

            if state == "DONE":
                return snapshot
            if state == "FAILED":
                raise CLIError(snapshot.get("error", "Job failed"))

            if time.perf_counter() - started > timeout_s:
                raise CLIError(f"Timed out after {timeout_s:.0f}s waiting for job {job_id}")
            time.sleep(poll_interval)


def _download_outputs(client: httpx.Client, job_id: str, snapshot: dict, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    def _write_text(relative: str, text: str) -> None:
        path = output_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    manifest = client.get(f"/jobs/{job_id}/manifest.json")
    manifest.raise_for_status()
    _write_text("manifest.json", manifest.text)

    markdown = client.get(f"/jobs/{job_id}/result.md")
    markdown.raise_for_status()
    _write_text("out.md", markdown.text)

    links = client.get(f"/jobs/{job_id}/links.json")
    if links.status_code == 200:
        _write_text("links.json", links.text)

    artifacts: Iterable[dict] = snapshot.get("artifacts", [])
    for artifact in artifacts:
        name = artifact.get("name") or artifact.get("path")
        if not name:
            continue
        resp = client.get(f"/jobs/{job_id}/artifact/{name}")
        if resp.status_code != 200:
            console.print(f"[yellow]Warning:[/] unable to download artifact {name}")
            continue
        artifact_path = output_dir / "artifact" / name
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_bytes(resp.content)


def run_capture(
    *,
    url: str,
    settings: CLISettings,
    out_dir: Path,
    tiles_long_side: Optional[int],
    overlap_px: Optional[int],
    concurrency: Optional[int],
    http2: bool,
    poll_interval: float,
    timeout_s: float,
) -> JobRunResult:
    payload = {
        "url": url,
        "capture_mode": "screenshot",
        "ocr": {
            "provider": "olmocr",
            "model": settings.ocr_model,
            "server": settings.ocr_server,
            "api_key": settings.ocr_api_key,
            "max_concurrency": concurrency or settings.concurrency_max,
        },
        "tiling": {
            "long_side_px": tiles_long_side or settings.tiles_long_side,
            "overlap_px": overlap_px or settings.tile_overlap_px,
        },
    }

    slug = _slugify(url)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    job_output = out_dir / f"{timestamp}_{slug}"

    with _http_client(settings, http2=http2) as client:
        start = time.perf_counter()
        job_id = _start_job(client, payload)
        snapshot = _poll_job(client, job_id, poll_interval=poll_interval, timeout_s=timeout_s)
        _download_outputs(client, job_id, snapshot, job_output)
        duration = time.perf_counter() - start

    tiles = snapshot.get("tiles", {})
    tiles_total = tiles.get("total") if isinstance(tiles, dict) else snapshot.get("tiles_total")
    tiles_failed = tiles.get("failed") if isinstance(tiles, dict) else snapshot.get("tiles_failed")

    return JobRunResult(
        url=url,
        job_id=job_id,
        duration_s=duration,
        output_dir=job_output,
        tiles_total=tiles_total,
        tiles_failed=tiles_failed,
        timings=snapshot.get("timings"),
    )


@app.command("show-env")
def show_env() -> None:
    """Print the capture/OCR environment that the CLI will use."""

    settings = load_settings()
    table = Table(title="Markdown Web Browser – OCR Environment", box=box.SIMPLE_HEAVY)
    table.add_column("Key")
    table.add_column("Value")
    rows = [
        ("API base", settings.api_base_url),
        ("MDWB API key", "set" if settings.mdwb_api_key else "(unset)"),
        ("OCR server", settings.ocr_server),
        ("OCR model", settings.ocr_model),
        ("OCR API key", "set" if settings.ocr_api_key else "(unset)"),
        ("Tiles longest side", str(settings.tiles_long_side)),
        ("Tile overlap", f"{settings.tile_overlap_px}px"),
        ("Viewport overlap", f"{settings.viewport_overlap_px}px"),
        ("OCR concurrency", f"{settings.concurrency_min}-{settings.concurrency_max}"),
        ("CfT version", f"{settings.cft_label} {settings.cft_version}".strip()),
        ("Playwright channel", settings.playwright_channel),
        ("Screenshot style hash", settings.screenshot_style_hash or "(auto)"),
    ]
    for key, value in rows:
        table.add_row(key, value)
    console.print(table)


@app.command()
def run(
    url: str = typer.Option(..., "--url", prompt=True, help="URL to capture."),
    out_dir: Path = typer.Option(
        Path("benchmarks/runs"), "--out-dir", help="Directory to store artifacts."
    ),
    tiles_long_side: Optional[int] = typer.Option(None, help="Override tile longest side (px)."),
    overlap_px: Optional[int] = typer.Option(None, help="Override tile overlap in pixels."),
    concurrency: Optional[int] = typer.Option(None, help="Override OCR max concurrency."),
    poll_interval: float = typer.Option(1.0, help="Seconds between /jobs polls."),
    timeout_s: float = typer.Option(900.0, help="Overall timeout in seconds."),
    http2: bool = typer.Option(True, "--http2/--no-http2", help="Use HTTP/2 for API calls."),
) -> None:
    """Run a single capture job and download artifacts/manifest."""

    settings = load_settings()
    try:
        result = run_capture(
            url=url,
            settings=settings,
            out_dir=out_dir,
            tiles_long_side=tiles_long_side,
            overlap_px=overlap_px,
            concurrency=concurrency,
            http2=http2,
            poll_interval=poll_interval,
            timeout_s=timeout_s,
        )
    except httpx.RequestError as exc:
        console.print(f"[red]Unable to reach API: {exc}[/]")
        raise typer.Exit(ExitCode.UPSTREAM_UNAVAILABLE) from exc
    except CLIError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(ExitCode.PARTIAL) from exc

    console.print(f"[green]Job {result.job_id} finished in {result.duration_s:.1f}s[/]")
    console.print(f"Artifacts stored under {result.output_dir}")
    if result.tiles_failed:
        console.print(f"[yellow]{result.tiles_failed} tiles failed[/]")
        raise typer.Exit(ExitCode.PARTIAL)


@app.command()
def bench(
    url_file: Path = typer.Option(
        ..., "--url-file", exists=True, readable=True, help="File with newline separated URLs."
    ),
    repeats: int = typer.Option(1, min=1, help="How many passes over the URL list."),
    shuffle: bool = typer.Option(False, "--shuffle/--no-shuffle", help="Shuffle URLs each repeat."),
    out_dir: Path = typer.Option(Path("benchmarks/bench"), help="Directory to store run outputs."),
    http2: bool = typer.Option(True, "--http2/--no-http2"),
    poll_interval: float = typer.Option(1.0, help="Seconds between poll requests."),
    timeout_s: float = typer.Option(900.0, help="Timeout per job in seconds."),
) -> None:
    """Run a batch of URLs and emit latency statistics."""

    urls = [
        line.strip() for line in url_file.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    if not urls:
        raise typer.BadParameter("URL file is empty")

    settings = load_settings()
    durations: list[float] = []
    failures: list[str] = []

    for iteration in range(repeats):
        batch = urls[:]
        if shuffle:
            random.shuffle(batch)
        for url in batch:
            console.rule(f"Run {iteration + 1} – {url}")
            try:
                result = run_capture(
                    url=url,
                    settings=settings,
                    out_dir=out_dir,
                    tiles_long_side=None,
                    overlap_px=None,
                    concurrency=None,
                    http2=http2,
                    poll_interval=poll_interval,
                    timeout_s=timeout_s,
                )
                durations.append(result.duration_s)
            except (CLIError, httpx.RequestError) as exc:
                failures.append(f"{url}: {exc}")
                console.print(f"[red]Failed {url}: {exc}[/]")

    table = Table(title="Latency Summary", box=box.SIMPLE_HEAVY)
    table.add_column("Metric")
    table.add_column("Value (s)")

    if durations:
        table.add_row("count", str(len(durations)))
        table.add_row("p50", f"{statistics.median(durations):.1f}")
        table.add_row("mean", f"{statistics.mean(durations):.1f}")
        for pct in (90, 95):
            table.add_row(f"p{pct}", f"{_percentile(durations, pct):.1f}")
    else:
        table.add_row("count", "0")
    console.print(table)

    if failures:
        console.print("[yellow]Failures:[/]")
        for failure in failures:
            console.print(f" - {failure}")
        raise typer.Exit(ExitCode.PARTIAL)


def _percentile(values: Iterable[float], percentile: int) -> float:
    sequence = sorted(values)
    if not sequence:
        return 0.0
    k = (len(sequence) - 1) * (percentile / 100)
    f = int(k)
    c = min(f + 1, len(sequence) - 1)
    if f == c:
        return sequence[int(k)]
    d0 = sequence[f] * (c - k)
    d1 = sequence[c] * (k - f)
    return d0 + d1


def main() -> None:
    app()


if __name__ == "__main__":
    main()
