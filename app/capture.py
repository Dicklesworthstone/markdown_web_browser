"""Playwright-based capture routines (viewport sweeps + metadata logging)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from html import escape
from importlib import metadata
from pathlib import Path
from typing import Any, Callable, List, Optional, Sequence

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from app.auth import current_owner
from app.blocklist import BlocklistConfig, apply_blocklist, cached_blocklist
from app.capture_warnings import (
    CaptureWarningEntry,
    build_sweep_warning,
    collect_capture_warnings,
)
from app.egress import EgressDenied, EgressProxy, network_policy, validate_network_url
from app.schemas import validate_profile_id
from app.settings import BrowserSettings, WarningSettings, get_settings
from app.tiler import (
    TileSlice,
    align_cross_sweep_overlap,
    set_tile_overlap,
    slice_into_tiles,
    validate_tiles,
)

LOGGER = logging.getLogger(__name__)
CAPTURE_PIPELINE_VERSION = "uniform-tiles-css-fonts-egress-v4"

_VERSION_NUM_RE = re.compile(r"(\d+\.\d+\.\d+(?:\.\d+)?)")


class BrowserPinMismatchError(RuntimeError, ValueError):
    """Raised when the launched browser build or executable does not match a required pin."""


class UnsupportedTransportError(RuntimeError, ValueError):
    """Raised when a requested browser transport protocol is unsupported or unavailable."""


@dataclass(slots=True)
class CaptureConfig:
    """Inputs that describe how we should drive Chromium."""

    url: str
    viewport_width: int = 1280
    viewport_height: int = 2000
    device_scale_factor: int = 2
    color_scheme: str = "light"
    reduced_motion: bool = True
    profile_id: str | None = None
    owner: str | None = None
    long_side_px: int | None = None
    ocr_policy: str | None = None
    ocr_model: str | None = None
    ocr_use_fp8: bool | None = None
    ocr_max_tiles_in_flight: int | None = None
    semantic_post_enabled: bool = False
    semantic_post_max_chars: int | None = None
    cache_seed: str | None = None
    cache_key: str | None = None
    browser_transport: str | None = None
    cft_version: str | None = None
    cft_label: str | None = None
    executable_path: Path | str | None = None
    enforce_version_pin: bool | None = None
    stage_callback: Callable[[str, int | None, int | None], None] | None = None

    def report_stage(self, stage: str, done: int | None = None, total: int | None = None) -> None:
        if self.stage_callback:
            self.stage_callback(stage, done, total)


@dataclass(slots=True)
class ScrollPolicy:
    """Manifest-friendly description of the viewport sweep routine."""

    settle_ms: int
    max_steps: int
    viewport_overlap_px: int
    viewport_step_px: int


@dataclass(slots=True)
class SweepStats:
    """Counters that help ops diagnose scroll instability."""

    sweep_count: int
    total_scroll_height: int
    shrink_events: int
    retry_attempts: int
    overlap_pairs: int
    overlap_match_ratio: float
    attempt_count: int = 1
    abandoned_tile_count: int = 0
    truncated_by_max_steps: bool = False
    busy_timeout_count: int = 0
    attempt_history: list[dict[str, object]] = field(default_factory=list)


@dataclass(slots=True)
class CaptureManifest:
    """Metadata recorded alongside tiles and manifests."""

    url: str
    cft_label: str
    cft_version: str
    playwright_channel: str
    playwright_version: str
    browser_transport: str
    screenshot_style_hash: str
    viewport_width: int
    viewport_height: int
    device_scale_factor: int
    long_side_px: int
    capture_ms: int
    tiles_total: int
    scroll_policy: ScrollPolicy
    sweep_stats: SweepStats
    user_agent: str
    shrink_retry_limit: int
    blocklist_version: str
    blocklist_hits: dict[str, int]
    warnings: list[CaptureWarningEntry]
    overlap_match_ratio: float | None
    validation_failures: list[str]
    profile_id: str | None
    cache_seed: str | None = None
    cache_key: str | None = None
    cache_hit: bool = False
    backend_id: str | None = None
    backend_mode: str | None = None
    hardware_path: str | None = None
    requested_ocr_policy: str | None = None
    selected_ocr_policy: str | None = None
    ocr_model: str | None = None
    served_ocr_model: str | None = None
    ocr_use_fp8: bool | None = None
    ocr_prompt_template: str | None = None
    ocr_max_tiles_in_flight: int | None = None
    backend_reason_codes: list[str] = field(default_factory=list)
    backend_reevaluate_after_s: int | None = None
    fallback_chain: list[str] = field(default_factory=list)
    hardware_capabilities: dict[str, object] | None = None
    ocr_ms: int | None = None
    selected_ocr_model: str | None = None
    served_ocr_models: list[str] = field(default_factory=list)
    request_options: dict[str, object] = field(default_factory=dict)
    capture_pipeline_version: str = CAPTURE_PIPELINE_VERSION
    configured_cft_version: str | None = None
    cft_pin_verified: bool = False
    configured_screenshot_style_hash: str | None = None
    requested_browser_transport: str | None = None
    requested_browser_channels: str | None = None
    fontconfig_sha256: str | None = None
    stitch_ms: int | None = None
    ocr_batches: list[dict[str, object]] = field(default_factory=list)
    ocr_quota: dict[str, object] | None = None
    ocr_local_service: dict[str, object] | None = None
    ocr_failover_events: list[dict[str, object]] = field(default_factory=list)
    dom_assists: list[dict[str, object]] = field(default_factory=list)
    dom_assist_summary: dict[str, object] | None = None
    ocr_autotune: dict[str, object] | None = None
    seam_markers: list[dict[str, object]] = field(default_factory=list)
    seam_marker_events: list[dict[str, object]] = field(default_factory=list)
    dedup_events: list[dict[str, object]] = field(default_factory=list)
    dedup_summary: dict[str, object] | None = None
    semantic_post_summary: dict[str, object] | None = None
    semantic_post_ms: int | None = None
    embedding_model: str | None = None
    embedding_dim: int | None = None
    sections_embedded: int | None = None
    network_policy: dict[str, object] = field(default_factory=dict)

    def metadata_checklist(self) -> dict[str, Any]:
        """Return the mandatory 6-item capture metadata checklist."""
        return capture_metadata_checklist(self)


def capture_metadata_checklist(manifest: CaptureManifest) -> dict[str, Any]:
    """Return the mandatory AGENTS.md capture metadata checklist."""
    retry_attempts = int(getattr(manifest.sweep_stats, "retry_attempts", 0) or 0)
    shrink_events = int(getattr(manifest.sweep_stats, "shrink_events", 0) or 0)
    return {
        "cft_label": manifest.cft_label,
        "cft_version": manifest.cft_version,
        "browser_transport": manifest.browser_transport,
        "playwright_version": manifest.playwright_version,
        "screenshot_style_hash": manifest.screenshot_style_hash,
        "ocr_model": manifest.ocr_model or manifest.selected_ocr_policy,
        "ocr_policy": manifest.selected_ocr_policy or manifest.requested_ocr_policy or manifest.ocr_model,
        "viewport_sweep_retries_triggered": retry_attempts > 0,
        "sweep_retry_attempts": retry_attempts,
        "sweep_shrink_events": shrink_events,
    }


@dataclass(slots=True)
class CaptureResult:
    """Tiles plus the manifest metadata required downstream."""

    tiles: List[TileSlice]
    manifest: CaptureManifest
    dom_snapshot: bytes | None = None
    abandoned_tiles: List[TileSlice] = field(default_factory=list)
    dom_overlay: list[dict[str, object]] = field(default_factory=list)


def resolve_browser_transport(transport: str) -> str:
    """Report the transport the launcher will actually exercise.

    BiDi-only requests are rejected. A preference list may name bidi only when cdp
    is also present, and the exercised transport is then cdp.
    """

    preferences = [value.strip().lower() for value in transport.split(",") if value.strip()]
    if not preferences or any(value not in {"cdp", "bidi"} for value in preferences):
        raise UnsupportedTransportError(
            "Browser transport must be cdp or a cdp/bidi preference list"
        )
    if "cdp" not in preferences:
        raise UnsupportedTransportError(
            "BiDi is unsupported by this Python capture adapter; select cdp"
        )
    return "cdp"


def format_cft_version(raw_version: str | None, fallback: str = "") -> str:
    """Normalize a browser version string into canonical ``chrome-<version>`` form."""
    cleaned = str(raw_version or "").strip()
    if not cleaned:
        return fallback
    match = _VERSION_NUM_RE.search(cleaned)
    if match:
        return f"chrome-{match.group(1)}"
    if cleaned.lower().startswith(("chrome-", "chromium-")):
        return cleaned
    return f"chrome-{cleaned}"


def verify_browser_build(
    *,
    observed_version: str | None,
    configured_version: str | None,
    enforce_pin: bool = False,
) -> str:
    """Verify the launched browser version against configured pin and return effective build string."""
    obs_raw = str(observed_version or "").strip()
    cfg_raw = str(configured_version or "").strip()
    obs_match = _VERSION_NUM_RE.search(obs_raw)
    cfg_match = _VERSION_NUM_RE.search(cfg_raw)
    obs_num = obs_match.group(1) if obs_match else obs_raw
    cfg_num = cfg_match.group(1) if cfg_match else cfg_raw
    obs_canonical = format_cft_version(obs_raw, fallback=cfg_raw or "chrome-unknown")

    if enforce_pin:
        if not obs_num:
            raise BrowserPinMismatchError(
                f"Browser version pin enforcement failed: expected {cfg_raw!r}, but browser reported no version"
            )
        if cfg_num and obs_num != cfg_num:
            raise BrowserPinMismatchError(
                f"Browser build mismatch: pinned CFT_VERSION {cfg_raw!r} != observed browser {obs_canonical!r}"
            )
    elif obs_num and cfg_num and obs_num != cfg_num:
        LOGGER.info(
            "Observed browser build %s differs from configured CFT_VERSION %s; recording observed build",
            obs_canonical,
            cfg_raw,
        )

    return obs_canonical if obs_num else (cfg_raw or "chrome-unknown")


def build_user_agent_for_version(browser_version: str | None) -> str:
    """User-Agent for the browser that was actually launched, including its platform."""
    match = _VERSION_NUM_RE.search(str(browser_version or ""))
    version_token = match.group(1) if match else "130.0.0.0"
    if sys.platform == "linux":
        platform_token = "X11; Linux x86_64"
    elif sys.platform == "darwin":
        platform_token = "Macintosh; Intel Mac OS X 10_15_7"
    else:
        platform_token = "Windows NT 10.0; Win64; x64"
    return (
        f"Mozilla/5.0 ({platform_token}) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{version_token} Safari/537.36"
    )


def compute_effective_style_hash(
    *,
    settings_browser: Any,
    config: CaptureConfig,
    blocklist_version: str | None = None,
) -> str:
    """Compute effective screenshot style hash reflecting runtime geometry/mask overrides."""
    base_hash = str(getattr(settings_browser, "screenshot_style_hash", "") or "dev-sweeps-v1")
    default_w = int(getattr(settings_browser, "viewport_width", 1280))
    default_h = int(getattr(settings_browser, "viewport_height", 2000))
    default_dpr = int(getattr(settings_browser, "device_scale_factor", 2))
    default_scheme = str(getattr(settings_browser, "color_scheme", "light")).lower()
    default_long_side = int(getattr(settings_browser, "long_side_px", 1288))
    effective_long_side = int(config.long_side_px or default_long_side)

    if (
        config.viewport_width == default_w
        and config.viewport_height == default_h
        and config.device_scale_factor == default_dpr
        and config.color_scheme.lower() == default_scheme
        and effective_long_side == default_long_side
    ):
        return base_hash

    payload = {
        "base_hash": base_hash,
        "viewport": {
            "width": config.viewport_width,
            "height": config.viewport_height,
            "device_scale_factor": config.device_scale_factor,
            "color_scheme": config.color_scheme.lower(),
        },
        "long_side_px": effective_long_side,
        "mask_selectors": list(getattr(settings_browser, "screenshot_mask_selectors", ()) or ()),
        "blocklist_version": blocklist_version or "",
    }
    digest = hashlib.sha1(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
    return f"{base_hash}-{digest[:8]}"


def _pinned_executable(settings_browser: Any) -> Path | None:
    executable = getattr(settings_browser, "cft_executable", None)
    if executable is None:
        executable = getattr(settings_browser, "executable_path", None)
    return executable


async def capture_tiles(config: CaptureConfig) -> CaptureResult:
    """Run a deterministic viewport sweep and return OCR-ready tiles."""

    settings = get_settings()
    validate_network_url(config.url)
    start = time.perf_counter()

    blocklist_cfg = cached_blocklist(str(settings.browser.blocklist_path))
    warning_cfg = settings.warnings
    extra_artifacts: dict[str, Any] = {"abandoned_tiles": [], "dom_overlay": []}

    async with (
        EgressProxy(network_policy(settings, owner=current_owner())) as egress,
        async_playwright() as playwright,
    ):
        config.report_stage("BROWSER_STARTING")
        browser, actual_channel, actual_transport = await _launch_browser(
            playwright,
            settings.browser.playwright_channel,
            expected_build=settings.browser.cft_version,
            transport=settings.browser.browser_transport,
            executable=_pinned_executable(settings.browser),
            fontconfig_file=getattr(settings.browser, "fontconfig_file", None),
            proxy_url=egress.url,
        )
        actual_build = browser.version
        context = None
        storage_state_path = None
        try:
            context, storage_state_path = await _build_context(
                browser,
                config,
                profiles_root=settings.storage.profiles_root,
            )
            (
                tiles,
                sweep_stats,
                user_agent,
                blocklist_hits,
                warnings,
                dom_snapshot,
                validation_failures,
                seam_markers,
            ) = await _perform_viewport_sweeps(
                context,
                config,
                viewport_overlap_px=settings.browser.viewport_overlap_px,
                tile_overlap_px=settings.browser.tile_overlap_px,
                target_long_side_px=config.long_side_px or settings.browser.long_side_px,
                settle_ms=settings.browser.scroll_settle_ms,
                max_steps=settings.browser.max_viewport_sweeps,
                mask_selectors=settings.browser.screenshot_mask_selectors,
                shrink_retry_limit=settings.browser.shrink_retry_limit,
                blocklist_config=blocklist_cfg,
                warning_settings=warning_cfg,
                _artifacts_out=extra_artifacts,
            )
        finally:
            try:
                if context is not None:
                    try:
                        if storage_state_path is not None:
                            storage_state_path.parent.mkdir(parents=True, exist_ok=True)
                            await context.storage_state(path=str(storage_state_path))
                    finally:
                        await context.close()
            finally:
                await browser.close()

    capture_ms = int((time.perf_counter() - start) * 1000)
    ocr_cfg = getattr(settings, "ocr", None)
    default_model = getattr(ocr_cfg, "model", None) if ocr_cfg is not None else None
    default_fp8 = getattr(ocr_cfg, "use_fp8", None) if ocr_cfg is not None else None
    selected_model = config.ocr_model or default_model

    manifest_payload = CaptureManifest(
        url=config.url,
        cft_label=settings.browser.cft_label if actual_channel == "cft" else "unqualified-stock",
        cft_version=f"chrome-{actual_build}",
        playwright_channel=actual_channel,
        playwright_version=_playwright_version(),
        browser_transport=actual_transport,
        screenshot_style_hash=effective_screenshot_hash(config, settings.browser),
        viewport_width=config.viewport_width,
        viewport_height=config.viewport_height,
        device_scale_factor=config.device_scale_factor,
        long_side_px=config.long_side_px or settings.browser.long_side_px,
        capture_ms=capture_ms,
        tiles_total=len(tiles),
        scroll_policy=ScrollPolicy(
            settle_ms=settings.browser.scroll_settle_ms,
            max_steps=settings.browser.max_viewport_sweeps,
            viewport_overlap_px=settings.browser.viewport_overlap_px,
            viewport_step_px=max(1, config.viewport_height - settings.browser.viewport_overlap_px),
        ),
        sweep_stats=sweep_stats,
        user_agent=user_agent,
        shrink_retry_limit=settings.browser.shrink_retry_limit,
        blocklist_version=blocklist_cfg.version,
        blocklist_hits=blocklist_hits,
        warnings=warnings,
        overlap_match_ratio=sweep_stats.overlap_match_ratio,
        validation_failures=validation_failures,
        profile_id=config.profile_id,
        cache_seed=config.cache_seed,
        cache_key=config.cache_key,
        requested_ocr_policy=config.ocr_policy,
        selected_ocr_policy=config.ocr_policy or selected_model,
        ocr_model=selected_model,
        ocr_use_fp8=config.ocr_use_fp8 if config.ocr_use_fp8 is not None else default_fp8,
        ocr_max_tiles_in_flight=config.ocr_max_tiles_in_flight,
        configured_cft_version=settings.browser.cft_version,
        cft_pin_verified=actual_channel == "cft",
        configured_screenshot_style_hash=settings.browser.screenshot_style_hash,
        requested_browser_transport=settings.browser.browser_transport,
        requested_browser_channels=settings.browser.playwright_channel,
        fontconfig_sha256=fontconfig_digest(getattr(settings.browser, "fontconfig_file", None)),
        network_policy=egress.summary(),
    )

    manifest_payload.seam_markers = seam_markers
    return CaptureResult(
        tiles=tiles,
        manifest=manifest_payload,
        dom_snapshot=dom_snapshot,
        abandoned_tiles=list(extra_artifacts.get("abandoned_tiles") or []),
        dom_overlay=list(extra_artifacts.get("dom_overlay") or []),
    )


async def _perform_viewport_sweeps(
    context: BrowserContext,
    config: CaptureConfig,
    *,
    viewport_overlap_px: int,
    tile_overlap_px: int,
    target_long_side_px: int,
    settle_ms: int,
    max_steps: int,
    mask_selectors: Sequence[str],
    shrink_retry_limit: int,
    blocklist_config: BlocklistConfig,
    warning_settings: WarningSettings,
    _artifacts_out: dict[str, Any] | None = None,
) -> tuple[
    List[TileSlice],
    SweepStats,
    str,
    dict[str, int],
    list[CaptureWarningEntry],
    bytes | None,
    list[str],
    list[dict[str, object]],
]:
    page = await context.new_page()
    await _mask_automation(page)
    mask_locators = [page.locator(selector) for selector in mask_selectors]

    config.report_stage("NAVIGATING")
    response = await page.goto(config.url, wait_until="domcontentloaded", timeout=30000)
    if response is not None and "x-mdwb-egress-denied" in response.headers:
        raise EgressDenied("Capture navigation refused by the network policy")
    blocklist_hits = await apply_blocklist(page, url=config.url, config=blocklist_config)
    await _ensure_watermark_injected(page)
    warning_entries: list[CaptureWarningEntry] = await collect_capture_warnings(
        page, warning_settings
    )
    await page.evaluate("window.scrollTo(0, 0)")
    busy_timeout_count = 0
    emitted_busy_warning = False
    busy_res = await _stabilize_viewport(page, settle_ms=settle_ms)
    if busy_res.get("busy_timed_out"):
        busy_timeout_count += 1
        if not emitted_busy_warning:
            emitted_busy_warning = True
            warning_entries.append(
                CaptureWarningEntry(
                    code="aria-busy-timeout",
                    message="Elements with [aria-busy='true'] did not settle before timeout; captured best-effort state.",
                    count=float(busy_res.get("busy_count", 1) or 1),
                    threshold=0.0,
                )
            )

    sweep_count = 0
    shrink_events = 0
    retry_attempts = 0
    attempt_count = 1
    overlap_pairs = 0
    overlap_matches = 0
    tile_index = 0
    tiles: List[TileSlice] = []
    abandoned_tiles: List[TileSlice] = []
    attempt_history: list[dict[str, object]] = []
    dom_overlay_entries: list[dict[str, object]] = []
    seen_overlay_keys: set[tuple[str, str, int]] = set()
    validation_failures: list[str] = []
    viewport_step = max(1, config.viewport_height - viewport_overlap_px)

    scroll_height = await _scroll_height(page)
    y_offset = 0
    previous_tile: TileSlice | None = None
    reached_bottom = False

    while sweep_count < max_steps:
        top_hash = _seam_hash(y_offset) if y_offset > 0 else None
        bottom_reference = min(y_offset + config.viewport_height, scroll_height)
        bottom_hash = _seam_hash(bottom_reference)
        await _update_watermark_overlay(
            page,
            top_hash=top_hash,
            bottom_hash=bottom_hash,
            show_top=y_offset > 0,
            show_bottom=True,
        )

        step_boxes = await _harvest_viewport_dom_overlay(
            page,
            y_offset=y_offset,
            sweep_index=sweep_count,
            dpr=float(config.device_scale_factor),
        )
        for box_entry in step_boxes:
            css_box = box_entry.get("css_box")
            css_y = int(round(float(css_box.get("y", 0)))) if isinstance(css_box, dict) else 0
            dedup_key = (
                str(box_entry.get("tag") or ""),
                str(box_entry.get("text") or ""),
                css_y,
            )
            if dedup_key not in seen_overlay_keys:
                seen_overlay_keys.add(dedup_key)
                dom_overlay_entries.append(box_entry)

        config.report_stage("CAPTURING")
        screenshot = await page.screenshot(
            type="png",
            full_page=False,
            animations="disabled",
            caret="hide",
            mask=mask_locators if mask_locators else None,
        )
        config.report_stage("TILING")
        new_tiles = await slice_into_tiles(
            screenshot,
            overlap_px=tile_overlap_px,
            tile_index_offset=tile_index,
            viewport_y_offset=y_offset,
            target_long_side_px=target_long_side_px,
            device_scale_factor=config.device_scale_factor,
        )
        try:
            validate_tiles(new_tiles)
        except ValueError as exc:
            failure = f"viewport sweep {sweep_count}: {exc}"
            validation_failures.append(failure)
            raise

        if new_tiles:
            if top_hash:
                new_tiles[0].seam_top_hash = top_hash
                if previous_tile is not None:
                    previous_tile.seam_bottom_hash = top_hash
            for intra_idx in range(len(new_tiles) - 1):
                intra_seam = _seam_hash(new_tiles[intra_idx + 1].css_y_offset)
                new_tiles[intra_idx].seam_bottom_hash = intra_seam
                new_tiles[intra_idx + 1].seam_top_hash = intra_seam
            if bottom_hash:
                new_tiles[-1].seam_bottom_hash = bottom_hash

        for tile in new_tiles:
            if previous_tile:
                await asyncio.to_thread(set_tile_overlap, previous_tile, tile)
                # Does not rehash when set_tile_overlap already stored strip evidence.
                align_cross_sweep_overlap(previous_tile, tile)
                match = _overlap_match(previous_tile, tile)
                if match is not None:
                    overlap_pairs += 1
                    if match:
                        overlap_matches += 1
            previous_tile = tile

        tiles.extend(new_tiles)
        tile_index += len(new_tiles)
        sweep_count += 1

        new_height = await _scroll_height(page)
        height_shrank = new_height < scroll_height
        if height_shrank:
            shrink_events += 1
            if retry_attempts < shrink_retry_limit:
                retry_attempts += 1
                attempt_history.append(
                    {
                        "attempt": attempt_count,
                        "reason": "scroll_height_shrank",
                        "previous_scroll_height": scroll_height,
                        "new_scroll_height": new_height,
                        "sweeps_completed": sweep_count,
                        "abandoned_tiles": len(tiles),
                    }
                )
                abandoned_tiles.extend(tiles)
                tiles.clear()
                tile_index = 0
                sweep_count = 0
                overlap_pairs = 0
                overlap_matches = 0
                previous_tile = None
                dom_overlay_entries.clear()
                seen_overlay_keys.clear()
                y_offset = 0
                scroll_height = new_height
                attempt_count += 1
                await page.evaluate("window.scrollTo(0, 0)")
                retry_busy = await _stabilize_viewport(page, settle_ms=settle_ms)
                if retry_busy.get("busy_timed_out"):
                    busy_timeout_count += 1
                continue
        scroll_height = new_height

        if y_offset + config.viewport_height >= scroll_height:
            reached_bottom = True
            break

        next_offset = min(
            y_offset + viewport_step,
            max(0, scroll_height - config.viewport_height),
        )
        if next_offset <= y_offset:
            reached_bottom = True
            break

        config.report_stage("SCROLLING")
        await page.evaluate(f"window.scrollTo(0, {next_offset})")
        step_busy = await _stabilize_viewport(page, settle_ms=settle_ms)
        if step_busy.get("busy_timed_out"):
            busy_timeout_count += 1
            if not emitted_busy_warning:
                emitted_busy_warning = True
                warning_entries.append(
                    CaptureWarningEntry(
                        code="aria-busy-timeout",
                        message="Elements with [aria-busy='true'] did not settle before timeout; captured best-effort state.",
                        count=float(step_busy.get("busy_count", 1) or 1),
                        threshold=0.0,
                    )
                )
        grown_height = await _scroll_height_if_grown(page, scroll_height)
        if grown_height > scroll_height:
            scroll_height = grown_height
        y_offset = next_offset

    truncated_by_max_steps = (
        not reached_bottom
        and sweep_count >= max_steps
        and (y_offset + config.viewport_height < scroll_height)
    )
    if truncated_by_max_steps:
        warning_entries.append(
            CaptureWarningEntry(
                code="max-sweeps-truncated",
                message="Viewport sweep reached max_viewport_sweeps before document bottom; capture is truncated.",
                count=float(sweep_count),
                threshold=float(max_steps),
            )
        )

    attempt_history.append(
        {
            "attempt": attempt_count,
            "reason": "truncated_max_steps" if truncated_by_max_steps else "completed",
            "scroll_height": scroll_height,
            "sweeps_completed": sweep_count,
            "active_tiles": len(tiles),
        }
    )

    user_agent = await page.evaluate("navigator.userAgent")
    dom_html = await page.content()
    await page.close()

    stats = SweepStats(
        sweep_count=sweep_count,
        total_scroll_height=scroll_height,
        shrink_events=shrink_events,
        retry_attempts=retry_attempts,
        overlap_pairs=overlap_pairs,
        overlap_match_ratio=_safe_ratio(overlap_matches, overlap_pairs),
        attempt_count=attempt_count,
        abandoned_tile_count=len(abandoned_tiles),
        truncated_by_max_steps=truncated_by_max_steps,
        busy_timeout_count=busy_timeout_count,
        attempt_history=attempt_history,
    )
    sweep_warnings = build_sweep_warning(
        shrink_events=stats.shrink_events,
        overlap_pairs=stats.overlap_pairs,
        overlap_match_ratio=stats.overlap_match_ratio,
        settings=warning_settings,
    )
    warning_entries.extend(sweep_warnings)
    dom_bytes: bytes | None = dom_html.encode("utf-8") if dom_html else None
    seam_markers = _collect_seam_markers(tiles)
    if _artifacts_out is not None:
        _artifacts_out["abandoned_tiles"] = abandoned_tiles
        _artifacts_out["dom_overlay"] = dom_overlay_entries
    return (
        tiles,
        stats,
        user_agent,
        blocklist_hits,
        warning_entries,
        dom_bytes,
        validation_failures,
        seam_markers,
    )


_WATERMARK_STYLE = """
#mdwb-watermark-top,#mdwb-watermark-bottom {
  position: fixed;
  left: 0;
  right: 0;
  height: 2px;
  z-index: 2147483647;
  pointer-events: none;
  mix-blend-mode: difference;
  opacity: 0.55;
  background-size: 24px 2px;
}
#mdwb-watermark-top { top: 0; }
#mdwb-watermark-bottom { bottom: 0; }
"""


def effective_screenshot_hash(config: CaptureConfig, browser: BrowserSettings) -> str:
    """Bind effective viewport, mask selectors and actual injected style bytes."""
    payload = {
        "configured_style_hash": browser.screenshot_style_hash,
        "watermark_css": _WATERMARK_STYLE,
        "masks": list(browser.screenshot_mask_selectors),
        "viewport": [config.viewport_width, config.viewport_height, config.device_scale_factor],
        "color_scheme": config.color_scheme,
        "reduced_motion": config.reduced_motion,
        "animations": "disabled",
        "caret": "hide",
        "full_page": False,
        "capture_pipeline_version": CAPTURE_PIPELINE_VERSION,
        "fontconfig_sha256": fontconfig_digest(getattr(browser, "fontconfig_file", None)),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _system_fontconfig_bytes() -> bytes:
    cache = escape(str(Path.home() / ".cache/mdwb/fontcache"), quote=False)
    return (
        '<?xml version="1.0"?><!DOCTYPE fontconfig SYSTEM "urn:fontconfig:fonts.dtd">'
        "<fontconfig><dir>/usr/share/fonts</dir><dir>/usr/local/share/fonts</dir>"
        f"<cachedir>{cache}</cachedir></fontconfig>"
    ).encode()


def fontconfig_digest(path: Path | None) -> str | None:
    if sys.platform != "linux":
        if path is not None:
            raise ValueError("Fontconfig overrides are supported only on Linux")
        return None
    content = path.read_bytes() if path else _system_fontconfig_bytes()
    return hashlib.sha256(content).hexdigest()


def _browser_font_environment(path: Path | None) -> dict[str, str] | None:
    """Isolate Linux capture fonts from user aliases; preserve the host config."""
    if sys.platform != "linux":
        fontconfig_digest(path)
        return None
    if path is None:
        content = _system_fontconfig_bytes()
        digest = hashlib.sha256(content).hexdigest()
        path = Path.home() / ".cache/mdwb/fonts" / f"{digest}.conf"
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("xb") as stream:
                stream.write(content)
        except FileExistsError:
            if path.read_bytes() != content:
                raise ValueError("Generated browser font configuration has unexpected bytes")
    if not path.is_file():
        raise ValueError("Browser font configuration is unavailable")
    return {**os.environ, "FONTCONFIG_FILE": str(path.resolve())}


async def _ensure_watermark_injected(page: Page) -> None:
    await page.add_style_tag(content=_WATERMARK_STYLE)
    await page.evaluate(
        """
(() => {
  if (window.__mdwbWatermarkReady) return;
  const top = document.getElementById('mdwb-watermark-top') || document.createElement('div');
  top.id = 'mdwb-watermark-top';
  const bottom = document.getElementById('mdwb-watermark-bottom') || document.createElement('div');
  bottom.id = 'mdwb-watermark-bottom';
  if (!top.isConnected) document.body.appendChild(top);
  if (!bottom.isConnected) document.body.appendChild(bottom);
  window.__mdwbWatermarkTop = top;
  window.__mdwbWatermarkBottom = bottom;
  window.__mdwbWatermarkReady = true;
  window.__mdwbSeamState = { topHash: null, bottomHash: null, showTop: false, showBottom: false };
})();
        """
    )
    await page.evaluate(
        """
(() => {
  if (window.__mdwbObserverState) return;
  const state = {
    lastMutationTs: performance.now(),
    lastIntersectionTs: performance.now(),
    intersectedCount: 0,
  };
  window.__mdwbObserverState = state;
  try {
    if (typeof IntersectionObserver !== 'undefined') {
      const io = new IntersectionObserver((entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting) {
            state.intersectedCount += 1;
            state.lastIntersectionTs = performance.now();
            const target = entry.target;
            if (target && target.getAttribute) {
              const dataSrc = target.getAttribute('data-src') || target.getAttribute('data-lazy-src');
              if (dataSrc && target.tagName === 'IMG' && !target.getAttribute('src')) {
                target.setAttribute('src', dataSrc);
              }
            }
          }
        }
      }, { root: null, rootMargin: '200px 0px', threshold: 0.01 });
      const observeAll = () => {
        const nodes = document.querySelectorAll('img[loading="lazy"], [data-lazy], [data-src], section, article, main, table, pre');
        nodes.forEach((n) => io.observe(n));
      };
      observeAll();
      state.observeAll = observeAll;
    }
    if (typeof MutationObserver !== 'undefined' && document.documentElement) {
      const mo = new MutationObserver(() => {
        state.lastMutationTs = performance.now();
        if (state.observeAll) state.observeAll();
      });
      mo.observe(document.documentElement, { childList: true, subtree: true, attributes: true, attributeFilter: ['aria-busy', 'style', 'class', 'hidden'] });
    }
  } catch (_err) {}
})();
        """
    )


async def _update_watermark_overlay(
    page: Page,
    *,
    top_hash: str | None,
    bottom_hash: str | None,
    show_top: bool,
    show_bottom: bool,
) -> None:
    await page.evaluate(
        """
({ topHash, bottomHash, showTop, showBottom }) => {
  window.__mdwbSeamState = { topHash, bottomHash, showTop, showBottom };
  if (!window.__mdwbWatermarkReady) {
    return;
  }
  const apply = (el, hash, visible) => {
    if (!el) return;
    if (!visible || !hash) {
      el.style.display = 'none';
      return;
    }
    const color = `#${hash}`;
    el.style.display = 'block';
    el.style.backgroundImage = `repeating-linear-gradient(90deg, ${color} 0 12px, transparent 12px 24px)`;
  };
  apply(window.__mdwbWatermarkTop, topHash, showTop);
  apply(window.__mdwbWatermarkBottom, bottomHash, showBottom);
};
        """,
        {
            "topHash": top_hash,
            "bottomHash": bottom_hash,
            "showTop": show_top,
            "showBottom": show_bottom,
        },
    )


async def _stabilize_viewport(page: Page, *, settle_ms: int) -> dict[str, Any]:
    """Wait for fonts, bounded [aria-busy='true'] completion, and IntersectionObserver layout settling."""
    busy_timeout_ms = min(2000, max(80, int(settle_ms) * 5))
    quiet_window_ms = min(150, max(20, int(settle_ms) // 2))
    res = await page.evaluate(
        """
async ({ busyTimeoutMs, quietWindowMs }) => {
  const start = performance.now();
  try {
    if (document.fonts && document.fonts.ready) {
      await Promise.race([
        document.fonts.ready,
        new Promise((r) => setTimeout(r, Math.min(800, busyTimeoutMs))),
      ]);
    }
  } catch (_e) {}

  const hasBusy = () => {
    const busyNodes = document.querySelectorAll('[aria-busy="true"]');
    return busyNodes ? busyNodes.length : 0;
  };

  const getScrollHeight = () => {
    const el = document.scrollingElement || document.documentElement || document.body;
    return el ? el.scrollHeight : 0;
  };

  let lastHeight = getScrollHeight();
  let stableSince = performance.now();

  while ((performance.now() - start) < busyTimeoutMs) {
    const busyCount = hasBusy();
    const currHeight = getScrollHeight();
    const obs = window.__mdwbObserverState;
    const recentObs = obs ? Math.max(obs.lastMutationTs || 0, obs.lastIntersectionTs || 0) : 0;
    const now = performance.now();

    if (currHeight !== lastHeight) {
      lastHeight = currHeight;
      stableSince = now;
    }

    const quietFor = now - Math.max(stableSince, recentObs);
    if (busyCount === 0 && quietFor >= quietWindowMs) {
      break;
    }
    await new Promise((r) => setTimeout(r, 25));
  }

  await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
  const remainingBusy = hasBusy();
  return {
    supported: true,
    busy_timed_out: remainingBusy > 0,
    busy_count: remainingBusy,
    scroll_height: getScrollHeight(),
  };
}
        """,
        {"busyTimeoutMs": busy_timeout_ms, "quietWindowMs": quiet_window_ms},
    )
    await page.wait_for_timeout(settle_ms)
    if isinstance(res, dict):
        return res
    return {"supported": False, "busy_timed_out": False, "busy_count": 0}


async def _scroll_height_if_grown(page: Page, current_height: int) -> int:
    """Return updated scrollHeight from live DOM without consuming _FakePage's scrollHeight iterator."""
    res = await page.evaluate(
        """
() => {
  const el = document.scrollingElement || document.documentElement || document.body;
  return el ? el.scrollHeight : null;
}
        """
    )
    if isinstance(res, (int, float)) and int(res) > current_height:
        return int(res)
    return current_height


async def _harvest_viewport_dom_overlay(
    page: Page,
    *,
    y_offset: int,
    sweep_index: int,
    dpr: float,
) -> list[dict[str, object]]:
    """Harvest viewport-synchronized DOM element bounding boxes and stamp coordinates onto elements."""
    raw = await page.evaluate(
        """
({ yOffset, sweepIndex, dpr }) => {
  const selectors = 'h1,h2,h3,h4,h5,h6,p,li,figcaption,caption,pre,code,table,th,td,a,[role="heading"]';
  const nodes = document.querySelectorAll(selectors);
  const vh = window.innerHeight || 2000;
  const vw = window.innerWidth || 1280;
  const scrollY = typeof window.scrollY === 'number' ? window.scrollY : yOffset;
  const scrollX = typeof window.scrollX === 'number' ? window.scrollX : 0;
  const results = [];
  for (const el of nodes) {
    const style = window.getComputedStyle(el);
    if (!style || style.display === 'none' || style.visibility === 'hidden' || parseFloat(style.opacity || '1') === 0) {
      continue;
    }
    const rect = el.getBoundingClientRect();
    if (!rect || rect.width <= 0 || rect.height <= 0) continue;
    if (rect.bottom <= 0 || rect.top >= vh || rect.right <= 0 || rect.left >= vw) continue;
    const text = (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
    if (!text) continue;
    const cssX = Math.round((rect.left + scrollX) * 10) / 10;
    const cssY = Math.round((rect.top + scrollY) * 10) / 10;
    const cssW = Math.round(rect.width * 10) / 10;
    const cssH = Math.round(rect.height * 10) / 10;
    const srcX = Math.round(cssX * dpr * 10) / 10;
    const srcY = Math.round(cssY * dpr * 10) / 10;
    const srcW = Math.round(cssW * dpr * 10) / 10;
    const srcH = Math.round(cssH * dpr * 10) / 10;
    try {
      el.setAttribute('data-mdwb-css-y', String(cssY));
      el.setAttribute('data-mdwb-css-h', String(cssH));
      el.setAttribute('data-mdwb-src-y', String(srcY));
      el.setAttribute('data-mdwb-src-h', String(srcH));
    } catch (_e) {}
    const tag = (el.tagName || '').toLowerCase();
    const role = el.getAttribute('role');
    const source = role === 'heading' ? "[role='heading']" : tag;
    const href = tag === 'a' ? (el.getAttribute('href') || null) : null;
    results.push({
      tag,
      source,
      text: text.slice(0, 500),
      href,
      sweep_index: sweepIndex,
      viewport_y_offset: yOffset,
      css_box: { x: cssX, y: cssY, width: cssW, height: cssH },
      screenshot_box: { x: srcX, y: srcY, width: srcW, height: srcH },
    });
  }
  return results;
}
        """,
        {"yOffset": int(y_offset), "sweepIndex": int(sweep_index), "dpr": float(dpr)},
    )
    if isinstance(raw, list):
        return [dict(item) for item in raw if isinstance(item, dict)]
    return []


def _seam_hash(value: int) -> str:
    digest = hashlib.sha1(str(value).encode("utf-8")).hexdigest()
    return digest[:6]


def _collect_seam_markers(tiles: Sequence[TileSlice]) -> list[dict[str, object]]:
    markers: list[dict[str, object]] = []
    for tile in tiles:
        if tile.seam_top_hash:
            markers.append(
                {
                    "tile_index": tile.index,
                    "position": "top",
                    "hash": tile.seam_top_hash,
                }
            )
        if tile.seam_bottom_hash:
            markers.append(
                {
                    "tile_index": tile.index,
                    "position": "bottom",
                    "hash": tile.seam_bottom_hash,
                }
            )
    return markers


_CHANNEL_ALIASES = {
    "chrome-for-testing": "cft",
}


async def _launch_browser(
    playwright,
    channel: str,
    *,
    expected_build: str | None = None,
    transport: str = "cdp",
    executable: Path | None = None,
    fontconfig_file: Path | None = None,
    proxy_url: str | None = None,
) -> tuple[Browser, str, str]:
    actual_transport = resolve_browser_transport(transport)
    version = _playwright_version()
    parts = re.match(r"^(\d+)\.(\d+)", version)
    if parts is None or (int(parts[1]), int(parts[2])) < (1, 50):
        raise ValueError(f"Playwright >=1.50 is required; found {version}")
    preferences = [_normalize_channel(value) for value in channel.split(",") if value.strip()]
    allowed = {
        "cft",
        "chromium",
        "chrome",
        "chrome-beta",
        "chrome-dev",
        "chrome-canary",
        "msedge",
        "msedge-beta",
        "msedge-dev",
        "msedge-canary",
    }
    if not preferences or any(value not in allowed for value in preferences):
        raise ValueError("Unsupported browser channel or empty preference list")
    browser_env = _browser_font_environment(fontconfig_file)

    launch_args = [
        "--headless=new",
        "--disable-blink-features=AutomationControlled",
        "--disable-dev-shm-usage",
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--window-size=1920,1080",
    ]
    if proxy_url:
        launch_args.extend(
            [
                "--disable-quic",
                "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
            ]
        )
    proxy = {"server": proxy_url, "bypass": "<-loopback>"} if proxy_url else None

    failures = []
    for selected in preferences:
        browser: Browser | None = None
        try:
            if selected == "cft":
                build = (expected_build or "").removeprefix("chrome-")
                if not re.fullmatch(r"\d+\.\d+\.\d+\.\d+", build):
                    raise ValueError("CFT_VERSION must name an exact four-part Chrome build")
                path = (
                    executable or Path.home() / ".cache/mdwb/cft" / build / "chrome-linux64/chrome"
                )
                if not path.is_file():
                    raise ValueError(
                        f"Pinned CfT executable is missing: {path}. Install that build or set CFT_EXECUTABLE."
                    )
                browser = await playwright.chromium.launch(
                    executable_path=str(path),
                    headless=True,
                    args=launch_args,
                    env=browser_env,
                    proxy=proxy,
                )
                if browser.version != build:
                    observed = browser.version
                    raise ValueError(f"CfT pin mismatch: expected {build}, launched {observed}")
            else:
                browser = await playwright.chromium.launch(
                    channel=selected,
                    headless=True,
                    args=launch_args,
                    env=browser_env,
                    proxy=proxy,
                )
            probe = await browser.new_page()
            try:
                width = await probe.evaluate("""() => {
                    const context = document.createElement('canvas').getContext('2d');
                    context.font = '16px sans-serif';
                    return context.measureText('Capture text 7319').width;
                }""")
            finally:
                await probe.close()
            if not isinstance(width, (int, float)) or width <= 0:
                raise ValueError(
                    "Browser fonts cannot render text; configure PLAYWRIGHT_FONTCONFIG_FILE"
                )
            LOGGER.info(
                "Launched browser build=%s channel=%s transport=%s",
                browser.version,
                selected,
                actual_transport,
            )
            return browser, selected, actual_transport
        except BaseException as exc:
            if browser is not None:
                await browser.close()
            if not isinstance(exc, Exception):
                raise
            failures.append(f"{selected}: {exc}")
            LOGGER.warning("Browser candidate failed: %s", failures[-1])
    raise ValueError("Browser launch failed: " + "; ".join(failures))


async def _build_context(
    browser: Browser,
    config: CaptureConfig,
    *,
    profiles_root: Path | None = None,
) -> tuple[BrowserContext, Path | None]:
    raw_version = getattr(browser, "version", None)
    user_agent = build_user_agent_for_version(
        str(raw_version) if isinstance(raw_version, str) else None
    )

    options: dict[str, Any] = {
        "viewport": {"width": config.viewport_width, "height": config.viewport_height},
        "device_scale_factor": config.device_scale_factor,
        "color_scheme": config.color_scheme,
        "locale": "en-US",
        "user_agent": user_agent,
        "reduced_motion": "reduce" if config.reduced_motion else "no-preference",
    }
    storage_state_path: Path | None = None
    if config.profile_id and profiles_root is not None:
        storage_state_path = _profile_storage_state_path(profiles_root, config.profile_id)
        storage_state_path.parent.mkdir(parents=True, exist_ok=True)
        if storage_state_path.exists():
            options["storage_state"] = storage_state_path
    context = await browser.new_context(**options)
    return context, storage_state_path


async def _mask_automation(page: Page) -> None:
    """Comprehensive automation masking to evade bot detection."""
    await page.add_init_script(
        """
        // Mask webdriver property
        Object.defineProperty(navigator, 'webdriver', {
            get: () => undefined,
        });

        // Mask automation-controlled flag
        delete navigator.__proto__.webdriver;

        // Override plugins to show realistic values (not empty array)
        Object.defineProperty(navigator, 'plugins', {
            get: () => [
                { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
                { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: 'Portable Document Format' },
                { name: 'Native Client', filename: 'internal-nacl-plugin', description: 'Native Client Executable' }
            ]
        });

        // Override languages to look more realistic
        Object.defineProperty(navigator, 'languages', {
            get: () => ['en-US', 'en']
        });

        // Add realistic chrome runtime
        if (!window.chrome) {
            window.chrome = {
                runtime: {},
                loadTimes: function() {},
                csi: function() {},
                app: {}
            };
        }

        // Mask permissions API that exposes automation
        const originalQuery = window.navigator.permissions.query;
        window.navigator.permissions.query = (parameters) => (
            parameters.name === 'notifications' ?
                Promise.resolve({ state: Notification.permission }) :
                originalQuery(parameters)
        );

        // Override connection type which can leak headless status
        Object.defineProperty(navigator, 'connection', {
            get: () => ({
                effectiveType: '4g',
                rtt: 50,
                downlink: 10,
                saveData: false
            })
        });

        // Add realistic hardware concurrency
        Object.defineProperty(navigator, 'hardwareConcurrency', {
            get: () => 8
        });

        // Mask device memory which can leak
        Object.defineProperty(navigator, 'deviceMemory', {
            get: () => 8
        });
        """
    )


async def _scroll_height(page: Page) -> int:
    return await page.evaluate("document.scrollingElement.scrollHeight")


def _playwright_version() -> str:
    try:
        return metadata.version("playwright")
    except metadata.PackageNotFoundError:  # pragma: no cover - dev fallback
        return "unknown"


def _validate_playwright_version(version_str: str | None = None) -> str:
    """Validate that Playwright is >= 1.50 as required by screenshot animation freeze."""
    ver = (version_str if version_str is not None else _playwright_version()).strip()
    if not ver or ver == "unknown":
        return ver or "unknown"
    parts = re.findall(r"\d+", ver)
    if len(parts) >= 2:
        major, minor = int(parts[0]), int(parts[1])
        if (major, minor) < (1, 50):
            raise RuntimeError(
                f"Playwright >= 1.50 is required for deterministic screenshot animation freeze; found {ver!r}"
            )
    return ver


def _normalize_channel(channel: str) -> str:
    if not channel:
        return "chromium"
    lowered = channel.strip().lower()
    return _CHANNEL_ALIASES.get(lowered, lowered)


def _profile_storage_state_path(base: Path, profile_id: str) -> Path:
    safe = validate_profile_id(profile_id)
    root = base.resolve()
    owner = current_owner()
    namespace = hashlib.sha256(owner.encode()).hexdigest() if owner else "anonymous"
    target = (root / namespace / safe / "storage_state.json").resolve()
    if not target.is_relative_to(root):
        raise ValueError("Profile storage path escapes profiles root")
    return target


def _overlap_match(previous_tile: TileSlice, current_tile: TileSlice) -> Optional[bool]:
    if not previous_tile.bottom_overlap_sha256 or not current_tile.top_overlap_sha256:
        return None
    return previous_tile.bottom_overlap_sha256 == current_tile.top_overlap_sha256


def _safe_ratio(numerator: int, denominator: int) -> float:
    if denominator == 0:
        return 0.0
    return round(numerator / denominator, 4)
