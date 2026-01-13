"""Settings API router for global application configuration."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.db import get_session
from app.models import Settings as SettingsModel
from app.schemas import Settings, SettingsUpdate
from app.services import RetentionService


router = APIRouter(prefix="/settings", tags=["settings"])


def _get_or_create_settings(db: Session) -> SettingsModel:
    """Get the singleton settings row, creating it if needed."""
    settings = db.query(SettingsModel).filter(SettingsModel.id == 1).first()
    if settings is None:
        settings = SettingsModel(id=1)
        db.add(settings)
        db.commit()
        db.refresh(settings)
    return settings


@router.get("/", response_model=Settings)
def get_settings(db: Session = Depends(get_session)) -> SettingsModel:
    """Get global application settings including retention policy."""
    return _get_or_create_settings(db)


@router.put("/", response_model=Settings)
def update_settings(
    payload: SettingsUpdate,
    db: Session = Depends(get_session),
) -> SettingsModel:
    """Update global application settings."""
    settings = _get_or_create_settings(db)
    
    update_data = payload.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(settings, key, value)
    
    db.add(settings)
    db.commit()
    db.refresh(settings)
    return settings


@router.post("/retention/preview")
def preview_retention(
    job_id: int,
    target_id: int,
    db: Session = Depends(get_session),
) -> dict:
    """Preview what retention would delete for a specific job/target pair.
    
    Returns counts and paths without actually deleting anything (dry run).
    """
    svc = RetentionService(db)
    return svc.preview(job_id, target_id)


@router.post("/retention/run")
def run_retention_cleanup(
    job_id: int | None = None,
    target_id: int | None = None,
    db: Session = Depends(get_session),
) -> dict:
    """Manually trigger retention cleanup.
    
    If job_id and target_id are provided, cleans up only that pair.
    Otherwise, runs cleanup for all job/target pairs.
    """
    svc = RetentionService(db)
    
    if job_id is not None and target_id is not None:
        return svc.apply_for_job_target(job_id, target_id)
    else:
        return svc.apply_all()
