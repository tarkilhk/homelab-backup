from __future__ import annotations

from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, Boolean, UniqueConstraint
from sqlalchemy.orm import relationship

from app.core.db import Base
from app.domain.enums import RunStatus, MaintenanceJobType
from .common import _utcnow, validate_cron_expression
from sqlalchemy import event


class MaintenanceJob(Base):
    """MaintenanceJob model representing scheduled maintenance tasks."""

    __tablename__ = "maintenance_jobs"

    id = Column(Integer, primary_key=True, index=True)
    key = Column(String(100), nullable=False, unique=True, index=True)  # Deterministic stable identifier
    job_type = Column(String(50), nullable=False, index=True)  # values in MaintenanceJobType
    name = Column(String(255), nullable=False)
    schedule_cron = Column(String(100), nullable=False)
    enabled = Column(Boolean, nullable=False, default=True, index=True)
    config_json = Column(Text, nullable=True)  # Job-specific configuration
    visible_in_ui = Column(Boolean, nullable=False, default=True, index=True)  # Hidden system jobs
    created_at = Column(DateTime, nullable=False, default=_utcnow)
    updated_at = Column(DateTime, nullable=False, default=_utcnow, onupdate=_utcnow)

    # Relationships
    runs = relationship("MaintenanceRun", back_populates="job", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<MaintenanceJob(id={self.id}, key='{self.key}', job_type='{self.job_type}', enabled={self.enabled})>"


@event.listens_for(MaintenanceJob, "before_insert")
@event.listens_for(MaintenanceJob, "before_update")
def _validate_maintenance_job_cron(mapper, connection, job: MaintenanceJob) -> None:  # pragma: no cover
    job.schedule_cron = validate_cron_expression(job.schedule_cron)


class MaintenanceRun(Base):
    """MaintenanceRun model representing individual maintenance job executions."""

    __tablename__ = "maintenance_runs"

    id = Column(Integer, primary_key=True, index=True)
    maintenance_job_id = Column(Integer, ForeignKey("maintenance_jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    started_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc), index=True)
    finished_at = Column(DateTime, nullable=True)
    status = Column(String(20), nullable=False, index=True)  # values in RunStatus
    message = Column(Text, nullable=True)  # Error message or success message
    result_json = Column(Text, nullable=True)  # JSON with execution results (stats, deleted_paths, error, etc.)

    # Relationships
    job = relationship("MaintenanceJob", back_populates="runs")

    def __repr__(self) -> str:
        return f"<MaintenanceRun(id={self.id}, maintenance_job_id={self.maintenance_job_id}, status='{self.status}', started_at={self.started_at})>"
