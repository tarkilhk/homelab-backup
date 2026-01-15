from __future__ import annotations

from typing import List, Optional
from sqlalchemy.orm import Session

from app.models import MaintenanceJob as MaintenanceJobModel, MaintenanceRun as MaintenanceRunModel


class MaintenanceService:
    """Business logic for MaintenanceJobs and MaintenanceRuns.
    
    Provides CRUD convenience for maintenance job definitions and execution history.
    """

    def __init__(self, db: Session) -> None:
        self.db = db

    def list_jobs(self, visible_in_ui: Optional[bool] = None) -> List[MaintenanceJobModel]:
        """List maintenance jobs, optionally filtered by visible_in_ui."""
        q = self.db.query(MaintenanceJobModel)
        if visible_in_ui is not None:
            q = q.filter(MaintenanceJobModel.visible_in_ui == visible_in_ui)
        return list(q.all())

    def get_job(self, job_id: int) -> Optional[MaintenanceJobModel]:
        """Get a maintenance job by ID."""
        return self.db.get(MaintenanceJobModel, job_id)

    def get_job_by_key(self, key: str) -> Optional[MaintenanceJobModel]:
        """Get a maintenance job by its deterministic key."""
        return self.db.query(MaintenanceJobModel).filter(MaintenanceJobModel.key == key).first()

    def list_runs(self, limit: Optional[int] = None) -> List[MaintenanceRunModel]:
        """List maintenance runs, sorted by most recent first."""
        q = self.db.query(MaintenanceRunModel).order_by(MaintenanceRunModel.started_at.desc())
        if limit is not None:
            q = q.limit(limit)
        return list(q.all())

    def get_run(self, run_id: int) -> Optional[MaintenanceRunModel]:
        """Get a maintenance run by ID."""
        return self.db.get(MaintenanceRunModel, run_id)
