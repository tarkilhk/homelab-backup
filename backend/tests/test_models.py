"""Tests for SQLAlchemy models."""

import pytest
from datetime import datetime, timezone
from sqlalchemy.orm import Session

from app.core.db import SessionLocal, init_db
from app.models import Target, Job, Run


@pytest.fixture
def db_session() -> Session:
    """Create a database session for testing."""
    init_db()
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
        # Clean up database after each test
        db = SessionLocal()
        try:
            db.query(Run).delete()
            db.query(Job).delete()
            db.query(Target).delete()
            db.commit()
        finally:
            db.close()


def test_target_model(db_session: Session) -> None:
    """Test Target model creation and relationships."""
    # Create a target
    target = Target(
        name="Test Database",
        slug="test-db-target",
        type="postgres",
        config_json='{"host": "localhost", "port": 5432, "database": "test"}'
    )
    
    db_session.add(target)
    db_session.commit()
    db_session.refresh(target)
    
    # Verify target was created
    assert target.id is not None
    assert target.name == "Test Database"
    assert target.slug == "test-db-target"
    assert target.type == "postgres"
    assert target.created_at is not None
    assert target.updated_at is not None
    
    # Test string representation
    assert "Test Database" in str(target)


def test_job_model(db_session: Session) -> None:
    """Test Job model creation and relationships."""
    # Create a target first
    target = Target(
        name="Test Database",
        slug="test-db-job",
        type="postgres",
        config_json='{"host": "localhost", "port": 5432, "database": "test"}'
    )
    db_session.add(target)
    db_session.commit()
    db_session.refresh(target)
    
    # Create a job
    job = Job(
        target_id=target.id,
        name="Daily Backup",
        schedule_cron="0 2 * * *",
        enabled="true",
        plugin="postgres_backup",
        plugin_version="1.0.0"
    )
    
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)
    
    # Verify job was created
    assert job.id is not None
    assert job.target_id == target.id
    assert job.name == "Daily Backup"
    assert job.schedule_cron == "0 2 * * *"
    assert job.enabled == "true"
    assert job.plugin == "postgres_backup"
    assert job.plugin_version == "1.0.0"
    
    # Test relationship
    assert job.target == target
    assert target.jobs == [job]


def test_run_model(db_session: Session) -> None:
    """Test Run model creation and relationships."""
    # Create target and job first
    target = Target(
        name="Test Database",
        slug="test-db-run",
        type="postgres",
        config_json='{"host": "localhost", "port": 5432, "database": "test"}'
    )
    db_session.add(target)
    db_session.commit()
    db_session.refresh(target)
    
    job = Job(
        target_id=target.id,
        name="Daily Backup",
        schedule_cron="0 2 * * *",
        enabled="true",
        plugin="postgres_backup",
        plugin_version="1.0.0"
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)
    
    # Create a run
    run = Run(
        job_id=job.id,
        status="success",
        message="Backup completed successfully",
        artifact_path="/backups/test-db-2024-01-01.sql",
        artifact_bytes=1024000,
        sha256="a1b2c3d4e5f6789012345678901234567890abcdef1234567890abcdef123456",
        logs_text="Starting backup...\nBackup completed successfully"
    )
    
    db_session.add(run)
    db_session.commit()
    db_session.refresh(run)
    
    # Verify run was created
    assert run.id is not None
    assert run.job_id == job.id
    assert run.status == "success"
    assert run.message == "Backup completed successfully"
    assert run.artifact_path == "/backups/test-db-2024-01-01.sql"
    assert run.artifact_bytes == 1024000
    assert run.sha256 == "a1b2c3d4e5f6789012345678901234567890abcdef1234567890abcdef123456"
    assert run.logs_text == "Starting backup...\nBackup completed successfully"
    assert run.started_at is not None
    assert run.finished_at is None
    
    # Test relationship
    assert run.job == job
    assert job.runs == [run]


def test_cascade_delete(db_session: Session) -> None:
    """Test cascade delete behavior."""
    # Create target, job, and run
    target = Target(
        name="Test Database",
        slug="test-db-cascade",
        type="postgres",
        config_json='{"host": "localhost", "port": 5432, "database": "test"}'
    )
    db_session.add(target)
    db_session.commit()
    db_session.refresh(target)
    
    job = Job(
        target_id=target.id,
        name="Daily Backup",
        schedule_cron="0 2 * * *",
        enabled="true",
        plugin="postgres_backup",
        plugin_version="1.0.0"
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)
    
    run = Run(
        job_id=job.id,
        status="success",
        message="Backup completed successfully"
    )
    db_session.add(run)
    db_session.commit()
    db_session.refresh(run)
    
    # Verify all exist
    assert db_session.query(Target).count() == 1
    assert db_session.query(Job).count() == 1
    assert db_session.query(Run).count() == 1
    
    # Delete target (should cascade to job and run)
    db_session.delete(target)
    db_session.commit()
    
    # Verify cascade delete worked
    assert db_session.query(Target).count() == 0
    assert db_session.query(Job).count() == 0
    assert db_session.query(Run).count() == 0
