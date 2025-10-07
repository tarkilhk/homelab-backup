from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from sqlalchemy.orm import Session, joinedload

from app.domain.enums import RunOperation
from app.models import (
    Run as RunModel,
    Job as JobModel,
    TargetTag as TargetTagModel,
    TargetRun as TargetRunModel,
)


def _assign_display_fields(run: RunModel) -> None:
    job_name = run.job.name if getattr(run, "job", None) else f"Job #{run.job_id}"
    tag_name = None
    if getattr(run, "job", None) and getattr(run.job, "tag", None):
        tag_name = run.job.tag.display_name

    display_job_name = job_name
    display_tag_name = tag_name

    if run.operation == RunOperation.RESTORE.value:
        destination_target = None
        for target_run in getattr(run, "target_runs", []) or []:
            destination_target = getattr(target_run, "target", None)
            if destination_target is not None:
                break
        if destination_target is not None:
            display_job_name = f"{destination_target.name} Restore"
            display_tag_name = destination_target.name
        else:
            display_job_name = "Target Restore"
    run.display_job_name = display_job_name
    run.display_tag_name = display_tag_name


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
            .options(
                joinedload(RunModel.job).joinedload(JobModel.tag),
                joinedload(RunModel.target_runs).joinedload(TargetRunModel.target),
            )
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
        runs = list(query.all())
        for run in runs:
            _assign_display_fields(run)
        return runs

    def get(self, run_id: int) -> Optional[RunModel]:
        run = (
            self.db.query(RunModel)
            .options(
                joinedload(RunModel.job).joinedload(JobModel.tag),
                joinedload(RunModel.target_runs).joinedload(TargetRunModel.target),
            )
            .filter(RunModel.id == run_id)
            .first()
        )
        if run is not None:
            _assign_display_fields(run)
        return run
