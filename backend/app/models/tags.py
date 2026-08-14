from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    event,
)
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Mapped, Mapper, mapped_column, relationship, validates

from app.core.db import Base

from .common import ValidationError422, _utcnow

if TYPE_CHECKING:
    from .groups import Group
    from .jobs import Job
    from .targets import Target


class Tag(Base):
    __tablename__ = "tags"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    slug: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(nullable=False, default=_utcnow, onupdate=_utcnow)

    group_tags: Mapped[list[GroupTag]] = relationship(
        back_populates="tag", cascade="all, delete-orphan"
    )
    target_tags: Mapped[list[TargetTag]] = relationship(
        back_populates="tag", cascade="all, delete-orphan"
    )
    jobs: Mapped[list[Job]] = relationship(back_populates="tag", cascade="all, delete-orphan")

    @validates("display_name")
    def _validate_and_sync_names(self, key: str, value: str) -> str:  # noqa: D401
        from .common import slugify

        if value is None or value.strip() == "":
            raise ValidationError422("Tag name cannot be empty")
        self.slug = slugify(value)
        return value


class GroupTag(Base):
    __tablename__ = "group_tags"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    group_id: Mapped[int] = mapped_column(
        ForeignKey("groups.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tag_id: Mapped[int] = mapped_column(
        ForeignKey("tags.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(nullable=False, default=_utcnow)

    group: Mapped[Group] = relationship(back_populates="group_tags")
    tag: Mapped[Tag] = relationship(back_populates="group_tags")

    __table_args__ = (UniqueConstraint("group_id", "tag_id", name="ux_group_tags_group_tag"),)


class TargetTag(Base):
    __tablename__ = "target_tags"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    target_id: Mapped[int] = mapped_column(
        ForeignKey("targets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tag_id: Mapped[int] = mapped_column(
        ForeignKey("tags.id", ondelete="CASCADE"), nullable=False, index=True
    )
    origin: Mapped[str] = mapped_column(String(10), nullable=False)
    source_group_id: Mapped[int | None] = mapped_column(
        ForeignKey("groups.id", ondelete="CASCADE"), nullable=True
    )
    is_auto_tag: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False, default=_utcnow)

    target: Mapped[Target] = relationship(back_populates="target_tags")
    tag: Mapped[Tag] = relationship(back_populates="target_tags")
    source_group: Mapped[Group | None] = relationship()

    __table_args__ = (
        UniqueConstraint("target_id", "tag_id", "origin", name="ux_target_tags_target_tag_origin"),
        CheckConstraint("origin IN ('AUTO','DIRECT','GROUP')", name="ck_target_tags_origin"),
        Index("idx_target_tags_tag", "tag_id"),
        Index("idx_target_tags_target", "target_id"),
    )


@event.listens_for(TargetTag, "before_insert")
@event.listens_for(TargetTag, "before_update")
def _validate_target_tag(
    mapper: Mapper[Any], connection: Connection, tt: TargetTag
) -> None:  # pragma: no cover
    origin = (tt.origin or "").strip().upper()
    if origin not in {"AUTO", "DIRECT", "GROUP"}:
        raise ValidationError422("Invalid origin")
    tt.origin = origin
    if origin == "GROUP":
        if tt.source_group_id is None:
            raise ValidationError422("GROUP origin requires source_group_id")
    else:
        if tt.source_group_id is not None:
            raise ValidationError422("AUTO/DIRECT must not have source_group_id")


# Global indexes for Tag
Index("ux_tags_slug", Tag.slug, unique=True)
