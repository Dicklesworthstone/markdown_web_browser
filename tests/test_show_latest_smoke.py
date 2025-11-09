from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

from typer.testing import CliRunner

MODULE_NAME = "scripts.show_latest_smoke"
runner = CliRunner()


def _load_cli():
    if MODULE_NAME in sys.modules:
        del sys.modules[MODULE_NAME]
    return importlib.import_module(MODULE_NAME)


def _write_pointer_files(
    root: Path,
    *,
    include_weekly: bool = True,
    include_slo: bool = False,
    over_budget: bool = False,
    use_seam_marker_list: bool = False,
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "latest.txt").write_text("2025-11-08\n", encoding="utf-8")
    (root / "latest_summary.md").write_text("# Summary\n\nSmoke output", encoding="utf-8")
    manifest_rows = [
        {
            "category": "docs",
            "url": "https://example.com/article",
            "capture_ms": 1200,
            "total_ms": 2200,
            "sweep_stats": {"overlap_match_ratio": 0.9},
            "validation_failures": ["tile checksum mismatch"],
            "seam_marker_count": 2,
            "seam_hash_count": 2,
            "seam_markers_summary": {
                "count": 2,
                "unique_hashes": 2,
                "event_count": 1,
            },
        },
        {
            "category": "apps",
            "url": "https://example.org/dashboard",
            "capture_ms": 3200,
            "total_ms": 4200,
        },
    ]
    if use_seam_marker_list:
        manifest_rows[0].pop("seam_marker_count", None)
        manifest_rows[0].pop("seam_hash_count", None)
        manifest_rows[0].pop("seam_markers_summary", None)
        manifest_rows[0].pop("seam_event_count", None)
        manifest_rows[0]["seam_markers"] = [
            {"hash": "tile-a"},
            {"hash": "tile-b"},
            {"hash": "tile-a"},  # duplicate hash exercises unique counting
        ]
    (root / "latest_manifest_index.json").write_text(json.dumps(manifest_rows), encoding="utf-8")
    metrics_payload = {
        "date": "2025-11-08",
        "categories": [{"name": "docs", "p95_capture_ms": 1200, "p95_total_ms": 2200}],
    }
    (root / "latest_metrics.json").write_text(json.dumps(metrics_payload), encoding="utf-8")
    if include_weekly:
        weekly_payload = {
            "window_days": 7,
            "generated_at": "2025-11-08T00:00:00Z",
            "categories": [
                {
                    "name": "Docs",
                    "runs": 5,
                    "budget_ms": 20000,
                    "capture_ms": {"p50": 11000, "p95": 15000, "p99": 18000},
                    "total_ms": {"p50": 19000, "p95": 26000 if over_budget else 18000, "p99": 30000},
                    "ocr_ms": {"p50": 6000, "p95": 9000, "p99": 12000},
                    "seam_markers": {
                        "count": {"p50": 1, "p95": 2},
                        "hashes": {"p50": 1, "p95": 1},
                        "events": {"p50": 0, "p95": 1},
                    },
                    "slo": {
                        "capture_budget_ms": 30000,
                        "capture_p99_ms": 32000 if over_budget else 20000,
                        "capture_ok": not over_budget,
                        "ocr_budget_ms": 18000,
                        "ocr_p99_ms": 15000,
                        "ocr_ok": True,
                    },
                }
            ],
        }
        (root / "weekly_summary.json").write_text(json.dumps(weekly_payload), encoding="utf-8")
    if include_slo:
        slo_payload = {
            "generated_at": "2025-11-08T01:00:00Z",
            "categories": {
                "docs": {
                    "count": 5,
                    "budget_ms": 25000,
                    "p50_capture_ms": 11000,
                    "p95_capture_ms": 15000,
                    "p50_ocr_ms": 6000,
                    "p95_ocr_ms": 9000,
                    "p50_total_ms": 18000,
                    "p95_total_ms": 23000,
                    "budget_breaches": 0,
                    "status": "within_budget",
                }
            },
            "aggregate": {"count": 5, "p50_total_ms": 18000, "p95_total_ms": 23000, "budget_breaches": 0},
        }
        (root / "latest_slo_summary.json").write_text(json.dumps(slo_payload), encoding="utf-8")


def _invoke_show(tmp_path: Path, *args: str):
    module = _load_cli()
    cmd = ["show", "--root", str(tmp_path)]
    cmd.extend(args)
    return runner.invoke(module.app, cmd)


def test_show_latest_smoke_respects_env_root(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("MDWB_SMOKE_ROOT", str(tmp_path))
    module = _load_cli()
    _write_pointer_files(tmp_path)
    result = runner.invoke(module.app, ["show", "--manifest", "--limit", "1"])
    assert result.exit_code == 0
    assert "Latest smoke run" in result.output


def test_show_latest_smoke_missing_pointer(tmp_path: Path):
    result = _invoke_show(tmp_path)
    assert result.exit_code == 1
    assert "No smoke runs" in result.output


def test_show_latest_smoke_respects_limit_and_no_summary(tmp_path: Path):
    _write_pointer_files(tmp_path)
    result = _invoke_show(
        tmp_path,
        "--manifest",
        "--limit",
        "1",
        "--metrics",
        "--no-summary",
        "--no-weekly",
    )
    assert result.exit_code == 0
    output = result.output
    assert "# Summary" not in output  # summary suppressed
    assert "https://example.com/article" in output
    assert "https://example.org/dashboard" not in output  # limit trimmed
    assert "Aggregated Metrics" in output
    assert "overlap=0.90" in output
    assert "validation_failures=1" in output
    assert "seams=2 hashes=2" in output


def test_show_latest_smoke_weekly_highlights_over_budget(tmp_path: Path):
    _write_pointer_files(tmp_path, over_budget=True)
    result = _invoke_show(tmp_path, "--weekly", "--no-summary", "--no-manifest", "--no-metrics")
    assert result.exit_code == 0
    assert "Weekly Summary" in result.output
    assert "⚠️ over budget" in result.output
    assert "Seam markers p50/p95: 1/2" in result.output
    assert "Seam events p50/p95: 0/1" in result.output
    assert "Capture SLO:" in result.output
    assert "OCR SLO:" in result.output


def test_show_latest_smoke_weekly_missing_file(tmp_path: Path):
    _write_pointer_files(tmp_path, include_weekly=False)
    result = _invoke_show(tmp_path, "--weekly")
    assert result.exit_code == 1
    assert "weekly_summary.json missing" in result.output


def test_show_latest_smoke_slo_summary(tmp_path: Path):
    _write_pointer_files(tmp_path, include_weekly=False, include_slo=True)
    result = _invoke_show(tmp_path, "--slo", "--no-summary", "--no-manifest", "--no-metrics", "--no-weekly")
    assert result.exit_code == 0
    assert "SLO Summary" in result.output
    assert "| docs | 5 |" in result.output


def test_show_latest_smoke_slo_missing_file(tmp_path: Path):
    _write_pointer_files(tmp_path, include_weekly=False, include_slo=False)
    result = _invoke_show(tmp_path, "--slo")
    assert result.exit_code == 1
    assert "latest_slo_summary.json missing" in result.output


def test_show_latest_smoke_json_output(tmp_path: Path):
    _write_pointer_files(tmp_path, include_slo=True)
    result = _invoke_show(
        tmp_path,
        "--manifest",
        "--limit",
        "1",
        "--metrics",
        "--weekly",
        "--slo",
        "--json",
    )
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["run_date"] == "2025-11-08"
    assert payload["root"] == str(tmp_path)
    assert payload["summary_markdown"].startswith("# Summary")
    assert len(payload["manifest"]) == 1
    assert payload["manifest"][0]["overlap_match_ratio"] == 0.9
    assert payload["manifest"][0]["validation_failure_count"] == 1
    assert payload["manifest"][0]["seam_marker_count"] == 2
    assert payload["manifest"][0]["seam_hash_count"] == 2
    assert payload["manifest"][0]["seam_event_count"] == 1
    assert "metrics" in payload
    assert "weekly_summary" in payload
    assert "slo_summary" in payload
    weekly_seams = payload["weekly_summary"]["categories"][0]["seam_markers"]
    assert weekly_seams["count"]["p95"] == 2
    assert weekly_seams["events"]["p95"] == 1
    slo = payload["weekly_summary"]["categories"][0]["slo"]
    assert slo["capture_ok"] is True
    assert slo["ocr_ok"] is True
    assert payload["slo_summary"]["categories"]["docs"]["status"] == "within_budget"


def test_show_latest_smoke_manifest_missing(tmp_path: Path):
    _write_pointer_files(tmp_path)
    (tmp_path / "latest_manifest_index.json").unlink()
    result = _invoke_show(tmp_path, "--manifest")
    assert result.exit_code == 1
    assert "latest_manifest_index" in result.output


def test_show_latest_smoke_manifest_missing_json(tmp_path: Path):
    _write_pointer_files(tmp_path)
    (tmp_path / "latest_manifest_index.json").unlink()
    result = _invoke_show(tmp_path, "--manifest", "--json")
    assert result.exit_code == 1
    assert "latest_manifest_index" in result.output


def test_show_latest_smoke_json_summarizes_raw_seam_markers(tmp_path: Path):
    _write_pointer_files(tmp_path, use_seam_marker_list=True)
    result = _invoke_show(
        tmp_path,
        "--manifest",
        "--json",
        "--no-summary",
        "--no-weekly",
    )
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["manifest"][0]["seam_marker_count"] == 3
    assert payload["manifest"][0]["seam_hash_count"] == 2
    assert "seam_event_count" not in payload["manifest"][0]


def test_show_latest_smoke_metrics_missing_json(tmp_path: Path):
    _write_pointer_files(tmp_path)
    (tmp_path / "latest_metrics.json").unlink()
    result = _invoke_show(tmp_path, "--metrics", "--json")
    assert result.exit_code == 1
    assert "latest_metrics.json missing" in result.output


def test_show_latest_smoke_metrics_only(tmp_path: Path):
    _write_pointer_files(tmp_path)
    result = _invoke_show(tmp_path, "--metrics", "--no-summary", "--no-manifest", "--no-weekly")
    assert result.exit_code == 0
    assert "Aggregated Metrics" in result.output
    assert "categories" in result.output


def test_show_latest_smoke_fails_when_pointer_empty(tmp_path: Path):
    _write_pointer_files(tmp_path)
    (tmp_path / "latest.txt").write_text("   \n", encoding="utf-8")
    result = _invoke_show(tmp_path, "--manifest")
    assert result.exit_code == 1
    assert "latest.txt is empty" in result.output


def test_show_latest_smoke_json_without_metrics(tmp_path: Path):
    _write_pointer_files(tmp_path)
    result = _invoke_show(tmp_path, "--manifest", "--json", "--no-weekly", "--no-summary")
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert "metrics" not in payload
    assert len(payload["manifest"]) == 2


def test_show_latest_smoke_weekly_no_categories(tmp_path: Path):
    _write_pointer_files(tmp_path, include_weekly=True)
    (tmp_path / "weekly_summary.json").write_text(json.dumps({"window_days": 7, "categories": []}), encoding="utf-8")
    result = _invoke_show(tmp_path, "--weekly", "--no-summary", "--no-manifest", "--no-metrics")
    assert result.exit_code == 0
    assert "No category data recorded" in result.output


def test_show_latest_smoke_check_passes(tmp_path: Path):
    module = _load_cli()
    _write_pointer_files(tmp_path)
    result = runner.invoke(module.app, ["check", "--root", str(tmp_path)])
    assert result.exit_code == 0
    assert "Smoke pointers present" in result.output


def test_show_latest_smoke_check_missing_file(tmp_path: Path):
    module = _load_cli()
    _write_pointer_files(tmp_path)
    (tmp_path / "latest_manifest_index.json").unlink()
    result = runner.invoke(module.app, ["check", "--root", str(tmp_path)])
    assert result.exit_code == 1
    assert "manifest_index" in result.output


def test_show_latest_smoke_check_json_success(tmp_path: Path):
    module = _load_cli()
    _write_pointer_files(tmp_path)
    result = runner.invoke(module.app, ["check", "--root", str(tmp_path), "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["status"] == "ok"
    assert payload["missing"] == []


def test_show_latest_smoke_check_uses_env_root(monkeypatch, tmp_path: Path):
    _write_pointer_files(tmp_path)
    monkeypatch.setenv("MDWB_SMOKE_ROOT", str(tmp_path))
    module = _load_cli()
    result = runner.invoke(module.app, ["check"])
    assert result.exit_code == 0
    assert "Smoke pointers present" in result.output


def test_show_latest_smoke_check_json_missing(tmp_path: Path):
    module = _load_cli()
    _write_pointer_files(tmp_path, include_weekly=False)
    result = runner.invoke(module.app, ["check", "--root", str(tmp_path), "--json"])
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["status"] == "missing"
    assert "weekly_summary" in payload["missing"]


def test_show_latest_smoke_check_json_missing_summary(tmp_path: Path):
    module = _load_cli()
    _write_pointer_files(tmp_path)
    (tmp_path / "latest_summary.md").unlink()
    result = runner.invoke(module.app, ["check", "--root", str(tmp_path), "--json"])
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["status"] == "missing"
    assert "summary" in payload["missing"]


def test_show_latest_smoke_check_skip_weekly(tmp_path: Path):
    module = _load_cli()
    _write_pointer_files(tmp_path, include_weekly=False)
    result = runner.invoke(
        module.app,
        [
            "check",
            "--root",
            str(tmp_path),
            "--no-weekly",
            "--json",
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["status"] == "ok"
    assert payload["weekly_required"] is False
    assert payload["missing"] == []


def test_show_latest_smoke_check_requires_slo(tmp_path: Path):
    module = _load_cli()
    _write_pointer_files(tmp_path, include_slo=False)
    result = runner.invoke(
        module.app,
        [
            "check",
            "--root",
            str(tmp_path),
            "--slo",
            "--json",
        ],
    )
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["status"] == "missing"
    assert "slo_summary" in payload["missing"]


def test_show_latest_smoke_check_requires_slo_present(tmp_path: Path):
    module = _load_cli()
    _write_pointer_files(tmp_path, include_slo=True)
    result = runner.invoke(
        module.app,
        [
            "check",
            "--root",
            str(tmp_path),
            "--slo",
        ],
    )
    assert result.exit_code == 0
    assert "Smoke pointers present" in result.output


def test_show_latest_smoke_check_pointer_missing(tmp_path: Path):
    module = _load_cli()
    _write_pointer_files(tmp_path)
    (tmp_path / "latest.txt").unlink()
    result = runner.invoke(module.app, ["check", "--root", str(tmp_path)])
    assert result.exit_code == 1
    assert "Missing smoke artifacts" in result.output
    assert "pointer" in result.output


def test_show_latest_smoke_check_fails_when_pointer_empty(tmp_path: Path):
    module = _load_cli()
    _write_pointer_files(tmp_path)
    (tmp_path / "latest.txt").write_text("\n", encoding="utf-8")
    result = runner.invoke(module.app, ["check", "--root", str(tmp_path)])
    assert result.exit_code == 1
    assert "latest.txt is empty" in result.output


def test_show_latest_smoke_show_missing_summary(tmp_path: Path):
    _write_pointer_files(tmp_path)
    (tmp_path / "latest_summary.md").unlink()
    result = _invoke_show(tmp_path, "--summary")
    assert result.exit_code == 0
    assert "latest_summary.md missing" in result.output


def test_show_latest_smoke_check_no_weekly_json(tmp_path: Path):
    module = _load_cli()
    _write_pointer_files(tmp_path, include_weekly=False)
    result = runner.invoke(
        module.app,
        [
            "check",
            "--root",
            str(tmp_path),
            "--no-weekly",
            "--json",
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["weekly_required"] is False
    assert payload["missing"] == []
