"""Playwright-based capture routines (viewport sweeps + metadata logging)."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path
from typing import Any, AsyncGenerator, Callable, List, Optional, Sequence

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from app.blocklist import BlocklistConfig, apply_blocklist, cached_blocklist
from app.capture_warnings import (
    CaptureWarningEntry,
    build_sweep_warning,
    collect_capture_warnings,
)
from app.schemas import validate_profile_id
from app.settings import WarningSettings, get_settings
from app.tiler import TileSlice, align_cross_sweep_overlap, slice_into_tiles, validate_tiles

LOGGER = logging.getLogger(__name__)

_DEFAULT_ASYNC_PLAYWRIGHT = async_playwright
_VERSION_NUM_RE = re.compile(r"(\d+\.\d+\.\d+(?:\.\d+)?)")
_KNOWN_TRANSPORTS = frozenset({"cdp", "bidi", "webdriverbidi"})


class BrowserPinMismatchError(RuntimeError, ValueError):
    """Raised when the launched browser build or executable does not match a required pin."""


class UnsupportedTransportError(RuntimeError, ValueError):
    """Raised when a requested browser transport protocol is unsupported or unavailable."""


def _emit_stage(config: CaptureConfig, stage: str, info: dict[str, Any] | None = None) -> None:
    callback = getattr(config, "on_stage", None)
    if callable(callback):
        try:
            callback(stage, info)
        except Exception:
            pass


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
    use_browser_pool: bool = True
    on_stage: Callable[[str, dict[str, Any] | None], None] | None = field(
        default=None, repr=False, compare=False
    )


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


def resolve_browser_transport(
    requested: str | None,
    *,
    supported: Sequence[str] = ("cdp",),
) -> str:
    """Resolve and validate browser transport from a single name or fallback preference list.

    Never silently reports an unexercised transport: if the requested transport(s)
    are unknown or none is supported by the active browser launcher, raises
    :class:`UnsupportedTransportError`.
    """
    raw = (requested or "cdp").strip()
    if not raw:
        raise UnsupportedTransportError("browser_transport cannot be empty")
    candidates: list[str] = []
    for part in raw.split(","):
        token = part.strip().lower()
        if not token:
            continue
        if token == "webdriverbidi":
            token = "bidi"
        if token not in _KNOWN_TRANSPORTS:
            raise UnsupportedTransportError(
                f"Unsupported browser_transport {part.strip()!r}; supported protocols: cdp, bidi"
            )
        if token not in candidates:
            candidates.append(token)
    if not candidates:
        raise UnsupportedTransportError(f"Unsupported browser_transport {requested!r}")

    supported_norm = {
        ("bidi" if s.strip().lower() == "webdriverbidi" else s.strip().lower())
        for s in supported
        if s and s.strip()
    }
    for idx, candidate in enumerate(candidates):
        if candidate in supported_norm:
            if idx > 0:
                LOGGER.warning(
                    "Browser transport %r unavailable; falling back to %r from preference list %r",
                    candidates[0],
                    candidate,
                    raw,
                )
            return candidate

    raise UnsupportedTransportError(
        f"Requested browser_transport {raw!r} is not supported by the active Playwright launcher "
        f"(supported: {sorted(supported_norm)}). Specify 'cdp' or a fallback list such as 'bidi,cdp'."
    )


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
    """Build a realistic User-Agent string matching the actual launched Chrome build."""
    match = _VERSION_NUM_RE.search(str(browser_version or ""))
    version_token = match.group(1) if match else "130.0.0.0"
    return (
        "Mozilla/5.0 (X11; Linux x86_64) "
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


class BrowserPool:
    """Bounded browser/context pool enforcing MAX_CONCURRENT_BROWSERS and crash recycling."""

    def __init__(self, max_concurrent: int = 4) -> None:
        self._max_concurrent = max(1, int(max_concurrent))
        self._loop_locks: dict[int, asyncio.Lock] = {}
        self._loop_semaphores: dict[int, tuple[int, asyncio.Semaphore]] = {}
        self._entries: dict[tuple[int, str, str, int, int], tuple[Any, Browser]] = {}
        self._launch_count = 0
        self._recycle_count = 0

    @property
    def max_concurrent(self) -> int:
        return self._max_concurrent

    @property
    def launch_count(self) -> int:
        return self._launch_count

    @property
    def recycle_count(self) -> int:
        return self._recycle_count

    def configure(self, max_concurrent: int) -> None:
        self._max_concurrent = max(1, int(max_concurrent))

    def _get_lock(self, loop_id: int) -> asyncio.Lock:
        lock = self._loop_locks.get(loop_id)
        if lock is None:
            lock = asyncio.Lock()
            self._loop_locks[loop_id] = lock
        return lock

    def _get_semaphore(self, loop_id: int, limit: int) -> asyncio.Semaphore:
        target = max(1, int(limit))
        existing = self._loop_semaphores.get(loop_id)
        if existing is None or existing[0] != target:
            sem = asyncio.Semaphore(target)
            self._loop_semaphores[loop_id] = (target, sem)
            return sem
        return existing[1]

    @staticmethod
    def _is_browser_healthy(browser: Browser | None) -> bool:
        if browser is None:
            return False
        is_connected = getattr(browser, "is_connected", None)
        if callable(is_connected):
            try:
                return bool(is_connected())
            except Exception:
                return False
        return True

    async def _discard_entry(self, key: tuple[int, str, str, int, int]) -> None:
        entry = self._entries.pop(key, None)
        if entry is None:
            return
        pw_instance, browser = entry
        try:
            await asyncio.shield(browser.close())
        except BaseException:
            pass
        stop_fn = getattr(pw_instance, "stop", None)
        if callable(stop_fn):
            try:
                await asyncio.shield(stop_fn())
            except BaseException:
                pass

    @asynccontextmanager
    async def acquire_browser(
        self,
        *,
        channel: str,
        executable_path: Path | str | None = None,
        viewport_width: int = 1280,
        viewport_height: int = 2000,
        max_concurrent: int | None = None,
    ) -> AsyncGenerator[Browser, None]:
        loop = asyncio.get_running_loop()
        loop_id = id(loop)
        limit = max(1, int(max_concurrent or self._max_concurrent))
        sem = self._get_semaphore(loop_id, limit)
        lock = self._get_lock(loop_id)
        key = (
            loop_id,
            _normalize_channel(channel),
            str(executable_path or ""),
            int(viewport_width),
            int(viewport_height),
        )

        async with sem:
            async with lock:
                existing = self._entries.get(key)
                if existing is not None:
                    _, cached_browser = existing
                    if not self._is_browser_healthy(cached_browser):
                        self._recycle_count += 1
                        await self._discard_entry(key)
                        existing = None
                if existing is None:
                    pw_cm = async_playwright()
                    pw_instance = await pw_cm.start()
                    try:
                        try:
                            browser = await _launch_browser(
                                pw_instance,
                                channel,
                                viewport_width=viewport_width,
                                viewport_height=viewport_height,
                                executable_path=executable_path,
                            )
                        except TypeError:
                            browser = await _launch_browser(pw_instance, channel)
                    except BaseException:
                        stop_fn = getattr(pw_instance, "stop", None)
                        if callable(stop_fn):
                            try:
                                await stop_fn()
                            except BaseException:
                                pass
                        raise
                    self._launch_count += 1
                    self._entries[key] = (pw_instance, browser)
                else:
                    browser = existing[1]

            try:
                yield browser
            finally:
                if not self._is_browser_healthy(browser):
                    async with lock:
                        self._recycle_count += 1
                        await self._discard_entry(key)

    async def close_all(self) -> None:
        for key in list(self._entries.keys()):
            await self._discard_entry(key)


_BROWSER_POOL = BrowserPool()


def get_browser_pool() -> BrowserPool:
    """Return the process-wide bounded BrowserPool instance."""
    return _BROWSER_POOL


async def capture_tiles(config: CaptureConfig) -> CaptureResult:
    """Run a deterministic viewport sweep and return OCR-ready tiles."""

    settings = get_settings()
    start = time.perf_counter()

    playwright_ver = _validate_playwright_version()
    requested_transport = config.browser_transport or settings.browser.browser_transport
    effective_transport = resolve_browser_transport(requested_transport)

    configured_cft_version = config.cft_version or settings.browser.cft_version
    configured_cft_label = config.cft_label or settings.browser.cft_label
    enforce_pin = (
        config.enforce_version_pin
        if config.enforce_version_pin is not None
        else getattr(settings.browser, "enforce_version_pin", False)
    )
    executable_path = (
        config.executable_path
        if config.executable_path is not None
        else getattr(settings.browser, "executable_path", None)
    )
    max_concurrent = getattr(settings.browser, "max_concurrent_browsers", 4)

    blocklist_cfg = cached_blocklist(str(settings.browser.blocklist_path))
    warning_cfg = settings.warnings

    _emit_stage(config, "BROWSER_STARTING", {"done": 0, "total": 0})

    extra_artifacts: dict[str, Any] = {"abandoned_tiles": [], "dom_overlay": []}

    async def _execute_with_browser(browser: Browser) -> tuple[
        List[TileSlice],
        SweepStats,
        str,
        dict[str, int],
        list[CaptureWarningEntry],
        bytes | None,
        list[str],
        list[dict[str, object]],
        str,
    ]:
        raw_browser_ver = getattr(browser, "version", None)
        observed_ver_str = str(raw_browser_ver) if isinstance(raw_browser_ver, str) else ""
        effective_cft_version = verify_browser_build(
            observed_version=observed_ver_str,
            configured_version=configured_cft_version,
            enforce_pin=bool(enforce_pin),
        )
        context: BrowserContext | None = None
        storage_state_path: Path | None = None
        extra_artifacts["abandoned_tiles"] = []
        extra_artifacts["dom_overlay"] = []
        try:
            context, storage_state_path = await _build_context(
                browser,
                config,
                profiles_root=settings.storage.profiles_root,
            )
            try:
                (
                    tiles_out,
                    sweep_stats_out,
                    user_agent_out,
                    blocklist_hits_out,
                    warnings_out,
                    dom_snapshot_out,
                    validation_failures_out,
                    seam_markers_out,
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
            except TypeError:
                (
                    tiles_out,
                    sweep_stats_out,
                    user_agent_out,
                    blocklist_hits_out,
                    warnings_out,
                    dom_snapshot_out,
                    validation_failures_out,
                    seam_markers_out,
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
            return (
                tiles_out,
                sweep_stats_out,
                user_agent_out,
                blocklist_hits_out,
                warnings_out,
                dom_snapshot_out,
                validation_failures_out,
                seam_markers_out,
                effective_cft_version,
            )
        finally:
            if context is not None and storage_state_path is not None:
                try:
                    storage_state_path.parent.mkdir(parents=True, exist_ok=True)
                    await asyncio.shield(context.storage_state(path=str(storage_state_path)))
                except BaseException:
                    pass
            if context is not None:
                try:
                    await asyncio.shield(context.close())
                except BaseException:
                    pass

    use_pool = bool(config.use_browser_pool) and (async_playwright is _DEFAULT_ASYNC_PLAYWRIGHT)
    if use_pool:
        try:
            async with _BROWSER_POOL.acquire_browser(
                channel=settings.browser.playwright_channel,
                executable_path=executable_path,
                viewport_width=config.viewport_width,
                viewport_height=config.viewport_height,
                max_concurrent=max_concurrent,
            ) as browser:
                (
                    tiles,
                    sweep_stats,
                    user_agent,
                    blocklist_hits,
                    warnings,
                    dom_snapshot,
                    validation_failures,
                    seam_markers,
                    effective_cft_version,
                ) = await _execute_with_browser(browser)
        except (BrowserPinMismatchError, UnsupportedTransportError):
            raise
        except Exception:
            # If a pooled browser crashed mid-context creation, recycle and retry once cleanly
            await _BROWSER_POOL.close_all()
            async with _BROWSER_POOL.acquire_browser(
                channel=settings.browser.playwright_channel,
                executable_path=executable_path,
                viewport_width=config.viewport_width,
                viewport_height=config.viewport_height,
                max_concurrent=max_concurrent,
            ) as browser:
                (
                    tiles,
                    sweep_stats,
                    user_agent,
                    blocklist_hits,
                    warnings,
                    dom_snapshot,
                    validation_failures,
                    seam_markers,
                    effective_cft_version,
                ) = await _execute_with_browser(browser)
    else:
        async with async_playwright() as playwright:
            try:
                browser = await _launch_browser(
                    playwright,
                    settings.browser.playwright_channel,
                    viewport_width=config.viewport_width,
                    viewport_height=config.viewport_height,
                    executable_path=executable_path,
                )
            except TypeError:
                browser = await _launch_browser(playwright, settings.browser.playwright_channel)
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
                    effective_cft_version,
                ) = await _execute_with_browser(browser)
            finally:
                try:
                    await asyncio.shield(browser.close())
                except BaseException:
                    pass

    capture_ms = int((time.perf_counter() - start) * 1000)
    effective_style_hash = compute_effective_style_hash(
        settings_browser=settings.browser,
        config=config,
        blocklist_version=blocklist_cfg.version,
    )

    manifest_payload = CaptureManifest(
        url=config.url,
        cft_label=configured_cft_label,
        cft_version=effective_cft_version,
        playwright_channel=_normalize_channel(settings.browser.playwright_channel),
        playwright_version=playwright_ver,
        browser_transport=effective_transport,
        screenshot_style_hash=effective_style_hash,
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
        selected_ocr_policy=config.ocr_policy or config.ocr_model or settings.ocr.model,
        ocr_model=config.ocr_model or settings.ocr.model,
        ocr_use_fp8=config.ocr_use_fp8 if config.ocr_use_fp8 is not None else settings.ocr.use_fp8,
        ocr_max_tiles_in_flight=config.ocr_max_tiles_in_flight,
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

    # Use 'domcontentloaded' for fastest reliable page load
    # This fires when HTML is parsed, before all resources load
    # Works better with Cloudflare challenge pages that may have delayed resources
    _emit_stage(config, "NAVIGATING", {"done": 0, "total": 0, "url": config.url})
    await page.goto(config.url, wait_until="domcontentloaded", timeout=30000)
    blocklist_hits = await apply_blocklist(page, url=config.url, config=blocklist_config)
    await _ensure_watermark_injected(page)
    warning_entries: list[CaptureWarningEntry] = await collect_capture_warnings(
        page, warning_settings
    )
    _emit_stage(config, "SCROLLING", {"done": 0, "total": 0})
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

        # Harvest viewport-synchronized DOM bounding boxes before screenshot
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

        _emit_stage(
            config,
            "CAPTURING",
            {"done": 0, "total": len(tiles), "sweep": sweep_count, "y_offset": y_offset},
        )
        screenshot = await page.screenshot(
            type="png",
            full_page=False,
            animations="disabled",
            caret="hide",
            mask=mask_locators if mask_locators else None,
        )
        try:
            new_tiles = await slice_into_tiles(
                screenshot,
                overlap_px=tile_overlap_px,
                tile_index_offset=tile_index,
                viewport_y_offset=y_offset,
                target_long_side_px=target_long_side_px,
                device_scale_factor=config.device_scale_factor,
            )
        except TypeError:
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
                if previous_tile is not None:
                    # Align previous sweep's bottom seam hash to the exact shared seam coordinate y_offset
                    previous_tile.seam_bottom_hash = top_hash
            for intra_idx in range(len(new_tiles) - 1):
                intra_seam = _seam_hash(new_tiles[intra_idx + 1].css_y_offset)
                new_tiles[intra_idx].seam_bottom_hash = intra_seam
                new_tiles[intra_idx + 1].seam_top_hash = intra_seam
            if bottom_hash:
                new_tiles[-1].seam_bottom_hash = bottom_hash

        for tile in new_tiles:
            if previous_tile:
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
        _emit_stage(
            config,
            "TILING",
            {"done": 0, "total": len(tiles), "sweep": sweep_count, "tiles_total": len(tiles)},
        )

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
        # Re-read scrollHeight in case lazy-loaded content expanded the document after scrolling
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


async def _ensure_watermark_injected(page: Page) -> None:
    """Install non-visual IntersectionObserver/MutationObserver tracking and strip any legacy watermark DOM."""
    await page.evaluate(
        """
(() => {
  // Remove any legacy visual watermark elements so they never pollute screenshot pixels
  for (const id of ['mdwb-watermark-top', 'mdwb-watermark-bottom', '__mdwb_capture_watermark']) {
    const existing = document.getElementById(id);
    if (existing && existing.parentNode) {
      existing.parentNode.removeChild(existing);
    }
  }
  window.__mdwbWatermarkReady = true;
  window.__mdwbSeamState = { topHash: null, bottomHash: null, showTop: false, showBottom: false };

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
  } catch (_err) {
    // Ignore observer errors on restricted pages
  }
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
    """Record seam hashes in window metadata without injecting visible bars into screenshot pixels."""
    await page.evaluate(
        """
({ topHash, bottomHash, showTop, showBottom }) => {
  window.__mdwbSeamState = { topHash, bottomHash, showTop, showBottom };
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


async def _launch_browser(
    playwright,
    channel: str,
    *,
    viewport_width: int = 1280,
    viewport_height: int = 2000,
    executable_path: Path | str | None = None,
) -> Browser:
    normalized = _normalize_channel(channel)
    if normalized != channel:
        LOGGER.debug(
            "Mapped Playwright channel alias '%s' to '%s'",
            channel,
            normalized,
        )

    launch_args = [
        "--headless=new",
        "--disable-blink-features=AutomationControlled",
        "--disable-dev-shm-usage",
        "--no-sandbox",
        "--disable-setuid-sandbox",
        f"--window-size={max(1, int(viewport_width))},{max(1, int(viewport_height))}",
    ]

    launch_kwargs: dict[str, Any] = {
        "headless": True,
        "args": launch_args,
    }
    if executable_path is not None and str(executable_path).strip():
        exec_path = Path(str(executable_path).strip())
        if not exec_path.exists():
            raise BrowserPinMismatchError(
                f"Configured browser executable_path does not exist: {exec_path}"
            )
        launch_kwargs["executable_path"] = str(exec_path)
    else:
        launch_kwargs["channel"] = normalized

    LOGGER.debug("launching chromium", extra={"channel": normalized, "args": launch_args})
    return await playwright.chromium.launch(**launch_kwargs)


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
        storage_state_path = _profile_storage_state_path(
            profiles_root, config.profile_id, owner=config.owner
        )
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


def _profile_storage_state_path(
    base: Path,
    profile_id: str,
    *,
    owner: str | None = None,
) -> Path:
    safe_profile = validate_profile_id(profile_id)
    base_resolved = base.resolve()
    if owner is not None and str(owner).strip():
        owner_clean = str(owner).strip()
        try:
            owner_segment = validate_profile_id(owner_clean)
        except ValueError as exc:
            raise ValueError(f"Invalid profile owner {owner!r}") from exc
        relative_dir = Path("owners") / owner_segment / safe_profile
    else:
        relative_dir = Path(safe_profile)
    candidate = base / relative_dir / "storage_state.json"
    candidate_resolved = (base_resolved / relative_dir / "storage_state.json").resolve()
    if not candidate_resolved.is_relative_to(base_resolved):
        raise ValueError(f"Profile path for {profile_id!r} escapes profiles_root")
    return candidate


def _overlap_match(previous_tile: TileSlice, current_tile: TileSlice) -> Optional[bool]:
    if not previous_tile.bottom_overlap_sha256 or not current_tile.top_overlap_sha256:
        return None
    return previous_tile.bottom_overlap_sha256 == current_tile.top_overlap_sha256


def _safe_ratio(numerator: int, denominator: int) -> float:
    if denominator == 0:
        return 0.0
    return round(numerator / denominator, 4)
