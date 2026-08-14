from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Boolean, ForeignKey, String, Text, event
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Mapped, Mapper, mapped_column, relationship

from app.core.db import Base

from .common import _utcnow, validate_cron_expression


class MaintenanceJob(Base):
    """MaintenanceJob model representing scheduled maintenance tasks."""

    __tablename__ = "maintenance_jobs"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    key: Mapped[str] = mapped_column(String(100), nullable=False, unique=True, index=True)
    job_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    schedule_cron: Mapped[str] = mapped_column(String(100), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    config_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    visible_in_ui: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(nullable=False, default=_utcnow, onupdate=_utcnow)

    # Relationships
    runs: Mapped[list[MaintenanceRun]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<MaintenanceJob(id={self.id}, key='{self.key}', job_type='{self.job_type}', enabled={self.enabled})>"


@event.listens_for(MaintenanceJob, "before_insert")
@event.listens_for(MaintenanceJob, "before_update")
def _validate_maintenance_job_cron(
    mapper: Mapper[Any], connection: Connection, job: MaintenanceJob
) -> None:  # pragma: no cover
    job.schedule_cron = validate_cron_expression(job.schedule_cron)


class MaintenanceRun(Base):
    """MaintenanceRun model representing individual maintenance job executions."""

    __tablename__ = "maintenance_runs"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    maintenance_job_id: Mapped[int] = mapped_column(
        ForeignKey("maintenance_jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    started_at: Mapped[datetime] = mapped_column(
        nullable=False, default=lambda: datetime.now(timezone.utc), index=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Relationships
    job: Mapped[MaintenanceJob] = relationship(back_populates="runs")

    def __repr__(self) -> str:
        return f"<MaintenanceRun(id={self.id}, maintenance_job_id={self.maintenance_job_id}, status='{self.status}', started_at={self.started_at})>"
