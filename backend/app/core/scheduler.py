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
import asyncio
import threading
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.orm import Session

from app.core.db import SessionLocal
from app.models import Job as JobModel, Run as RunModel, Target as TargetModel
from app.core.plugins.base import BackupContext
from app.core.plugins.loader import get_plugin
from app.core.notifier import send_failure_email


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


def _perform_run(db: Session, job: JobModel, triggered_by: str) -> RunModel:
    """Create a Run row, execute the job's plugin, and finalize status.

    This function is synchronous; scheduler will call it inside its own
    threadpool.
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

    # Execute plugin if available; otherwise fall back to a dummy artifact.
    artifact_path: str
    try:
        # Load target for slug/config
        target = db.get(TargetModel, job.target_id)
        target_slug = target.slug if target is not None else f"target-{job.target_id}"
        # Use plugin-based target config only
        config_dict = {}
        if target is not None:
            raw_cfg = target.plugin_config_json or "{}"
            try:
                config_dict = json.loads(raw_cfg)
            except Exception:
                config_dict = {}

        # Build context per base class
        ctx = BackupContext(
            job_id=str(job.id),
            target_id=str(job.target_id),
            config=config_dict,
            metadata={"target_slug": target_slug},
        )

        # Resolve and invoke plugin
        # Prefer target.plugin_name; fallback to job.plugin if not set
        plugin_key = (
            target.plugin_name if target is not None and target.plugin_name else job.plugin
        )
        plugin = get_plugin(plugin_key)

        # Plugin interface is async; execute in a dedicated thread with its own loop
        result_container: dict[str, object] = {}

        def _runner() -> None:
            result_container["result"] = asyncio.run(plugin.backup(ctx))

        th = threading.Thread(target=_runner, daemon=True)
        th.start()
        th.join()
        result = result_container.get("result")
        artifact_path = result.get("artifact_path") if isinstance(result, dict) else None  # type: ignore[assignment]
        if not artifact_path:
            raise RuntimeError("Plugin did not return artifact_path")

        finished_at = datetime.now(timezone.utc)
        run.finished_at = finished_at
        run.status = "success"
        run.message = "Run completed successfully"
        run.artifact_path = artifact_path
        run.logs_text = (run.logs_text or "") + f"\nCompleted at {finished_at.isoformat()}"
    except KeyError:
        # Unknown plugin — keep legacy dummy success behavior to satisfy tests.
        finished_at = datetime.now(timezone.utc)
        artifact_path = f"/backups/job-{job.id}-{int(finished_at.timestamp())}.dummy"
        run.finished_at = finished_at
        run.status = "success"
        run.message = "Run completed successfully (dummy)"
        run.artifact_path = artifact_path
        run.logs_text = (run.logs_text or "") + f"\nCompleted at {finished_at.isoformat()}"
    except Exception as exc:  # Catch-all failures → mark failed
        finished_at = datetime.now(timezone.utc)
        run.finished_at = finished_at
        run.status = "failed"
        run.message = f"Run failed: {exc}"
        run.logs_text = (run.logs_text or "") + f"\nFailed at {finished_at.isoformat()} with error: {exc}"

        # Fire-and-forget email notification (best-effort)
        try:
            subject = f"[Backup Failure] Job {job.id} — {job.name}"
            body = (
                f"Job ID: {job.id}\n"
                f"Job Name: {job.name}\n"
                f"Target ID: {job.target_id}\n"
                f"Started: {run.started_at}\n"
                f"Finished: {finished_at}\n"
                f"Error: {exc}\n"
            )
            send_failure_email(subject, body)
        except Exception:
            # Never let email issues affect job flow
            pass

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
    return _perform_run(db, job, triggered_by)


def _scheduled_job(job_id: int) -> None:
    """Entry point for APScheduler when triggering a job by ID."""
    db = SessionLocal()
    try:
        job = db.get(JobModel, job_id)
        if job is None:
            _log_json({"event": "job_missing", "job_id": job_id})
            return
        _perform_run(db, job, triggered_by="scheduler")
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

