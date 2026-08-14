"""Schemas for application settings (global retention policy, etc.)."""

from __future__ import annotations

from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class RetentionRule(BaseModel):
    """A single retention rule specifying how many backups to keep per time unit."""

    unit: Literal["day", "week", "month", "year"] = Field(
        ...,
        description="Time unit: 'day', 'week', 'month', or 'year'",
    )
    window: int = Field(
        ..., ge=1, description="How many units back to consider (e.g., 7 for last 7 days)"
    )
    keep: int = Field(
        default=1, ge=1, description="How many backups to keep per bucket (usually 1)"
    )

    model_config = ConfigDict(extra="forbid")


class RetentionPolicy(BaseModel):
    """Retention policy containing multiple rules."""

    rules: List[RetentionRule] = Field(..., description="List of retention rules")

    model_config = ConfigDict(extra="forbid")


def normalize_retention_policy_json(value: Optional[str]) -> Optional[str]:
    """Validate and canonically serialize a policy stored as JSON text."""

    if value is None:
        return None
    policy = RetentionPolicy.model_validate_json(value)
    return policy.model_dump_json()


class SettingsBase(BaseModel):
    """Base schema for Settings."""

    global_retention_policy_json: Optional[str] = Field(
        None, description="Global retention policy as JSON string"
    )

    _normalize_policy = field_validator("global_retention_policy_json")(
        normalize_retention_policy_json
    )


class SettingsUpdate(BaseModel):
    """Schema for updating Settings."""

    global_retention_policy_json: Optional[str] = Field(
        None, description="Global retention policy as JSON string"
    )

    _normalize_policy = field_validator("global_retention_policy_json")(
        normalize_retention_policy_json
    )


class Settings(SettingsBase):
    """Schema for Settings responses."""

    id: int = Field(..., description="Settings ID (always 1)")
    created_at: datetime = Field(..., description="Creation timestamp")
    updated_at: datetime = Field(..., description="Last update timestamp")

    model_config = ConfigDict(from_attributes=True)
