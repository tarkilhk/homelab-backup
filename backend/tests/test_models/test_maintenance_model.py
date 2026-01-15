"""Tests for MaintenanceJob and MaintenanceRun models."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.models import MaintenanceJob as MaintenanceJobModel, MaintenanceRun as MaintenanceRunModel
from app.domain.enums import RunStatus, MaintenanceJobType


def test_maintenance_job_creation(db: Session):
    """Test creating a MaintenanceJob."""
    job = MaintenanceJobModel(
        key="test_retention_cleanup",
        job_type=MaintenanceJobType.RETENTION_CLEANUP.value,
        name="Test Retention Cleanup",
        schedule_cron="0 3 * * *",
        enabled=True,
        visible_in_ui=True,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    
    assert job.id is not None
    assert job.key == "test_retention_cleanup"
    assert job.job_type == MaintenanceJobType.RETENTION_CLEANUP.value
    assert job.name == "Test Retention Cleanup"
    assert job.schedule_cron == "0 3 * * *"
    assert job.enabled is True
    assert job.visible_in_ui is True


def test_maintenance_job_key_unique(db: Session):
    """Test that MaintenanceJob.key must be unique."""
    job1 = MaintenanceJobModel(
        key="unique_key",
        job_type=MaintenanceJobType.RETENTION_CLEANUP.value,
        name="Job 1",
        schedule_cron="0 3 * * *",
    )
    db.add(job1)
    db.commit()
    
    job2 = MaintenanceJobModel(
        key="unique_key",  # Same key
        job_type=MaintenanceJobType.RETENTION_CLEANUP.value,
        name="Job 2",
        schedule_cron="0 4 * * *",
    )
    db.add(job2)
    
    with pytest.raises(Exception):  # IntegrityError or similar
        db.commit()


def test_maintenance_run_creation(db: Session):
    """Test creating a MaintenanceRun."""
    job = MaintenanceJobModel(
        key="test_job",
        job_type=MaintenanceJobType.RETENTION_CLEANUP.value,
        name="Test Job",
        schedule_cron="0 3 * * *",
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    
    run = MaintenanceRunModel(
        maintenance_job_id=job.id,
        status=RunStatus.RUNNING.value,
        message="Test run",
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    
    assert run.id is not None
    assert run.maintenance_job_id == job.id
    assert run.status == RunStatus.RUNNING.value
    assert run.message == "Test run"
    assert run.started_at is not None


def test_maintenance_job_runs_relationship(db: Session):
    """Test MaintenanceJob.runs relationship."""
    job = MaintenanceJobModel(
        key="test_job",
        job_type=MaintenanceJobType.RETENTION_CLEANUP.value,
        name="Test Job",
        schedule_cron="0 3 * * *",
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    
    run1 = MaintenanceRunModel(
        maintenance_job_id=job.id,
        status=RunStatus.SUCCESS.value,
    )
    run2 = MaintenanceRunModel(
        maintenance_job_id=job.id,
        status=RunStatus.FAILED.value,
    )
    db.add(run1)
    db.add(run2)
    db.commit()
    
    db.refresh(job)
    assert len(job.runs) == 2
    assert {r.status for r in job.runs} == {RunStatus.SUCCESS.value, RunStatus.FAILED.value}


def test_maintenance_run_cascade_delete(db: Session):
    """Test that deleting MaintenanceJob cascades to MaintenanceRun."""
    job = MaintenanceJobModel(
        key="test_job",
        job_type=MaintenanceJobType.RETENTION_CLEANUP.value,
        name="Test Job",
        schedule_cron="0 3 * * *",
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    
    run = MaintenanceRunModel(
        maintenance_job_id=job.id,
        status=RunStatus.SUCCESS.value,
    )
    db.add(run)
    db.commit()
    run_id = run.id
    
    # Delete job
    db.delete(job)
    db.commit()
    
    # Run should be deleted
    deleted_run = db.get(MaintenanceRunModel, run_id)
    assert deleted_run is None
