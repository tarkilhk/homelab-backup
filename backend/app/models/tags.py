from __future__ import annotations

from sqlalchemy import (
    Column,
    Integer,
    String,
    Text,
    DateTime,
    ForeignKey,
    Boolean,
    UniqueConstraint,
    CheckConstraint,
    Index,
    event,
)
from sqlalchemy.orm import relationship, validates

from app.core.db import Base
from .common import _utcnow, ValidationError422


class Tag(Base):
    __tablename__ = "tags"

    id = Column(Integer, primary_key=True, index=True)
    slug = Column(String(255), nullable=False, unique=True, index=True)
    display_name = Column(String(255), nullable=False)
    created_at = Column(DateTime, nullable=False, default=_utcnow)
    updated_at = Column(DateTime, nullable=False, default=_utcnow, onupdate=_utcnow)

    group_tags = relationship("GroupTag", back_populates="tag", cascade="all, delete-orphan")
    target_tags = relationship("TargetTag", back_populates="tag", cascade="all, delete-orphan")
    jobs = relationship("Job", back_populates="tag", cascade="all, delete-orphan")

    @validates("display_name")
    def _validate_and_sync_names(self, key: str, value: str) -> str:  # noqa: D401
        from .common import slugify

        if value is None or value.strip() == "":
            raise ValidationError422("Tag name cannot be empty")
        self.slug = slugify(value)
        return value


class GroupTag(Base):
    __tablename__ = "group_tags"

    id = Column(Integer, primary_key=True, index=True)
    group_id = Column(Integer, ForeignKey("groups.id", ondelete="CASCADE"), nullable=False, index=True)
    tag_id = Column(Integer, ForeignKey("tags.id", ondelete="CASCADE"), nullable=False, index=True)
    created_at = Column(DateTime, nullable=False, default=_utcnow)

    group = relationship("Group", back_populates="group_tags")
    tag = relationship("Tag", back_populates="group_tags")

    __table_args__ = (
        UniqueConstraint("group_id", "tag_id", name="ux_group_tags_group_tag"),
    )


class TargetTag(Base):
    __tablename__ = "target_tags"

    id = Column(Integer, primary_key=True, index=True)
    target_id = Column(Integer, ForeignKey("targets.id", ondelete="CASCADE"), nullable=False, index=True)
    tag_id = Column(Integer, ForeignKey("tags.id", ondelete="CASCADE"), nullable=False, index=True)
    origin = Column(String(10), nullable=False)  # 'AUTO', 'DIRECT', 'GROUP'
    source_group_id = Column(Integer, ForeignKey("groups.id", ondelete="CASCADE"), nullable=True)
    is_auto_tag = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, nullable=False, default=_utcnow)

    target = relationship("Target", back_populates="target_tags")
    tag = relationship("Tag", back_populates="target_tags")
    source_group = relationship("Group")

    __table_args__ = (
        UniqueConstraint("target_id", "tag_id", "origin", name="ux_target_tags_target_tag_origin"),
        CheckConstraint("origin IN ('AUTO','DIRECT','GROUP')", name="ck_target_tags_origin"),
        Index("idx_target_tags_tag", "tag_id"),
        Index("idx_target_tags_target", "target_id"),
    )


@event.listens_for(TargetTag, "before_insert")
@event.listens_for(TargetTag, "before_update")
def _validate_target_tag(mapper, connection, tt: TargetTag) -> None:  # pragma: no cover
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


