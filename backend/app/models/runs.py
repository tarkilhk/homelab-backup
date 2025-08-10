from __future__ import annotations

from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey
from sqlalchemy.orm import relationship

from app.core.db import Base


class Run(Base):
    """Run model representing individual backup job executions."""

    __tablename__ = "runs"

    id = Column(Integer, primary_key=True, index=True)
    job_id = Column(Integer, ForeignKey("jobs.id"), nullable=False, index=True)
    started_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    finished_at = Column(DateTime, nullable=True)
    status = Column(String(20), nullable=False, index=True)  # "running", "success", "failed"
    message = Column(Text, nullable=True)  # Error message or success message
    artifact_path = Column(String(500), nullable=True)  # Path to backup artifact
    artifact_bytes = Column(Integer, nullable=True)  # Size in bytes
    sha256 = Column(String(64), nullable=True)  # SHA256 hash of artifact
    logs_text = Column(Text, nullable=True)  # Log output from the backup process

    # Relationships
    job = relationship("Job", back_populates="runs")
    target_runs = relationship("TargetRun", back_populates="run", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        """String representation of the Run model."""
        return f"<Run(id={self.id}, job_id={self.job_id}, status='{self.status}', started_at={self.started_at})>"


class TargetRun(Base):
    __tablename__ = "target_runs"

    id = Column(Integer, primary_key=True, index=True)
    run_id = Column(Integer, ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True)
    target_id = Column(Integer, ForeignKey("targets.id", ondelete="CASCADE"), nullable=False, index=True)
    started_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    finished_at = Column(DateTime, nullable=True)
    status = Column(String(20), nullable=False, index=True)
    message = Column(Text, nullable=True)
    artifact_path = Column(String(500), nullable=True)
    artifact_bytes = Column(Integer, nullable=True)
    sha256 = Column(String(64), nullable=True)
    logs_text = Column(Text, nullable=True)

    run = relationship("Run", back_populates="target_runs")
    target = relationship("Target")

    def __repr__(self) -> str:
        return f"<TargetRun(id={self.id}, run_id={self.run_id}, target_id={self.target_id}, status='{self.status}')>"


