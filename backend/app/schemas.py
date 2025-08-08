"""Pydantic schemas for the homelab backup system."""

from datetime import datetime, timezone
from typing import List, Optional

from pydantic import BaseModel, Field, ConfigDict, model_validator


# Target schemas
class TargetBase(BaseModel):
    """Base schema for Target model (plugin-based only)."""

    name: str = Field(..., description="Human-readable name for the target")
    slug: Optional[str] = Field(
        None, description="URL-friendly identifier (auto-generated if omitted)"
    )
    # Plugin-first (required)
    plugin_name: Optional[str] = Field(
        None, description="Plugin key, e.g., 'pihole'"
    )
    plugin_config_json: Optional[str] = Field(
        None, description="Plugin configuration JSON"
    )

    @model_validator(mode="after")
    def _validate_mode(self) -> "TargetBase":
        # Enforce plugin-based creation only
        if not self.plugin_name:
            raise ValueError("plugin_name is required")
        if not self.plugin_config_json:
            raise ValueError("plugin_config_json is required when plugin_name is provided")
        return self


class TargetCreate(TargetBase):
    """Schema for creating a new Target."""
    pass


class TargetUpdate(BaseModel):
    """Schema for updating a Target."""

    name: Optional[str] = Field(
        None, description="Human-readable name for the target"
    )
    slug: Optional[str] = Field(
        None, description="URL-friendly identifier for the target"
    )
    plugin_name: Optional[str] = Field(
        None, description="Plugin key, e.g., 'pihole'"
    )
    plugin_config_json: Optional[str] = Field(
        None, description="Plugin configuration JSON"
    )


class Target(BaseModel):
    """Schema for Target responses (no legacy fields)."""

    id: int = Field(..., description="Unique identifier")
    name: str
    slug: str
    plugin_name: Optional[str] = None
    plugin_config_json: Optional[str] = None
    created_at: datetime = Field(..., description="Creation timestamp")
    updated_at: datetime = Field(
        ..., description="Last update timestamp"
    )

    model_config = ConfigDict(from_attributes=True)


# Job schemas
class JobBase(BaseModel):
    """Base schema for Job model."""
    
    target_id: int = Field(..., description="ID of the associated target")
    name: str = Field(..., description="Human-readable name for the job")
    schedule_cron: str = Field(..., description="Cron expression for job scheduling")
    enabled: str = Field(default="true", description="Whether the job is enabled ('true'/'false')")


class JobCreate(JobBase):
    """Schema for creating a new Job."""
    pass


class JobUpdate(BaseModel):
    """Schema for updating a Job."""
    
    target_id: Optional[int] = Field(None, description="ID of the associated target")
    name: Optional[str] = Field(None, description="Human-readable name for the job")
    schedule_cron: Optional[str] = Field(None, description="Cron expression for job scheduling")
    enabled: Optional[str] = Field(None, description="Whether the job is enabled")


class Job(JobBase):
    """Schema for Job responses."""
    
    id: int = Field(..., description="Unique identifier")
    created_at: datetime = Field(..., description="Creation timestamp")
    updated_at: datetime = Field(..., description="Last update timestamp")
    
    model_config = ConfigDict(from_attributes=True)


# Run schemas
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


# Response schemas with relationships
class TargetWithJobs(Target):
    """Schema for Target with related Jobs."""
    
    jobs: List[Job] = Field(default_factory=list, description="List of associated jobs")


class JobWithTarget(Job):
    """Schema for Job with related Target."""
    
    target: Target = Field(..., description="Associated target")


class JobWithRuns(Job):
    """Schema for Job with related Runs."""
    
    runs: List[Run] = Field(default_factory=list, description="List of associated runs")


class RunWithJob(Run):
    """Schema for Run with related Job."""
    
    job: Job = Field(..., description="Associated job")
