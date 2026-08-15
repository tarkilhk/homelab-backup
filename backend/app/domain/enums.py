from __future__ import annotations

from enum import Enum


class RunStatus(str, Enum):
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    PARTIAL = "partial"
    SKIPPED = "skipped"


class RunOperation(str, Enum):
    BACKUP = "backup"
    RESTORE = "restore"


class TargetRunStatus(str, Enum):
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    PARTIAL = "partial"
    SKIPPED = "skipped"


class TargetRunOperation(str, Enum):
    BACKUP = "backup"
    RESTORE = "restore"


class ProtectionGapReason(str, Enum):
    NOT_SCHEDULED = "not_scheduled"
    NEVER_SUCCEEDED = "never_succeeded"
    SCHEDULED_BACKUP_MISSING = "scheduled_backup_missing"


class MaintenanceJobType(str, Enum):
    RETENTION_CLEANUP = "retention_cleanup"
