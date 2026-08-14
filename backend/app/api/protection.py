from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.db import get_session
from app.schemas.protection import TargetProtectionSummary
from app.services.protection import ProtectionSummaryService

router = APIRouter(prefix="/protection", tags=["protection"])


@router.get("/targets", response_model=list[TargetProtectionSummary])
def list_target_protection(
    db: Session = Depends(get_session),
) -> list[TargetProtectionSummary]:
    """Return one derived protection summary for every configured target."""
    return ProtectionSummaryService(db).list_targets()
