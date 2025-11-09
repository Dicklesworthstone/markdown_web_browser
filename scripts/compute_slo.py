from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

import typer

app = typer.Typer(help="Compute capture/OCR SLO summaries from manifest indexes.")


def _coerce_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)) and not math.isnan(value):
        return int(value)
    return None


def _percentile(values: Iterable[int], percentile: float) -> int | None:
    ordered = sorted(values)
    if not ordered:
        return None
    if len(ordered) == 1:
        return ordered[0]
    rank = (percentile / 100.0) * (len(ordered) - 1)
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return ordered[int(rank)]
    fraction = rank - lower
    interpolated = ordered[lower] + (ordered[upper] - ordered[lower]) * fraction
    return int(round(interpolated))


def _extract_timings(entry: Mapping[str, Any]) -> tuple[int | None, int | None, int | None, int | None]:
    timings = entry.get("timings") or {}
    capture = _coerce_int(entry.get("capture_ms") or timings.get("capture_ms"))
    ocr = _coerce_int(entry.get("ocr_ms") or timings.get("ocr_ms"))
    stitch = _coerce_int(entry.get("stitch_ms") or timings.get("stitch_ms"))
    total = _coerce_int(entry.get("total_ms") or timings.get("total_ms"))
    if total is None:
        stage_values = [value for value in (capture, ocr, stitch) if value is not None]
        if stage_values:
            total = sum(stage_values)
    return capture, ocr, stitch, total


def load_budgets(path: Path | None) -> dict[str, int]:
    if not path:
        return {}
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    categories = payload.get("categories") or []
    mapping: dict[str, int] = {}
    for category in categories:
        name = category.get("name")
        budget = _coerce_int(category.get("p95_budget_ms"))
        if isinstance(name, str) and budget is not None:
            mapping[name] = budget
    return mapping


def compute_slo_summary(
    entries: Iterable[Mapping[str, Any]],
    *,
    budget_map: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    budgets = dict(budget_map or {})
    category_payload: dict[str, dict[str, Any]] = {}
    aggregate_totals: list[int] = []
    aggregate_breaches = 0
    total_entries = 0

    for entry in entries:
        category = entry.get("category") or "unknown"
        if not isinstance(category, str) or not category:
            category = "unknown"
        stats = category_payload.setdefault(
            category,
            {
                "capture_ms": [],
                "ocr_ms": [],
                "stitch_ms": [],
                "total_ms": [],
                "budget_ms": budgets.get(category),
                "budget_breaches": 0,
            },
        )
        capture, ocr, stitch, total = _extract_timings(entry)
        for key, value in (
            ("capture_ms", capture),
            ("ocr_ms", ocr),
            ("stitch_ms", stitch),
            ("total_ms", total),
        ):
            if value is not None:
                stats[key].append(value)
                if key == "total_ms":
                    aggregate_totals.append(value)
        budget = _coerce_int(entry.get("budget_ms")) or stats.get("budget_ms") or budgets.get(category)
        if budget is not None:
            stats["budget_ms"] = budget
            if total is not None and total > budget:
                stats["budget_breaches"] += 1
                aggregate_breaches += 1
        total_entries += 1

    categories_summary: dict[str, Any] = {}
    overall_status = "no_data"
    for category in sorted(category_payload.keys()):
        stats = category_payload[category]
        totals = stats["total_ms"]
        capture_vals = stats["capture_ms"]
        ocr_vals = stats["ocr_ms"]
        count = len(totals) if totals else 0
        summary = {
            "count": count,
            "p50_total_ms": _percentile(totals, 50),
            "p95_total_ms": _percentile(totals, 95),
            "p50_capture_ms": _percentile(capture_vals, 50),
            "p95_capture_ms": _percentile(capture_vals, 95),
            "p50_ocr_ms": _percentile(ocr_vals, 50),
            "p95_ocr_ms": _percentile(ocr_vals, 95),
            "budget_ms": stats.get("budget_ms"),
            "budget_breaches": stats.get("budget_breaches", 0),
        }
        budget = summary.get("budget_ms")
        p95 = summary.get("p95_total_ms")
        if count == 0:
            summary["status"] = "no_data"
        elif isinstance(budget, int) and isinstance(p95, int):
            summary["status"] = "within_budget" if p95 <= budget else "breached"
        else:
            summary["status"] = "unknown"
        breaches = summary["budget_breaches"] or 0
        summary["budget_breach_ratio"] = breaches / count if count else 0.0
        summary["within_budget"] = 1 if summary["status"] == "within_budget" else 0
        if count:
            if summary["status"] == "breached":
                overall_status = "breached"
            elif overall_status != "breached":
                overall_status = "within_budget"
        categories_summary[category] = summary

    aggregate_ratio = (aggregate_breaches / total_entries) if total_entries else 0.0
    aggregate_summary = {
        "count": total_entries,
        "p50_total_ms": _percentile(aggregate_totals, 50),
        "p95_total_ms": _percentile(aggregate_totals, 95),
        "budget_breaches": aggregate_breaches,
        "budget_breach_ratio": aggregate_ratio,
    }
    breach_ratio = float(aggregate_summary.get("budget_breach_ratio") or 0.0)
    aggregate_summary["status"] = ("within_budget" if breach_ratio <= 0.01 else "breached") if total_entries else "no_data"

    return {
        "categories": categories_summary,
        "aggregate": aggregate_summary,
        "status": overall_status,
    }


def write_prom_metrics(summary: Mapping[str, Any], output_path: Path) -> None:
    """Emit a Prometheus textfile containing SLO metrics."""

    categories = summary.get("categories", {})
    aggregate = summary.get("aggregate", {})
    lines: list[str] = []

    def _metric(header: str, metric_type: str) -> None:
        lines.append(header)
        lines.append(metric_type)

    def _format_metric(name: str, labels: Mapping[str, str] | None, value: Any) -> None:
        if value is None:
            return
        if isinstance(value, bool):
            value = int(value)
        elif isinstance(value, (int, float)):
            pass
        else:
            return
        label_text = ""
        if labels:
            formatted = ",".join(f'{key}="{val}"' for key, val in labels.items())
            label_text = f"{{{formatted}}}"
        lines.append(f"{name}{label_text} {value}")

    _metric(
        "# HELP mdwb_slo_p95_total_ms Rolling p95 total latency per category (ms)",
        "# TYPE mdwb_slo_p95_total_ms gauge",
    )
    for category, data in categories.items():
        labels = {"category": category}
        _format_metric("mdwb_slo_p95_total_ms", labels, data.get("p95_total_ms"))
        _format_metric("mdwb_slo_p95_capture_ms", labels, data.get("p95_capture_ms"))
        _format_metric("mdwb_slo_p95_ocr_ms", labels, data.get("p95_ocr_ms"))
        _format_metric("mdwb_slo_budget_ms", labels, data.get("budget_ms"))
        _format_metric("mdwb_slo_budget_breach_ratio", labels, data.get("budget_breach_ratio"))
        _format_metric("mdwb_slo_budget_breaches", labels, data.get("budget_breaches"))
        _format_metric("mdwb_slo_within_budget", labels, data.get("within_budget"))

    _metric(
        "# HELP mdwb_slo_overall_status Whether the overall SLO is within budget (1=yes)",
        "# TYPE mdwb_slo_overall_status gauge",
    )
    _format_metric("mdwb_slo_overall_status", None, 1 if summary.get("status") == "within_budget" else 0)

    _metric(
        "# HELP mdwb_slo_aggregate_budget_breach_ratio Aggregate budget breach ratio across all categories",
        "# TYPE mdwb_slo_aggregate_budget_breach_ratio gauge",
    )
    _format_metric(
        "mdwb_slo_aggregate_budget_breach_ratio",
        None,
        aggregate.get("budget_breach_ratio"),
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


@app.command()
def main(
    root: Path = typer.Option(Path("benchmarks/production"), help="Directory containing manifest pointers."),
    manifest: Path | None = typer.Option(None, help="Path to manifest index JSON (defaults to ROOT/latest_manifest_index.json)."),
    budget_file: Path | None = typer.Option(
        Path("benchmarks/production_set.json"), help="Budget definition file (PLAN §22)."
    ),
    out: Path | None = typer.Option(None, help="Optional output file; stdout is used when omitted."),
    pretty: bool = typer.Option(True, help="Emit human-readable JSON when true."),
    prom_output: Path | None = typer.Option(
        None,
        "--prom-output",
        help="Optional Prometheus textfile output path.",
    ),
) -> None:
    manifest_path = manifest or (root / "latest_manifest_index.json")
    if not manifest_path.exists():
        typer.secho(f"Manifest index not found: {manifest_path}", fg="red", err=True)
        raise typer.Exit(code=1)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        typer.secho("Manifest index must be a JSON array.", fg="red", err=True)
        raise typer.Exit(code=1)
    budgets = load_budgets(budget_file)
    summary = compute_slo_summary(payload, budget_map=budgets)
    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "manifest_path": str(manifest_path),
        "root": str(root),
        **summary,
    }
    text = json.dumps(result, indent=2 if pretty else None)
    if out:
        out.write_text(text + ("\n" if pretty else ""), encoding="utf-8")
    else:
        typer.echo(text)
    if prom_output:
        write_prom_metrics(summary=result, output_path=prom_output)


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    app()
