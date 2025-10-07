"""Jobs API router."""

from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
import logging
from sqlalchemy.orm import Session

from app.core.db import get_session
from app.models import Job as JobModel, Run as RunModel
from app.schemas import Job, JobCreate, JobUpdate, Run, UpcomingJob
from app.services import JobService, RunService
from app.services.jobs import run_job_for_tag
from app.models import Job as JobModel
from sqlalchemy.orm import Session


router = APIRouter(prefix="/jobs", tags=["jobs"])
logger = logging.getLogger(__name__)


@router.get("/", response_model=List[Job])
def list_jobs(db: Session = Depends(get_session)) -> List[JobModel]:
    svc = JobService(db)
    return svc.list()


# NOTE: Define static paths before dynamic `/{job_id}` to avoid path conflicts
@router.get("/upcoming", response_model=list[UpcomingJob])
def upcoming_jobs(db: Session = Depends(get_session)) -> list[UpcomingJob]:
    svc = JobService(db)
    return svc.upcoming(limit=10)


@router.post("/", response_model=Job, status_code=status.HTTP_201_CREATED)
def create_job(payload: JobCreate, db: Session = Depends(get_session)) -> JobModel:
    svc = JobService(db)
    return svc.create(
        tag_id=payload.tag_id,
        name=payload.name,
        schedule_cron=payload.schedule_cron,
        enabled=payload.enabled,
    )


@router.get("/{job_id}", response_model=Job)
def get_job(job_id: int, db: Session = Depends(get_session)) -> JobModel:
    svc = JobService(db)
    job = svc.get(job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    return job


@router.put("/{job_id}", response_model=Job)
def update_job(job_id: int, payload: JobUpdate, db: Session = Depends(get_session)) -> JobModel:
    svc = JobService(db)
    update_data = payload.model_dump(exclude_unset=True)
    try:
        return svc.update(job_id, **update_data)
    except KeyError as exc:
        key = str(exc).strip("'")
        if key == "job_not_found":
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
        if key == "tag_not_found":
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tag not found")
        raise


@router.delete("/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_job(job_id: int, db: Session = Depends(get_session)) -> None:
    svc = JobService(db)
    try:
        svc.delete(job_id)
    except KeyError as exc:
        if str(exc).strip("'") == "job_not_found":
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
        raise
    return None


@router.post("/{job_id}/run", response_model=Run)
def run_job_now(job_id: int, db: Session = Depends(get_session)) -> RunModel:
    logger.info("run_job_now called | job_id=%s", job_id)
    svc = JobService(db)
    try:
        run_model = svc.run_now(job_id, triggered_by="manual_api")
    except ValueError:
        logger.warning("run_job_now missing | job_id=%s", job_id)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    run = RunService(db).get(run_model.id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    logger.info(
        "run_job_now dispatched | job_id=%s run_id=%s status=%s",
        job_id,
        getattr(run, "id", None),
        getattr(run, "status", None),
    )
    return run


@router.post("/by-tag/{tag_id}/run", response_model=List[dict])
def run_jobs_by_tag(tag_id: int, db: Session = Depends(get_session)) -> List[dict]:
    # Find jobs referencing this tag and run across resolved targets.
    jobs = db.query(JobModel).filter(JobModel.tag_id == tag_id).all()
    results: list[dict] = []
    for job in jobs:
        summary = run_job_for_tag(db, job_id=job.id, tag_id=tag_id, runner=lambda t: None)
        # Minimal response per TDD: list of per-target results
        results.extend(summary.get("results", []))
    return results
