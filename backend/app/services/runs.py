from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from sqlalchemy.orm import Session, joinedload

from app.models import Run as RunModel, Job as JobModel, TargetTag as TargetTagModel


class RunService:
    """Business logic for Runs: list with filters, get with job eager-load."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def list(
        self,
        *,
        status: Optional[str] = None,
        start_dt: Optional[datetime] = None,
        end_dt: Optional[datetime] = None,
        target_id: Optional[int] = None,
        tag_id: Optional[int] = None,
    ) -> List[RunModel]:
        query = (
            self.db.query(RunModel)
            .join(JobModel, RunModel.job_id == JobModel.id)
            .options(joinedload(RunModel.job))
        )
        if status:
            query = query.filter(RunModel.status == status)
        if start_dt:
            query = query.filter(RunModel.started_at >= start_dt)
        if end_dt:
            query = query.filter(RunModel.started_at <= end_dt)
        if target_id:
            # Filter runs whose job is associated with the target via tag linkage
            query = (
                query.join(TargetTagModel, TargetTagModel.tag_id == JobModel.tag_id)
                .filter(TargetTagModel.target_id == target_id)
            )
        if tag_id:
            query = query.filter(JobModel.tag_id == tag_id)
        query = query.order_by(RunModel.started_at.desc())
        return list(query.all())

    def get(self, run_id: int) -> Optional[RunModel]:
        return (
            self.db.query(RunModel)
            .options(joinedload(RunModel.job), joinedload(RunModel.target_runs))
            .filter(RunModel.id == run_id)
            .first()
        )


