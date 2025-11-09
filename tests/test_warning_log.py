import json

from app.schemas import (
    ConcurrencyWindow,
    ManifestEnvironment,
    ManifestMetadata,
    ManifestWarning,
    ViewportSettings,
)
from app.warning_log import append_warning_log


class _DummyWarnings:
    canvas_warning_threshold = 3
    video_warning_threshold = 2
    shrink_warning_threshold = 1
    overlap_warning_ratio = 0.65
    seam_warning_ratio = 0.9
    seam_warning_min_pairs = 5


def _settings_with_log(path):
    class DummyLogging:
        warning_log_path = path

    class DummySettings:
        logging = DummyLogging()
        warnings = _DummyWarnings()

    return DummySettings()


def _demo_manifest(with_warning: bool = True, with_hits: bool = False) -> ManifestMetadata:
    env = ManifestEnvironment(
        cft_version="chrome-130",
        cft_label="Stable-1",
        playwright_channel="cft",
        playwright_version="1.55.0",
        browser_transport="cdp",
        viewport=ViewportSettings(width=1280, height=2000, device_scale_factor=2, color_scheme="light"),
        viewport_overlap_px=120,
        tile_overlap_px=120,
        scroll_settle_ms=350,
        max_viewport_sweeps=200,
        screenshot_style_hash="demo",
        screenshot_mask_selectors=(),
        ocr_model="olmOCR-2-7B-1025-FP8",
        ocr_use_fp8=True,
        ocr_concurrency=ConcurrencyWindow(min=2, max=8),
    )
    manifest = ManifestMetadata(environment=env)
    if with_warning:
        manifest.warnings.append(
            ManifestWarning(code="canvas-heavy", message="demo", count=5, threshold=3)
        )
    if with_hits:
        manifest.blocklist_hits = {"#cookie": 2}
    manifest.blocklist_version = "2025-11-07"
    return manifest


def test_append_warning_log_writes_file(monkeypatch, tmp_path):
    log_path = tmp_path / "warnings.jsonl"

    monkeypatch.setattr("app.warning_log.get_settings", lambda: _settings_with_log(log_path))

    append_warning_log(job_id="run-1", url="https://example.com", manifest=_demo_manifest())

    assert log_path.exists()
    content = log_path.read_text().strip()
    assert "canvas-heavy" in content


def test_append_warning_log_skips_when_empty(monkeypatch, tmp_path):
    log_path = tmp_path / "warnings.jsonl"

    monkeypatch.setattr("app.warning_log.get_settings", lambda: _settings_with_log(log_path))

    append_warning_log(job_id="run-2", url="https://example.com", manifest=_demo_manifest(with_warning=False, with_hits=False))

    assert not log_path.exists()


def test_append_warning_log_records_validation_failures_without_other_warnings(monkeypatch, tmp_path):
    log_path = tmp_path / "warnings.jsonl"
    monkeypatch.setattr("app.warning_log.get_settings", lambda: _settings_with_log(log_path))

    class DummyManifest:
        def __init__(self) -> None:
            self.warnings = []
            self.blocklist_hits = {}
            self.blocklist_version = "2025-11-08"
            self.validation_failures = ["Tile 1 checksum mismatch"]
            self.sweep_stats = {
                "sweep_count": 1,
                "total_scroll_height": 2200,
                "shrink_events": 0,
                "retry_attempts": 1,
                "overlap_pairs": 2,
                "overlap_match_ratio": 0.8,
            }
            self.overlap_match_ratio = 0.8

    append_warning_log(job_id="run-3", url="https://example.com/retry", manifest=DummyManifest())

    assert log_path.exists()
    lines = log_path.read_text().strip().splitlines()
    record = json.loads(lines[-1])
    assert record["validation_failures"] == ["Tile 1 checksum mismatch"]
    assert record["sweep_stats"]["retry_attempts"] == 1
    assert record["overlap_match_ratio"] == 0.8


def test_append_warning_log_includes_seam_summary(monkeypatch, tmp_path):
    log_path = tmp_path / "warnings.jsonl"
    monkeypatch.setattr("app.warning_log.get_settings", lambda: _settings_with_log(log_path))

    manifest = _demo_manifest()
    manifest.seam_markers = [
        {"tile_index": 0, "position": "top", "hash": "abc111"},
        {"tile_index": 1, "position": "bottom", "hash": "def222"},
    ]

    append_warning_log(job_id="run-4", url="https://example.com/seams", manifest=manifest)

    record = json.loads(log_path.read_text().strip())
    seam_summary = record.get("seam_markers")
    assert seam_summary["count"] == 2
    assert seam_summary["unique_tiles"] == 2
    assert seam_summary["unique_hashes"] == 2
    assert seam_summary["sample"][0]["hash"] == "abc111"


def test_append_warning_log_includes_seam_usage_when_events_exist(monkeypatch, tmp_path):
    log_path = tmp_path / "warnings.jsonl"
    monkeypatch.setattr("app.warning_log.get_settings", lambda: _settings_with_log(log_path))

    manifest = _demo_manifest()
    manifest.seam_markers = [
        {"tile_index": 0, "position": "top", "hash": "abc111"},
        {"tile_index": 1, "position": "bottom", "hash": "abc111"},
    ]
    manifest.seam_marker_events = [
        {"prev_tile_index": 0, "curr_tile_index": 1, "seam_hash": "abc111"},
        {"prev_tile_index": 1, "curr_tile_index": 2, "seam_hash": "def222"},
    ]

    append_warning_log(job_id="run-5", url="https://example.com/seams", manifest=manifest)

    record = json.loads(log_path.read_text().strip())
    seam_summary = record.get("seam_markers") or {}
    usage = seam_summary.get("usage") or {}
    assert usage.get("count") == 2
    sample = usage.get("sample")
    assert isinstance(sample, list) and sample[0]["prev_tile_index"] == 0
