#!/usr/bin/env python3
"""Nightly smoke runner + weekly latency aggregation."""

from __future__ import annotations

import argparse
import json
import random
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from scripts import olmocr_cli

PRODUCTION_SET_PATH = Path("benchmarks/production_set.json")
PRODUCTION_ROOT = Path("benchmarks/production")
WEEKLY_SUMMARY_PATH = PRODUCTION_ROOT / "weekly_summary.json"


def _load_production_set() -> dict[str, Any]:
    if not PRODUCTION_SET_PATH.exists():
        raise FileNotFoundError(
            "benchmarks/production_set.json missing; see PLAN §22 for requirements"
        )
    return json.loads(PRODUCTION_SET_PATH.read_text(encoding="utf-8"))


@dataclass
class Category:
    name: str
    budget_ms: int
    urls: list[dict[str, str]]


def _parse_categories(raw: dict[str, Any]) -> list[Category]:
    categories: list[Category] = []
    for entry in raw.get("categories", []):
        categories.append(
            Category(
                name=entry["name"],
                budget_ms=int(entry.get("p95_budget_ms", 0)),
                urls=list(entry.get("urls", [])),
            )
        )
    if not categories:
        raise ValueError("production_set.json has no categories")
    return categories


@dataclass
class RunRecord:
    category: str
    budget_ms: int
    slug: str
    url: str
    job_id: str
    run_dir: Path
    manifest_path: Path
    capture_ms: int | None
    total_ms: int | None
    timings: dict[str, Any] | None


def _ensure_date_dir(date_str: str) -> Path:
    target = PRODUCTION_ROOT / date_str
    target.mkdir(parents=True, exist_ok=True)
    return target


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    sequence = sorted(values)
    k = (len(sequence) - 1) * pct
    f = int(k)
    c = min(f + 1, len(sequence) - 1)
    if f == c:
        return sequence[f]
    return sequence[f] * (c - k) + sequence[c] * (k - f)


def _slug_from_url(url_entry: dict[str, str]) -> str:
    slug = url_entry.get("slug")
    if slug:
        return slug
    url = url_entry.get("url", "")
    sanitized = re.sub(r"[^a-zA-Z0-9]+", "-", url).strip("-")
    return sanitized or "job"


def _manifest_metrics(manifest_path: Path) -> tuple[int | None, int | None, dict[str, Any] | None]:
    if not manifest_path.exists():
        return None, None, None
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    timings = manifest.get("timings") or {}
    capture_ms = timings.get("capture_ms")
    total_ms = timings.get("total_ms")
    if total_ms is None:
        partials = [timings.get(key) for key in ("capture_ms", "ocr_ms", "stitch_ms")]
        total_ms = sum(value for value in partials if value is not None)
    return capture_ms, total_ms, timings


def run_category(
    category: Category,
    out_dir: Path,
    settings: olmocr_cli.CLISettings,
    http2: bool,
    poll_interval: float,
    timeout_s: float,
) -> list[RunRecord]:
    results: list[RunRecord] = []
    category_dir = out_dir / category.name
    category_dir.mkdir(parents=True, exist_ok=True)
    for url_entry in category.urls:
        slug = _slug_from_url(url_entry)
        url = url_entry["url"]
        print(f"[smoke] {category.name} → {url}")
        result = olmocr_cli.run_capture(
            url=url,
            settings=settings,
            out_dir=category_dir,
            tiles_long_side=None,
            overlap_px=None,
            concurrency=None,
            http2=http2,
            poll_interval=poll_interval,
            timeout_s=timeout_s,
        )
        manifest_path = result.output_dir / "manifest.json"
        capture_ms, total_ms, timings = _manifest_metrics(manifest_path)
        results.append(
            RunRecord(
                category=category.name,
                budget_ms=category.budget_ms,
                slug=slug,
                url=url,
                job_id=result.job_id,
                run_dir=result.output_dir,
                manifest_path=manifest_path,
                capture_ms=capture_ms,
                total_ms=total_ms,
                timings=timings,
            )
        )
    return results


def run_category_dry(category: Category, out_dir: Path, seed: int | None = None) -> list[RunRecord]:
    rng = random.Random(seed)
    results: list[RunRecord] = []
    category_dir = out_dir / category.name
    category_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%H%M%S")

    for url_entry in category.urls:
        slug = _slug_from_url(url_entry)
        url = url_entry["url"]
        job_id = f"dry-{category.name}-{slug}-{stamp}"
        job_dir = category_dir / f"{slug}-{stamp}"
        job_dir.mkdir(parents=True, exist_ok=True)

        capture_ms = int(rng.uniform(6_000, 18_000))
        ocr_ms = int(rng.uniform(8_000, 22_000))
        stitch_ms = int(rng.uniform(500, 2_000))
        manifest: dict[str, Any] = {
            "job_id": job_id,
            "url": url,
            "timings": {
                "capture_ms": capture_ms,
                "ocr_ms": ocr_ms,
                "stitch_ms": stitch_ms,
                "total_ms": capture_ms + ocr_ms + stitch_ms,
            },
            "environment": {
                "cft_version": "dry-run",
                "playwright_channel": "cft",
            },
            "warnings": [],
            "blocklist_hits": {},
        }
        (job_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        (job_dir / "out.md").write_text("# Dry Run\n\nNo real capture was executed.", encoding="utf-8")
        (job_dir / "links.json").write_text("[]", encoding="utf-8")

        timings_value = manifest.get("timings")
        if not isinstance(timings_value, dict):
            timings_value = None

        results.append(
            RunRecord(
                category=category.name,
                budget_ms=category.budget_ms,
                slug=slug,
                url=url,
                job_id=job_id,
                run_dir=job_dir,
                manifest_path=job_dir / "manifest.json",
                capture_ms=capture_ms,
                total_ms=capture_ms + ocr_ms + stitch_ms,
                timings=timings_value,
            )
        )

    return results


def write_manifest_index(date_dir: Path, records: list[RunRecord]) -> Path:
    payload = [
        {
            "category": record.category,
            "budget_ms": record.budget_ms,
            "slug": record.slug,
            "url": record.url,
            "job_id": record.job_id,
            "run_dir": str(record.run_dir),
            "manifest": str(record.manifest_path),
            "capture_ms": record.capture_ms,
            "total_ms": record.total_ms,
            "timings": record.timings,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
        for record in records
    ]
    index_path = date_dir / "manifest_index.json"
    index_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return index_path


def write_summary_markdown(date_dir: Path, records: list[RunRecord]) -> Path:
    lines: list[str] = [f"# Nightly Smoke — {date_dir.name}", ""]
    grouped: dict[str, list[RunRecord]] = defaultdict(list)
    for record in records:
        grouped[record.category].append(record)

    lines.append("| Category | Budget (ms) | Jobs | p95 capture (ms) | p95 total (ms) | Status |")
    lines.append("| --- | --- | --- | --- | --- | --- |")

    for category in sorted(grouped):
        cat_records = grouped[category]
        capture_values = [r.capture_ms for r in cat_records if r.capture_ms is not None]
        total_values = [r.total_ms for r in cat_records if r.total_ms is not None]
        p95_capture = _percentile(capture_values, 0.95)
        p95_total = _percentile(total_values, 0.95)
        budget = cat_records[0].budget_ms
        status = "OK"
        if budget and p95_total and p95_total > budget:
            status = "⚠️ over budget"
        lines.append(
            "| {category} | {budget} | {jobs} | {p95_cap:.0f} | {p95_tot:.0f} | {status} |".format(
                category=category,
                budget=budget or "—",
                jobs=len(cat_records),
                p95_cap=p95_capture or 0,
                p95_tot=p95_total or 0,
                status=status,
            )
        )

    lines.append("")
    for category in sorted(grouped):
        lines.append(f"## {category}")
        lines.append("| URL | job_id | capture_ms | total_ms |")
        lines.append("| --- | --- | --- | --- |")
        for record in grouped[category]:
            lines.append(
                f"| {record.url} | `{record.job_id}` | {record.capture_ms or '—'} | {record.total_ms or '—'} |"
            )
        lines.append("")

    summary_path = date_dir / "summary.md"
    summary_path.write_text("\n".join(lines), encoding="utf-8")
    return summary_path


def _collect_history(days: int) -> list[Path]:
    if not PRODUCTION_ROOT.exists():
        return []
    cut_off = datetime.now(timezone.utc).date() - timedelta(days=days - 1)
    history_dirs: list[Path] = []
    for child in sorted(PRODUCTION_ROOT.iterdir()):
        if not child.is_dir():
            continue
        try:
            child_date = datetime.strptime(child.name, "%Y-%m-%d").date()
        except ValueError:
            continue
        if child_date >= cut_off:
            history_dirs.append(child)
    return history_dirs


def update_weekly_summary(config: dict[str, Any], window_days: int = 7) -> None:
    history_dirs = _collect_history(window_days)
    metrics: dict[str, list[dict[str, Any]]] = {}
    for history_dir in history_dirs:
        index_path = history_dir / "manifest_index.json"
        if not index_path.exists():
            continue
        entries: list[dict[str, Any]] = json.loads(index_path.read_text(encoding="utf-8"))
        for entry in entries:
            metrics.setdefault(entry["category"], []).append(entry)

    categories_summary: list[dict[str, Any]] = []
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window_days": window_days,
        "categories": categories_summary,
    }
    budgets = {cat["name"]: cat.get("p95_budget_ms") for cat in config.get("categories", [])}

    for category, entries in sorted(metrics.items()):
        capture_values = [entry["capture_ms"] for entry in entries if entry.get("capture_ms") is not None]
        total_values = [entry["total_ms"] for entry in entries if entry.get("total_ms") is not None]
        budget = budgets.get(category)
        categories_summary.append(
            {
                "name": category,
                "runs": len(entries),
                "budget_ms": budget,
                "capture_ms": {
                    "p50": _percentile(capture_values, 0.5),
                    "p95": _percentile(capture_values, 0.95),
                },
                "total_ms": {
                    "p50": _percentile(total_values, 0.5),
                    "p95": _percentile(total_values, 0.95),
                },
            }
        )

    WEEKLY_SUMMARY_PATH.write_text(json.dumps(summary, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run nightly smoke captures per PLAN §22")
    parser.add_argument("--date", help="Override run date (YYYY-MM-DD)")
    parser.add_argument("--http2", action=argparse.BooleanOptionalAction, default=True, help="Toggle HTTP/2")
    parser.add_argument("--poll-interval", type=float, default=1.0)
    parser.add_argument("--timeout", type=float, default=900.0)
    parser.add_argument("--dry-run", action=argparse.BooleanOptionalAction, default=False, help="Skip API calls and emit synthetic records.")
    parser.add_argument("--seed", type=int, help="Optional RNG seed for --dry-run mode (default deterministic)")
    args = parser.parse_args()

    run_date = args.date or datetime.now(timezone.utc).date().isoformat()
    config = _load_production_set()
    categories = _parse_categories(config)
    settings = olmocr_cli.load_settings()
    date_dir = _ensure_date_dir(run_date)

    all_records: list[RunRecord] = []
    for category in categories:
        if args.dry_run:
            records = run_category_dry(category, date_dir, seed=args.seed or 0)
        else:
            records = run_category(
                category,
                date_dir,
                settings=settings,
                http2=args.http2,
                poll_interval=args.poll_interval,
                timeout_s=args.timeout,
            )
        all_records.extend(records)

    write_manifest_index(date_dir, all_records)
    write_summary_markdown(date_dir, all_records)
    update_weekly_summary(config)
    print(f"Smoke run complete for {run_date}; artifacts under {date_dir}")


if __name__ == "__main__":
    main()
