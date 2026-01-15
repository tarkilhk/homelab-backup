"""Tests for MaintenanceService."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.models import MaintenanceJob as MaintenanceJobModel, MaintenanceRun as MaintenanceRunModel
from app.domain.enums import RunStatus, MaintenanceJobType
from app.services.maintenance import MaintenanceService


def test_list_jobs_all(db: Session):
    """Test listing all maintenance jobs."""
    svc = MaintenanceService(db)
    
    job1 = MaintenanceJobModel(
        key="job1",
        job_type=MaintenanceJobType.RETENTION_CLEANUP.value,
        name="Job 1",
        schedule_cron="0 3 * * *",
        enabled=True,
        visible_in_ui=True,
    )
    job2 = MaintenanceJobModel(
        key="job2",
        job_type=MaintenanceJobType.RETENTION_CLEANUP.value,
        name="Job 2",
        schedule_cron="0 4 * * *",
        enabled=True,
        visible_in_ui=False,
    )
    db.add(job1)
    db.add(job2)
    db.commit()
    
    jobs = svc.list_jobs()
    assert len(jobs) == 2


def test_list_jobs_filtered_by_visible(db: Session):
    """Test listing maintenance jobs filtered by visible_in_ui."""
    svc = MaintenanceService(db)
    
    job1 = MaintenanceJobModel(
        key="job1",
        job_type=MaintenanceJobType.RETENTION_CLEANUP.value,
        name="Job 1",
        schedule_cron="0 3 * * *",
        visible_in_ui=True,
    )
    job2 = MaintenanceJobModel(
        key="job2",
        job_type=MaintenanceJobType.RETENTION_CLEANUP.value,
        name="Job 2",
        schedule_cron="0 4 * * *",
        visible_in_ui=False,
    )
    db.add(job1)
    db.add(job2)
    db.commit()
    
    visible_jobs = svc.list_jobs(visible_in_ui=True)
    assert len(visible_jobs) == 1
    assert visible_jobs[0].key == "job1"
    
    hidden_jobs = svc.list_jobs(visible_in_ui=False)
    assert len(hidden_jobs) == 1
    assert hidden_jobs[0].key == "job2"


def test_get_job_by_id(db: Session):
    """Test getting a maintenance job by ID."""
    svc = MaintenanceService(db)
    
    job = MaintenanceJobModel(
        key="test_job",
        job_type=MaintenanceJobType.RETENTION_CLEANUP.value,
        name="Test Job",
        schedule_cron="0 3 * * *",
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    
    found = svc.get_job(job.id)
    assert found is not None
    assert found.id == job.id
    assert found.key == "test_job"
    
    not_found = svc.get_job(99999)
    assert not_found is None


def test_get_job_by_key(db: Session):
    """Test getting a maintenance job by key."""
    svc = MaintenanceService(db)
    
    job = MaintenanceJobModel(
        key="retention_cleanup_manual",
        job_type=MaintenanceJobType.RETENTION_CLEANUP.value,
        name="Manual Retention",
        schedule_cron="0 0 1 1 *",
    )
    db.add(job)
    db.commit()
    
    found = svc.get_job_by_key("retention_cleanup_manual")
    assert found is not None
    assert found.key == "retention_cleanup_manual"
    
    not_found = svc.get_job_by_key("nonexistent")
    assert not_found is None


def test_list_runs(db: Session):
    """Test listing maintenance runs."""
    svc = MaintenanceService(db)
    
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
    
    runs = svc.list_runs()
    assert len(runs) == 2
    # Should be sorted by started_at descending (most recent first)
    assert runs[0].started_at >= runs[1].started_at


def test_list_runs_with_limit(db: Session):
    """Test listing maintenance runs with limit."""
    svc = MaintenanceService(db)
    
    job = MaintenanceJobModel(
        key="test_job",
        job_type=MaintenanceJobType.RETENTION_CLEANUP.value,
        name="Test Job",
        schedule_cron="0 3 * * *",
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    
    for i in range(5):
        run = MaintenanceRunModel(
            maintenance_job_id=job.id,
            status=RunStatus.SUCCESS.value,
        )
        db.add(run)
    db.commit()
    
    runs = svc.list_runs(limit=3)
    assert len(runs) == 3


def test_get_run(db: Session):
    """Test getting a maintenance run by ID."""
    svc = MaintenanceService(db)
    
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
        message="Test message",
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    
    found = svc.get_run(run.id)
    assert found is not None
    assert found.id == run.id
    assert found.message == "Test message"
    
    not_found = svc.get_run(99999)
    assert not_found is None
