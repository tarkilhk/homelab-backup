"""Settings API router for global application configuration."""

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.db import get_session
from app.domain.enums import RunStatus
from app.models import MaintenanceJob as MaintenanceJobModel
from app.models import MaintenanceRun as MaintenanceRunModel
from app.models import Settings as SettingsModel
from app.schemas import Settings, SettingsUpdate
from app.services import RetentionService
from app.services.maintenance import MaintenanceService

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
    job_id: int | None = None,
    target_id: int | None = None,
    db: Session = Depends(get_session),
) -> dict:
    """Preview what retention would delete for one pair or globally.

    Returns counts and paths without actually deleting anything (dry run).
    """
    if (job_id is None) != (target_id is None):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="job_id and target_id must be provided together",
        )

    svc = RetentionService(db)
    if job_id is None or target_id is None:
        return svc.apply_all(dry_run=True)
    return svc.preview(job_id, target_id)


@router.post("/retention/run")
def run_retention_cleanup(
    job_id: int | None = None,
    target_id: int | None = None,
    confirmed: bool = False,
    db: Session = Depends(get_session),
) -> dict:
    """Manually trigger retention cleanup.

    If job_id and target_id are provided, cleans up only that pair (legacy behavior).
    Otherwise, runs cleanup for all job/target pairs and creates a MaintenanceRun record.
    """
    if not confirmed:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Retention cleanup requires confirmed=true after reviewing a preview",
        )
    if (job_id is None) != (target_id is None):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="job_id and target_id must be provided together",
        )

    if job_id is not None and target_id is not None:
        # Legacy behavior: specific job/target pair
        svc = RetentionService(db)
        return svc.apply_for_job_target(job_id, target_id)
    else:
        # New behavior: all pairs, tracked via MaintenanceRun
        maintenance_svc = MaintenanceService(db)
        retention_svc = RetentionService(db)

        # Lookup the hidden manual retention job
        job = maintenance_svc.get_job_by_key("retention_cleanup_manual")
        if job is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Maintenance job 'retention_cleanup_manual' not found. Please run migrations.",
            )

        # Create MaintenanceRun in running state
        started_at = datetime.now(timezone.utc)
        run = MaintenanceRunModel(
            maintenance_job_id=job.id,
            started_at=started_at,
            status=RunStatus.RUNNING.value,
            message="Manual retention cleanup started",
        )
        db.add(run)
        db.commit()
        db.refresh(run)

        try:
            # Execute retention cleanup
            result = retention_svc.apply_all()

            failed_count = result.get("failed_count", 0)
            run.finished_at = datetime.now(timezone.utc)
            run.status = RunStatus.FAILED.value if failed_count else RunStatus.SUCCESS.value
            run.message = (
                f"Retention cleanup completed: {result.get('targets_processed', 0)} "
                f"targets processed, {failed_count} deletion failures"
            )
            run.result_json = json.dumps(
                {
                    "targets_processed": result.get("targets_processed", 0),
                    "deleted_count": result.get("delete_count", 0),
                    "kept_count": result.get("keep_count", 0),
                    "deleted_paths": result.get("deleted_paths", []),
                    "failed_count": failed_count,
                    "failed_paths": result.get("failed_paths", []),
                }
            )
            db.add(run)
            db.commit()

            return result
        except Exception as exc:
            # Update run with failure
            run.finished_at = datetime.now(timezone.utc)
            run.status = RunStatus.FAILED.value
            run.message = f"Retention cleanup failed: {str(exc)}"
            run.result_json = json.dumps(
                {
                    "error": str(exc),
                }
            )
            db.add(run)
            db.commit()
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Retention cleanup failed: {str(exc)}",
            )
