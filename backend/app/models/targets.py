from __future__ import annotations

from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, Index, event
from sqlalchemy.orm import relationship

from app.core.db import Base
from .common import _utcnow, slugify


class Target(Base):
    __tablename__ = "targets"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False, unique=True, index=True)
    slug = Column(String(100), nullable=False, unique=True, index=True)
    plugin_name = Column(String(100), nullable=True, index=True)
    plugin_config_json = Column(Text, nullable=True)
    group_id = Column(Integer, ForeignKey("groups.id", ondelete="SET NULL"), nullable=True, index=True)
    created_at = Column(DateTime, nullable=False, default=_utcnow)
    updated_at = Column(DateTime, nullable=False, default=_utcnow, onupdate=_utcnow)

    group = relationship("Group", back_populates="targets")
    target_tags = relationship("TargetTag", back_populates="target", cascade="all, delete-orphan")

    def __repr__(self) -> str:  # noqa: D401
        return f"<Target(id={self.id}, name='{self.name}', slug='{self.slug}', plugin='{self.plugin_name}')>"


@event.listens_for(Target, "before_insert")
def _target_before_insert(mapper, connection, target: Target) -> None:  # pragma: no cover
    if not target.slug:
        target.slug = slugify(target.name)


@event.listens_for(Target, "before_update")
def _target_before_update(mapper, connection, target: Target) -> None:  # pragma: no cover
    state = connection.execute(
        mapper.local_table.select().where(mapper.local_table.c.id == target.id)
    ).mappings().first()
    if state is not None:
        existing_slug = state.get("slug")
        if target.slug != existing_slug:
            target.slug = existing_slug


# Indexes
Index("idx_targets_group_id", Target.group_id)


