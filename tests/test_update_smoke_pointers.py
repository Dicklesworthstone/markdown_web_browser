from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

import scripts.update_smoke_pointers as usp

runner = CliRunner()


def test_update_smoke_pointers_copies_files(tmp_path: Path):
    source = tmp_path / "2025-11-07"
    source.mkdir()
    (source / "summary.md").write_text("summary", encoding="utf-8")
    (source / "manifest_index.json").write_text("[{\"category\": \"docs\"}]", encoding="utf-8")
    (source / "metrics.json").write_text("{\"categories\": []}", encoding="utf-8")
    weekly = tmp_path / "weekly_summary.json"
    weekly.write_text("{\"generated_at\": \"now\"}", encoding="utf-8")

    root = tmp_path / "pointers"
    result = runner.invoke(
        usp.app,
        [
            str(source),
            "--root",
            str(root),
            "--weekly-source",
            str(weekly),
        ],
    )

    assert result.exit_code == 0
    assert (root / "latest_summary.md").read_text(encoding="utf-8") == "summary"
    assert (root / "latest_manifest_index.json").read_text(encoding="utf-8") == "[{\"category\": \"docs\"}]"
    assert (root / "latest_metrics.json").read_text(encoding="utf-8") == "{\"categories\": []}"
    assert (root / "weekly_summary.json").read_text(encoding="utf-8") == "{\"generated_at\": \"now\"}"
    assert (root / "latest.txt").read_text(encoding="utf-8").strip() == "2025-11-07"


def test_update_missing_required_file(tmp_path: Path):
    source = tmp_path / "2025-11-07"
    source.mkdir()
    root = tmp_path / "pointers"

    result = runner.invoke(
        usp.app,
        [
            str(source),
            "--root",
            str(root),
        ],
    )

    assert result.exit_code != 0
    assert "Required file missing" in result.output
