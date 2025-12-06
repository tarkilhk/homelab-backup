"""Schemas for backups from disk."""

from pydantic import BaseModel, Field


class BackupFromDiskResponse(BaseModel):
    """Response schema for a backup artifact found on disk."""

    artifact_path: str = Field(..., description="Full path to the backup artifact file")
    target_slug: str | None = Field(None, description="Target slug inferred from path or sidecar")
    date: str | None = Field(None, description="Date (YYYY-MM-DD) inferred from path or sidecar")
    plugin_name: str | None = Field(None, description="Plugin name from sidecar or inferred from filename")
    file_size: int = Field(..., description="File size in bytes")
    modified_at: str = Field(..., description="File modification timestamp (ISO format)")
    metadata_source: str = Field(..., description="Source of metadata: 'sidecar' or 'inferred'")


