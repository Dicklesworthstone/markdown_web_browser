"""Depth-1 web crawler for link expansion and site capture."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from collections.abc import Callable
from typing import Any, Dict, List, Optional, Set
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import json
from pathlib import Path
import httpx

LOGGER = logging.getLogger(__name__)


class CrawlStatus(str, Enum):
    """Status of a crawl job."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class CrawlConfig:
    """Configuration for a crawl job."""

    seed_url: str
    max_depth: int = 1
    domain_allowlist: List[str] = field(default_factory=list)
    max_pages: int = 100
    respect_robots_txt: bool = True
    crawl_delay_ms: int = 1000  # Delay between requests
    user_agent: str = "MarkdownWebBrowser/0.1.0"


@dataclass
class CrawlResult:
    """Result of a single page crawl."""

    url: str
    status: str  # "success", "failed", "skipped"
    job_id: Optional[str] = None
    error: Optional[str] = None
    discovered_links: List[str] = field(default_factory=list)
    depth: int = 0


@dataclass
class CrawlState:
    """State of an ongoing crawl."""

    crawl_id: str
    config: CrawlConfig
    status: CrawlStatus
    started_at: datetime
    finished_at: Optional[datetime] = None

    # URL tracking
    visited: Set[str] = field(default_factory=set)
    pending: Set[str] = field(default_factory=set)
    failed: Set[str] = field(default_factory=set)

    # Results
    results: List[CrawlResult] = field(default_factory=list)

    # Depth tracking
    url_depths: Dict[str, int] = field(default_factory=dict)


class RobotsChecker:
    """Check robots.txt compliance."""

    def __init__(self, user_agent: str = "MarkdownWebBrowser/0.1.0"):
        self.user_agent = user_agent
        self._parsers: Dict[str, RobotFileParser] = {}

    async def can_fetch(self, url: str) -> bool:
        """Check if URL can be crawled according to robots.txt.

        Args:
            url: URL to check

        Returns:
            True if crawling is allowed, False otherwise
        """
        parsed = urlparse(url)
        domain = f"{parsed.scheme}://{parsed.netloc}"
        robots_url = urljoin(domain, "/robots.txt")

        # Get or fetch robots.txt parser
        if domain not in self._parsers:
            parser = RobotFileParser()
            parser.set_url(robots_url)

            try:
                async with httpx.AsyncClient() as client:
                    response = await client.get(robots_url, timeout=5.0)
                    if response.status_code == 200:
                        parser.parse(response.text.splitlines())
                    # If robots.txt doesn't exist, allow everything
            except Exception:
                # On error, allow everything (conservative approach)
                pass

            self._parsers[domain] = parser

        parser = self._parsers[domain]
        return parser.can_fetch(self.user_agent, url)


class CrawlOrchestrator:
    """Orchestrates depth-1 web crawling."""

    def __init__(self, *, store: Any | None = None) -> None:
        self._active_crawls: Dict[str, CrawlState] = {}
        self._robots_checker: Optional[RobotsChecker] = None
        # Optional JobManager-backed store; lets the crawler read links.json from
        # completed jobs so depth-1 expansion follows real anchors (not stubs).
        self._store = store

    def _should_crawl(self, url: str, config: CrawlConfig, current_depth: int) -> bool:
        """Determine if a URL should be crawled.

        Args:
            url: URL to check
            config: Crawl configuration
            current_depth: Current depth in crawl tree

        Returns:
            True if URL should be crawled
        """
        # Check depth
        if current_depth > config.max_depth:
            return False

        # Check domain allowlist
        if config.domain_allowlist:
            parsed = urlparse(url)
            domain = parsed.netloc.lower()

            allowed = False
            for allowed_domain in config.domain_allowlist:
                if domain == allowed_domain or domain.endswith(f".{allowed_domain}"):
                    allowed = True
                    break

            if not allowed:
                return False

        # Check scheme (only http/https)
        parsed = urlparse(url)
        if parsed.scheme not in ["http", "https"]:
            return False

        return True

    async def _extract_links(
        self,
        *,
        job_id: str,
        store: Any | None = None,
    ) -> List[str]:
        """Extract outbound links from a completed capture job.

        Reads ``links.json`` (harvested by ``app.dom_links``) so depth-1 expansion
        follows the same anchors operators see in the dashboard. Returns absolute
        URLs only; relative URLs and fragments are dropped (handled by the seed
        domain filter at expansion time).
        """
        if not job_id or store is None:
            return []
        try:
            record = store.get_run_record(job_id)  # type: ignore[attr-defined]
        except Exception as exc:
            LOGGER.warning("crawl: could not load run record for %s: %s", job_id, exc)
            return []
        if record is None:
            return []
        try:
            artifact_root = record.artifact_root
        except AttributeError:
            return []
        links_path = Path(artifact_root) / "links.json"
        if not links_path.exists():
            return []
        try:
            payload = json.loads(links_path.read_text(encoding="utf-8"))
        except Exception as exc:
            LOGGER.warning("crawl: failed to parse links.json for %s: %s", job_id, exc)
            return []
        anchors = payload.get("anchors") if isinstance(payload, dict) else None
        if not isinstance(anchors, list):
            return []
        urls: list[str] = []
        for entry in anchors:
            if not isinstance(entry, dict):
                continue
            href = entry.get("href")
            if isinstance(href, str) and href.startswith(("http://", "https://")):
                urls.append(href)
        return urls

    async def start_crawl(
        self,
        config: CrawlConfig,
        capture_fn: Callable[[str], Any] | None = None,
    ) -> str:
        """Start a new crawl job.

        Args:
            config: Crawl configuration
            capture_fn: Function to call for capturing each URL
                       Signature: async def capture(url: str) -> str (returns job_id)

        Returns:
            crawl_id: Unique identifier for this crawl

        Example:
            async def my_capture(url: str) -> str:
                # Submit capture job and return job_id
                return "job_abc123"

            crawl_id = await orchestrator.start_crawl(config, my_capture)
        """
        import uuid

        crawl_id = f"crawl_{uuid.uuid4().hex[:12]}"

        state = CrawlState(
            crawl_id=crawl_id,
            config=config,
            status=CrawlStatus.RUNNING,
            started_at=datetime.now(timezone.utc),
        )

        # Add seed URL to pending
        state.pending.add(config.seed_url)
        state.url_depths[config.seed_url] = 0

        self._active_crawls[crawl_id] = state

        # Start crawl in background
        asyncio.create_task(self._run_crawl(crawl_id, capture_fn))

        LOGGER.info(f"Started crawl {crawl_id} from {config.seed_url}")

        return crawl_id

    async def _run_crawl(
        self,
        crawl_id: str,
        capture_fn: Callable[[str], Any] | None = None,
    ) -> None:
        """Execute the crawl (runs in background).

        Args:
            crawl_id: Crawl identifier
            capture_fn: Function to capture each URL
        """
        state = self._active_crawls[crawl_id]
        config = state.config

        # Initialize robots checker if needed
        if config.respect_robots_txt and not self._robots_checker:
            self._robots_checker = RobotsChecker(config.user_agent)

        try:
            while state.pending and len(state.visited) < config.max_pages:
                # Get next URL
                url = state.pending.pop()
                depth = state.url_depths.get(url, 0)

                LOGGER.info(f"Crawling {url} (depth={depth})")

                # Check if we should crawl
                if not self._should_crawl(url, config, depth):
                    LOGGER.debug(f"Skipping {url} (depth or domain filter)")
                    continue

                # Check robots.txt
                if config.respect_robots_txt and self._robots_checker:
                    if not await self._robots_checker.can_fetch(url):
                        LOGGER.info(f"Skipping {url} (robots.txt)")
                        continue

                # Mark as visited
                state.visited.add(url)

                # Capture the page
                result = CrawlResult(
                    url=url,
                    status="pending",
                    depth=depth,
                )

                if capture_fn:
                    try:
                        job_id = await capture_fn(url)
                        result.job_id = job_id
                        result.status = "success"

                        # Extract links from the just-completed seed job
                        if depth == 0 and job_id:
                            links = await self._extract_links(
                                job_id=job_id,
                                store=self._store,
                            )
                            result.discovered_links = links

                            # Add discovered links to pending
                            for link in links:
                                if link not in state.visited and link not in state.pending:
                                    state.pending.add(link)
                                    state.url_depths[link] = depth + 1

                    except Exception as e:
                        LOGGER.error(f"Failed to capture {url}: {e}")
                        result.status = "failed"
                        result.error = str(e)
                        state.failed.add(url)
                else:
                    result.status = "skipped"

                state.results.append(result)

                # Delay between requests
                await asyncio.sleep(config.crawl_delay_ms / 1000.0)

            # Mark as completed
            state.status = CrawlStatus.COMPLETED
            state.finished_at = datetime.now(timezone.utc)

            LOGGER.info(
                f"Crawl {crawl_id} completed: "
                f"visited={len(state.visited)}, "
                f"failed={len(state.failed)}"
            )

        except Exception as e:
            LOGGER.error(f"Crawl {crawl_id} failed: {e}")
            state.status = CrawlStatus.FAILED
            state.finished_at = datetime.now(timezone.utc)

    def get_crawl_status(self, crawl_id: str) -> Optional[Dict]:
        """Get status of a crawl job.

        Args:
            crawl_id: Crawl identifier

        Returns:
            Status dict or None if not found
        """
        state = self._active_crawls.get(crawl_id)
        if not state:
            return None

        return {
            "crawl_id": crawl_id,
            "seed_url": state.config.seed_url,
            "status": state.status.value,
            "started_at": state.started_at.isoformat(),
            "finished_at": state.finished_at.isoformat() if state.finished_at else None,
            "discovered": len(state.visited) + len(state.pending),
            "completed": len([r for r in state.results if r.status == "success"]),
            "failed": len(state.failed),
            "pending": len(state.pending),
            "results": [
                {
                    "url": r.url,
                    "status": r.status,
                    "job_id": r.job_id,
                    "depth": r.depth,
                    "discovered_links": len(r.discovered_links),
                }
                for r in state.results
            ],
        }

    async def cancel_crawl(self, crawl_id: str) -> bool:
        """Cancel an active crawl.

        Args:
            crawl_id: Crawl identifier

        Returns:
            True if cancelled, False if not found
        """
        state = self._active_crawls.get(crawl_id)
        if not state:
            return False

        state.status = CrawlStatus.FAILED
        state.finished_at = datetime.now(timezone.utc)

        LOGGER.info(f"Crawl {crawl_id} cancelled")
        return True


# Global crawler instance
_global_crawler: Optional[CrawlOrchestrator] = None


def get_crawler(*, store: Any | None = None) -> CrawlOrchestrator:
    """Get or create the global crawler instance."""
    global _global_crawler

    if _global_crawler is None:
        _global_crawler = CrawlOrchestrator(store=store)

    return _global_crawler


async def submit_and_wait_for_job(
    job_manager: Any,
    *,
    url: str,
    profile_id: str | None = None,
    ocr_policy: str | None = None,
    reuse_cache: bool = True,
    poll_interval_s: float = 1.0,
    timeout_s: float = 600.0,
) -> str:
    """Submit a capture via JobManager and block until it finishes.

    Returns the job id. This is the bridge between CrawlOrchestrator and the
    capture pipeline so depth-1 expansion reuses the same profile/OCR/cache
    plumbing as a direct ``POST /jobs`` call.
    """
    from app.schemas import JobCreateRequest

    request = JobCreateRequest(
        url=url,
        profile_id=profile_id,
        ocr_policy=ocr_policy,
        reuse_cache=reuse_cache,
    )
    snapshot = await job_manager.create_job(request)
    job_id = str(snapshot["job_id"]) if isinstance(snapshot, dict) else snapshot.job_id
    deadline = asyncio.get_event_loop().time() + timeout_s
    while asyncio.get_event_loop().time() < deadline:
        snap = job_manager.get_snapshot(job_id)
        if snap is None:
            return job_id
        state = snap.get("state") if isinstance(snap, dict) else getattr(snap, "state", None)
        if state in {"DONE", "FAILED", "CANCELLED"}:
            return job_id
        await asyncio.sleep(poll_interval_s)
    return job_id


# Convenience function for simple crawls
async def run_crawl(
    seed_url: str,
    max_depth: int = 1,
    domain_allowlist: List[str] | None = None,
    max_pages: int = 100,
) -> Dict:
    """Run a simple crawl and wait for completion.

    Args:
        seed_url: Starting URL
        max_depth: Maximum crawl depth (default: 1)
        domain_allowlist: Allowed domains (default: seed domain only)
        max_pages: Maximum pages to crawl

    Returns:
        Crawl results dictionary

    Example:
        result = await run_crawl("https://example.com", max_depth=1)
        print(f"Crawled {result['completed']} pages")
    """
    # Extract domain from seed URL if no allowlist provided
    if domain_allowlist is None:
        parsed = urlparse(seed_url)
        domain_allowlist = [parsed.netloc]

    config = CrawlConfig(
        seed_url=seed_url,
        max_depth=max_depth,
        domain_allowlist=domain_allowlist,
        max_pages=max_pages,
    )

    crawler = get_crawler()
    crawl_id = await crawler.start_crawl(config)

    # Wait for completion
    while True:
        status = crawler.get_crawl_status(crawl_id)
        if status and status["status"] in ["completed", "failed"]:
            return status

        await asyncio.sleep(1.0)
