"""Content-addressed caching utilities for deterministic captures."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from app.schemas import CaptureConfig


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

    This is a convenience wrapper around compute_cache_key that extracts
    parameters from configuration objects.

    Args:
        config: CaptureConfig with capture parameters
        settings: Settings object with OCR configuration

    Returns:
        Cache key (64-character hex string)
    """
    return compute_cache_key(
        url=config.url,
        viewport_width=config.viewport_width,
        viewport_height=config.viewport_height,
        device_scale_factor=config.device_scale_factor,
        color_scheme=config.color_scheme,
        long_side_px=settings.browser.long_side_px,
        viewport_overlap_px=settings.browser.viewport_overlap_px,
        tile_overlap_px=settings.browser.tile_overlap_px,
        scroll_settle_ms=settings.browser.scroll_settle_ms,
        screenshot_style_hash=settings.browser.screenshot_style_hash,
        mask_selectors=config.mask_selectors or [],
        blocklist_selectors=config.blocklist_selectors or [],
        ocr_model=settings.ocr.model,
        ocr_use_fp8=settings.ocr.use_fp8,
    )


class CacheManager:
    """Manages content-addressed cache with TTL and invalidation."""

    def __init__(self, cache_root: Path, default_ttl_hours: int = 168):
        """Initialize cache manager.

        Args:
            cache_root: Root directory for cache storage
            default_ttl_hours: Default TTL in hours (default: 168 = 1 week)
        """
        self.cache_root = Path(cache_root)
        self.default_ttl = timedelta(hours=default_ttl_hours)

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
        """Check if cache entry exists and is not expired.

        Args:
            cache_key: Cache key to check
            ttl: Custom TTL (uses default if not provided)

        Returns:
            True if cache is valid, False otherwise
        """
        cache_path = self.get_cache_path(cache_key)

        if not cache_path.exists():
            return False

        # Check TTL using manifest timestamp
        manifest_path = cache_path / "artifact" / "manifest.json"
        if not manifest_path.exists():
            return False

        try:
            with open(manifest_path) as f:
                manifest = json.load(f)

            # Get timestamp from manifest
            timestamp_str = manifest.get("metadata", {}).get("started_at")
            if not timestamp_str:
                return False

            timestamp = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
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
            return None

        try:
            with open(manifest_path) as f:
                return json.load(f)
        except Exception:
            return None

    def invalidate_cache(self, cache_key: str) -> bool:
        """Invalidate a cache entry by removing it.

        Args:
            cache_key: Cache key to invalidate

        Returns:
            True if cache was deleted, False if it didn't exist
        """
        import shutil

        cache_path = self.get_cache_path(cache_key)

        if cache_path.exists():
            shutil.rmtree(cache_path)
            return True

        return False

    def invalidate_url(self, url: str) -> int:
        """Invalidate all cache entries for a specific URL.

        Args:
            url: URL to invalidate

        Returns:
            Number of cache entries invalidated
        """
        # Normalize URL
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

        # Iterate through all buckets
        for bucket_dir in cache_base.iterdir():
            if not bucket_dir.is_dir():
                continue

            # Check each cache entry
            for cache_dir in bucket_dir.iterdir():
                if not cache_dir.is_dir():
                    continue

                metadata = self.get_cache_metadata(cache_dir.name)
                if metadata and metadata.get("url") == normalized_url:
                    if self.invalidate_cache(cache_dir.name):
                        count += 1

        return count

    def cleanup_expired(self, ttl: Optional[timedelta] = None) -> int:
        """Remove expired cache entries.

        Args:
            ttl: Custom TTL (uses default if not provided)

        Returns:
            Number of entries removed
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
