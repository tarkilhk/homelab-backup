"""Maintenance API router."""

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.db import get_session
from app.models import MaintenanceJob as MaintenanceJobModel, MaintenanceRun as MaintenanceRunModel
from app.schemas.maintenance import MaintenanceJob, MaintenanceRun, MaintenanceRunResult
from app.services.maintenance import MaintenanceService
from sqlalchemy.orm import joinedload
import json


router = APIRouter(prefix="/maintenance", tags=["maintenance"])


@router.get("/jobs", response_model=List[MaintenanceJob])
def list_maintenance_jobs(
    visible_in_ui: Optional[bool] = Query(True, description="Filter by visible_in_ui"),
    db: Session = Depends(get_session),
) -> List[MaintenanceJobModel]:
    """List maintenance jobs, optionally filtered by visible_in_ui."""
    svc = MaintenanceService(db)
    return svc.list_jobs(visible_in_ui=visible_in_ui if visible_in_ui is not None else None)


@router.get("/jobs/{job_id}", response_model=MaintenanceJob)
def get_maintenance_job(job_id: int, db: Session = Depends(get_session)) -> MaintenanceJobModel:
    """Get a specific maintenance job by ID."""
    svc = MaintenanceService(db)
    job = svc.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Maintenance job not found")
    return job


@router.get("/runs", response_model=List[MaintenanceRun])
def list_maintenance_runs(
    limit: Optional[int] = Query(None, description="Limit number of results"),
    db: Session = Depends(get_session),
) -> List[MaintenanceRun]:
    """List maintenance runs, sorted by most recent first."""
    # Query with job relationship loaded
    q = db.query(MaintenanceRunModel).options(joinedload(MaintenanceRunModel.job)).order_by(MaintenanceRunModel.started_at.desc())
    if limit is not None:
        q = q.limit(limit)
    runs = list(q.all())
    
    # Convert to response models with parsed result_json
    result = []
    for run in runs:
        result.append(MaintenanceRun.from_orm_with_result(run))
    return result


@router.get("/runs/{run_id}", response_model=MaintenanceRun)
def get_maintenance_run(run_id: int, db: Session = Depends(get_session)) -> MaintenanceRun:
    """Get a specific maintenance run by ID."""
    # Query with job relationship loaded
    run = db.query(MaintenanceRunModel).options(joinedload(MaintenanceRunModel.job)).filter(MaintenanceRunModel.id == run_id).first()
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Maintenance run not found")
    return MaintenanceRun.from_orm_with_result(run)
