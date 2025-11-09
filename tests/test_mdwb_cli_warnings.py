from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from scripts import mdwb_cli

runner = CliRunner()


def test_warnings_tail_json_includes_enriched_fields(tmp_path: Path, monkeypatch) -> None:
    log_path = tmp_path / "warnings.jsonl"
    record = {
        "timestamp": "2025-11-08T08:00:00Z",
        "job_id": "run-1",
        "warnings": [],
        "blocklist_hits": {},
        "sweep_stats": {
            "shrink_events": 1,
            "retry_attempts": 1,
            "overlap_pairs": 4,
            "overlap_match_ratio": 0.9,
        },
        "validation_failures": ["tile checksum mismatch", "tile decode failed"],
        "seam_markers": {
            "count": 3,
            "unique_tiles": 2,
            "unique_hashes": 2,
            "sample": [
                {"tile_index": 0, "position": "top", "hash": "abc111"},
                {"tile_index": 1, "position": "bottom", "hash": "def222"},
            ],
        },
        "dom_assist_summary": {
            "count": 2,
            "reasons": ["low-alpha", "punctuation"],
            "reason_counts": [
                {"reason": "low-alpha", "count": 1},
                {"reason": "punctuation", "count": 1},
            ],
            "sample": {"reason": "low-alpha", "dom_text": "Revenue", "tile_index": 0, "line": 3},
        },
    }
    log_path.write_text(json.dumps(record) + "\n", encoding="utf-8")

    result = runner.invoke(
        mdwb_cli.cli,
        [
            "warnings",
            "tail",
            "--count",
            "1",
            "--json",
            "--log-path",
            str(log_path),
        ],
    )

    assert result.exit_code == 0
    assert "\"validation_failure_count\"" in result.output
    assert "sweep_summary" in result.output
    assert "dom_assist_summary" in result.output
    assert "overlap_match_ratio" in result.output
    assert "seam_summary_text" in result.output


def test_warnings_tail_pretty_output(tmp_path: Path, monkeypatch) -> None:
    log_path = tmp_path / "warnings.jsonl"
    record = {
        "timestamp": "2025-11-08T08:00:00Z",
        "job_id": "run-2",
        "warnings": [
            {"code": "canvas-heavy", "count": 5, "threshold": 3, "message": "canvas"}
        ],
        "blocklist_hits": {"#cookie": 1},
        "sweep_stats": {"shrink_events": 0, "retry_attempts": 0, "overlap_pairs": 2, "overlap_match_ratio": 0.95},
        "validation_failures": [],
        "seam_markers": {
            "count": 1,
            "unique_tiles": 1,
            "unique_hashes": 1,
            "sample": [{"tile_index": 2, "position": "bottom", "hash": "xyz999"}],
        },
        "dom_assist_summary": {
            "count": 1,
            "reasons": ["low-alpha"],
            "reason_counts": [{"reason": "low-alpha", "count": 1}],
            "sample": {"reason": "low-alpha", "dom_text": "Revenue", "tile_index": 2, "line": 5},
        },
    }
    log_path.write_text(json.dumps(record) + "\n", encoding="utf-8")

    result = runner.invoke(
        mdwb_cli.cli,
        [
            "warnings",
            "tail",
            "--log-path",
            str(log_path),
            "--count",
            "1",
        ],
    )

    assert result.exit_code == 0
    assert "canvas" in result.output
    assert "pairs=2" in result.output
    assert "xyz999" in result.output
    assert "assist" in result.output
    assert "job-2" not in result.output  # ensures only run-2 shown once


def test_warnings_tail_handles_missing_file(tmp_path: Path) -> None:
    missing_log = tmp_path / "missing.jsonl"

    result = runner.invoke(
        mdwb_cli.cli,
        [
            "warnings",
            "tail",
            "--log-path",
            str(missing_log),
            "--count",
            "5",
        ],
    )

    assert result.exit_code == 0
    assert "Warning log not found" in result.output
    assert str(missing_log) in result.output
