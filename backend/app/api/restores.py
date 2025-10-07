"""Restore API router."""

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.db import get_session
from app.schemas import RunWithJob
from app.services import RestoreService


class RestoreRequest(BaseModel):
    source_target_run_id: int = Field(..., description="Target run ID containing the backup artifact")
    destination_target_id: int = Field(..., description="Target to restore the artifact to")
    triggered_by: str | None = Field(None, description="Audit string for who initiated the restore")


router = APIRouter(prefix="/restores", tags=["restores"])


@router.post("/", response_model=RunWithJob, status_code=status.HTTP_201_CREATED)
def trigger_restore(payload: RestoreRequest, db: Session = Depends(get_session)) -> RunWithJob:
    svc = RestoreService(db)
    try:
        run = svc.restore(
            source_target_run_id=payload.source_target_run_id,
            destination_target_id=payload.destination_target_id,
            triggered_by=payload.triggered_by or "manual_api",
        )
    except KeyError as exc:
        key = str(exc).strip("'")
        if key == "source_target_run_not_found":
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Source target run not found")
        if key == "destination_target_not_found":
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Destination target not found")
        raise
    except ValueError as exc:
        detail_map = {
            "artifact_path_missing": "Source target run does not have an artifact",
            "artifact_path_not_found": "Artifact file not found on disk",
            "source_run_not_found": "Source run could not be loaded",
            "source_target_not_found": "Source target could not be loaded",
            "plugin_missing": "Plugin missing on source or destination target",
            "plugin_mismatch": "Source and destination targets must use the same plugin",
            "plugin_not_registered": "Plugin is not registered",
        }
        detail = detail_map.get(str(exc), str(exc))
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)
    return run
