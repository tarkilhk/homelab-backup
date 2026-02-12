"""Schemas for application settings (global retention policy, etc.)."""

from __future__ import annotations

from typing import Optional, List
from datetime import datetime

from pydantic import BaseModel, Field, ConfigDict


class RetentionRule(BaseModel):
    """A single retention rule specifying how many backups to keep per time unit."""

    unit: str = Field(..., description="Time unit: 'day', 'week', 'month', or 'year'")
    window: int = Field(..., ge=1, description="How many units back to consider (e.g., 7 for last 7 days)")
    keep: int = Field(default=1, ge=1, description="How many backups to keep per bucket (usually 1)")


class RetentionPolicy(BaseModel):
    """Retention policy containing multiple rules."""

    rules: List[RetentionRule] = Field(default_factory=list, description="List of retention rules")


class SettingsBase(BaseModel):
    """Base schema for Settings."""

    global_retention_policy_json: Optional[str] = Field(
        None, description="Global retention policy as JSON string"
    )


class SettingsUpdate(BaseModel):
    """Schema for updating Settings."""

    global_retention_policy_json: Optional[str] = Field(
        None, description="Global retention policy as JSON string"
    )


class Settings(SettingsBase):
    """Schema for Settings responses."""

    id: int = Field(..., description="Settings ID (always 1)")
    created_at: datetime = Field(..., description="Creation timestamp")
    updated_at: datetime = Field(..., description="Last update timestamp")

    model_config = ConfigDict(from_attributes=True)
