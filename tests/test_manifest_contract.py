from app.schemas import (
    ConcurrencyWindow,
    ManifestEnvironment,
    ManifestMetadata,
    ManifestOCRBatch,
    ManifestOCRQuota,
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
            viewport=ViewportSettings(
                width=1280, height=2000, device_scale_factor=2, color_scheme="light"
            ),
            viewport_overlap_px=120,
            tile_overlap_px=120,
            scroll_settle_ms=350,
            max_viewport_sweeps=200,
            screenshot_style_hash="dev-sweeps-v1",
            screenshot_mask_selectors=(),
            ocr_model="olmOCR-2-7B-1025-FP8",
            ocr_provider="olmocr",
            ocr_use_fp8=True,
            ocr_concurrency=ConcurrencyWindow(min=2, max=8),
            ocr_backend_id="olmocr-remote-openai",
            ocr_backend_mode="openai-compatible",
            ocr_hardware_path="remote",
            ocr_fallback_chain=("olmocr-remote-openai",),
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
        ocr_batches=[
            ManifestOCRBatch(
                tile_ids=["tile_0001", "tile_0002"],
                latency_ms=875,
                status_code=200,
                request_id="req-abc",
                payload_bytes=1_500_000,
                attempts=1,
            )
        ],
        ocr_quota=ManifestOCRQuota(
            limit=500000, used=200000, threshold_ratio=0.7, warning_triggered=False
        ),
        ocr_local_service={
            "enabled": True,
            "endpoint": "http://localhost:8001/v1",
            "healthy": True,
            "action": "reused",
        },
        ocr_failover_events=[
            {
                "event": "backend_failed",
                "backend_id": "glm-ocr-local-openai",
                "reason_code": "runtime.failover.local-unhealthy",
            }
        ],
        hardware_capabilities={
            "cpu_logical_cores": 16,
            "gpu_count": 1,
            "has_gpu": True,
            "preferred_hardware_path": "gpu",
        },
        backend_reason_codes=["policy.local.gpu-preferred"],
        backend_reevaluate_after_s=120,
        cache_seed="seed-xyz",
        cache_key="runtime-key-xyz",
        dom_assists=[
            {
                "tile_index": 0,
                "line": 3,
                "reason": "low-alpha",
                "dom_text": "Revenue Q4",
                "original_text": "Rev3nue?",
            }
        ],
    )

    assert manifest.blocklist_version == "2025-11-07"
    assert manifest.blocklist_hits["#onetrust-consent-sdk"] == 2
    assert manifest.warnings[0].code == "canvas-heavy"
    assert manifest.sweep_stats is not None
    assert manifest.sweep_stats.overlap_pairs == 6
    assert manifest.overlap_match_ratio == 0.83
    assert manifest.validation_failures == ["tile 0003 checksum mismatch"]
    assert manifest.ocr_batches[0].request_id == "req-abc"
    assert manifest.ocr_quota is not None
    assert manifest.ocr_quota.limit == 500000
    assert manifest.ocr_local_service is not None
    assert manifest.ocr_local_service["action"] == "reused"
    assert manifest.ocr_failover_events[0]["reason_code"] == "runtime.failover.local-unhealthy"
    assert manifest.dom_assists[0]["reason"] == "low-alpha"
    assert manifest.backend_id == "olmocr-remote-openai"
    assert manifest.backend_mode == "openai-compatible"
    assert manifest.hardware_path == "remote"
    assert manifest.fallback_chain == ["olmocr-remote-openai"]
    assert manifest.backend_reason_codes == ["policy.local.gpu-preferred"]
    assert manifest.backend_reevaluate_after_s == 120
    assert manifest.cache_seed == "seed-xyz"
    assert manifest.cache_key == "runtime-key-xyz"
    assert manifest.hardware_capabilities is not None
    assert manifest.hardware_capabilities["gpu_count"] == 1
