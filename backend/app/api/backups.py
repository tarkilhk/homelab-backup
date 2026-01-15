"""Backups from disk API router."""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.db import get_session
from app.schemas.backups import BackupFromDiskResponse
from app.services.backups_from_disk import BackupsFromDiskService


router = APIRouter(prefix="/backups", tags=["backups"])


@router.get("/from-disk", response_model=list[BackupFromDiskResponse])
def list_backups_from_disk(db: Session = Depends(get_session)) -> list[BackupFromDiskResponse]:
    """Scan the backup directory and return all backup artifacts found on disk.
    
    This endpoint discovers ALL backup artifacts that exist on disk, regardless of
    whether they have corresponding database records. This allows users to restore
    from any backup file available on disk, useful for both normal restore operations
    and disaster recovery scenarios where the database is lost but backup files remain intact.
    
    Returns:
        List of backup artifacts with metadata (from sidecar files when available,
        otherwise inferred from filename and path structure).
    """
    svc = BackupsFromDiskService(db)
    backups = svc.scan_backups()
    
    return [
        BackupFromDiskResponse(
            artifact_path=backup.artifact_path,
            target_slug=backup.target_slug,
            date=backup.date,
            plugin_name=backup.plugin_name,
            file_size=backup.file_size,
            modified_at=backup.modified_at,
            metadata_source=backup.metadata_source,
        )
        for backup in backups
    ]


