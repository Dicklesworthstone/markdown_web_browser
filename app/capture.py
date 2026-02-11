"""Playwright-based capture routines (viewport sweeps + metadata logging)."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path
import re
from typing import Any, List, Optional, Sequence

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from app.blocklist import BlocklistConfig, apply_blocklist, cached_blocklist
from app.capture_warnings import (
    CaptureWarningEntry,
    build_sweep_warning,
    collect_capture_warnings,
)
from app.settings import WarningSettings, get_settings
from app.tiler import TileSlice, slice_into_tiles, validate_tiles

LOGGER = logging.getLogger(__name__)


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
    long_side_px: int | None = None
    cache_seed: str | None = None
    cache_key: str | None = None


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
    backend_reason_codes: list[str] = field(default_factory=list)
    backend_reevaluate_after_s: int | None = None
    fallback_chain: list[str] = field(default_factory=list)
    hardware_capabilities: dict[str, object] | None = None
    ocr_ms: int | None = None
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


@dataclass(slots=True)
class CaptureResult:
    """Tiles plus the manifest metadata required downstream."""

    tiles: List[TileSlice]
    manifest: CaptureManifest
    dom_snapshot: bytes | None = None


async def capture_tiles(config: CaptureConfig) -> CaptureResult:
    """Run a deterministic viewport sweep and return OCR-ready tiles."""

    settings = get_settings()
    start = time.perf_counter()

    blocklist_cfg = cached_blocklist(str(settings.browser.blocklist_path))
    warning_cfg = settings.warnings

    async with async_playwright() as playwright:
        browser = await _launch_browser(playwright, settings.browser.playwright_channel)
        context, storage_state_path = await _build_context(
            browser,
            config,
            profiles_root=settings.storage.profiles_root,
        )
        try:
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
            )
        finally:
            if storage_state_path is not None:
                storage_state_path.parent.mkdir(parents=True, exist_ok=True)
                await context.storage_state(path=str(storage_state_path))
            await context.close()
            await browser.close()

    capture_ms = int((time.perf_counter() - start) * 1000)

    manifest_payload = CaptureManifest(
        url=config.url,
        cft_label=settings.browser.cft_label,
        cft_version=settings.browser.cft_version,
        playwright_channel=settings.browser.playwright_channel,
        playwright_version=_playwright_version(),
        browser_transport=settings.browser.browser_transport,
        screenshot_style_hash=settings.browser.screenshot_style_hash,
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
    )

    manifest_payload.seam_markers = seam_markers
    return CaptureResult(tiles=tiles, manifest=manifest_payload, dom_snapshot=dom_snapshot)


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

    # Use 'domcontentloaded' for fastest reliable page load
    # This fires when HTML is parsed, before all resources load
    # Works better with Cloudflare challenge pages that may have delayed resources
    await page.goto(config.url, wait_until="domcontentloaded", timeout=30000)
    blocklist_hits = await apply_blocklist(page, url=config.url, config=blocklist_config)
    await _ensure_watermark_injected(page)
    warning_entries: list[CaptureWarningEntry] = await collect_capture_warnings(
        page, warning_settings
    )
    await page.evaluate("window.scrollTo(0, 0)")
    await page.wait_for_timeout(settle_ms)
    sweep_count = 0
    shrink_events = 0
    retry_attempts = 0
    overlap_pairs = 0
    overlap_matches = 0
    tile_index = 0
    tiles: List[TileSlice] = []
    validation_failures: list[str] = []
    viewport_step = max(1, config.viewport_height - viewport_overlap_px)

    scroll_height = await _scroll_height(page)
    y_offset = 0
    previous_tile: TileSlice | None = None

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

        screenshot = await page.screenshot(
            type="png",
            full_page=False,
            animations="disabled",
            caret="hide",
            mask=mask_locators if mask_locators else None,
        )
        new_tiles = await slice_into_tiles(
            screenshot,
            overlap_px=tile_overlap_px,
            tile_index_offset=tile_index,
            viewport_y_offset=y_offset,
            target_long_side_px=target_long_side_px,
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
            if bottom_hash:
                new_tiles[-1].seam_bottom_hash = bottom_hash

        for tile in new_tiles:
            if previous_tile:
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
                scroll_height = new_height
                await page.wait_for_timeout(settle_ms)
                continue
        scroll_height = new_height

        if y_offset + config.viewport_height >= scroll_height:
            break

        next_offset = min(
            y_offset + viewport_step,
            max(0, scroll_height - config.viewport_height),
        )
        if next_offset <= y_offset:
            break

        await page.evaluate(f"window.scrollTo(0, {next_offset})")
        await page.wait_for_timeout(settle_ms)
        y_offset = next_offset

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


def _seam_hash(value: int) -> str:
    import hashlib

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
    "cft": "chrome",
    "chrome-for-testing": "chrome",
}


async def _launch_browser(playwright, channel: str) -> Browser:
    normalized = _normalize_channel(channel)
    if normalized != channel:
        LOGGER.warning(
            "Playwright channel '%s' is not supported; falling back to '%s'",
            channel,
            normalized,
        )

    # Use Chrome's new headless mode (--headless=new) which is virtually undetectable
    # Combined with automation masking to evade bot detection
    launch_args = [
        "--headless=new",  # New headless mode - much harder to detect
        "--disable-blink-features=AutomationControlled",
        "--disable-dev-shm-usage",
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--window-size=1920,1080",
    ]

    LOGGER.debug("launching chromium", extra={"channel": normalized, "args": launch_args})
    return await playwright.chromium.launch(
        channel=normalized,
        headless=True,  # Keep this True, the --headless=new arg is what matters
        args=launch_args,
    )


async def _build_context(
    browser: Browser,
    config: CaptureConfig,
    *,
    profiles_root: Path | None = None,
) -> tuple[BrowserContext, Path | None]:
    # Use realistic user-agent to avoid bot detection
    # This should match the Chrome version being used
    user_agent = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0.0.0 Safari/537.36"
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


def _normalize_channel(channel: str) -> str:
    if not channel:
        return "chromium"
    lowered = channel.strip().lower()
    return _CHANNEL_ALIASES.get(lowered, lowered)


_PROFILE_SAFE_PATTERN = re.compile(r"[^A-Za-z0-9._-]+")


def _profile_storage_state_path(base: Path, profile_id: str) -> Path:
    safe = _PROFILE_SAFE_PATTERN.sub("_", profile_id.strip())
    if not safe:
        safe = "default"
    return base / safe / "storage_state.json"


def _overlap_match(previous_tile: TileSlice, current_tile: TileSlice) -> Optional[bool]:
    if not previous_tile.bottom_overlap_sha256 or not current_tile.top_overlap_sha256:
        return None
    return previous_tile.bottom_overlap_sha256 == current_tile.top_overlap_sha256


def _safe_ratio(numerator: int, denominator: int) -> float:
    if denominator == 0:
        return 0.0
    return round(numerator / denominator, 4)
