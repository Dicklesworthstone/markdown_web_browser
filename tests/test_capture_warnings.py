from app.capture_warnings import WarningStats, build_warnings
from app.settings import WarningSettings


def test_build_warnings_emits_canvas_and_video(monkeypatch):
    settings = WarningSettings(canvas_warning_threshold=3, video_warning_threshold=2)
    stats = WarningStats(canvas_count=4, video_count=2, sticky_count=0, dialog_count=0)

    warnings = build_warnings(stats, settings=settings)

    assert [w.code for w in warnings] == ["canvas-heavy", "video-heavy"]
    assert warnings[0].count == 4
    assert warnings[1].threshold == 2


def test_build_warnings_handles_sticky_overlays():
    settings = WarningSettings(canvas_warning_threshold=10, video_warning_threshold=10)
    stats = WarningStats(canvas_count=0, video_count=0, sticky_count=5, dialog_count=1)

    warnings = build_warnings(stats, settings=settings)

    assert [w.code for w in warnings] == ["sticky-chrome"]
    assert warnings[0].count == 6
