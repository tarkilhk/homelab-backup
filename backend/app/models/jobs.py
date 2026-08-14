from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, ForeignKey, String, Text, event
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Mapped, Mapper, mapped_column, relationship

from app.core.db import Base

from .common import _utcnow, validate_cron_expression

if TYPE_CHECKING:
    from .runs import Run
    from .tags import Tag


class Job(Base):
    """Job model representing scheduled backup jobs (tag-based)."""

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    tag_id: Mapped[int] = mapped_column(
        ForeignKey("tags.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    schedule_cron: Mapped[str] = mapped_column(String(100), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    retention_policy_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(nullable=False, default=_utcnow, onupdate=_utcnow)

    tag: Mapped[Tag] = relationship(back_populates="jobs")
    runs: Mapped[list[Run]] = relationship(back_populates="job", cascade="all, delete-orphan")

    def __repr__(self) -> str:  # noqa: D401
        return (
            f"<Job(id={self.id}, name='{self.name}', tag_id={self.tag_id}, enabled={self.enabled})>"
        )


@event.listens_for(Job, "before_insert")
@event.listens_for(Job, "before_update")
def _validate_job_cron(
    mapper: Mapper[Any], connection: Connection, job: Job
) -> None:  # pragma: no cover
    job.schedule_cron = validate_cron_expression(job.schedule_cron)
