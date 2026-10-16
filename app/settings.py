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
    "DeduplicationSettings",
    "SemanticPostConfig",
    "EmbedderConfig",
    "Settings",
    "resolve_ocr_backend_defaults",
    "resolve_semantic_post_settings",
    "resolve_openai_embedder_settings",
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
    executable_path: Path | None = None
    enforce_version_pin: bool = False
    max_concurrent_browsers: int = 4


@dataclass(frozen=True, slots=True)
class OCRSettings:
    """Parameters for remote/local olmOCR orchestration."""

    server_url: str
    api_key: str | None
    model: str
    local_url: str | None
    local_autostart: bool
    local_startup_timeout_s: int
    local_healthcheck_timeout_s: int
    local_max_restarts: int
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
    profiles_root: Path


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
class DeduplicationSettings:
    """Tile overlap deduplication configuration."""

    enabled: bool
    min_overlap_lines: int
    sequence_similarity_threshold: float
    fuzzy_line_threshold: float
    max_search_window: int
    log_events: bool


@dataclass(frozen=True, slots=True)
class LoggingSettings:
    """Filesystem paths for ops logging."""

    warning_log_path: Path


@dataclass(frozen=True, slots=True)
class SemanticPostConfig:
    """Configuration for optional semantic post-processing."""

    enabled: bool = False
    endpoint: str = ""
    model: str = ""
    api_key: str = ""
    timeout_ms: int = 30000
    max_chars: int | None = None


@dataclass(frozen=True, slots=True)
class EmbedderConfig:
    """Configuration for OpenAI-compatible and local embedding backends."""

    openai_base_url: str = ""
    openai_api_key: str = ""
    openai_embedding_model: str = "text-embedding-3-small"


@dataclass(frozen=True, slots=True)
class Settings:
    """Top-level immutable configuration container."""

    env_path: str
    browser: BrowserSettings
    ocr: OCRSettings
    telemetry: TelemetrySettings
    storage: StorageSettings
    warnings: WarningSettings
    deduplication: DeduplicationSettings
    logging: LoggingSettings
    webhook_secret: str
    server_runtime: str

    # Authentication & rate-limit settings
    REQUIRE_API_KEY: bool = False
    API_KEY_HEADER: str = "X-API-Key"
    RATE_LIMIT_PER_MINUTE: int = 60
    api_base_url: str = "http://localhost:8007"
    semantic_post: SemanticPostConfig = SemanticPostConfig()
    embedders: EmbedderConfig = EmbedderConfig()

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
        backend_id, backend_mode, hardware_path, fallback_chain, provider = (
            resolve_ocr_backend_defaults(self.ocr)
        )
        return ManifestEnvironment(
            cft_version=self.browser.cft_version,
            cft_label=self.browser.cft_label,
            server_runtime=self.server_runtime,
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
            ocr_provider=provider,
            ocr_use_fp8=self.ocr.use_fp8,
            ocr_concurrency=concurrency,
            ocr_backend_id=backend_id,
            ocr_backend_mode=backend_mode,
            ocr_hardware_path=hardware_path,
            ocr_fallback_chain=fallback_chain,
        )


def load_config(env_path: str = ".env") -> DecoupleConfig:
    """Return a python-decouple config anchored to the repository .env file."""

    target = Path(env_path)
    if not target.exists():
        fallback = (
            target.with_suffix(target.suffix + ".example")
            if target.suffix
            else Path(f"{env_path}.example")
        )
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


def resolve_ocr_backend_defaults(
    ocr: OCRSettings,
) -> tuple[str, str, str, tuple[str, ...], str]:
    """Infer backend provenance defaults from OCR settings.

    Returns ``(backend_id, backend_mode, hardware_path, fallback_chain, provider)``.
    The strategy stays backward-compatible with existing olmOCR OpenAI-compatible setups
    while exposing structured fields required by contract-v2 manifests.
    """

    model_lower = ocr.model.lower()
    provider = "glm-ocr" if "glm-ocr" in model_lower else "olmocr"
    server_url = (ocr.server_url or "").strip().lower()
    local_url = (ocr.local_url or "").strip().lower()

    if local_url:
        primary_id = f"{provider}-local-openai"
        fallback_chain = [primary_id]
        if server_url:
            if "layout_parsing" in server_url or "open.bigmodel.cn" in server_url:
                fallback_id = "glm-ocr-maas"
            else:
                fallback_id = f"{provider}-remote-openai"
            if fallback_id != primary_id:
                fallback_chain.append(fallback_id)
        return primary_id, "openai-compatible", "local-auto", tuple(fallback_chain), provider

    if "layout_parsing" in server_url or "open.bigmodel.cn" in server_url:
        return "glm-ocr-maas", "maas", "remote", ("glm-ocr-maas",), "glm-ocr"

    backend_id = f"{provider}-remote-openai"
    return backend_id, "openai-compatible", "remote", (backend_id,), provider


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

    exec_raw = str(
        cfg("CFT_EXECUTABLE_PATH", default="")
        or cfg("PLAYWRIGHT_EXECUTABLE_PATH", default="")
        or ""
    ).strip()
    executable_path = Path(exec_raw) if exec_raw else None
    enforce_version_pin = _bool(cfg, "CFT_ENFORCE_PIN", default=False)
    max_concurrent_browsers = max(1, _int(cfg, "MAX_CONCURRENT_BROWSERS", default=4))

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
        executable_path=executable_path,
        enforce_version_pin=enforce_version_pin,
        max_concurrent_browsers=max_concurrent_browsers,
    )
    ocr_server = cfg("OLMOCR_SERVER", default=None) or cfg(
        "OCR_SERVER", default="https://ai2endpoints.cirrascale.ai/api"
    )
    ocr_api_key = cfg("OLMOCR_API_KEY", default=None) or cfg("OCR_API_KEY", default=None)
    ocr_model = cfg("OLMOCR_MODEL", default=None) or cfg(
        "OCR_MODEL", default="olmOCR-2-7B-1025-FP8"
    )
    ocr = OCRSettings(
        server_url=ocr_server,
        api_key=ocr_api_key,
        model=ocr_model,
        local_url=cfg("OCR_LOCAL_URL", default=None),
        local_autostart=_bool(cfg, "OCR_LOCAL_AUTOSTART", default=True),
        local_startup_timeout_s=_int(cfg, "OCR_LOCAL_STARTUP_TIMEOUT_S", default=180),
        local_healthcheck_timeout_s=_int(cfg, "OCR_LOCAL_HEALTHCHECK_TIMEOUT_S", default=5),
        local_max_restarts=_int(cfg, "OCR_LOCAL_MAX_RESTARTS", default=1),
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
    if ocr.local_startup_timeout_s < 1:
        msg = "OCR_LOCAL_STARTUP_TIMEOUT_S must be >= 1"
        raise ValueError(msg)
    if ocr.local_healthcheck_timeout_s < 1:
        msg = "OCR_LOCAL_HEALTHCHECK_TIMEOUT_S must be >= 1"
        raise ValueError(msg)
    if ocr.local_max_restarts < 0:
        msg = "OCR_LOCAL_MAX_RESTARTS must be >= 0"
        raise ValueError(msg)

    telemetry = TelemetrySettings(
        prometheus_port=_int(cfg, "PROMETHEUS_PORT", default=9000),
        htmx_sse_heartbeat_ms=_int(cfg, "HTMX_SSE_HEARTBEAT_MS", default=4000),
    )
    cache_root = Path(cfg("CACHE_ROOT", default=".cache"))
    storage = StorageSettings(
        cache_root=cache_root,
        db_path=Path(cfg("RUNS_DB_PATH", default="runs.db")),
        profiles_root=cache_root / "profiles",
    )
    warning_settings = WarningSettings(
        canvas_warning_threshold=_int(cfg, "CANVAS_WARNING_THRESHOLD", default=3),
        video_warning_threshold=_int(cfg, "VIDEO_WARNING_THRESHOLD", default=2),
        shrink_warning_threshold=_int(cfg, "SCROLL_SHRINK_WARNING_THRESHOLD", default=1),
        overlap_warning_ratio=_float(cfg, "OVERLAP_WARNING_RATIO", default=0.65),
        seam_warning_ratio=_float(cfg, "SEAM_WARNING_RATIO", default=0.9),
        seam_warning_min_pairs=_int(cfg, "SEAM_WARNING_MIN_PAIRS", default=5),
    )
    dedup_settings = DeduplicationSettings(
        enabled=_bool(cfg, "DEDUP_ENABLED", default=True),
        min_overlap_lines=_int(cfg, "DEDUP_MIN_OVERLAP_LINES", default=2),
        sequence_similarity_threshold=_float(cfg, "DEDUP_SEQUENCE_THRESHOLD", default=0.90),
        fuzzy_line_threshold=_float(cfg, "DEDUP_FUZZY_THRESHOLD", default=0.85),
        max_search_window=_int(cfg, "DEDUP_MAX_SEARCH_WINDOW", default=40),
        log_events=_bool(cfg, "DEDUP_LOG_EVENTS", default=True),
    )
    logging_settings = LoggingSettings(
        warning_log_path=Path(cfg("WARNING_LOG_PATH", default="ops/warnings.jsonl")),
    )
    webhook_secret = cfg("WEBHOOK_SECRET", default="mdwb-dev-webhook")
    server_runtime = cfg("MDWB_SERVER_IMPL", default="uvicorn").lower()
    api_base_url = str(cfg("API_BASE_URL", default="http://localhost:8007")).strip()

    semantic_post_cfg = SemanticPostConfig(
        enabled=_bool(cfg, "SEMANTIC_POST_ENABLED", default=False),
        endpoint=str(cfg("SEMANTIC_POST_ENDPOINT", default="") or "").strip(),
        model=str(cfg("SEMANTIC_POST_MODEL", default="") or "").strip(),
        api_key=str(cfg("SEMANTIC_POST_API_KEY", default="") or "").strip(),
        timeout_ms=max(1000, _int(cfg, "SEMANTIC_POST_TIMEOUT_MS", default=30000)),
        max_chars=_optional_int(cfg, "SEMANTIC_POST_MAX_CHARS"),
    )
    embedders_cfg = EmbedderConfig(
        openai_base_url=str(cfg("OPENAI_BASE_URL", default="") or "").strip(),
        openai_api_key=str(cfg("OPENAI_API_KEY", default="") or "").strip(),
        openai_embedding_model=str(
            cfg("OPENAI_EMBEDDING_MODEL", default="text-embedding-3-small")
            or "text-embedding-3-small"
        ).strip(),
    )

    # Authentication & rate-limit settings
    require_api_key = _bool(cfg, "REQUIRE_API_KEY", default=False)
    api_key_header = cfg("API_KEY_HEADER", default="X-API-Key")
    rate_limit_per_minute = max(1, _int(cfg, "RATE_LIMIT_PER_MINUTE", default=60))

    return Settings(
        env_path=env_path,
        browser=browser,
        ocr=ocr,
        telemetry=telemetry,
        storage=storage,
        warnings=warning_settings,
        deduplication=dedup_settings,
        logging=logging_settings,
        webhook_secret=webhook_secret,
        server_runtime=server_runtime,
        REQUIRE_API_KEY=require_api_key,
        API_KEY_HEADER=api_key_header,
        RATE_LIMIT_PER_MINUTE=rate_limit_per_minute,
        api_base_url=api_base_url,
        semantic_post=semantic_post_cfg,
        embedders=embedders_cfg,
    )


def resolve_semantic_post_settings(
    active_settings: Settings | None = None,
    *,
    enabled_override: bool | None = None,
    max_chars_override: int | None = None,
) -> SemanticPostConfig:
    """Resolve semantic post-processing config exclusively via python-decouple."""

    s = active_settings or settings
    cfg = load_config(s.env_path)
    enabled = (
        bool(enabled_override)
        if enabled_override is not None
        else _bool(cfg, "SEMANTIC_POST_ENABLED", default=s.semantic_post.enabled)
    )
    endpoint = str(
        cfg("SEMANTIC_POST_ENDPOINT", default=s.semantic_post.endpoint) or ""
    ).strip()
    model = str(cfg("SEMANTIC_POST_MODEL", default=s.semantic_post.model) or "").strip()
    api_key = str(
        cfg("SEMANTIC_POST_API_KEY", default=s.semantic_post.api_key) or ""
    ).strip()
    timeout_ms = max(
        1000,
        _int(cfg, "SEMANTIC_POST_TIMEOUT_MS", default=s.semantic_post.timeout_ms),
    )
    max_chars = (
        max_chars_override
        if max_chars_override is not None
        else _optional_int(cfg, "SEMANTIC_POST_MAX_CHARS") or s.semantic_post.max_chars
    )
    return SemanticPostConfig(
        enabled=enabled,
        endpoint=endpoint,
        model=model,
        api_key=api_key,
        timeout_ms=timeout_ms,
        max_chars=max_chars,
    )


def resolve_openai_embedder_settings(
    active_settings: Settings | None = None,
) -> EmbedderConfig:
    """Resolve OpenAI-compatible embedder settings exclusively via python-decouple."""

    s = active_settings or settings
    cfg = load_config(s.env_path)
    base_url = str(
        cfg("OPENAI_BASE_URL", default=s.embedders.openai_base_url) or ""
    ).strip()
    api_key = str(cfg("OPENAI_API_KEY", default=s.embedders.openai_api_key) or "").strip()
    model = str(
        cfg("OPENAI_EMBEDDING_MODEL", default=s.embedders.openai_embedding_model)
        or "text-embedding-3-small"
    ).strip()
    return EmbedderConfig(
        openai_base_url=base_url,
        openai_api_key=api_key,
        openai_embedding_model=model,
    )


def effective_require_api_key(active_settings: Settings | None = None) -> bool:
    """Resolve REQUIRE_API_KEY honoring both the Settings object and python-decouple overrides."""
    s = active_settings or settings
    if s.REQUIRE_API_KEY:
        return True
    try:
        cfg = load_config(s.env_path)
        return _bool(cfg, "REQUIRE_API_KEY", default=s.REQUIRE_API_KEY)
    except Exception:
        return bool(s.REQUIRE_API_KEY)


def effective_api_key_header(active_settings: Settings | None = None) -> str:
    """Resolve API_KEY_HEADER honoring both the Settings object and python-decouple overrides."""
    s = active_settings or settings
    if s.API_KEY_HEADER and s.API_KEY_HEADER != "X-API-Key":
        return s.API_KEY_HEADER
    try:
        cfg = load_config(s.env_path)
        header = str(cfg("API_KEY_HEADER", default=s.API_KEY_HEADER or "X-API-Key")).strip()
        return header or "X-API-Key"
    except Exception:
        return s.API_KEY_HEADER or "X-API-Key"


def effective_rate_limit_per_minute(active_settings: Settings | None = None) -> int:
    """Resolve RATE_LIMIT_PER_MINUTE honoring both the Settings object and python-decouple overrides."""
    s = active_settings or settings
    default_rpm = getattr(s, "RATE_LIMIT_PER_MINUTE", 60)
    if default_rpm != 60:
        return max(1, int(default_rpm))
    try:
        cfg = load_config(s.env_path)
        return max(1, _int(cfg, "RATE_LIMIT_PER_MINUTE", default=int(default_rpm)))
    except Exception:
        return max(1, int(default_rpm))


# Statically importable settings singleton for modules that prefer constants over DI.
settings: Final[Settings] = get_settings()
