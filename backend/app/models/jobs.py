from __future__ import annotations

from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey, Text, event
from sqlalchemy.orm import relationship

from app.core.db import Base
from .common import _utcnow, validate_cron_expression


class Job(Base):
    """Job model representing scheduled backup jobs (tag-based)."""

    __tablename__ = "jobs"

    id = Column(Integer, primary_key=True, index=True)
    tag_id = Column(Integer, ForeignKey("tags.id", ondelete="RESTRICT"), nullable=False, index=True)
    name = Column(String(255), nullable=False, index=True)
    schedule_cron = Column(String(100), nullable=False)
    enabled = Column(Boolean, nullable=False, default=True)
    retention_policy_json = Column(Text, nullable=True)  # NULL means use global
    created_at = Column(DateTime, nullable=False, default=_utcnow)
    updated_at = Column(DateTime, nullable=False, default=_utcnow, onupdate=_utcnow)

    tag = relationship("Tag", back_populates="jobs")
    runs = relationship("Run", back_populates="job", cascade="all, delete-orphan")

    def __repr__(self) -> str:  # noqa: D401
        return f"<Job(id={self.id}, name='{self.name}', tag_id={self.tag_id}, enabled={self.enabled})>"


@event.listens_for(Job, "before_insert")
@event.listens_for(Job, "before_update")
def _validate_job_cron(mapper, connection, job: Job) -> None:  # pragma: no cover
    job.schedule_cron = validate_cron_expression(job.schedule_cron)


