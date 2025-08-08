"""APScheduler configuration and job management."""

import logging
from datetime import datetime
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)

# Global scheduler instance
_scheduler: Optional[AsyncIOScheduler] = None


def get_scheduler() -> AsyncIOScheduler:
    """Get the global scheduler instance."""
    global _scheduler
    if _scheduler is None:
        _scheduler = AsyncIOScheduler(
            timezone="Asia/Singapore",
            job_defaults={
                "coalesce": True,
                "max_instances": 1,
            },
        )
        _setup_default_jobs()
    return _scheduler


def _setup_default_jobs() -> None:
    """Setup default scheduled jobs."""
    scheduler = get_scheduler()
    
    # Add a no-op job that runs every minute for testing
    scheduler.add_job(
        func=_noop_job,
        trigger=CronTrigger(minute="*"),
        id="noop_job",
        name="No-op test job",
        replace_existing=True,
    )
    logger.info("Added no-op test job (runs every minute)")


async def _noop_job() -> None:
    """No-op job for testing scheduler functionality."""
    logger.info(f"No-op job executed at {datetime.now()}")
