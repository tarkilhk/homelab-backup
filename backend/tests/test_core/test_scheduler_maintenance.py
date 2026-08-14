"""Tests for scheduler with maintenance jobs."""

from __future__ import annotations

from unittest.mock import Mock, patch

import pytest
from sqlalchemy.orm import Session

from app.core.scheduler import scheduled_tick  # For backup job execution
from app.core.scheduler import (
    ScheduledItem,
    execute_maintenance_job,
    reconcile_interrupted_runs,
    remove_job,
    reschedule_job,
    schedule_jobs_on_startup,
    scheduled_dispatch,
)
from app.domain.enums import MaintenanceJobType, RunStatus
from app.models import Job as JobModel
from app.models import MaintenanceJob as MaintenanceJobModel
from app.models import MaintenanceRun as MaintenanceRunModel
from app.models import Run as RunModel
from app.models import Tag as TagModel
from app.models import Target as TargetModel
from app.models import TargetRun as TargetRunModel
from app.models import TargetTag as TargetTagModel


def test_reconcile_interrupted_runs_marks_only_unfinished_attempts_failed(
    db_session: Session,
):
    tag = TagModel(display_name="interrupted")
    target = TargetModel(
        name="Interrupted target",
        slug="interrupted-target",
        plugin_name="pihole",
        plugin_config_json="{}",
    )
    db_session.add_all([tag, target])
    db_session.flush()
    db_session.add(
        TargetTagModel(
            target_id=target.id,
            tag_id=tag.id,
            origin="AUTO",
            is_auto_tag=True,
        )
    )
    job = JobModel(
        tag_id=tag.id,
        name="Interrupted backup",
        schedule_cron="0 2 * * *",
        enabled=True,
    )
    maintenance_job = MaintenanceJobModel(
        key="interrupted_maintenance",
        job_type=MaintenanceJobType.RETENTION_CLEANUP.value,
        name="Interrupted maintenance",
        schedule_cron="0 3 * * *",
        enabled=True,
    )
    db_session.add_all([job, maintenance_job])
    db_session.flush()

    interrupted_run = RunModel(
        job_id=job.id,
        status=RunStatus.RUNNING.value,
        message="Run started",
    )
    completed_run = RunModel(
        job_id=job.id,
        status=RunStatus.SUCCESS.value,
        message="Completed",
    )
    db_session.add_all([interrupted_run, completed_run])
    db_session.flush()
    interrupted_target_run = TargetRunModel(
        run_id=interrupted_run.id,
        target_id=target.id,
        status=RunStatus.RUNNING.value,
        message="Target run started",
    )
    completed_target_run = TargetRunModel(
        run_id=completed_run.id,
        target_id=target.id,
        status=RunStatus.SUCCESS.value,
        message="Completed",
    )
    interrupted_maintenance = MaintenanceRunModel(
        maintenance_job_id=maintenance_job.id,
        status=RunStatus.RUNNING.value,
        message="Maintenance started",
    )
    completed_maintenance = MaintenanceRunModel(
        maintenance_job_id=maintenance_job.id,
        status=RunStatus.SUCCESS.value,
        message="Completed",
    )
    db_session.add_all(
        [
            interrupted_target_run,
            completed_target_run,
            interrupted_maintenance,
            completed_maintenance,
        ]
    )
    db_session.commit()

    reconciled = reconcile_interrupted_runs(db_session)
    db_session.refresh(interrupted_run)
    db_session.refresh(interrupted_target_run)
    db_session.refresh(interrupted_maintenance)
    db_session.refresh(completed_run)
    db_session.refresh(completed_target_run)
    db_session.refresh(completed_maintenance)

    assert reconciled == {"runs": 1, "target_runs": 1, "maintenance_runs": 1}
    for attempt in (interrupted_run, interrupted_target_run, interrupted_maintenance):
        assert attempt.status == RunStatus.FAILED.value
        assert attempt.finished_at is not None
        assert attempt.message == "Interrupted by application restart"
    for attempt in (completed_run, completed_target_run, completed_maintenance):
        assert attempt.status == RunStatus.SUCCESS.value
        assert attempt.finished_at is None
        assert attempt.message == "Completed"


def test_reschedule_and_remove_use_startup_scheduler_id(monkeypatch):
    """A job must keep one scheduler identity across its full lifecycle."""

    scheduler = Mock()
    scheduler.get_job.return_value = object()
    monkeypatch.setattr("app.core.scheduler.get_scheduler", lambda: scheduler)

    assert reschedule_job(42, "0 2 * * *", enabled=True) is True
    scheduler.remove_job.assert_called_once_with("backup:42")
    assert scheduler.add_job.call_args.kwargs["id"] == "backup:42"
    assert scheduler.add_job.call_args.kwargs["kwargs"] == {
        "kind": "backup",
        "job_id": 42,
    }

    scheduler.reset_mock()
    scheduler.get_job.return_value = object()
    assert remove_job(42) is True
    scheduler.remove_job.assert_called_once_with("backup:42")


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
        runs = (
            db_session.query(MaintenanceRunModel)
            .filter(MaintenanceRunModel.maintenance_job_id == job_id)
            .all()
        )
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
        runs = (
            db_session.query(MaintenanceRunModel)
            .filter(MaintenanceRunModel.maintenance_job_id == job_id)
            .all()
        )
        assert len(runs) == 1

        run = runs[0]
        assert run.status == RunStatus.FAILED.value
        assert run.finished_at is not None
        assert "error" in run.message.lower() or "failed" in run.message.lower()

        import json

        result = json.loads(run.result_json)
        assert "error" in result
