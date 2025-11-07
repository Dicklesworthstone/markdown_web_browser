from app.capture_warnings import WarningStats, build_sweep_warning, build_warnings
from app.settings import WarningSettings


def _settings(
    *,
    canvas: int = 3,
    video: int = 2,
    shrink: int = 1,
    overlap_ratio: float = 0.65,
    seam_ratio: float = 0.9,
    seam_pairs: int = 5,
) -> WarningSettings:
    return WarningSettings(
        canvas_warning_threshold=canvas,
        video_warning_threshold=video,
        shrink_warning_threshold=shrink,
        overlap_warning_ratio=overlap_ratio,
        seam_warning_ratio=seam_ratio,
        seam_warning_min_pairs=seam_pairs,
    )


def test_build_warnings_emits_canvas_and_video(monkeypatch):
    settings = _settings()
    stats = WarningStats(canvas_count=4, video_count=2, sticky_count=0, dialog_count=0)

    warnings = build_warnings(stats, settings=settings)

    assert [w.code for w in warnings] == ["canvas-heavy", "video-heavy"]
    assert warnings[0].count == 4
    assert warnings[1].threshold == 2


def test_build_warnings_handles_sticky_overlays():
    settings = _settings(canvas=10, video=10)
    stats = WarningStats(canvas_count=0, video_count=0, sticky_count=5, dialog_count=1)

    warnings = build_warnings(stats, settings=settings)

    assert [w.code for w in warnings] == ["sticky-chrome"]
    assert warnings[0].count == 6


def test_build_sweep_warning_flags_shrink_events():
    settings = _settings(shrink=2)

    warnings = build_sweep_warning(
        shrink_events=3,
        overlap_pairs=0,
        overlap_match_ratio=1.0,
        settings=settings,
    )

    assert [w.code for w in warnings] == ["scroll-shrink"]
    assert warnings[0].count == 3


def test_build_sweep_warning_flags_overlap_ratio():
    settings = _settings(overlap_ratio=0.8)

    warnings = build_sweep_warning(
        shrink_events=0,
        overlap_pairs=4,
        overlap_match_ratio=0.5,
        settings=settings,
    )

    assert [w.code for w in warnings] == ["overlap-low"]
    assert warnings[0].threshold == 0.8
    assert abs(warnings[0].count - 0.5) < 1e-6


def test_build_sweep_warning_emits_seam_warning():
    settings = _settings(seam_ratio=0.8, seam_pairs=2)

    warnings = build_sweep_warning(
        shrink_events=0,
        overlap_pairs=3,
        overlap_match_ratio=0.9,
        settings=settings,
    )

    assert warnings[-1].code == "duplicate-seam"


def test_build_sweep_warning_respects_seam_thresholds():
    settings = _settings(seam_ratio=0.95, seam_pairs=5)

    warnings = build_sweep_warning(
        shrink_events=0,
        overlap_pairs=3,
        overlap_match_ratio=0.9,
        settings=settings,
    )

    assert all(w.code != "duplicate-seam" for w in warnings)
