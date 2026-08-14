from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import ForeignKey, Index, String, Text, event
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Mapped, Mapper, mapped_column, relationship

from app.core.db import Base

from .common import _utcnow, slugify

if TYPE_CHECKING:
    from .groups import Group
    from .tags import TargetTag


class Target(Base):
    __tablename__ = "targets"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    slug: Mapped[str] = mapped_column(String(100), nullable=False, unique=True, index=True)
    plugin_name: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    plugin_config_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    group_id: Mapped[int | None] = mapped_column(
        ForeignKey("groups.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(nullable=False, default=_utcnow, onupdate=_utcnow)

    group: Mapped[Group | None] = relationship(back_populates="targets")
    target_tags: Mapped[list[TargetTag]] = relationship(
        back_populates="target", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:  # noqa: D401
        return f"<Target(id={self.id}, name='{self.name}', slug='{self.slug}', plugin='{self.plugin_name}')>"


@event.listens_for(Target, "before_insert")
def _target_before_insert(
    mapper: Mapper[Any], connection: Connection, target: Target
) -> None:  # pragma: no cover
    if not target.slug:
        target.slug = slugify(target.name)


@event.listens_for(Target, "before_update")
def _target_before_update(
    mapper: Mapper[Any], connection: Connection, target: Target
) -> None:  # pragma: no cover
    state = (
        connection.execute(mapper.local_table.select().where(mapper.local_table.c.id == target.id))
        .mappings()
        .first()
    )
    if state is not None:
        existing_slug = state.get("slug")
        if isinstance(existing_slug, str) and target.slug != existing_slug:
            target.slug = existing_slug


# Indexes
Index("idx_targets_group_id", Target.group_id)
