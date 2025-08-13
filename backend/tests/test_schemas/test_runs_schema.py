from __future__ import annotations

from datetime import datetime, timezone
import pytest
from pydantic import ValidationError

from app.schemas import RunCreate, RunUpdate, Run


def test_run_create_schema() -> None:
    data = {
        "job_id": 1,
        "status": "running",
        "message": "Starting backup...",
        "artifact_path": "/backups/test.sql",
        "artifact_bytes": 1024,
        "sha256": "a" * 64,
        "logs_text": "Starting backup...\nDone",
    }
    run = RunCreate(**data)
    assert run.job_id == 1
    assert run.status == "running"


def test_run_update_schema() -> None:
    now = datetime.now(timezone.utc)
    update = RunUpdate(status="success", finished_at=now, message="ok")
    assert update.status == "success"
    assert update.finished_at == now


def test_run_response_schema() -> None:
    started_at = datetime.now(timezone.utc)
    finished_at = datetime.now(timezone.utc)
    data = {
        "id": 1,
        "job_id": 1,
        "started_at": started_at,
        "finished_at": finished_at,
        "status": "success",
        "message": "OK",
        "artifact_path": "/backups/test.sql",
        "artifact_bytes": 100,
        "sha256": "a" * 64,
        "logs_text": "log",
    }
    run = Run(**data)
    assert run.id == 1
    assert run.job_id == 1


def test_run_invalid_data_validation() -> None:
    with pytest.raises(ValidationError):
        RunCreate(job_id="not_int", status="running")  # type: ignore[arg-type]


