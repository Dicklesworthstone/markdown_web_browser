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


def _write_pointer_files(root: Path, *, include_weekly: bool = True, over_budget: bool = False) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "latest.txt").write_text("2025-11-08\n", encoding="utf-8")
    (root / "latest_summary.md").write_text("# Summary\n\nSmoke output", encoding="utf-8")
    manifest_rows = [
        {"category": "docs", "url": "https://example.com/article", "capture_ms": 1200, "total_ms": 2200},
        {"category": "apps", "url": "https://example.org/dashboard", "capture_ms": 3200, "total_ms": 4200},
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
                    "capture_ms": {"p50": 11000, "p95": 15000},
                    "total_ms": {"p50": 19000, "p95": 26000 if over_budget else 18000},
                }
            ],
        }
        (root / "weekly_summary.json").write_text(json.dumps(weekly_payload), encoding="utf-8")


def _invoke_show(tmp_path: Path, *args: str):
    module = _load_cli()
    cmd = ["show", "--root", str(tmp_path)]
    cmd.extend(args)
    return runner.invoke(module.app, cmd)


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


def test_show_latest_smoke_weekly_highlights_over_budget(tmp_path: Path):
    _write_pointer_files(tmp_path, over_budget=True)
    result = _invoke_show(tmp_path, "--weekly", "--no-summary", "--no-manifest", "--no-metrics")
    assert result.exit_code == 0
    assert "Weekly Summary" in result.output
    assert "⚠️ over budget" in result.output


def test_show_latest_smoke_weekly_missing_file(tmp_path: Path):
    _write_pointer_files(tmp_path, include_weekly=False)
    result = _invoke_show(tmp_path, "--weekly")
    assert result.exit_code == 1
    assert "weekly_summary.json missing" in result.output


def test_show_latest_smoke_json_output(tmp_path: Path):
    _write_pointer_files(tmp_path)
    result = _invoke_show(
        tmp_path,
        "--manifest",
        "--limit",
        "1",
        "--metrics",
        "--weekly",
        "--json",
    )
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["run_date"] == "2025-11-08"
    assert payload["summary_markdown"].startswith("# Summary")
    assert len(payload["manifest"]) == 1
    assert "metrics" in payload
    assert "weekly_summary" in payload


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


def test_show_latest_smoke_check_json_missing(tmp_path: Path):
    module = _load_cli()
    _write_pointer_files(tmp_path, include_weekly=False)
    result = runner.invoke(module.app, ["check", "--root", str(tmp_path), "--json"])
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["status"] == "missing"
    assert "weekly_summary" in payload["missing"]
