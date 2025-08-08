"""Tests for Pydantic schemas (plugin-first)."""

import pytest
from datetime import datetime, timezone
from pydantic import ValidationError

from app.schemas import (
    TargetCreate, TargetUpdate, Target,
    JobCreate, JobUpdate, Job,
    RunCreate, RunUpdate, Run,
)


def test_target_create_schema() -> None:
    data = {
        "name": "Pi-hole",
        "slug": "pihole",
        "plugin_name": "pihole",
        "plugin_config_json": "{}",
    }
    target = TargetCreate(**data)
    assert target.name == "Pi-hole"
    assert target.plugin_name == "pihole"


def test_target_update_schema() -> None:
    update = TargetUpdate(name="New Name")
    assert update.name == "New Name"
    assert update.plugin_name is None


def test_target_response_schema() -> None:
    now = datetime.now(timezone.utc)
    data = {
        "id": 1,
        "name": "Pi-hole",
        "slug": "pihole",
        "plugin_name": "pihole",
        "plugin_config_json": "{}",
        "created_at": now,
        "updated_at": now,
    }
    target = Target(**data)
    assert target.id == 1
    assert target.created_at == now


def test_job_create_schema() -> None:
    data = {
        "target_id": 1,
        "name": "Daily Backup",
        "schedule_cron": "0 2 * * *",
        "enabled": "true",
    }
    job = JobCreate(**data)
    assert job.enabled == "true"


def test_job_update_schema() -> None:
    update = JobUpdate(name="Weekly Backup", schedule_cron="0 2 * * 0", enabled="false")
    assert update.name == "Weekly Backup"
    assert update.schedule_cron == "0 2 * * 0"
    assert update.enabled == "false"


def test_job_response_schema() -> None:
    now = datetime.now(timezone.utc)
    data = {
        "id": 1,
        "target_id": 1,
        "name": "Daily Backup",
        "schedule_cron": "0 2 * * *",
        "enabled": "true",
        "created_at": now,
        "updated_at": now,
    }
    job = Job(**data)
    assert job.id == 1


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


def test_invalid_data_validation() -> None:
    with pytest.raises(ValidationError):
        # Missing plugin fields should raise in TargetCreate
        TargetCreate(name="X")  # type: ignore[call-arg]

    with pytest.raises(ValidationError):
        RunCreate(job_id="not_int", status="running")  # type: ignore[arg-type]
