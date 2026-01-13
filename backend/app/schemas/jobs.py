from __future__ import annotations

from typing import Optional, List
from datetime import datetime

from pydantic import BaseModel, Field, ConfigDict


class JobBase(BaseModel):
    """Base schema for Job model (tag-based)."""

    tag_id: int = Field(..., description="ID of the associated tag")
    name: str = Field(..., description="Human-readable name for the job")
    schedule_cron: str = Field(..., description="Cron expression for job scheduling")
    enabled: bool = Field(default=True, description="Whether the job is enabled")
    retention_policy_json: Optional[str] = Field(None, description="Per-job retention policy JSON (null = use global)")


class JobCreate(JobBase):
    """Schema for creating a new Job."""
    pass


class JobUpdate(BaseModel):
    """Schema for updating a Job."""

    tag_id: Optional[int] = Field(None, description="ID of the associated tag")
    name: Optional[str] = Field(None, description="Human-readable name for the job")
    schedule_cron: Optional[str] = Field(None, description="Cron expression for job scheduling")
    enabled: Optional[bool] = Field(None, description="Whether the job is enabled")
    retention_policy_json: Optional[str] = Field(None, description="Per-job retention policy JSON (null = use global)")


class Job(BaseModel):
    """Schema for Job responses."""

    id: int = Field(..., description="Unique identifier")
    tag_id: int = Field(..., description="ID of the associated tag")
    name: str = Field(..., description="Human-readable name for the job")
    schedule_cron: str = Field(..., description="Cron expression for job scheduling")
    enabled: bool = Field(default=True, description="Whether the job is enabled")
    retention_policy_json: Optional[str] = Field(None, description="Per-job retention policy JSON (null = use global)")
    created_at: datetime = Field(..., description="Creation timestamp")
    updated_at: datetime = Field(..., description="Last update timestamp")

    model_config = ConfigDict(from_attributes=True)


class UpcomingJob(BaseModel):
    """Lightweight schema representing the next scheduled run for a job."""

    job_id: int = Field(..., description="Job ID")
    name: str = Field(..., description="Job name")
    next_run_at: datetime = Field(..., description="Next scheduled run time")

    model_config = ConfigDict(from_attributes=True)


class JobWithRuns(Job):
    """Schema for Job with related Runs."""

    runs: List["Run"] = Field(default_factory=list, description="List of associated runs")


