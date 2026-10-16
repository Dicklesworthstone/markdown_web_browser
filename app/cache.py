"""Content-addressed caching utilities for deterministic captures."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from app.capture import CaptureConfig


def compute_cache_key(
    url: str,
    viewport_width: int,
    viewport_height: int,
    device_scale_factor: int,
    color_scheme: str,
    long_side_px: int,
    viewport_overlap_px: int,
    tile_overlap_px: int,
    scroll_settle_ms: int,
    screenshot_style_hash: str,
    mask_selectors: list[str] | tuple[str, ...],
    blocklist_selectors: list[str] | tuple[str, ...],
    ocr_model: str,
    ocr_use_fp8: bool,
) -> str:
    """Compute deterministic cache key from capture parameters.

    The cache key ensures that identical capture configurations produce
    the same key, enabling deduplication and cache reuse.

    Args:
        url: Target URL
        viewport_width: Browser viewport width in pixels
        viewport_height: Browser viewport height in pixels
        device_scale_factor: DPR (1 or 2)
        color_scheme: "light" or "dark"
        long_side_px: Max tile dimension (e.g., 1288)
        viewport_overlap_px: Overlap between viewport sweeps
        tile_overlap_px: Overlap between tiles
        scroll_settle_ms: Wait time after scrolling
        screenshot_style_hash: Hash of CSS/blocklist affecting screenshots
        mask_selectors: CSS selectors to mask in screenshots
        blocklist_selectors: CSS selectors to hide before capture
        ocr_model: OCR model identifier
        ocr_use_fp8: Whether FP8 quantization is used

    Returns:
        Hex-encoded SHA256 hash (64 characters)
    """
    # Normalize URL (remove trailing slash, lowercase scheme/host)
    from urllib.parse import urlparse, urlunparse

    parsed = urlparse(url)
    normalized_url = urlunparse(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path.rstrip("/") or "/",
            parsed.params,
            parsed.query,
            "",  # Remove fragment
        )
    )

    # Create deterministic representation
    cache_data = {
        "url": normalized_url,
        "viewport": {
            "width": viewport_width,
            "height": viewport_height,
            "dpr": device_scale_factor,
            "color_scheme": color_scheme,
        },
        "tiling": {
            "long_side_px": long_side_px,
            "viewport_overlap_px": viewport_overlap_px,
            "tile_overlap_px": tile_overlap_px,
        },
        "scroll_settle_ms": scroll_settle_ms,
        "screenshot_style_hash": screenshot_style_hash,
        "mask_selectors": sorted(mask_selectors),  # Sorted for determinism
        "blocklist_selectors": sorted(blocklist_selectors),
        "ocr": {
            "model": ocr_model,
            "use_fp8": ocr_use_fp8,
        },
    }

    # Serialize to JSON with sorted keys
    cache_json = json.dumps(cache_data, sort_keys=True)

    # Compute SHA256 hash
    return hashlib.sha256(cache_json.encode()).hexdigest()


def compute_cache_key_from_config(config: CaptureConfig, settings: Any) -> str:
    """Compute cache key from CaptureConfig and Settings objects.

    This is a convenience wrapper that delegates to the canonical runtime
    cache key builder in ``app.jobs`` when a full Settings object is provided.
    """
    try:
        from app.jobs import _build_cache_key

        return _build_cache_key(config=config, settings=settings)
    except Exception:
        from app.blocklist import cached_blocklist
        from app.capture import compute_effective_style_hash

        blocklist_selectors: tuple[str, ...] = ()
        blocklist_version = ""
        blocklist_path = getattr(settings.browser, "blocklist_path", None)
        if blocklist_path:
            try:
                bl = cached_blocklist(str(blocklist_path))
                blocklist_version = str(bl.version or "")
                blocklist_selectors = tuple(bl.selectors_for_url(config.url))
            except Exception:
                pass
        effective_style_hash = compute_effective_style_hash(
            settings_browser=settings.browser,
            config=config,
            blocklist_version=blocklist_version,
        )
        return compute_cache_key(
            url=config.url,
            viewport_width=config.viewport_width,
            viewport_height=config.viewport_height,
            device_scale_factor=config.device_scale_factor,
            color_scheme=config.color_scheme,
            long_side_px=config.long_side_px or settings.browser.long_side_px,
            viewport_overlap_px=settings.browser.viewport_overlap_px,
            tile_overlap_px=settings.browser.tile_overlap_px,
            scroll_settle_ms=settings.browser.scroll_settle_ms,
            screenshot_style_hash=effective_style_hash,
            mask_selectors=settings.browser.screenshot_mask_selectors or (),
            blocklist_selectors=blocklist_selectors,
            ocr_model=config.ocr_model or config.ocr_policy or settings.ocr.model,
            ocr_use_fp8=(
                config.ocr_use_fp8
                if config.ocr_use_fp8 is not None
                else settings.ocr.use_fp8
            ),
        )


class CacheManager:
    """Manages content-addressed cache with TTL and non-destructive invalidation."""

    def __init__(self, cache_root: Path, default_ttl_hours: int = 168):
        """Initialize cache manager.

        Args:
            cache_root: Root directory for cache storage
            default_ttl_hours: Default TTL in hours (default: 168 = 1 week)
        """
        self.cache_root = Path(cache_root)
        self.default_ttl = timedelta(hours=default_ttl_hours)
        self._invalidated_keys: set[str] = set()

    def get_cache_path(self, cache_key: str) -> Path:
        """Get filesystem path for a cache key.

        Uses first 2 chars as bucket for better filesystem distribution.

        Args:
            cache_key: Cache key (hex string)

        Returns:
            Path to cache directory
        """
        bucket = cache_key[:2]
        return self.cache_root / "cache" / bucket / cache_key

    def is_cache_valid(
        self,
        cache_key: str,
        ttl: Optional[timedelta] = None,
    ) -> bool:
        """Check if cache entry exists, is not invalidated, and is not expired.

        Args:
            cache_key: Cache key to check
            ttl: Custom TTL (uses default if not provided)

        Returns:
            True if cache is valid, False otherwise
        """
        if cache_key in self._invalidated_keys:
            return False

        cache_path = self.get_cache_path(cache_key)

        if not cache_path.exists():
            return False

        # Check TTL using manifest timestamp
        manifest_path = cache_path / "artifact" / "manifest.json"
        if not manifest_path.exists():
            manifest_path = cache_path / "manifest.json"
        if not manifest_path.exists():
            return False

        try:
            with open(manifest_path, encoding="utf-8") as f:
                manifest = json.load(f)

            if manifest.get("cache_invalidated"):
                return False
            if manifest.get("validation_failures") or manifest.get("error"):
                return False
            sweep_stats = manifest.get("sweep_stats")
            if isinstance(sweep_stats, dict) and sweep_stats.get("truncated_by_max_steps"):
                return False

            # Get timestamp from manifest
            timestamp_str = (
                manifest.get("metadata", {}).get("started_at")
                or manifest.get("started_at")
                or manifest.get("finished_at")
            )
            if not timestamp_str:
                return False

            timestamp = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            age = datetime.now(timezone.utc) - timestamp

            effective_ttl = ttl or self.default_ttl
            return age < effective_ttl

        except Exception:
            # If we can't read the manifest, consider cache invalid
            return False

    def get_cache_metadata(self, cache_key: str) -> Optional[Dict[str, Any]]:
        """Get metadata for a cached capture.

        Args:
            cache_key: Cache key

        Returns:
            Manifest metadata if cache exists, None otherwise
        """
        cache_path = self.get_cache_path(cache_key)
        manifest_path = cache_path / "artifact" / "manifest.json"
        if not manifest_path.exists():
            manifest_path = cache_path / "manifest.json"

        if not manifest_path.exists():
            return None

        try:
            with open(manifest_path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def invalidate_cache(self, cache_key: str) -> bool:
        """Mark a cache entry invalidated in metadata without deleting files on disk.

        Args:
            cache_key: Cache key to invalidate

        Returns:
            True if cache entry existed and was invalidated, False otherwise
        """
        cache_path = self.get_cache_path(cache_key)
        if not cache_path.exists():
            return False

        self._invalidated_keys.add(cache_key)
        for candidate in (
            cache_path / "artifact" / "manifest.json",
            cache_path / "manifest.json",
        ):
            if candidate.exists():
                try:
                    payload = json.loads(candidate.read_text(encoding="utf-8"))
                    if isinstance(payload, dict):
                        payload["cache_invalidated"] = True
                        candidate.write_text(json.dumps(payload, indent=2), encoding="utf-8")
                except Exception:
                    pass
        return True

    def invalidate_url(self, url: str) -> int:
        """Invalidate all cache entries for a specific URL without deleting files.

        Args:
            url: URL to invalidate

        Returns:
            Number of cache entries invalidated
        """
        from urllib.parse import urlparse, urlunparse

        parsed = urlparse(url)
        normalized_url = urlunparse(
            (
                parsed.scheme.lower(),
                parsed.netloc.lower(),
                parsed.path.rstrip("/") or "/",
                parsed.params,
                parsed.query,
                "",
            )
        )

        count = 0
        cache_base = self.cache_root / "cache"

        if not cache_base.exists():
            return 0

        for bucket_dir in cache_base.iterdir():
            if not bucket_dir.is_dir():
                continue

            for cache_dir in bucket_dir.iterdir():
                if not cache_dir.is_dir():
                    continue

                metadata = self.get_cache_metadata(cache_dir.name)
                if metadata and metadata.get("url") in (url, normalized_url):
                    if self.invalidate_cache(cache_dir.name):
                        count += 1

        return count

    def cleanup_expired(self, ttl: Optional[timedelta] = None) -> int:
        """Mark expired cache entries invalidated without deleting files.

        Args:
            ttl: Custom TTL (uses default if not provided)

        Returns:
            Number of entries invalidated
        """
        count = 0
        cache_base = self.cache_root / "cache"

        if not cache_base.exists():
            return 0

        for bucket_dir in cache_base.iterdir():
            if not bucket_dir.is_dir():
                continue

            for cache_dir in bucket_dir.iterdir():
                if not cache_dir.is_dir():
                    continue

                if not self.is_cache_valid(cache_dir.name, ttl):
                    if cache_dir.name not in self._invalidated_keys:
                        if self.invalidate_cache(cache_dir.name):
                            count += 1

        return count

    def get_cache_stats(self) -> Dict[str, Any]:
        """Get cache statistics.

        Returns:
            dict: Statistics about cache size and entries
        """
        cache_base = self.cache_root / "cache"

        if not cache_base.exists():
            return {
                "total_entries": 0,
                "total_size_bytes": 0,
                "buckets": 0,
            }

        total_entries = 0
        total_size = 0
        bucket_count = 0

        for bucket_dir in cache_base.iterdir():
            if not bucket_dir.is_dir():
                continue

            bucket_count += 1

            for cache_dir in bucket_dir.iterdir():
                if not cache_dir.is_dir():
                    continue

                total_entries += 1

                # Calculate size
                for file in cache_dir.rglob("*"):
                    if file.is_file():
                        total_size += file.stat().st_size

        return {
            "total_entries": total_entries,
            "total_size_bytes": total_size,
            "total_size_gb": round(total_size / (1024**3), 2),
            "buckets": bucket_count,
        }
