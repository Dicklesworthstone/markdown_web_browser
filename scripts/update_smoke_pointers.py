#!/usr/bin/env python3
"""Refresh smoke pointer files (latest_summary/manifest/etc.) from a specific run."""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

import typer

from scripts.compute_slo import compute_slo_summary, load_budgets, write_prom_metrics

app = typer.Typer(help="Copy summary/manifest/metrics from a run directory into the latest_* pointers.")


def _copy_file(src: Path, dest: Path, *, required: bool) -> None:
    if not src.exists():
        if required:
            raise typer.BadParameter(f"Required file missing: {src}")
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)


def _default_budget_path(root: Path) -> Path:
    candidate = Path("benchmarks/production_set.json")
    if candidate.exists():
        return candidate
    # Fall back to sibling of root (helps when root is elsewhere, e.g., env overrides).
    sibling = root.parent / "production_set.json"
    return sibling if sibling.exists() else candidate


def _compute_slo_summary(
    *,
    manifest_path: Path,
    root: Path,
    budget_file: Path | None,
) -> dict[str, object]:
    manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest_payload, list):
        raise typer.BadParameter(f"Manifest index must be an array: {manifest_path}")
    budget_path = budget_file or _default_budget_path(root)
    budgets = load_budgets(budget_path)
    summary = compute_slo_summary(manifest_payload, budget_map=budgets)
    payload: dict[str, object] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "manifest_path": str(manifest_path),
        "root": str(root),
        "budget_file": str(budget_path),
    }
    payload.update(summary)
    return payload


def _sync_prom_export(prom_path: Path) -> None:
    export_target = os.environ.get("MDWB_SLO_PROM_EXPORT")
    if not export_target or not prom_path.exists():
        return
    dest = Path(export_target)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(prom_path, dest)


@app.command()
def update(
    source: Path = typer.Argument(..., help="Run directory containing summary/manifest/metrics files."),
    root: Path | None = typer.Option(None, help="Pointer root (defaults to MDWB_SMOKE_ROOT)."),
    weekly_source: Path = typer.Option(
        None,
        "--weekly-source",
        help="Optional weekly_summary.json file to copy alongside latest pointers.",
    ),
    compute_slo: bool = typer.Option(
        True,
        "--compute-slo/--no-compute-slo",
        help="When enabled, recompute latest_slo_summary.json using the updated manifest pointer.",
    ),
    weekly_slo_source: Path = typer.Option(
        None,
        "--weekly-slo-source",
        help="Optional weekly_slo.json file to copy (defaults to <source>/weekly_slo.json).",
    ),
    weekly_slo_prom_source: Path = typer.Option(
        None,
        "--weekly-slo-prom-source",
        help="Optional weekly_slo.prom file to copy (defaults to <source>/weekly_slo.prom).",
    ),
    budget_file: Path | None = typer.Option(
        None,
        "--budget-file",
        help="Budget definition used when computing SLO summaries (defaults to benchmarks/production_set.json).",
    ),
) -> None:
    """Refresh pointer files from ``source`` into ``root``."""

    if not source.exists():
        raise typer.BadParameter(f"Source directory not found: {source}")
    root_path = root or Path(os.environ.get("MDWB_SMOKE_ROOT", "benchmarks/production"))
    root_path.mkdir(parents=True, exist_ok=True)

    summary_src = source / "summary.md"
    manifest_src = source / "manifest_index.json"
    metrics_src = source / "metrics.json"
    slo_summary_src = source / "slo_summary.json"
    slo_prom_src = source / "slo.prom"

    _copy_file(summary_src, root_path / "latest_summary.md", required=True)
    latest_manifest = root_path / "latest_manifest_index.json"
    _copy_file(manifest_src, latest_manifest, required=True)
    _copy_file(metrics_src, root_path / "latest_metrics.json", required=False)

    marker = root_path / "latest.txt"
    marker.write_text(f"{source.name}\n", encoding="utf-8")

    if weekly_source:
        weekly_dest = root_path / "weekly_summary.json"
        _copy_file(weekly_source, weekly_dest, required=True)

    weekly_slo_src = weekly_slo_source or (source / "weekly_slo.json")
    weekly_slo_dest = root_path / "weekly_slo.json"
    if weekly_slo_source and not weekly_slo_src.exists():
        raise typer.BadParameter(f"Required file missing: {weekly_slo_src}")
    if weekly_slo_src.exists():
        _copy_file(weekly_slo_src, weekly_slo_dest, required=False)
        prom_source = weekly_slo_prom_source or (source / "weekly_slo.prom")
        if weekly_slo_prom_source and not prom_source.exists():
            raise typer.BadParameter(f"Required file missing: {prom_source}")
        weekly_prom_dest = root_path / "weekly_slo.prom"
        if prom_source.exists():
            _copy_file(prom_source, weekly_prom_dest, required=False)

    if compute_slo:
        slo_payload = _compute_slo_summary(
            manifest_path=latest_manifest,
            root=root_path,
            budget_file=budget_file,
        )
        slo_dest = root_path / "latest_slo_summary.json"
        slo_dest.write_text(json.dumps(slo_payload, indent=2), encoding="utf-8")
        prom_dest = root_path / "latest_slo.prom"
        write_prom_metrics(summary=slo_payload, output_path=prom_dest)
        _sync_prom_export(prom_dest)
    else:
        _copy_file(slo_summary_src, root_path / "latest_slo_summary.json", required=False)
        dest_prom = root_path / "latest_slo.prom"
        _copy_file(slo_prom_src, dest_prom, required=False)
        if dest_prom.exists():
            _sync_prom_export(dest_prom)

    typer.secho(
        f"Updated pointers under {root_path} using run {source}", fg=typer.colors.GREEN
    )


if __name__ == "__main__":  # pragma: no cover
    app()
