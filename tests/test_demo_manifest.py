import json
import sys
import types

if "pyvips" not in sys.modules:
    pyvips_stub = types.ModuleType("pyvips")
    pyvips_stub.Image = object  # type: ignore[attr-defined]
    sys.modules["pyvips"] = pyvips_stub

from app.main import _demo_manifest_payload, _snapshot_events


def test_demo_manifest_contains_warnings_and_blocklist():
    payload = _demo_manifest_payload()

    warnings = payload.get("warnings", [])
    hits = payload.get("blocklist_hits", {})

    assert warnings, "demo manifest should include warning entries"
    codes = {entry["code"] for entry in warnings}
    assert "canvas-heavy" in codes

    assert hits.get("#onetrust-consent-sdk") == 2
    assert payload.get("blocklist_version")


def test_snapshot_events_include_manifest_breadcrumbs():
    snapshot = {
        "state": "DONE",
        "progress": {"done": 2, "total": 2},
        "manifest": {
            "warnings": [{"code": "canvas-heavy", "count": 5, "threshold": 3}],
            "blocklist_hits": {"#cookie": 2},
            "sweep_stats": {"shrink_events": 1},
            "overlap_match_ratio": 0.95,
            "validation_failures": ["Tile checksum mismatch"],
        },
    }

    events = dict(_snapshot_events(snapshot))

    assert "blocklist" in events
    assert "sweep" in events
    assert "validation" in events
    assert "dom_assist" not in events  # no assists in this snapshot


def test_snapshot_events_surface_dom_assist_summary():
    snapshot = {
        "state": "DONE",
        "manifest": {
            "dom_assists": [
                {"tile_index": 0, "line": 3, "reason": "low-alpha"},
                {"tile_index": 1, "line": 1, "reason": "punctuation"},
            ]
        },
    }

    events = dict(_snapshot_events(snapshot))

    assert "dom_assist" in events
    summary = json.loads(events["dom_assist"])
    assert summary["count"] == 2
