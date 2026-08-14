from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.domain.enums import ProtectionGapReason


class CoveringJobSummary(BaseModel):
    job_id: int
    name: str
    schedule_cron: str
    next_run_at: datetime


class BackupAttemptSummary(BaseModel):
    run_id: int
    target_run_id: int
    started_at: datetime
    finished_at: datetime | None
    status: str
    message: str | None


class ValidatedBackupSummary(BaseModel):
    run_id: int
    target_run_id: int
    finished_at: datetime
    artifact_path: str
    artifact_bytes: int
    sha256: str
    age_seconds: float = Field(ge=0)


class TargetProtectionSummary(BaseModel):
    target_id: int
    target_name: str
    target_slug: str
    plugin_name: str | None
    covering_jobs: list[CoveringJobSummary]
    latest_attempt: BackupAttemptSummary | None
    latest_success: ValidatedBackupSummary | None
    next_run_at: datetime | None
    consecutive_failures: int = Field(ge=0)
    gap_reason: ProtectionGapReason | None
