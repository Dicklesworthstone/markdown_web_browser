"""Typed configuration objects backed by python-decouple settings."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Final

from decouple import Config as DecoupleConfig, RepositoryEnv

from app.schemas import ConcurrencyWindow, ManifestEnvironment, ViewportSettings

LOGGER = logging.getLogger(__name__)

__all__ = [
    "BrowserSettings",
    "OCRSettings",
    "TelemetrySettings",
    "StorageSettings",
    "WarningSettings",
    "Settings",
    "load_config",
    "get_settings",
]


@dataclass(frozen=True, slots=True)
class BrowserSettings:
    """Capture/browser-related knobs that surface in manifests."""

    cft_version: str
    cft_label: str
    playwright_channel: str
    browser_transport: str
    viewport_width: int
    viewport_height: int
    device_scale_factor: int
    color_scheme: str
    long_side_px: int
    viewport_overlap_px: int
    tile_overlap_px: int
    scroll_settle_ms: int
    max_viewport_sweeps: int
    shrink_retry_limit: int
    screenshot_mask_selectors: tuple[str, ...]
    screenshot_style_hash: str
    blocklist_path: Path


@dataclass(frozen=True, slots=True)
class OCRSettings:
    """Parameters for remote/local olmOCR orchestration."""

    server_url: str
    api_key: str | None
    model: str
    local_url: str | None
    use_fp8: bool
    min_concurrency: int
    max_concurrency: int
    max_batch_tiles: int
    max_batch_bytes: int
    daily_quota_tiles: int | None


@dataclass(frozen=True, slots=True)
class TelemetrySettings:
    """Ports/intervals for Prometheus + HTMX SSE plumbing."""

    prometheus_port: int
    htmx_sse_heartbeat_ms: int


@dataclass(frozen=True, slots=True)
class StorageSettings:
    """Filesystem + SQLite layout for job artifacts."""

    cache_root: Path
    db_path: Path


@dataclass(frozen=True, slots=True)
class WarningSettings:
    """Thresholds that control capture warning heuristics."""

    canvas_warning_threshold: int
    video_warning_threshold: int
    shrink_warning_threshold: int
    overlap_warning_ratio: float
    seam_warning_ratio: float
    seam_warning_min_pairs: int


@dataclass(frozen=True, slots=True)
class LoggingSettings:
    """Filesystem paths for ops logging."""

    warning_log_path: Path


@dataclass(frozen=True, slots=True)
class Settings:
    """Top-level immutable configuration container."""

    env_path: str
    browser: BrowserSettings
    ocr: OCRSettings
    telemetry: TelemetrySettings
    storage: StorageSettings
    warnings: WarningSettings
    logging: LoggingSettings
    webhook_secret: str

    def manifest_environment(self, *, playwright_version: str | None = None) -> ManifestEnvironment:
        """Return the manifest metadata block used across captures."""

        viewport = ViewportSettings(
            width=self.browser.viewport_width,
            height=self.browser.viewport_height,
            device_scale_factor=self.browser.device_scale_factor,
            color_scheme=self.browser.color_scheme,
        )
        concurrency = ConcurrencyWindow(
            min=self.ocr.min_concurrency,
            max=self.ocr.max_concurrency,
        )
        return ManifestEnvironment(
            cft_version=self.browser.cft_version,
            cft_label=self.browser.cft_label,
            playwright_channel=self.browser.playwright_channel,
            playwright_version=playwright_version,
            browser_transport=self.browser.browser_transport,
            viewport=viewport,
            viewport_overlap_px=self.browser.viewport_overlap_px,
            tile_overlap_px=self.browser.tile_overlap_px,
            scroll_settle_ms=self.browser.scroll_settle_ms,
            max_viewport_sweeps=self.browser.max_viewport_sweeps,
            screenshot_style_hash=self.browser.screenshot_style_hash,
            screenshot_mask_selectors=self.browser.screenshot_mask_selectors,
            ocr_model=self.ocr.model,
            ocr_use_fp8=self.ocr.use_fp8,
            ocr_concurrency=concurrency,
        )


def load_config(env_path: str = ".env") -> DecoupleConfig:
    """Return a python-decouple config anchored to the repository .env file."""

    target = Path(env_path)
    if not target.exists():
        fallback = target.with_suffix(target.suffix + ".example") if target.suffix else Path(f"{env_path}.example")
        if fallback.exists():
            LOGGER.warning("%s missing; falling back to %s", env_path, fallback)
            target = fallback
    return DecoupleConfig(RepositoryEnv(str(target)))


def _int(cfg: DecoupleConfig, key: str, *, default: int) -> int:
    return cfg(key, cast=int, default=default)


def _float(cfg: DecoupleConfig, key: str, *, default: float) -> float:
    return cfg(key, cast=float, default=default)


def _bool(cfg: DecoupleConfig, key: str, *, default: bool) -> bool:
    return cfg(key, cast=bool, default=default)


def _csv_tuple(cfg: DecoupleConfig, key: str) -> tuple[str, ...]:
    raw = cfg(key, default="")
    if not raw:
        return tuple()
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _optional_int(cfg: DecoupleConfig, key: str) -> int | None:
    value = cfg(key, default=None)
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:  # pragma: no cover - defensive
        raise ValueError(f"{key} must be an integer") from exc


def _derive_screenshot_hash(
    *,
    explicit: str,
    viewport_width: int,
    viewport_height: int,
    device_scale_factor: int,
    color_scheme: str,
    long_side_px: int,
    viewport_overlap_px: int,
    tile_overlap_px: int,
    screenshot_mask_selectors: tuple[str, ...],
) -> str:
    if explicit:
        return explicit

    import hashlib
    import json

    payload = {
        "viewport": {
            "width": viewport_width,
            "height": viewport_height,
            "device_scale_factor": device_scale_factor,
            "color_scheme": color_scheme,
        },
        "long_side_px": long_side_px,
        "overlap": {
            "viewport_px": viewport_overlap_px,
            "tile_px": tile_overlap_px,
        },
        "mask_selectors": screenshot_mask_selectors,
    }
    digest = hashlib.sha1(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
    return digest[:8]


@lru_cache(maxsize=1)
def get_settings(env_path: str = ".env") -> Settings:
    """Load and memoize structured settings for the current process."""

    cfg = load_config(env_path)

    viewport_width = _int(cfg, "CAPTURE_VIEWPORT_WIDTH", default=1280)
    viewport_height = _int(cfg, "CAPTURE_VIEWPORT_HEIGHT", default=2000)
    device_scale_factor = _int(cfg, "CAPTURE_DEVICE_SCALE_FACTOR", default=2)
    color_scheme = cfg("CAPTURE_COLOR_SCHEME", default="light")
    viewport_overlap_px = _int(cfg, "VIEWPORT_OVERLAP_PX", default=120)
    tile_overlap_px = _int(cfg, "TILE_OVERLAP_PX", default=120)
    scroll_settle_ms = _int(cfg, "SCROLL_SETTLE_MS", default=350)
    max_viewport_sweeps = _int(cfg, "MAX_VIEWPORT_SWEEPS", default=200)
    long_side_px = _int(cfg, "CAPTURE_LONG_SIDE_PX", default=1288)
    shrink_retry_limit = _int(cfg, "SCROLL_SHRINK_RETRIES", default=2)
    blocklist_path = Path(cfg("BLOCKLIST_PATH", default="config/blocklist.json"))
    mask_selectors = _csv_tuple(cfg, "SCREENSHOT_MASK_SELECTORS")
    screenshot_hash = _derive_screenshot_hash(
        explicit=cfg("SCREENSHOT_STYLE_HASH", default=""),
        viewport_width=viewport_width,
        viewport_height=viewport_height,
        device_scale_factor=device_scale_factor,
        color_scheme=color_scheme,
        long_side_px=long_side_px,
        viewport_overlap_px=viewport_overlap_px,
        tile_overlap_px=tile_overlap_px,
        screenshot_mask_selectors=mask_selectors,
    )

    browser = BrowserSettings(
        cft_version=cfg("CFT_VERSION", default="chrome-130.0.6723.69"),
        cft_label=cfg("CFT_LABEL", default="Stable-1"),
        playwright_channel=cfg("PLAYWRIGHT_CHANNEL", default="cft"),
        browser_transport=cfg("PLAYWRIGHT_TRANSPORT", default="cdp"),
        viewport_width=viewport_width,
        viewport_height=viewport_height,
        device_scale_factor=device_scale_factor,
        color_scheme=color_scheme,
        long_side_px=long_side_px,
        viewport_overlap_px=viewport_overlap_px,
        tile_overlap_px=tile_overlap_px,
        scroll_settle_ms=scroll_settle_ms,
        max_viewport_sweeps=max_viewport_sweeps,
        shrink_retry_limit=shrink_retry_limit,
        screenshot_mask_selectors=mask_selectors,
        screenshot_style_hash=screenshot_hash,
        blocklist_path=blocklist_path,
    )
    ocr = OCRSettings(
        server_url=cfg("OLMOCR_SERVER", default="https://ai2endpoints.cirrascale.ai/api"),
        api_key=cfg("OLMOCR_API_KEY", default=None),
        model=cfg("OLMOCR_MODEL", default="olmOCR-2-7B-1025-FP8"),
        local_url=cfg("OCR_LOCAL_URL", default=None),
        use_fp8=_bool(cfg, "OCR_USE_FP8", default=True),
        min_concurrency=_int(cfg, "OCR_MIN_CONCURRENCY", default=2),
        max_concurrency=_int(cfg, "OCR_MAX_CONCURRENCY", default=8),
        max_batch_tiles=_int(cfg, "OCR_MAX_BATCH_TILES", default=3),
        max_batch_bytes=_int(cfg, "OCR_MAX_BATCH_BYTES", default=25_000_000),
        daily_quota_tiles=_optional_int(cfg, "OCR_DAILY_QUOTA_TILES"),
    )
    if ocr.max_concurrency < ocr.min_concurrency:
        msg = "OCR_MAX_CONCURRENCY must be >= OCR_MIN_CONCURRENCY"
        raise ValueError(msg)

    telemetry = TelemetrySettings(
        prometheus_port=_int(cfg, "PROMETHEUS_PORT", default=9000),
        htmx_sse_heartbeat_ms=_int(cfg, "HTMX_SSE_HEARTBEAT_MS", default=4000),
    )
    storage = StorageSettings(
        cache_root=Path(cfg("CACHE_ROOT", default=".cache")),
        db_path=Path(cfg("RUNS_DB_PATH", default="runs.db")),
    )
    warning_settings = WarningSettings(
        canvas_warning_threshold=_int(cfg, "CANVAS_WARNING_THRESHOLD", default=3),
        video_warning_threshold=_int(cfg, "VIDEO_WARNING_THRESHOLD", default=2),
        shrink_warning_threshold=_int(cfg, "SCROLL_SHRINK_WARNING_THRESHOLD", default=1),
        overlap_warning_ratio=_float(cfg, "OVERLAP_WARNING_RATIO", default=0.65),
        seam_warning_ratio=_float(cfg, "SEAM_WARNING_RATIO", default=0.9),
        seam_warning_min_pairs=_int(cfg, "SEAM_WARNING_MIN_PAIRS", default=5),
    )
    logging_settings = LoggingSettings(
        warning_log_path=Path(cfg("WARNING_LOG_PATH", default="ops/warnings.jsonl")),
    )
    webhook_secret = cfg("WEBHOOK_SECRET", default="mdwb-dev-webhook")

    return Settings(
        env_path=env_path,
        browser=browser,
        ocr=ocr,
        telemetry=telemetry,
        storage=storage,
        warnings=warning_settings,
        logging=logging_settings,
        webhook_secret=webhook_secret,
    )


# Statically importable settings singleton for modules that prefer constants over DI.
settings: Final[Settings] = get_settings()
