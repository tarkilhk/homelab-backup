from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.domain.enums import RunOperation, TargetRunOperation

if TYPE_CHECKING:
    from .jobs import Job
    from .targets import Target


class Run(Base):
    """Run model representing individual backup job executions or restore operations."""

    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("jobs.id"), nullable=True, index=True)
    started_at: Mapped[datetime] = mapped_column(
        nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    operation: Mapped[str] = mapped_column(
        String(20), nullable=False, default=RunOperation.BACKUP.value, index=True
    )
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    logs_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Relationships
    job: Mapped[Job | None] = relationship(back_populates="runs")
    target_runs: Mapped[list[TargetRun]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        """String representation of the Run model."""
        return f"<Run(id={self.id}, job_id={self.job_id}, status='{self.status}', started_at={self.started_at})>"


class TargetRun(Base):
    __tablename__ = "target_runs"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    target_id: Mapped[int] = mapped_column(
        ForeignKey("targets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    started_at: Mapped[datetime] = mapped_column(
        nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    operation: Mapped[str] = mapped_column(
        String(20), nullable=False, default=TargetRunOperation.BACKUP.value, index=True
    )
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    artifact_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    artifact_bytes: Mapped[int | None] = mapped_column(nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_identity_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    logs_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    run: Mapped[Run] = relationship(back_populates="target_runs")
    target: Mapped[Target] = relationship()

    def __repr__(self) -> str:
        return f"<TargetRun(id={self.id}, run_id={self.run_id}, target_id={self.target_id}, status='{self.status}')>"
