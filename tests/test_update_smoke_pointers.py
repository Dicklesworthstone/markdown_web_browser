from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from scripts import run_smoke
import scripts.update_smoke_pointers as usp

runner = CliRunner()


@pytest.mark.parametrize("seam_summary", [None, {"event_count": 4}])
def test_weekly_summary_retains_category_report_with_seam_metadata(
    tmp_path, monkeypatch, seam_summary
):
    root = tmp_path / "production"
    history = root / datetime.now(timezone.utc).date().isoformat()
    history.mkdir(parents=True)
    entry = {
        "category": "docs",
        "capture_ms": 1000,
        "total_ms": 3000,
        "timings": {"ocr_ms": 2000},
        "seam_marker_count": 2,
        "seam_hash_count": 1,
        "seam_event_count": 4,
        "seam_markers_summary": seam_summary,
    }
    (history / "manifest_index.json").write_text(json.dumps([entry]), encoding="utf-8")
    summary_path = root / "weekly_summary.json"
    monkeypatch.setattr(run_smoke, "PRODUCTION_ROOT", root)
    monkeypatch.setattr(run_smoke, "WEEKLY_SUMMARY_PATH", summary_path)
    run_smoke.update_weekly_summary({"categories": [{"name": "docs", "p95_budget_ms": 9999}]})
    report = json.loads(summary_path.read_text(encoding="utf-8"))
    assert report["window_days"] == 7
    assert report["generated_at"]
    assert len(report["categories"]) == 1
    category = report["categories"][0]
    assert category["name"] == "docs"
    assert category["runs"] == 1
    assert category["capture_ms"] == {"p50": 1000, "p95": 1000, "p99": 1000}
    assert category["ocr_ms"] == {"p50": 2000, "p95": 2000, "p99": 2000}
    assert category["total_ms"] == {"p50": 3000, "p95": 3000, "p99": 3000}
    assert category["seam_markers"]["events"] == {"p50": 4, "p95": 4}


def test_smoke_dry_run_previews_without_api_calls_or_filesystem_mutation(
    tmp_path, monkeypatch, capsys
):
    production = tmp_path / "production"
    production.mkdir()
    (production / "latest.txt").write_text("existing-real-run\n", encoding="utf-8")
    config_path = tmp_path / "production_set.json"
    config_path.write_text(
        json.dumps(
            {
                "categories": [
                    {
                        "name": "Docs",
                        "p95_budget_ms": 5000,
                        "urls": [{"url": "https://example.com"}],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    before = {
        str(path.relative_to(tmp_path)): path.read_bytes() if path.is_file() else None
        for path in tmp_path.rglob("*")
    }
    monkeypatch.setattr(run_smoke, "PRODUCTION_SET_PATH", config_path)
    monkeypatch.setattr(run_smoke, "PRODUCTION_ROOT", production)

    def forbid(*args, **kwargs):
        raise AssertionError(
            "Dry-run preview must not load credentials, capture, or publish reports"
        )

    monkeypatch.setattr(run_smoke.olmocr_cli, "load_settings", forbid)
    monkeypatch.setattr(run_smoke, "run_category", forbid)
    monkeypatch.setattr(run_smoke, "update_latest_markers", forbid)
    monkeypatch.setattr(sys, "argv", ["run_smoke.py", "--dry-run", "--category", "Docs"])
    run_smoke.main()
    after = {
        str(path.relative_to(tmp_path)): path.read_bytes() if path.is_file() else None
        for path in tmp_path.rglob("*")
    }
    assert after == before
    output = capsys.readouterr().out
    assert "https://example.com" in output
    assert "5000 ms" in output
    assert "no capture or qualification was performed" in output


def test_weekly_aggregates_exclude_preserved_legacy_synthetic_records(tmp_path, monkeypatch):
    root = tmp_path / "production"
    history = root / datetime.now(timezone.utc).date().isoformat()
    history.mkdir(parents=True)
    legacy_manifest = history / "legacy-synthetic.json"
    legacy_manifest.write_text(
        json.dumps({"environment": {"cft_version": "dry-run"}}), encoding="utf-8"
    )
    real = {
        "category": "Docs",
        "job_id": "real-fixture",
        "capture_ms": 1000,
        "total_ms": 3000,
        "timings": {"ocr_ms": 2000},
    }
    dry_id = {**real, "job_id": "dry-Docs-example", "total_ms": 999999}
    dry_manifest = {
        **real,
        "job_id": "legacy-fixture",
        "manifest": str(legacy_manifest),
        "total_ms": 888888,
    }
    dry_flag = {**real, "job_id": "flag-fixture", "synthetic": True, "total_ms": 777777}
    index_path = history / "manifest_index.json"
    index_path.write_text(json.dumps([real, dry_id, dry_manifest, dry_flag]), encoding="utf-8")
    original_index = index_path.read_bytes()
    original_legacy = legacy_manifest.read_bytes()
    weekly = root / "weekly_summary.json"
    weekly_slo = root / "weekly_slo.json"
    monkeypatch.setattr(run_smoke, "PRODUCTION_ROOT", root)
    monkeypatch.setattr(run_smoke, "WEEKLY_SUMMARY_PATH", weekly)
    monkeypatch.setattr(run_smoke, "WEEKLY_SLO_PATH", weekly_slo)
    monkeypatch.setattr(run_smoke, "WEEKLY_SLO_PROM_PATH", root / "weekly_slo.prom")
    run_smoke.update_weekly_summary({"categories": [{"name": "Docs", "p95_budget_ms": 5000}]})
    run_smoke.update_weekly_slo_summary({"Docs": 5000})
    assert json.loads(weekly.read_text(encoding="utf-8"))["categories"][0]["runs"] == 1
    summary = json.loads(weekly_slo.read_text(encoding="utf-8"))["summary"]
    assert summary["aggregate"]["count"] == 1
    assert summary["aggregate"]["p95_total_ms"] == 3000
    assert index_path.read_bytes() == original_index
    assert legacy_manifest.read_bytes() == original_legacy


@pytest.mark.parametrize("only_synthetic", [False, True])
def test_latest_smoke_pointers_reject_synthetic_records_before_writes(
    tmp_path, monkeypatch, only_synthetic
):
    root = tmp_path / "production"
    history = root / "2026-10-13"
    history.mkdir(parents=True)
    marker = root / "latest.txt"
    marker.write_text("previous-real-run\n", encoding="utf-8")
    entries = [{"job_id": "dry-fixture"}]
    if not only_synthetic:
        entries.append({"job_id": "real-fixture"})
    (history / "manifest_index.json").write_text(json.dumps(entries), encoding="utf-8")
    monkeypatch.setattr(run_smoke, "PRODUCTION_ROOT", root)
    with pytest.raises(ValueError, match="only real capture records"):
        run_smoke.update_latest_markers(history)
    assert marker.read_text(encoding="utf-8") == "previous-real-run\n"


def test_synthetic_only_history_does_not_preserve_a_green_weekly_slo(tmp_path, monkeypatch):
    root = tmp_path / "production"
    history = root / datetime.now(timezone.utc).date().isoformat()
    history.mkdir(parents=True)
    index_path = history / "manifest_index.json"
    index_path.write_text(
        json.dumps([{"job_id": "dry-fixture", "total_ms": 1000}]), encoding="utf-8"
    )
    original_index = index_path.read_bytes()
    weekly_slo = root / "weekly_slo.json"
    weekly_slo.write_text(json.dumps({"summary": {"status": "within_budget"}}), encoding="utf-8")
    monkeypatch.setattr(run_smoke, "PRODUCTION_ROOT", root)
    monkeypatch.setattr(run_smoke, "WEEKLY_SLO_PATH", weekly_slo)
    monkeypatch.setattr(run_smoke, "WEEKLY_SLO_PROM_PATH", root / "weekly_slo.prom")
    run_smoke.update_weekly_slo_summary({"Docs": 5000})
    summary = json.loads(weekly_slo.read_text(encoding="utf-8"))["summary"]
    assert summary["status"] == "no_data"
    assert summary["aggregate"]["count"] == 0
    assert index_path.read_bytes() == original_index


def _write_basic_run(
    source: Path,
    *,
    include_metrics: bool = True,
    include_weekly_slo: bool = False,
) -> None:
    source.mkdir()
    (source / "summary.md").write_text("summary", encoding="utf-8")
    (source / "manifest_index.json").write_text("[]", encoding="utf-8")
    if include_metrics:
        (source / "metrics.json").write_text("{}", encoding="utf-8")
    if include_weekly_slo:
        (source / "weekly_slo.json").write_text('{"summary": {}}', encoding="utf-8")
        (source / "weekly_slo.prom").write_text("weekly_prom\n", encoding="utf-8")


def test_update_smoke_pointers_copies_files(tmp_path: Path):
    source = tmp_path / "2025-11-07"
    _write_basic_run(source, include_weekly_slo=True)
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
    assert (root / "latest_manifest_index.json").read_text(
        encoding="utf-8"
    ) == '[{"category": "docs"}]'
    assert (root / "latest_metrics.json").read_text(encoding="utf-8") == "{}"
    assert (root / "weekly_summary.json").read_text(encoding="utf-8") == '{"generated_at": "now"}'
    assert (root / "weekly_slo.json").read_text(encoding="utf-8") == '{"summary": {}}'
    assert (root / "weekly_slo.prom").read_text(encoding="utf-8") == "weekly_prom\n"
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
    _write_basic_run(source)
    manifest_entries = [
        {
            "job_id": "a1",
            "category": "docs",
            "capture_ms": 1000,
            "ocr_ms": 2000,
            "stitch_ms": 300,
            "total_ms": 3300,
        },
        {
            "job_id": "b1",
            "category": "apps",
            "capture_ms": 2000,
            "ocr_ms": 4000,
            "stitch_ms": 700,
            "total_ms": 6700,
        },
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
    prom_path = root / "latest_slo.prom"
    assert prom_path.exists()
    assert "mdwb_slo" in prom_path.read_text(encoding="utf-8")


def test_update_smoke_pointers_can_skip_slo(tmp_path: Path):
    source = tmp_path / "2025-11-12"
    _write_basic_run(source, include_metrics=False)
    (source / "slo_summary.json").write_text('{"status": "within_budget"}', encoding="utf-8")
    (source / "slo.prom").write_text("metric 1", encoding="utf-8")

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
    assert (root / "latest_slo_summary.json").read_text(encoding="utf-8")
    assert (root / "latest_slo.prom").read_text(encoding="utf-8") == "metric 1"


def test_update_smoke_pointers_requires_explicit_weekly_slo(tmp_path: Path):
    source = tmp_path / "2025-11-13"
    _write_basic_run(source)

    root = tmp_path / "pointers"
    missing = tmp_path / "no_weekly_slo.json"
    result = runner.invoke(
        usp.app,
        [
            str(source),
            "--root",
            str(root),
            "--weekly-slo-source",
            str(missing),
        ],
    )

    assert result.exit_code != 0
    assert "Required file missing" in result.output


def test_update_smoke_pointers_syncs_prom_export(tmp_path: Path, monkeypatch):
    source = tmp_path / "2025-11-14"
    _write_basic_run(source)
    manifest_entries = [
        {"job_id": "a1", "category": "docs", "total_ms": 1500},
    ]
    (source / "manifest_index.json").write_text(json.dumps(manifest_entries), encoding="utf-8")
    export_path = tmp_path / "export.prom"
    monkeypatch.setenv("MDWB_SLO_PROM_EXPORT", str(export_path))

    root = tmp_path / "pointers"
    result = runner.invoke(usp.app, [str(source), "--root", str(root)])

    assert result.exit_code == 0
    assert export_path.exists()
    assert export_path.read_text(encoding="utf-8") == (root / "latest_slo.prom").read_text(
        encoding="utf-8"
    )
