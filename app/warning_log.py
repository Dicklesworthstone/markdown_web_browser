"""Helpers to append capture warning/blocklist events to ops logs."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
import json
from typing import Any, Mapping, Sequence

from app.schemas import ManifestWarning
from app.settings import get_settings

__all__ = ["append_warning_log", "summarize_dom_assists", "summarize_seam_markers"]


def _normalize_warning(entry: Any) -> dict[str, Any]:
    if isinstance(entry, ManifestWarning):
        return entry.model_dump()
    if hasattr(entry, "model_dump"):
        return entry.model_dump()
    if is_dataclass(entry):
        return asdict(entry)
    if isinstance(entry, dict):
        return entry
    return {"code": str(entry)}


def append_warning_log(
    *,
    job_id: str,
    url: str,
    manifest: Any,
) -> None:
    """Append warning/blocklist events for ops review.

    Writes a JSON line containing job identifiers, timestamp, warning list, and
    blocklist stats. No-op when neither warnings nor blocklist hits exist.
    """

    settings = get_settings()
    warning_entries = getattr(manifest, "warnings", []) or []
    warnings = [_normalize_warning(entry) for entry in warning_entries]
    blocklist_hits = getattr(manifest, "blocklist_hits", {}) or {}
    validation_failures = list(getattr(manifest, "validation_failures", []) or [])
    sweep_stats = _coerce_mapping(getattr(manifest, "sweep_stats", None))
    overlap_ratio = getattr(manifest, "overlap_match_ratio", None)
    if overlap_ratio is None and sweep_stats:
        overlap_ratio = sweep_stats.get("overlap_match_ratio")
    seam_summary = summarize_seam_markers(
        getattr(manifest, "seam_markers", None),
        events=getattr(manifest, "seam_marker_events", None),
    )
    tiles_total = getattr(manifest, "tiles_total", None)
    dom_summary = getattr(manifest, "dom_assist_summary", None)
    if not dom_summary:
        dom_summary = summarize_dom_assists(
            getattr(manifest, "dom_assists", None), tiles_total=tiles_total
        )

    should_log = bool(warnings or blocklist_hits or validation_failures)
    if not should_log and sweep_stats:
        retried = int(sweep_stats.get("retry_attempts") or 0) > 0
        shrank = int(sweep_stats.get("shrink_events") or 0) > 0
        should_log = retried or shrank
    if not should_log and sweep_stats and overlap_ratio is not None:
        overlap_pairs = int(sweep_stats.get("overlap_pairs") or 0)
        warn_cfg = settings.warnings
        seam_condition = (
            warn_cfg.seam_warning_ratio > 0
            and overlap_pairs >= warn_cfg.seam_warning_min_pairs
            and overlap_ratio >= warn_cfg.seam_warning_ratio
        )
        overlap_condition = (
            warn_cfg.overlap_warning_ratio > 0
            and overlap_pairs > 0
            and overlap_ratio < warn_cfg.overlap_warning_ratio
        )
        should_log = seam_condition or overlap_condition

    if not should_log:
        return

    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "job_id": job_id,
        "url": url,
        "warnings": warnings,
        "blocklist_version": getattr(manifest, "blocklist_version", None),
        "blocklist_hits": blocklist_hits,
    }
    if sweep_stats:
        record["sweep_stats"] = sweep_stats
    if overlap_ratio is not None:
        record["overlap_match_ratio"] = overlap_ratio
    if validation_failures:
        record["validation_failures"] = validation_failures
    if seam_summary:
        record["seam_markers"] = seam_summary
    if dom_summary:
        record["dom_assist_summary"] = dom_summary

    log_path = settings.logging.warning_log_path
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record))
        handle.write("\n")


def _coerce_mapping(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, Mapping):
        return dict(value)
    if is_dataclass(value):
        return asdict(value)
    if hasattr(value, "model_dump"):
        try:
            data = value.model_dump()
            if isinstance(data, dict):
                return data
        except Exception:  # pragma: no cover - defensive
            return None
    return None


def summarize_seam_markers(
    markers: Any,
    *,
    events: Any = None,
    sample_limit: int = 3,
) -> dict[str, Any] | None:
    if not isinstance(markers, Sequence):
        return None
    normalized: list[dict[str, Any]] = []
    tile_ids: set[int] = set()
    hashes: set[str] = set()
    for entry in markers:
        if not isinstance(entry, Mapping):
            continue
        item: dict[str, Any] = {}
        tile_index = entry.get("tile_index")
        if isinstance(tile_index, int):
            item["tile_index"] = tile_index
            tile_ids.add(tile_index)
        position = entry.get("position")
        if isinstance(position, str):
            item["position"] = position
        seam_hash = entry.get("hash")
        if isinstance(seam_hash, str):
            item["hash"] = seam_hash
            hashes.add(seam_hash)
        normalized.append(item)
    if not normalized:
        return None
    sample = normalized[:sample_limit]
    summary: dict[str, Any] = {
        "count": len(normalized),
        "unique_tiles": len(tile_ids) or None,
        "unique_hashes": len(hashes) or None,
        "sample": sample,
    }
    usage_summary = _summarize_seam_usage(events, sample_limit=sample_limit)
    if usage_summary:
        summary["usage"] = usage_summary
    return summary


def _summarize_seam_usage(events: Any, *, sample_limit: int = 3) -> dict[str, Any] | None:
    if not isinstance(events, Sequence):
        return None
    normalized: list[dict[str, Any]] = []
    for entry in events:
        if not isinstance(entry, Mapping):
            continue
        item: dict[str, Any] = {}
        prev_idx = entry.get("prev_tile_index")
        curr_idx = entry.get("curr_tile_index")
        seam_hash = entry.get("seam_hash") or entry.get("hash")
        if isinstance(prev_idx, int):
            item["prev_tile_index"] = prev_idx
        if isinstance(curr_idx, int):
            item["curr_tile_index"] = curr_idx
        if isinstance(seam_hash, str):
            item["seam_hash"] = seam_hash
        normalized.append(item)
    if not normalized:
        return None
    return {
        "count": len(normalized),
        "sample": normalized[:sample_limit],
    }


def summarize_dom_assists(entries: Sequence[Any] | None, *, tiles_total: int | None = None) -> dict[str, Any] | None:
    if not entries:
        return None
    normalized: list[dict[str, Any]] = []
    for entry in entries:
        if entry is None:
            continue
        if isinstance(entry, Mapping):
            normalized.append(dict(entry))
        elif is_dataclass(entry):
            normalized.append(asdict(entry))
    if not normalized:
        return None
    counter = Counter(str(item.get("reason", "unknown")) for item in normalized)
    total_assists = len(normalized)
    density = None
    if tiles_total and tiles_total > 0:
        density = total_assists / tiles_total
    reason_counts: list[dict[str, Any]] = []
    for reason, count in counter.most_common():
        ratio = None
        if tiles_total and tiles_total > 0:
            ratio = count / tiles_total
        elif total_assists > 0:
            ratio = count / total_assists
        reason_counts.append({"reason": reason, "count": count, "ratio": ratio})
    summary: dict[str, Any] = {
        "count": total_assists,
        "reasons": sorted(reason for reason in counter if isinstance(reason, str)),
        "reason_counts": reason_counts,
    }
    if density is not None:
        summary["assist_density"] = density
    if tiles_total is not None:
        summary["tiles_total"] = tiles_total
    sample = next((entry for entry in normalized if entry.get("reason")), normalized[0])
    summary["sample"] = {
        "tile_index": sample.get("tile_index"),
        "line": sample.get("line"),
        "reason": sample.get("reason"),
        "dom_text": sample.get("dom_text"),
    }
    return summary
