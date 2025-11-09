#!/usr/bin/env python3
"""Start Arq worker for processing background jobs."""

import asyncio
import logging
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.queue import WorkerSettings, get_redis_settings, QueueConfig
from arq import run_worker
from rich.console import Console
from rich.logging import RichHandler

console = Console()

# Configure logging with Rich
logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    handlers=[RichHandler(rich_tracebacks=True, console=console)],
)

LOGGER = logging.getLogger(__name__)


async def main():
    """Run the Arq worker."""
    console.print("\n🚀 [bold cyan]Starting Arq Worker for Markdown Web Browser[/bold cyan]\n")

    config = QueueConfig.from_env()
    redis_settings = get_redis_settings(config)

    console.print(f"📡 Redis: {redis_settings.host}:{redis_settings.port}")
    console.print(f"⚙️  Max concurrent jobs: {WorkerSettings.max_jobs}")
    console.print(f"⏱️  Job timeout: {WorkerSettings.job_timeout}s")
    console.print(f"🔄 Max retries: {WorkerSettings.max_tries - 1}")
    console.print(f"📋 Registered functions: {len(WorkerSettings.functions)}")

    for func in WorkerSettings.functions:
        console.print(f"   - {func.name}")

    console.print("\n✅ [bold green]Worker ready, waiting for jobs...[/bold green]\n")

    try:
        # Run the worker (blocks until shutdown)
        await run_worker(WorkerSettings, redis_settings=redis_settings)
    except KeyboardInterrupt:
        console.print("\n⚠️  [yellow]Worker shutdown requested[/yellow]")
    except Exception as e:
        console.print(f"\n❌ [bold red]Worker error: {e}[/bold red]")
        LOGGER.exception("Worker failed")
        sys.exit(1)

    console.print("\n👋 [dim]Worker stopped[/dim]\n")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
