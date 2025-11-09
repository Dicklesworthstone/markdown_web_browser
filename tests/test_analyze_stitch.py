from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from scripts import analyze_stitch

runner = CliRunner()


def _write_run(tmp_path: Path, slug: str, *, hyphen: bool) -> Path:
    run_dir = tmp_path / slug
    run_dir.mkdir(parents=True, exist_ok=True)
    out_md = run_dir / "out.md"
    if hyphen:
        out_md.write_text(
            "## Heading\n<!-- dom-assist: tile=0, line=1, reason=hyphen-break, replacement='Revenue Growth' -->\n",
            encoding="utf-8",
        )
    else:
        out_md.write_text("## Heading\n", encoding="utf-8")
    return run_dir


def _write_manifest_index(tmp_path: Path, entries: list[dict[str, object]]) -> Path:
    manifest_path = tmp_path / "manifest_index.json"
    manifest_path.write_text(json.dumps(entries), encoding="utf-8")
    return manifest_path


def test_analyze_json_output(tmp_path: Path):
    run_a = _write_run(tmp_path, "job-a", hyphen=True)
    run_b = _write_run(tmp_path, "job-b", hyphen=False)
    manifest = _write_manifest_index(
        tmp_path,
        [
            {
                "category": "docs",
                "slug": "job-a",
                "run_dir": str(run_a),
                "seam_marker_count": 5,
                "seam_hash_count": 4,
                "seam_event_count": 2,
            },
            {
                "category": "apps",
                "slug": "job-b",
                "run_dir": str(run_b),
                "seam_marker_count": 1,
            },
        ],
    )

    result = runner.invoke(
        analyze_stitch.cli,
        [str(manifest), "--json"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload[0]["hyphen_assists"] == 1
    assert payload[1]["hyphen_assists"] == 0


def test_analyze_table_output(tmp_path: Path):
    run_a = _write_run(tmp_path, "job-a", hyphen=False)
    manifest = _write_manifest_index(
        tmp_path,
        [
            {
                "category": "docs",
                "slug": "job-a",
                "run_dir": str(run_a),
                "seam_marker_count": 3,
                "seam_hash_count": 2,
                "seam_event_count": 1,
            }
        ],
    )

    result = runner.invoke(analyze_stitch.cli, [str(manifest), "--limit", "1"])

    assert result.exit_code == 0
    assert "docs" in result.output
    assert "job-a" in result.output
