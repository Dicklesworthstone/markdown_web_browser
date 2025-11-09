from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from scripts import compute_slo


runner = CliRunner()


def test_compute_slo_summary_with_budgets():
    entries = [
        {"category": "docs_articles", "capture_ms": 1200, "ocr_ms": 600, "total_ms": 2100},
        {"category": "docs_articles", "capture_ms": 1400, "ocr_ms": 700, "total_ms": 2600},
        {"category": "dashboards_apps", "capture_ms": 28000, "ocr_ms": 9000, "total_ms": 42000},
    ]
    summary = compute_slo.compute_slo_summary(
        entries,
        budget_map={
            "docs_articles": 3000,
            "dashboards_apps": 35000,
        },
    )

    docs = summary["categories"]["docs_articles"]
    apps = summary["categories"]["dashboards_apps"]

    assert docs["status"] == "within_budget"
    assert apps["status"] == "breached"
    assert pytest.approx(docs["budget_breach_ratio"], rel=1e-6) == 0.0
    assert pytest.approx(apps["budget_breach_ratio"], rel=1e-6) == 1.0
    assert summary["status"] == "breached"


def test_compute_slo_cli_outputs_json_and_prom(tmp_path: Path):
    manifest_entries = [
        {"category": "docs_articles", "capture_ms": 1200, "ocr_ms": 500, "total_ms": 2000},
        {"category": "dashboards_apps", "capture_ms": 25000, "ocr_ms": 8000, "total_ms": 36000},
    ]
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest_entries), encoding="utf-8")

    budget_payload = {
        "categories": [
            {"name": "docs_articles", "p95_budget_ms": 2500},
            {"name": "dashboards_apps", "p95_budget_ms": 30000},
        ]
    }
    budget_path = tmp_path / "budgets.json"
    budget_path.write_text(json.dumps(budget_payload), encoding="utf-8")

    out_path = tmp_path / "slo.json"
    prom_path = tmp_path / "slo.prom"

    result = runner.invoke(
        compute_slo.app,
        [
            "--manifest",
            str(manifest_path),
            "--budget-file",
            str(budget_path),
            "--out",
            str(out_path),
            "--prom-output",
            str(prom_path),
        ],
    )

    assert result.exit_code == 0
    summary = json.loads(out_path.read_text(encoding="utf-8"))
    assert summary["categories"]["docs_articles"]["status"] == "within_budget"
    assert summary["categories"]["dashboards_apps"]["status"] == "breached"

    prom_text = prom_path.read_text(encoding="utf-8")
    assert 'mdwb_slo_p95_total_ms{category="docs_articles"}' in prom_text
    assert 'mdwb_slo_within_budget{category="dashboards_apps"} 0' in prom_text
