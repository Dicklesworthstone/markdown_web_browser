from app.schemas import (
    ConcurrencyWindow,
    ManifestEnvironment,
    ManifestMetadata,
    ManifestSweepStats,
    ManifestWarning,
    ViewportSettings,
)


def test_manifest_metadata_accepts_blocklist_and_warnings() -> None:
    manifest = ManifestMetadata(
        environment=ManifestEnvironment(
            cft_version="chrome-130.0.6723.69",
            cft_label="Stable-1",
            playwright_channel="cft",
            playwright_version="1.55.0",
            browser_transport="cdp",
            viewport=ViewportSettings(width=1280, height=2000, device_scale_factor=2, color_scheme="light"),
            viewport_overlap_px=120,
            tile_overlap_px=120,
            scroll_settle_ms=350,
            max_viewport_sweeps=200,
            screenshot_style_hash="dev-sweeps-v1",
            screenshot_mask_selectors=(),
            ocr_model="olmOCR-2-7B-1025-FP8",
            ocr_use_fp8=True,
            ocr_concurrency=ConcurrencyWindow(min=2, max=8),
        ),
        blocklist_version="2025-11-07",
        blocklist_hits={"#onetrust-consent-sdk": 2},
        warnings=[
            ManifestWarning(
                code="canvas-heavy",
                message="High canvas count may hide chart labels.",
                count=6,
                threshold=3,
            )
        ],
        sweep_stats=ManifestSweepStats(
            sweep_count=4,
            total_scroll_height=12000,
            shrink_events=1,
            retry_attempts=1,
            overlap_pairs=6,
            overlap_match_ratio=0.83,
        ),
        overlap_match_ratio=0.83,
        validation_failures=["tile 0003 checksum mismatch"],
    )

    assert manifest.blocklist_version == "2025-11-07"
    assert manifest.blocklist_hits["#onetrust-consent-sdk"] == 2
    assert manifest.warnings[0].code == "canvas-heavy"
    assert manifest.sweep_stats is not None
    assert manifest.sweep_stats.overlap_pairs == 6
    assert manifest.overlap_match_ratio == 0.83
    assert manifest.validation_failures == ["tile 0003 checksum mismatch"]
