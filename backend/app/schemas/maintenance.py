from __future__ import annotations

from typing import Optional, Dict, Any
from datetime import datetime
import json

from pydantic import BaseModel, Field, ConfigDict


class MaintenanceJobBase(BaseModel):
    """Base schema for MaintenanceJob model."""

    key: str = Field(..., description="Deterministic stable identifier")
    job_type: str = Field(..., description="Type of maintenance job (e.g., 'retention_cleanup')")
    name: str = Field(..., description="Human-readable name for the job")
    schedule_cron: str = Field(..., description="Cron expression for job scheduling")
    enabled: bool = Field(default=True, description="Whether the job is enabled")
    config_json: Optional[str] = Field(None, description="Job-specific configuration JSON")
    visible_in_ui: bool = Field(default=True, description="Whether the job should appear in UI lists")


class MaintenanceJobCreate(MaintenanceJobBase):
    """Schema for creating a new MaintenanceJob."""
    pass


class MaintenanceJobUpdate(BaseModel):
    """Schema for updating a MaintenanceJob."""

    name: Optional[str] = Field(None, description="Human-readable name for the job")
    schedule_cron: Optional[str] = Field(None, description="Cron expression for job scheduling")
    enabled: Optional[bool] = Field(None, description="Whether the job is enabled")
    config_json: Optional[str] = Field(None, description="Job-specific configuration JSON")
    visible_in_ui: Optional[bool] = Field(None, description="Whether the job should appear in UI lists")


class MaintenanceJob(BaseModel):
    """Schema for MaintenanceJob responses."""

    id: int = Field(..., description="Unique identifier")
    key: str = Field(..., description="Deterministic stable identifier")
    job_type: str = Field(..., description="Type of maintenance job")
    name: str = Field(..., description="Human-readable name for the job")
    schedule_cron: str = Field(..., description="Cron expression for job scheduling")
    enabled: bool = Field(..., description="Whether the job is enabled")
    config_json: Optional[str] = Field(None, description="Job-specific configuration JSON")
    visible_in_ui: bool = Field(..., description="Whether the job should appear in UI lists")
    created_at: datetime = Field(..., description="Creation timestamp")
    updated_at: datetime = Field(..., description="Last update timestamp")

    model_config = ConfigDict(from_attributes=True)


class MaintenanceRunResult(BaseModel):
    """Parsed result_json from MaintenanceRun."""

    targets_processed: Optional[int] = Field(None, description="Number of distinct targets processed")
    deleted_count: Optional[int] = Field(None, description="Number of backups deleted")
    kept_count: Optional[int] = Field(None, description="Number of backups kept")
    deleted_paths: Optional[list[str]] = Field(None, description="List of deleted artifact paths")
    error: Optional[str] = Field(None, description="Error message if execution failed")


class MaintenanceRun(BaseModel):
    """Schema for MaintenanceRun responses."""

    id: int = Field(..., description="Unique identifier")
    maintenance_job_id: int = Field(..., description="ID of the associated MaintenanceJob")
    started_at: datetime = Field(..., description="Start timestamp")
    finished_at: Optional[datetime] = Field(None, description="Finish timestamp")
    status: str = Field(..., description="Run status (running, success, failed)")
    message: Optional[str] = Field(None, description="Status message")
    result: Optional[MaintenanceRunResult] = Field(None, description="Parsed execution results")
    job: Optional[MaintenanceJob] = Field(None, description="Associated MaintenanceJob (if included)")

    model_config = ConfigDict(from_attributes=True)

    @classmethod
    def from_orm_with_result(cls, obj: Any) -> "MaintenanceRun":
        """Create MaintenanceRun from ORM object, parsing result_json."""
        data = {
            "id": obj.id,
            "maintenance_job_id": obj.maintenance_job_id,
            "started_at": obj.started_at,
            "finished_at": obj.finished_at,
            "status": obj.status,
            "message": obj.message,
        }
        
        # Parse result_json if present
        if obj.result_json:
            try:
                result_dict = json.loads(obj.result_json)
                data["result"] = MaintenanceRunResult(**result_dict)
            except (json.JSONDecodeError, TypeError):
                data["result"] = None
        else:
            data["result"] = None
        
        # Include job if relationship is loaded
        if hasattr(obj, "job") and obj.job:
            data["job"] = MaintenanceJob.model_validate(obj.job)
        
        return cls(**data)
