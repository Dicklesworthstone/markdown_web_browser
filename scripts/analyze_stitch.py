#!/usr/bin/env python3
"""Stitching diagnostics helper for overlap/seam/hyphen research."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import typer

cli = typer.Typer(help="Inspect seam marker stats and hyphen-break assists across runs.")

DOM_ASSIST_RE = re.compile(r"dom-assist:[^>]*reason=(?P<reason>[^,>]+)")


def _load_manifest_index(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise typer.BadParameter(f"Manifest index not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise typer.BadParameter("Manifest index must be a JSON array")
    return data


def _count_hyphen_assists(markdown_path: Path) -> int:
    if not markdown_path.exists():
        return 0
    count = 0
    for line in markdown_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        match = DOM_ASSIST_RE.search(line)
        if not match:
            continue
        reason = match.group("reason").strip().strip("'\"")
        if reason == "hyphen-break":
            count += 1
    return count


@cli.command()
def analyze(
    manifest_index: Path = typer.Argument(
        Path("benchmarks/production/latest_manifest_index.json"),
        help="Path to manifest_index.json produced by scripts/run_smoke.py",
    ),
    limit: int = typer.Option(20, help="Maximum number of rows to display (sorted by seam count)."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON output."),
) -> None:
    """Summarize seam markers + hyphen-break assists for recent runs."""

    entries = _load_manifest_index(manifest_index)
    results: list[dict[str, Any]] = []
    for entry in entries:
        run_dir = Path(entry.get("run_dir", ""))
        hyphen_count = _count_hyphen_assists(run_dir / "out.md") if run_dir else 0
        results.append(
            {
                "category": entry.get("category"),
                "slug": entry.get("slug"),
                "seam_marker_count": entry.get("seam_marker_count"),
                "seam_hash_count": entry.get("seam_hash_count"),
                "seam_event_count": entry.get("seam_event_count"),
                "hyphen_assists": hyphen_count,
                "run_dir": str(run_dir) if run_dir else None,
            }
        )

    results.sort(key=lambda row: (row.get("seam_marker_count") or 0), reverse=True)
    trimmed = results[:limit] if limit > 0 else results

    if json_output:
        typer.echo(json.dumps(trimmed, indent=2))
        return

    header = f"{'Category':<18} {'Slug':<24} {'Seams':>6} {'Hashes':>6} {'Events':>6} {'Hyphen':>6}"
    typer.echo(header)
    typer.echo("-" * len(header))
    for row in trimmed:
        typer.echo(
            "{category:<18} {slug:<24} {seams:>6} {hashes:>6} {events:>6} {hyphen:>6}".format(
                category=(row.get("category") or "")[:18],
                slug=(row.get("slug") or "")[:24],
                seams=row.get("seam_marker_count", 0) or 0,
                hashes=row.get("seam_hash_count", 0) or 0,
                events=row.get("seam_event_count", 0) or 0,
                hyphen=row.get("hyphen_assists", 0) or 0,
            )
        )


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
