#!/usr/bin/env python3
"""Refresh smoke pointer files (latest_summary/manifest/etc.) from a specific run."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import typer

DEFAULT_ROOT = Path(os.environ.get("MDWB_SMOKE_ROOT", "benchmarks/production"))

app = typer.Typer(help="Copy summary/manifest/metrics from a run directory into the latest_* pointers.")


def _copy_file(src: Path, dest: Path, *, required: bool) -> None:
    if not src.exists():
        if required:
            raise typer.BadParameter(f"Required file missing: {src}")
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)


@app.command()
def update(
    source: Path = typer.Argument(..., help="Run directory containing summary/manifest/metrics files."),
    root: Path = typer.Option(DEFAULT_ROOT, help="Pointer root (defaults to MDWB_SMOKE_ROOT)."),
    weekly_source: Path = typer.Option(
        None,
        "--weekly-source",
        help="Optional weekly_summary.json file to copy alongside latest pointers.",
    ),
) -> None:
    """Refresh pointer files from ``source`` into ``root``."""

    if not source.exists():
        raise typer.BadParameter(f"Source directory not found: {source}")
    root.mkdir(parents=True, exist_ok=True)

    summary_src = source / "summary.md"
    manifest_src = source / "manifest_index.json"
    metrics_src = source / "metrics.json"

    _copy_file(summary_src, root / "latest_summary.md", required=True)
    _copy_file(manifest_src, root / "latest_manifest_index.json", required=True)
    _copy_file(metrics_src, root / "latest_metrics.json", required=False)

    marker = root / "latest.txt"
    marker.write_text(f"{source.name}\n", encoding="utf-8")

    if weekly_source:
        weekly_dest = root / "weekly_summary.json"
        _copy_file(weekly_source, weekly_dest, required=True)

    typer.secho(
        f"Updated pointers under {root} using run {source}", fg=typer.colors.GREEN
    )


if __name__ == "__main__":  # pragma: no cover
    app()
