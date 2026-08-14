"""Global application settings model (singleton pattern)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base

from .common import _utcnow


class Settings(Base):
    """Singleton settings row storing global configuration like retention policy.

    This table should contain exactly one row with id=1.
    """

    __tablename__ = "settings"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    global_retention_policy_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(nullable=False, default=_utcnow, onupdate=_utcnow)

    def __repr__(self) -> str:
        return f"<Settings(id={self.id})>"
