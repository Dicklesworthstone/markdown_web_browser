from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

import scripts.update_smoke_pointers as usp

runner = CliRunner()


def _write_basic_run(source: Path, *, include_metrics: bool = True) -> None:
    source.mkdir()
    (source / "summary.md").write_text("summary", encoding="utf-8")
    (source / "manifest_index.json").write_text("[]", encoding="utf-8")
    if include_metrics:
        (source / "metrics.json").write_text("{}", encoding="utf-8")


def test_update_smoke_pointers_copies_files(tmp_path: Path):
    source = tmp_path / "2025-11-07"
    _write_basic_run(source)
    (source / "manifest_index.json").write_text('[{"category": "docs"}]', encoding="utf-8")
    weekly = tmp_path / "weekly_summary.json"
    weekly.write_text('{"generated_at": "now"}', encoding="utf-8")

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
    assert (root / "latest_manifest_index.json").read_text(encoding="utf-8") == '[{"category": "docs"}]'
    assert (root / "latest_metrics.json").read_text(encoding="utf-8") == "{}"
    assert (root / "weekly_summary.json").read_text(encoding="utf-8") == '{"generated_at": "now"}'
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


def test_update_missing_source_directory(tmp_path: Path):
    source = tmp_path / "missing"
    root = tmp_path / "pointers"
    result = runner.invoke(usp.app, [str(source), "--root", str(root)])

    assert result.exit_code != 0
    assert "Source directory not found" in result.output


def test_update_allows_missing_metrics(tmp_path: Path):
    source = tmp_path / "2025-11-08"
    _write_basic_run(source, include_metrics=False)
    root = tmp_path / "pointers"

    result = runner.invoke(usp.app, [str(source), "--root", str(root)])

    assert result.exit_code == 0
    assert (root / "latest_summary.md").exists()
    assert (root / "latest_manifest_index.json").exists()
    assert not (root / "latest_metrics.json").exists()


def test_update_smoke_pointers_uses_env_root(monkeypatch, tmp_path: Path):
    source = tmp_path / "2025-11-08"
    _write_basic_run(source)

    env_root = tmp_path / "env-root"
    monkeypatch.setenv("MDWB_SMOKE_ROOT", str(env_root))

    result = runner.invoke(usp.app, [str(source)])

    assert result.exit_code == 0
    assert (env_root / "latest_summary.md").exists()
    assert (env_root / "latest_manifest_index.json").exists()
    assert (env_root / "latest_metrics.json").exists()


def test_update_smoke_pointers_requires_weekly_when_flag_used(tmp_path: Path):
    source = tmp_path / "2025-11-10"
    _write_basic_run(source)

    root = tmp_path / "pointers"
    missing_weekly = tmp_path / "missing_weekly.json"

    result = runner.invoke(
        usp.app,
        [
            str(source),
            "--root",
            str(root),
            "--weekly-source",
            str(missing_weekly),
        ],
    )

    assert result.exit_code != 0
    assert "Required file missing" in result.output


def test_update_smoke_pointers_computes_slo(tmp_path: Path):
    source = tmp_path / "2025-11-11"
    source.mkdir()
    (source / "summary.md").write_text("summary", encoding="utf-8")
    manifest_entries = [
        {"job_id": "a1", "category": "docs", "capture_ms": 1000, "ocr_ms": 2000, "stitch_ms": 300, "total_ms": 3300},
        {"job_id": "b1", "category": "apps", "capture_ms": 2000, "ocr_ms": 4000, "stitch_ms": 700, "total_ms": 6700},
    ]
    (source / "manifest_index.json").write_text(json.dumps(manifest_entries), encoding="utf-8")
    (source / "metrics.json").write_text("{}", encoding="utf-8")

    budget_file = tmp_path / "production_set.json"
    budget_file.write_text(
        json.dumps(
            {
                "categories": [
                    {"name": "docs", "p95_budget_ms": 4000},
                    {"name": "apps", "p95_budget_ms": 8000},
                ]
            }
        ),
        encoding="utf-8",
    )

    root = tmp_path / "pointers"
    result = runner.invoke(
        usp.app,
        [
            str(source),
            "--root",
            str(root),
            "--budget-file",
            str(budget_file),
        ],
    )

    assert result.exit_code == 0
    slo_path = root / "latest_slo_summary.json"
    assert slo_path.exists()
    payload = json.loads(slo_path.read_text(encoding="utf-8"))
    assert set(payload["categories"].keys()) == {"docs", "apps"}
    assert payload["categories"]["docs"]["status"] == "within_budget"


def test_update_smoke_pointers_can_skip_slo(tmp_path: Path):
    source = tmp_path / "2025-11-12"
    _write_basic_run(source, include_metrics=False)

    root = tmp_path / "pointers"
    result = runner.invoke(
        usp.app,
        [
            str(source),
            "--root",
            str(root),
            "--no-compute-slo",
        ],
    )

    assert result.exit_code == 0
    assert not (root / "latest_slo_summary.json").exists()
