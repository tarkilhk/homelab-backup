from __future__ import annotations

from enum import Enum


class RunStatus(str, Enum):
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    PARTIAL = "partial"


class RunOperation(str, Enum):
    BACKUP = "backup"
    RESTORE = "restore"


class TargetRunStatus(str, Enum):
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"


class TargetRunOperation(str, Enum):
    BACKUP = "backup"
    RESTORE = "restore"

