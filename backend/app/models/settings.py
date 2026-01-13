"""Global application settings model (singleton pattern)."""

from __future__ import annotations

from sqlalchemy import Column, Integer, Text, DateTime

from app.core.db import Base
from .common import _utcnow


class Settings(Base):
    """Singleton settings row storing global configuration like retention policy.

    This table should contain exactly one row with id=1.
    """

    __tablename__ = "settings"

    id = Column(Integer, primary_key=True, default=1)
    global_retention_policy_json = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, default=_utcnow)
    updated_at = Column(DateTime, nullable=False, default=_utcnow, onupdate=_utcnow)

    def __repr__(self) -> str:
        return f"<Settings(id={self.id})>"
