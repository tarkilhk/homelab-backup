"""Jobs API router."""

from datetime import datetime, timezone
from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
import logging
from sqlalchemy.orm import Session

from app.core.db import get_session
from app.core.scheduler import run_job_immediately
from zoneinfo import ZoneInfo
from app.models import Job as JobModel, Run as RunModel
from app.schemas import Job, JobCreate, JobUpdate, Run, UpcomingJob


router = APIRouter(prefix="/jobs", tags=["jobs"])
logger = logging.getLogger(__name__)


@router.get("/", response_model=List[Job])
def list_jobs(db: Session = Depends(get_session)) -> List[JobModel]:
    """List all jobs."""
    return db.query(JobModel).all()


# NOTE: Define static paths before dynamic `/{job_id}` to avoid path conflicts
@router.get("/upcoming", response_model=list[UpcomingJob])
def upcoming_jobs(db: Session = Depends(get_session)) -> list[UpcomingJob]:
    """Return next scheduled run time for enabled jobs using their cron.

    This approach computes the next fire time directly from each job's
    cron expression, independent of whether APScheduler has already
    scheduled it in-memory. This ensures newly-created jobs are visible
    immediately without requiring an app restart.
    """
    from apscheduler.triggers.cron import CronTrigger  # local import to keep module import order simple

    tz = ZoneInfo("Asia/Singapore")
    now = datetime.now(tz)

    rows = db.query(JobModel).filter(JobModel.enabled == "true").all()
    results: list[UpcomingJob] = []
    for job in rows:
        try:
            trigger = CronTrigger.from_crontab(job.schedule_cron, timezone=tz)
            # Next occurrence at/after now
            next_time = trigger.get_next_fire_time(previous_fire_time=None, now=now)
            if next_time is None:
                continue
            results.append(
                UpcomingJob(
                    job_id=job.id,
                    name=job.name,
                    target_id=job.target_id,
                    next_run_at=next_time,
                )
            )
        except Exception:
            # Invalid cron — skip
            continue

    results.sort(key=lambda r: r.next_run_at)
    return results[:10]


@router.post("/", response_model=Job, status_code=status.HTTP_201_CREATED)
def create_job(payload: JobCreate, db: Session = Depends(get_session)) -> JobModel:
    """Create a new job."""
    job = JobModel(
        target_id=payload.target_id,
        name=payload.name,
        schedule_cron=payload.schedule_cron,
        enabled=payload.enabled,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


@router.get("/{job_id}", response_model=Job)
def get_job(job_id: int, db: Session = Depends(get_session)) -> JobModel:
    """Get job by ID."""
    job = db.get(JobModel, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    return job


@router.put("/{job_id}", response_model=Job)
def update_job(job_id: int, payload: JobUpdate, db: Session = Depends(get_session)) -> JobModel:
    """Update an existing job."""
    job = db.get(JobModel, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    update_data = payload.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(job, key, value)

    db.add(job)
    db.commit()
    db.refresh(job)
    return job


@router.delete("/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_job(job_id: int, db: Session = Depends(get_session)) -> None:
    """Delete a job by ID."""
    job = db.get(JobModel, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    db.delete(job)
    db.commit()
    return None


@router.post("/{job_id}/run", response_model=Run)
def run_job_now(job_id: int, db: Session = Depends(get_session)) -> RunModel:
    """Trigger a manual run for a job using the same logic as the scheduler."""
    logger.info("run_job_now called | job_id=%s", job_id)
    job = db.get(JobModel, job_id)
    if job is None:
        logger.warning("run_job_now missing | job_id=%s", job_id)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    run = run_job_immediately(db, job_id=job.id, triggered_by="manual_api")
    logger.info(
        "run_job_now dispatched | job_id=%s run_id=%s status=%s",
        job.id,
        getattr(run, "id", None),
        getattr(run, "status", None),
    )
    return run


