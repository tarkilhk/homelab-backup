from __future__ import annotations

from typing import Optional, List
from datetime import datetime, timezone

from pydantic import BaseModel, Field, ConfigDict


class RunBase(BaseModel):
    """Base schema for Run model."""

    job_id: int = Field(..., description="ID of the associated job")
    status: str = Field(..., description="Status of the run ('running', 'success', 'failed')")
    message: Optional[str] = Field(None, description="Error message or success message")
    artifact_path: Optional[str] = Field(None, description="Path to backup artifact")
    artifact_bytes: Optional[int] = Field(None, description="Size of artifact in bytes")
    sha256: Optional[str] = Field(None, description="SHA256 hash of the artifact")
    logs_text: Optional[str] = Field(None, description="Log output from the backup process")


class RunCreate(RunBase):
    """Schema for creating a new Run."""

    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), description="Start timestamp")


class RunUpdate(BaseModel):
    """Schema for updating a Run."""

    job_id: Optional[int] = Field(None, description="ID of the associated job")
    status: Optional[str] = Field(None, description="Status of the run")
    finished_at: Optional[datetime] = Field(None, description="Completion timestamp")
    message: Optional[str] = Field(None, description="Error message or success message")
    artifact_path: Optional[str] = Field(None, description="Path to backup artifact")
    artifact_bytes: Optional[int] = Field(None, description="Size of artifact in bytes")
    sha256: Optional[str] = Field(None, description="SHA256 hash of the artifact")
    logs_text: Optional[str] = Field(None, description="Log output from the backup process")


class Run(RunBase):
    """Schema for Run responses."""

    id: int = Field(..., description="Unique identifier")
    started_at: datetime = Field(..., description="Start timestamp")
    finished_at: Optional[datetime] = Field(None, description="Completion timestamp")

    model_config = ConfigDict(from_attributes=True)


class TargetRun(BaseModel):
    """Per-target execution details for a Run."""
    id: int
    run_id: int
    target_id: int
    started_at: datetime
    finished_at: Optional[datetime] = None
    status: str
    message: Optional[str] = None
    artifact_path: Optional[str] = None
    artifact_bytes: Optional[int] = None
    sha256: Optional[str] = None
    logs_text: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class RunWithJob(Run):
    """Schema for Run with related Job."""

    job: "Job" = Field(..., description="Associated job")
    target_runs: List[TargetRun] = Field(default_factory=list, description="Per-target execution results")


