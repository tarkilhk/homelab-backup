"""Runs API router."""

from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session, joinedload

from app.core.db import get_session
from app.models import Job as JobModel
from app.models import Run as RunModel
from app.schemas import RunWithJob, Run as RunSchema
from app.services import RunService


router = APIRouter(prefix="/runs", tags=["runs"])


def _parse_datetime(dt_str: Optional[str]) -> Optional[datetime]:
    if not dt_str:
        return None
    # Be lenient with formatting: support unencoded '+' converted to space and 'Z' suffix
    normalized = dt_str.replace(" ", "+").replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


@router.get("/", response_model=List[RunWithJob])
def list_runs(
    db: Session = Depends(get_session),
    *,
    status: Optional[str] = Query(None, description="Filter by run status"),
    start_date: Optional[str] = Query(
        None, description="Filter runs with started_at >= this ISO timestamp"
    ),
    end_date: Optional[str] = Query(
        None, description="Filter runs with started_at <= this ISO timestamp"
    ),
    target_id: Optional[int] = Query(None, description="Filter by target ID"),
) -> List[RunModel]:
    """List runs with optional filters. Jobs are eager-loaded for display.

    The query supports filtering by status, date range, and target. Target
    filtering is achieved by joining via the related job.
    """
    svc = RunService(db)
    start_dt = _parse_datetime(start_date)
    end_dt = _parse_datetime(end_date)
    return svc.list(status=status, start_dt=start_dt, end_dt=end_dt, target_id=target_id)


@router.get("/{run_id}", response_model=RunWithJob)
def get_run(run_id: int, db: Session = Depends(get_session)) -> RunModel:
    """Get run by ID, including its associated job."""
    svc = RunService(db)
    run = svc.get(run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    return run


