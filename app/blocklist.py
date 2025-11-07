"""Selector blocklist helpers used to mask overlays during capture."""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Mapping
from urllib.parse import urlparse

from playwright.async_api import Page


@dataclass(frozen=True)
class BlocklistConfig:
    """Parsed selectors grouped by global/domain scope."""

    version: str
    global_selectors: tuple[str, ...]
    domain_selectors: Mapping[str, tuple[str, ...]]

    def selectors_for_url(self, url: str) -> tuple[str, ...]:
        """Return the selector set applicable to a given URL (global + domain)."""

        host = (urlparse(url).hostname or "").lower()
        selectors: list[str] = list(self.global_selectors)
        for pattern, scoped in self.domain_selectors.items():
            if _host_matches_pattern(host, pattern.lower()):
                selectors.extend(scoped)
        # Preserve order while deduplicating
        deduped: dict[str, None] = {selector: None for selector in selectors}
        return tuple(deduped.keys())


async def apply_blocklist(page: Page, *, url: str, config: BlocklistConfig) -> dict[str, int]:
    """Inject CSS to hide overlays and return selector hit counts."""

    selectors = config.selectors_for_url(url)
    if selectors:
        css_rules = ";".join(f"{selector}{{display:none!important}}" for selector in selectors)
        await page.add_style_tag(content=css_rules)

    if not selectors:
        return {}

    return await page.evaluate(
        """
        (selectors) => {
            const stats = {};
            for (const selector of selectors) {
                try {
                    stats[selector] = document.querySelectorAll(selector).length;
                } catch (err) {
                    stats[selector] = 0;
                }
            }
            return stats;
        }
        """,
        list(selectors),
    )


def load_blocklist(path: Path) -> BlocklistConfig:
    """Parse the JSON blocklist file."""

    data = json.loads(path.read_text("utf-8"))
    global_selectors = tuple(data.get("global", []))
    domains_raw: Mapping[str, Iterable[str]] = data.get("domains", {})
    domain_selectors = {
        domain: tuple(selectors)
        for domain, selectors in domains_raw.items()
    }
    return BlocklistConfig(
        version=data.get("version", "unknown"),
        global_selectors=global_selectors,
        domain_selectors=domain_selectors,
    )


@lru_cache(maxsize=1)
def cached_blocklist(path: str) -> BlocklistConfig:
    """Memoized blocklist loader suitable for per-process reuse."""

    return load_blocklist(Path(path))


def _host_matches_pattern(host: str, pattern: str) -> bool:
    if not pattern:
        return False
    if pattern.startswith("*."):
        suffix = pattern[1:]
        return host.endswith(suffix)
    return host == pattern


async def detect_overlay_warnings(
    page: Page,
    *,
    canvas_threshold: int = 3,
    video_threshold: int = 2,
) -> list[str]:
    """Return coarse warnings about canvas/video/sticky overlays."""

    stats = await page.evaluate(
        """
        () => {
            const stickySelectors = "[style*='position:fixed'],[style*='position: sticky'],header[style*='position']";
            const sticky = document.querySelectorAll(stickySelectors).length;
            const canvas = document.querySelectorAll('canvas').length;
            const video = document.querySelectorAll('video').length;
            const dialog = document.querySelectorAll('[role="dialog"], [aria-modal="true"]').length;
            return { sticky, canvas, video, dialog };
        }
        """,
    )
    warnings: list[str] = []
    if stats["canvas"] >= canvas_threshold:
        warnings.append("canvas_heavy")
    if stats["video"] >= video_threshold:
        warnings.append("video_overlay")
    if stats["sticky"] >= 3 or stats["dialog"] >= 1:
        warnings.append("sticky_chrome")
    return warnings
