"""Weekly bead-health telemetry.

Generates a JSONL report at ``ops/bead_health.jsonl`` with one record per
run. Each record contains: bead counts (open/in_progress/closed), open-age
distribution (oldest, median, p95 days), and bead-issue-type breakdown.

This is the file the ``/health/beads`` endpoint reads.
"""
from __future__ import annotations

import json
import os
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_OUTPUT = Path("ops/bead_health.jsonl")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run_br(*args: str) -> dict:
    """Run ``br`` with the given args and return parsed JSON or {} on failure."""
    try:
        result = subprocess.run(
            ["br", *args, "--json"],
            capture_output=True,
            text=True,
            timeout=15.0,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {}
    if result.returncode != 0:
        return {}
    try:
        out = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}
    return out if isinstance(out, dict) else {"issues": out}


def _bead_ages_days(beads):
    """Compute oldest / median / p95 open-bead age in days."""
    now = datetime.now(timezone.utc)
    ages = []
    for b in beads:
        if b.get("status") not in {"open", "in_progress"}:
            continue
        ts = b.get("created_at") or b.get("updated_at")
        if not ts:
            continue
        try:
            created = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            continue
        ages.append((now - created).total_seconds() / 86400.0)
    if not ages:
        return {"count": 0, "oldest_days": None, "median_days": None, "p95_days": None}
    ages.sort()
    n = len(ages)
    median = ages[n // 2] if n % 2 == 1 else (ages[n // 2 - 1] + ages[n // 2]) / 2
    p95_idx = max(0, min(n - 1, int(n * 0.95)))
    return {
        "count": n,
        "oldest_days": round(ages[-1], 2),
        "median_days": round(median, 2),
        "p95_days": round(ages[p95_idx], 2),
    }


def snapshot():
    """Build one bead-health snapshot (does not write to disk)."""
    data = _run_br("list")
    issues = data.get("issues") if isinstance(data, dict) else []
    if not isinstance(issues, list):
        issues = []
    counts = Counter(b.get("status", "unknown") for b in issues)
    type_counts = Counter(b.get("issue_type") or "unknown" for b in issues)
    priority_counts = Counter(b.get("priority", 0) for b in issues)

    return {
        "generated_at": _now_iso(),
        "total": len(issues),
        "by_status": dict(counts),
        "by_issue_type": dict(type_counts),
        "by_priority": {str(k): v for k, v in priority_counts.items()},
        "open_age": _bead_ages_days(issues),
    }


def write_report(output_path=DEFAULT_OUTPUT):
    """Compute a snapshot and append it to the JSONL report file."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    snap = snapshot()
    with output_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(snap) + "\n")
    return output_path


def read_latest(output_path=DEFAULT_OUTPUT):
    """Read the most recent snapshot from the JSONL report."""
    output_path = Path(output_path)
    if not output_path.exists():
        return {"status": "no-data"}
    last_line = ""
    with output_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                last_line = line
    if not last_line:
        return {"status": "no-data"}
    try:
        return json.loads(last_line)
    except json.JSONDecodeError:
        return {"status": "no-data"}


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Bead health telemetry")
    parser.add_argument(
        "--out",
        default=os.environ.get("MDWB_BEAD_HEALTH_OUT", str(DEFAULT_OUTPUT)),
        help=f"Output JSONL path (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--read",
        action="store_true",
        help="Print the most recent snapshot from the JSONL report and exit.",
    )
    args = parser.parse_args()

    if args.read:
        snap = read_latest(args.out)
        print(json.dumps(snap, indent=2))
        return

    written = write_report(args.out)
    snap = read_latest(args.out)
    print(f"[green]Wrote bead-health snapshot to {written}[/]")
    print(f"  total={snap.get('total')}  by_status={snap.get('by_status')}")
    age = snap.get("open_age") or {}
    if age.get("count", 0) > 0:
        print(
            f"  open_age: oldest={age['oldest_days']}d  "
            f"median={age['median_days']}d  p95={age['p95_days']}d"
        )


if __name__ == "__main__":
    main()
