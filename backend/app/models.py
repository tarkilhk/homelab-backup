"""SQLAlchemy models for the homelab backup system."""

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import relationship

from app.core.db import Base


class Target(Base):
    """Target model representing backup targets."""
    
    __tablename__ = "targets"
    
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False, index=True)
    slug = Column(String(100), nullable=False, unique=True, index=True)
    # Plugin-based configuration only
    plugin_name = Column(String(100), nullable=True, index=True)
    plugin_config_json = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    
    # Relationships
    jobs = relationship("Job", back_populates="target", cascade="all, delete-orphan")
    
    def __repr__(self) -> str:
        """String representation of the Target model."""
        return f"<Target(id={self.id}, name='{self.name}', slug='{self.slug}', plugin='{self.plugin_name}')>"


class Job(Base):
    """Job model representing scheduled backup jobs."""
    
    __tablename__ = "jobs"
    
    id = Column(Integer, primary_key=True, index=True)
    target_id = Column(Integer, ForeignKey("targets.id"), nullable=False, index=True)
    name = Column(String(255), nullable=False, index=True)
    schedule_cron = Column(String(100), nullable=False)  # Cron expression
    enabled = Column(String(10), nullable=False, default="true")  # "true"/"false" string
    plugin = Column(String(100), nullable=False, index=True)
    plugin_version = Column(String(50), nullable=False)
    created_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    
    # Relationships
    target = relationship("Target", back_populates="jobs")
    runs = relationship("Run", back_populates="job", cascade="all, delete-orphan")
    
    def __repr__(self) -> str:
        """String representation of the Job model."""
        return f"<Job(id={self.id}, name='{self.name}', target_id={self.target_id}, enabled={self.enabled})>"


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
    
    def __repr__(self) -> str:
        """String representation of the Run model."""
        return f"<Run(id={self.id}, job_id={self.job_id}, status='{self.status}', started_at={self.started_at})>"
