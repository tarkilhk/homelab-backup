"""Runs API router."""

from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.db import get_session
from app.models import Run as RunModel
from app.schemas import Run


router = APIRouter(prefix="/runs", tags=["runs"])


@router.get("/", response_model=List[Run])
def list_runs(db: Session = Depends(get_session)) -> List[RunModel]:
    """List all runs."""
    return db.query(RunModel).all()


@router.get("/{run_id}", response_model=Run)
def get_run(run_id: int, db: Session = Depends(get_session)) -> RunModel:
    """Get run by ID."""
    run = db.get(RunModel, run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    return run


