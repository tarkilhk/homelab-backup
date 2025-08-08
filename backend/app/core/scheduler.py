"""APScheduler configuration and job management.

Responsibilities:
- Provide a singleton `AsyncIOScheduler` instance
- Load enabled `Job`s from DB on startup and schedule them
- Execute a scheduled job by creating a `Run` row, logging structured JSON, and
  marking success with a dummy artifact path (placeholder for real plugins)
- Provide helpers to trigger the same logic immediately (manual run)
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.orm import Session

from app.core.db import SessionLocal
from app.models import Job as JobModel, Run as RunModel


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
    return _scheduler


def _log_json(event: dict) -> None:
    """Emit a structured JSON log line to stdout."""
    try:
        logger.info(json.dumps(event, default=str))
    except Exception:
        # Ensure logging never breaks the job path
        logger.info(str(event))


def _perform_dummy_run(db: Session, job: JobModel, triggered_by: str) -> RunModel:
    """Create a Run row for the given Job, mark running then success (dummy).

    This function is intentionally synchronous and small; scheduler will call it
    inside its own threadpool.
    """
    started_at = datetime.now(timezone.utc)

    run = RunModel(
        job_id=job.id,
        started_at=started_at,
        status="running",
        message=f"Run started (triggered_by={triggered_by})",
        logs_text=f"Run started at {started_at.isoformat()} (triggered_by={triggered_by})",
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    _log_json({
        "event": "run_started",
        "job_id": job.id,
        "run_id": run.id,
        "triggered_by": triggered_by,
        "started_at": run.started_at,
        "job_name": job.name,
    })

    # Placeholder for real work. Immediately mark success with dummy artifact.
    finished_at = datetime.now(timezone.utc)
    artifact_path = f"/backups/job-{job.id}-{int(finished_at.timestamp())}.dummy"
    run.finished_at = finished_at
    run.status = "success"
    run.message = "Run completed successfully (dummy)"
    run.artifact_path = artifact_path
    run.logs_text = (run.logs_text or "") + f"\nCompleted at {finished_at.isoformat()}"

    db.add(run)
    db.commit()
    db.refresh(run)

    _log_json({
        "event": "run_finished",
        "job_id": job.id,
        "run_id": run.id,
        "triggered_by": triggered_by,
        "finished_at": run.finished_at,
        "status": run.status,
        "artifact_path": run.artifact_path,
    })

    return run


def run_job_immediately(db: Session, job_id: int, triggered_by: str = "manual") -> RunModel:
    """Execute the job immediately and return the created Run row.

    This is used by the API "Run now" endpoint so both manual and scheduled
    runs share the exact same logic.
    """
    job = db.get(JobModel, job_id)
    if job is None:
        raise ValueError("Job not found")
    return _perform_dummy_run(db, job, triggered_by)


def _scheduled_job(job_id: int) -> None:
    """Entry point for APScheduler when triggering a job by ID."""
    db = SessionLocal()
    try:
        job = db.get(JobModel, job_id)
        if job is None:
            _log_json({"event": "job_missing", "job_id": job_id})
            return
        _perform_dummy_run(db, job, triggered_by="scheduler")
    finally:
        db.close()


def schedule_jobs_on_startup(scheduler: AsyncIOScheduler, db: Session) -> None:
    """Load enabled jobs from DB and schedule them with APScheduler.

    - Uses cron in `jobs.schedule_cron`
    - Ensures `max_instances=1` via scheduler job_defaults or per-job arg
    """
    # In tests, a dummy scheduler may be provided without `add_job`.
    if not hasattr(scheduler, "add_job"):
        return

    enabled_jobs = db.query(JobModel).filter(JobModel.enabled == "true").all()

    for job in enabled_jobs:
        try:
            trigger = CronTrigger.from_crontab(job.schedule_cron)
        except Exception:
            _log_json({
                "event": "invalid_cron",
                "job_id": job.id,
                "schedule_cron": job.schedule_cron,
            })
            continue

        scheduler.add_job(
            func=_scheduled_job,
            trigger=trigger,
            id=f"job:{job.id}",
            name=f"{job.name}",
            replace_existing=True,
            kwargs={"job_id": job.id},
            max_instances=1,
        )
        _log_json({
            "event": "job_scheduled",
            "job_id": job.id,
            "name": job.name,
            "schedule_cron": job.schedule_cron,
        })

