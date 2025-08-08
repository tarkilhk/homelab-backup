"""Jobs API router."""

from datetime import datetime, timezone
from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.db import get_session
from app.models import Job as JobModel, Run as RunModel
from app.schemas import Job, JobCreate, JobUpdate, Run


router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.get("/", response_model=List[Job])
def list_jobs(db: Session = Depends(get_session)) -> List[JobModel]:
    """List all jobs."""
    return db.query(JobModel).all()


@router.post("/", response_model=Job, status_code=status.HTTP_201_CREATED)
def create_job(payload: JobCreate, db: Session = Depends(get_session)) -> JobModel:
    """Create a new job."""
    job = JobModel(
        target_id=payload.target_id,
        name=payload.name,
        schedule_cron=payload.schedule_cron,
        enabled=payload.enabled,
        plugin=payload.plugin,
        plugin_version=payload.plugin_version,
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
    """Trigger a manual run for a job.

    For now: enqueue a dummy task and immediately mark as success.
    """
    job = db.get(JobModel, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")

    # Create a run with status 'success' immediately (dummy behavior)
    run = RunModel(
        job_id=job.id,
        started_at=datetime.now(timezone.utc),
        finished_at=datetime.now(timezone.utc),
        status="success",
        message="Manual run executed (dummy)",
        logs_text="Run triggered manually; dummy execution marked as success.",
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


