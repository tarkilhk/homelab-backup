"""Tests for scheduler with maintenance jobs."""

from __future__ import annotations

import pytest
from unittest.mock import Mock, patch
from sqlalchemy.orm import Session

from app.models import MaintenanceJob as MaintenanceJobModel, MaintenanceRun as MaintenanceRunModel
from app.models import Job as JobModel
from app.domain.enums import MaintenanceJobType, RunStatus
from app.core.scheduler import (
    ScheduledItem,
    schedule_jobs_on_startup,
    scheduled_dispatch,
    execute_maintenance_job,
    scheduled_tick,  # For backup job execution
)


def test_scheduled_item_from_backup_job(db_session: Session):
    """Test ScheduledItem adapter for backup jobs."""
    from app.models import Tag as TagModel
    
    tag = TagModel(display_name="test-tag")
    db_session.add(tag)
    db_session.commit()
    db_session.refresh(tag)
    
    job = JobModel(
        tag_id=tag.id,
        name="Test Backup Job",
        schedule_cron="0 2 * * *",
        enabled=True,
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)
    
    item = ScheduledItem.from_backup_job(job)
    assert item.kind == "backup"
    assert item.id == job.id
    assert item.name == "Test Backup Job"
    assert item.schedule_cron == "0 2 * * *"
    assert item.enabled is True


def test_scheduled_item_from_maintenance_job(db_session: Session):
    """Test ScheduledItem adapter for maintenance jobs."""
    job = MaintenanceJobModel(
        key="test_maintenance",
        job_type=MaintenanceJobType.RETENTION_CLEANUP.value,
        name="Test Maintenance Job",
        schedule_cron="0 3 * * *",
        enabled=True,
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)
    
    item = ScheduledItem.from_maintenance_job(job)
    assert item.kind == "maintenance"
    assert item.id == job.id
    assert item.name == "Test Maintenance Job"
    assert item.schedule_cron == "0 3 * * *"
    assert item.enabled is True


def test_schedule_jobs_on_startup_loads_both_types(db_session: Session):
    """Test that schedule_jobs_on_startup loads both backup and maintenance jobs."""
    from app.models import Tag as TagModel
    
    # Create backup job
    tag = TagModel(display_name="test-tag")
    db_session.add(tag)
    db_session.commit()
    db_session.refresh(tag)
    
    backup_job = JobModel(
        tag_id=tag.id,
        name="Backup Job",
        schedule_cron="0 2 * * *",
        enabled=True,
    )
    db_session.add(backup_job)
    
    # Create maintenance job
    maint_job = MaintenanceJobModel(
        key="test_maintenance",
        job_type=MaintenanceJobType.RETENTION_CLEANUP.value,
        name="Maintenance Job",
        schedule_cron="0 3 * * *",
        enabled=True,
    )
    db_session.add(maint_job)
    db_session.commit()
    
    # Mock scheduler
    mock_scheduler = Mock()
    mock_scheduler.add_job = Mock()
    
    schedule_jobs_on_startup(mock_scheduler, db_session)
    
    # Should have called add_job for both jobs
    assert mock_scheduler.add_job.call_count == 2
    
    # Check that both kinds were scheduled
    calls = mock_scheduler.add_job.call_args_list
    job_ids = [call.kwargs.get("id") for call in calls]
    assert "backup:1" in job_ids or any("backup:" in str(id) for id in job_ids)
    assert "maintenance:1" in job_ids or any("maintenance:" in str(id) for id in job_ids)
    
    # Check that both use scheduled_dispatch
    for call in calls:
        assert call.kwargs["func"] == scheduled_dispatch
        assert "kind" in call.kwargs["kwargs"]
        assert "job_id" in call.kwargs["kwargs"]


def test_scheduled_dispatch_routes_to_maintenance(db_session: Session):
    """Test that scheduled_dispatch routes maintenance jobs correctly."""
    job = MaintenanceJobModel(
        key="test_maintenance",
        job_type=MaintenanceJobType.RETENTION_CLEANUP.value,
        name="Test Maintenance",
        schedule_cron="0 3 * * *",
        enabled=True,
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)
    
    # Mock execute_maintenance_job to avoid actual execution
    with patch("app.core.scheduler.execute_maintenance_job") as mock_exec:
        scheduled_dispatch("maintenance", job.id)
        mock_exec.assert_called_once_with(job.id)


def test_execute_maintenance_job_creates_run(db_session: Session):
    """Test that execute_maintenance_job creates a MaintenanceRun."""
    job = MaintenanceJobModel(
        key="test_maintenance",
        job_type=MaintenanceJobType.RETENTION_CLEANUP.value,
        name="Test Maintenance",
        schedule_cron="0 3 * * *",
        enabled=True,
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)
    
    # Mock apply_retention_all to avoid actual file operations
    with patch("app.core.scheduler.apply_retention_all") as mock_retention:
        mock_retention.return_value = {
            "targets_processed": 5,
            "delete_count": 2,
            "keep_count": 3,
            "deleted_paths": [],
        }
        
        # Mock get_session to return our test db_session
        with patch("app.core.db.get_session") as mock_get_session:
            mock_get_session.return_value = iter([db_session])
            job_id = job.id
            execute_maintenance_job(job_id)
        
        # Check that MaintenanceRun was created
        runs = db_session.query(MaintenanceRunModel).filter(
            MaintenanceRunModel.maintenance_job_id == job_id
        ).all()
        assert len(runs) == 1
        
        run = runs[0]
        assert run.status == RunStatus.SUCCESS.value
        assert run.finished_at is not None
        assert run.result_json is not None
        
        import json
        result = json.loads(run.result_json)
        assert result["targets_processed"] == 5
        assert result["deleted_count"] == 2


def test_execute_maintenance_job_handles_failure(db_session: Session):
    """Test that execute_maintenance_job handles failures correctly."""
    job = MaintenanceJobModel(
        key="test_maintenance",
        job_type=MaintenanceJobType.RETENTION_CLEANUP.value,
        name="Test Maintenance",
        schedule_cron="0 3 * * *",
        enabled=True,
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)
    job_id = job.id
    
    # Mock apply_retention_all to raise an error
    with patch("app.core.scheduler.apply_retention_all") as mock_retention:
        mock_retention.side_effect = Exception("Test error")
        
        # Mock get_session to return our test db_session
        with patch("app.core.db.get_session") as mock_get_session:
            mock_get_session.return_value = iter([db_session])
            execute_maintenance_job(job_id)
        
        # Check that MaintenanceRun was created with failure status
        runs = db_session.query(MaintenanceRunModel).filter(
            MaintenanceRunModel.maintenance_job_id == job_id
        ).all()
        assert len(runs) == 1
        
        run = runs[0]
        assert run.status == RunStatus.FAILED.value
        assert run.finished_at is not None
        assert "error" in run.message.lower() or "failed" in run.message.lower()
        
        import json
        result = json.loads(run.result_json)
        assert "error" in result
