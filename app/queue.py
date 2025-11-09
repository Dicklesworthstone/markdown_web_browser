"""Job queue infrastructure using Arq for async task processing."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Callable, Dict, Optional

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings
from arq.jobs import Job, JobStatus
from arq.worker import Worker, create_worker, func

from app.settings import Settings, settings as global_settings

LOGGER = logging.getLogger(__name__)


class JobPriority(int, Enum):
    """Job priority levels (lower number = higher priority)."""

    CRITICAL = 0
    HIGH = 1
    NORMAL = 2
    LOW = 3


@dataclass
class QueueConfig:
    """Configuration for the job queue."""

    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_database: int = 0
    redis_password: Optional[str] = None

    # Worker configuration
    max_jobs: int = 10  # Max concurrent jobs per worker
    job_timeout: int = 3600  # Default job timeout in seconds (1 hour)
    keep_result: int = 3600  # How long to keep results in Redis (1 hour)

    # Retry configuration
    max_retries: int = 3
    retry_delay: int = 60  # Seconds between retries

    @classmethod
    def from_env(cls, settings: Settings | None = None) -> QueueConfig:
        """Load configuration from environment variables."""
        active_settings = settings or global_settings

        # In a real implementation, these would come from settings
        # For now, use defaults
        return cls()


def get_redis_settings(config: QueueConfig | None = None) -> RedisSettings:
    """Get Redis settings for Arq."""
    config = config or QueueConfig.from_env()

    return RedisSettings(
        host=config.redis_host,
        port=config.redis_port,
        database=config.redis_database,
        password=config.redis_password,
    )


# Arq worker functions
# These are the actual task implementations that will be executed by workers


async def process_capture_job(
    ctx: Dict[str, Any],
    job_id: str,
    url: str,
    profile_id: Optional[str] = None,
    cache_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Process a web page capture job.

    This is an Arq worker function that will be executed asynchronously.

    Args:
        ctx: Arq worker context
        job_id: Unique job identifier
        url: URL to capture
        profile_id: Optional browser profile ID
        cache_key: Optional cache key for deduplication

    Returns:
        dict: Job result with status and output paths
    """
    from app.jobs import run_capture_job

    LOGGER.info(f"Starting capture job {job_id} for URL: {url}")

    try:
        # Execute the actual capture logic
        result = await run_capture_job(
            job_id=job_id,
            url=url,
            profile_id=profile_id,
            cache_key=cache_key,
        )

        LOGGER.info(f"Completed capture job {job_id}")

        return {
            "status": "success",
            "job_id": job_id,
            "url": url,
            "result": result,
        }

    except Exception as e:
        LOGGER.error(f"Failed capture job {job_id}: {e}", exc_info=True)
        raise


async def process_crawl_job(
    ctx: Dict[str, Any],
    job_id: str,
    seed_url: str,
    max_depth: int = 1,
    domain_allowlist: Optional[list[str]] = None,
) -> Dict[str, Any]:
    """Process a crawl job (depth-1 link expansion).

    Args:
        ctx: Arq worker context
        job_id: Unique job identifier
        seed_url: Starting URL
        max_depth: Maximum crawl depth
        domain_allowlist: Allowed domains for crawling

    Returns:
        dict: Crawl results with captured URLs
    """
    from app.crawler import run_crawl

    LOGGER.info(f"Starting crawl job {job_id} from {seed_url}")

    try:
        result = await run_crawl(
            seed_url=seed_url,
            max_depth=max_depth,
            domain_allowlist=domain_allowlist or [],
        )

        LOGGER.info(f"Completed crawl job {job_id}, captured {len(result['urls'])} pages")

        return {
            "status": "success",
            "job_id": job_id,
            "seed_url": seed_url,
            "result": result,
        }

    except Exception as e:
        LOGGER.error(f"Failed crawl job {job_id}: {e}", exc_info=True)
        raise


# Arq worker function registry
# These functions will be available to the worker
WORKER_FUNCTIONS = [
    func(process_capture_job, name="capture"),
    func(process_crawl_job, name="crawl"),
]


class JobQueue:
    """High-level interface for job queue operations."""

    def __init__(self, config: QueueConfig | None = None):
        """Initialize job queue.

        Args:
            config: Queue configuration (uses defaults if not provided)
        """
        self.config = config or QueueConfig.from_env()
        self.redis_settings = get_redis_settings(self.config)
        self._pool: Optional[ArqRedis] = None

    async def get_pool(self) -> ArqRedis:
        """Get or create Redis connection pool."""
        if self._pool is None:
            self._pool = await create_pool(self.redis_settings)
        return self._pool

    async def enqueue(
        self,
        function: str,
        *args,
        priority: JobPriority = JobPriority.NORMAL,
        timeout: Optional[int] = None,
        **kwargs,
    ) -> Job:
        """Enqueue a job for processing.

        Args:
            function: Name of the worker function to execute
            *args: Positional arguments for the function
            priority: Job priority (affects queue order)
            timeout: Job timeout in seconds
            **kwargs: Keyword arguments for the function

        Returns:
            Job: Arq job object for tracking

        Example:
            job = await queue.enqueue(
                "capture",
                job_id="abc123",
                url="https://example.com",
                priority=JobPriority.HIGH,
            )
        """
        pool = await self.get_pool()

        # Convert priority to queue name (Arq supports multiple queues)
        queue_name = f"queue:{priority.name.lower()}"

        job = await pool.enqueue_job(
            function,
            *args,
            _queue_name=queue_name,
            _job_timeout=timeout or self.config.job_timeout,
            _max_tries=self.config.max_retries + 1,  # +1 for initial attempt
            _defer_by=timedelta(seconds=0),
            **kwargs,
        )

        LOGGER.info(f"Enqueued job {job.job_id} to {queue_name} (function={function})")

        return job

    async def get_job(self, job_id: str) -> Optional[Job]:
        """Get job by ID.

        Args:
            job_id: Job identifier

        Returns:
            Job object if found, None otherwise
        """
        pool = await self.get_pool()
        return Job(job_id, pool)

    async def get_job_status(self, job_id: str) -> Optional[JobStatus]:
        """Get status of a job.

        Args:
            job_id: Job identifier

        Returns:
            JobStatus if job exists, None otherwise
        """
        job = await self.get_job(job_id)
        if job:
            return await job.status()
        return None

    async def get_job_result(self, job_id: str, timeout: int = 60) -> Any:
        """Wait for job completion and get result.

        Args:
            job_id: Job identifier
            timeout: Maximum time to wait in seconds

        Returns:
            Job result if successful

        Raises:
            TimeoutError: If job doesn't complete in time
            Exception: If job failed
        """
        job = await self.get_job(job_id)
        if not job:
            raise ValueError(f"Job {job_id} not found")

        result = await job.result(timeout=timeout, poll_delay=0.5)
        return result

    async def cancel_job(self, job_id: str) -> bool:
        """Cancel a pending or running job.

        Args:
            job_id: Job identifier

        Returns:
            True if job was cancelled, False otherwise
        """
        job = await self.get_job(job_id)
        if job:
            await job.abort()
            LOGGER.info(f"Cancelled job {job_id}")
            return True
        return False

    async def get_queue_stats(self) -> Dict[str, Any]:
        """Get queue statistics.

        Returns:
            dict: Statistics about queues and jobs
        """
        pool = await self.get_pool()

        # Get info from Redis
        info = await pool.info()

        stats = {
            "queues": {},
            "workers": info.get("connected_clients", 0),
            "total_processed": 0,
        }

        # Get stats for each priority queue
        for priority in JobPriority:
            queue_name = f"queue:{priority.name.lower()}"
            queue_length = await pool.llen(queue_name)
            stats["queues"][priority.name] = {
                "pending": queue_length,
                "priority": priority.value,
            }

        return stats

    async def close(self) -> None:
        """Close Redis connection pool."""
        if self._pool:
            await self._pool.close()
            self._pool = None


def create_arq_worker(
    config: QueueConfig | None = None,
    functions: list[Callable] | None = None,
) -> Worker:
    """Create an Arq worker instance.

    Args:
        config: Queue configuration
        functions: List of worker functions (uses WORKER_FUNCTIONS if not provided)

    Returns:
        Worker: Configured Arq worker

    Example:
        # In a worker script:
        worker = create_arq_worker()
        worker.run()
    """
    config = config or QueueConfig.from_env()
    functions = functions or WORKER_FUNCTIONS

    return create_worker(
        WorkerSettings,
        redis_settings=get_redis_settings(config),
    )


class WorkerSettings:
    """Arq worker settings."""

    functions = WORKER_FUNCTIONS

    # Job defaults
    job_timeout = 3600  # 1 hour
    keep_result = 3600  # Keep results for 1 hour

    # Retry configuration
    max_tries = 4  # 1 initial + 3 retries
    retry_jobs = True

    # Worker configuration
    max_jobs = 10  # Max concurrent jobs
    poll_delay = 0.5  # Seconds between queue polls
    queue_name = "queue:normal"  # Default queue

    # Health check
    health_check_interval = 30  # Seconds between health checks

    # Logging
    on_startup = lambda ctx: LOGGER.info("Arq worker started")
    on_shutdown = lambda ctx: LOGGER.info("Arq worker shutdown")
    on_job_start = lambda ctx: LOGGER.info(f"Job started: {ctx['job_id']}")
    on_job_end = lambda ctx: LOGGER.info(f"Job completed: {ctx['job_id']}")


# Global queue instance
_global_queue: Optional[JobQueue] = None


async def get_queue(config: QueueConfig | None = None) -> JobQueue:
    """Get or create the global job queue instance."""
    global _global_queue

    if _global_queue is None:
        _global_queue = JobQueue(config)

    return _global_queue
