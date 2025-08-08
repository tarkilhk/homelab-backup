"""Tests for Pydantic schemas."""

import pytest
from datetime import datetime, timezone
from pydantic import ValidationError

from app.schemas import (
    TargetCreate, TargetUpdate, Target,
    JobCreate, JobUpdate, Job,
    RunCreate, RunUpdate, Run
)


def test_target_create_schema() -> None:
    """Test TargetCreate schema validation."""
    # Valid target creation
    target_data = {
        "name": "Test Database",
        "slug": "test-db",
        "type": "postgres",
        "config_json": '{"host": "localhost", "port": 5432, "database": "test"}'
    }
    
    target = TargetCreate(**target_data)
    assert target.name == "Test Database"
    assert target.slug == "test-db"
    assert target.type == "postgres"
    assert target.config_json == '{"host": "localhost", "port": 5432, "database": "test"}'


def test_target_update_schema() -> None:
    """Test TargetUpdate schema validation."""
    # Partial update
    update_data = {
        "name": "Updated Database",
        "type": "mysql"
    }
    
    target_update = TargetUpdate(**update_data)
    assert target_update.name == "Updated Database"
    assert target_update.type == "mysql"
    assert target_update.slug is None
    assert target_update.config_json is None


def test_target_response_schema() -> None:
    """Test Target response schema."""
    now = datetime.now(timezone.utc)
    target_data = {
        "id": 1,
        "name": "Test Database",
        "slug": "test-db",
        "type": "postgres",
        "config_json": '{"host": "localhost", "port": 5432, "database": "test"}',
        "created_at": now,
        "updated_at": now
    }
    
    target = Target(**target_data)
    assert target.id == 1
    assert target.name == "Test Database"
    assert target.created_at == now
    assert target.updated_at == now


def test_job_create_schema() -> None:
    """Test JobCreate schema validation."""
    job_data = {
        "target_id": 1,
        "name": "Daily Backup",
        "schedule_cron": "0 2 * * *",
        "enabled": "true",
        "plugin": "postgres_backup",
        "plugin_version": "1.0.0"
    }
    
    job = JobCreate(**job_data)
    assert job.target_id == 1
    assert job.name == "Daily Backup"
    assert job.schedule_cron == "0 2 * * *"
    assert job.enabled == "true"
    assert job.plugin == "postgres_backup"
    assert job.plugin_version == "1.0.0"


def test_job_update_schema() -> None:
    """Test JobUpdate schema validation."""
    update_data = {
        "name": "Weekly Backup",
        "schedule_cron": "0 2 * * 0",
        "enabled": "false"
    }
    
    job_update = JobUpdate(**update_data)
    assert job_update.name == "Weekly Backup"
    assert job_update.schedule_cron == "0 2 * * 0"
    assert job_update.enabled == "false"
    assert job_update.target_id is None
    assert job_update.plugin is None
    assert job_update.plugin_version is None


def test_job_response_schema() -> None:
    """Test Job response schema."""
    now = datetime.now(timezone.utc)
    job_data = {
        "id": 1,
        "target_id": 1,
        "name": "Daily Backup",
        "schedule_cron": "0 2 * * *",
        "enabled": "true",
        "plugin": "postgres_backup",
        "plugin_version": "1.0.0",
        "created_at": now,
        "updated_at": now
    }
    
    job = Job(**job_data)
    assert job.id == 1
    assert job.target_id == 1
    assert job.name == "Daily Backup"
    assert job.created_at == now
    assert job.updated_at == now


def test_run_create_schema() -> None:
    """Test RunCreate schema validation."""
    run_data = {
        "job_id": 1,
        "status": "running",
        "message": "Starting backup...",
        "artifact_path": "/backups/test.sql",
        "artifact_bytes": 1024000,
        "sha256": "a1b2c3d4e5f6789012345678901234567890abcdef1234567890abcdef123456",
        "logs_text": "Starting backup...\nBackup completed successfully"
    }
    
    run = RunCreate(**run_data)
    assert run.job_id == 1
    assert run.status == "running"
    assert run.message == "Starting backup..."
    assert run.artifact_path == "/backups/test.sql"
    assert run.artifact_bytes == 1024000
    assert run.sha256 == "a1b2c3d4e5f6789012345678901234567890abcdef1234567890abcdef123456"
    assert run.logs_text == "Starting backup...\nBackup completed successfully"
    assert run.started_at is not None


def test_run_update_schema() -> None:
    """Test RunUpdate schema validation."""
    now = datetime.now(timezone.utc)
    update_data = {
        "status": "success",
        "finished_at": now,
        "message": "Backup completed successfully"
    }
    
    run_update = RunUpdate(**update_data)
    assert run_update.status == "success"
    assert run_update.finished_at == now
    assert run_update.message == "Backup completed successfully"
    assert run_update.job_id is None
    assert run_update.artifact_path is None
    assert run_update.artifact_bytes is None
    assert run_update.sha256 is None
    assert run_update.logs_text is None


def test_run_response_schema() -> None:
    """Test Run response schema."""
    started_at = datetime.now(timezone.utc)
    finished_at = datetime.now(timezone.utc)
    run_data = {
        "id": 1,
        "job_id": 1,
        "started_at": started_at,
        "finished_at": finished_at,
        "status": "success",
        "message": "Backup completed successfully",
        "artifact_path": "/backups/test.sql",
        "artifact_bytes": 1024000,
        "sha256": "a1b2c3d4e5f6789012345678901234567890abcdef1234567890abcdef123456",
        "logs_text": "Starting backup...\nBackup completed successfully"
    }
    
    run = Run(**run_data)
    assert run.id == 1
    assert run.job_id == 1
    assert run.started_at == started_at
    assert run.finished_at == finished_at
    assert run.status == "success"
    assert run.message == "Backup completed successfully"


def test_optional_fields() -> None:
    """Test that optional fields work correctly."""
    # Run with minimal data
    run_data = {
        "job_id": 1,
        "status": "running"
    }
    
    run = RunCreate(**run_data)
    assert run.job_id == 1
    assert run.status == "running"
    assert run.message is None
    assert run.artifact_path is None
    assert run.artifact_bytes is None
    assert run.sha256 is None
    assert run.logs_text is None


def test_invalid_data_validation() -> None:
    """Test that invalid data raises ValidationError."""
    # Missing required field
    with pytest.raises(ValidationError):
        TargetCreate(
            name="Test Database",
            # Missing slug, type, config_json
        )
    
    # Invalid job_id type
    with pytest.raises(ValidationError):
        RunCreate(
            job_id="not_an_integer",  # Should be int
            status="running"
        )
