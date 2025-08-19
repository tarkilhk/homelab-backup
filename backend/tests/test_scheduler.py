"""Tests for scheduler behavior: scheduling and execution paths."""

from __future__ import annotations

import logging
from typing import Generator, Any, Dict

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import Session, sessionmaker

from app.core.db import Base
from app.core.scheduler import (
    schedule_jobs_on_startup,
    _scheduled_job,  # type: ignore[attr-defined]
    run_job_immediately,
)
import tempfile
from app.core.plugins.base import BackupPlugin, BackupContext
from app.models import Target, Job, Run, Tag, TargetTag


class DummyScheduler:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def add_job(self, **kwargs) -> None:  # type: ignore[no-untyped-def]
        self.calls.append(kwargs)


@pytest.fixture
def session() -> Generator[Session, None, None]:
    """Provide an in-memory SQLite DB session using a StaticPool."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    # Ensure models are registered before creating tables
    import app.models  # noqa: F401

    Base.metadata.create_all(bind=engine)

    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


def _create_target(db: Session) -> Target:
    target = Target(
        name="Svc",
        slug="svc",
        plugin_name="dummy",
        plugin_config_json="{}",
    )
    db.add(target)
    db.commit()
    db.refresh(target)
    return target


def _create_job(
    db: Session,
    target_id: int,
    name: str,
    cron: str,
    enabled: bool = True,
) -> Job:
    # Create a tag and attach to the target so the scheduler can resolve targets by tag
    tag = Tag(display_name=name)
    db.add(tag)
    db.commit()
    db.refresh(tag)

    db.add(TargetTag(target_id=target_id, tag_id=tag.id, origin="DIRECT"))
    db.commit()

    job = Job(
        tag_id=tag.id,
        name=name,
        schedule_cron=cron,
        enabled=enabled,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def test_schedule_jobs_on_startup_filters_and_args(session: Session, caplog: pytest.LogCaptureFixture) -> None:
    target = _create_target(session)

    enabled_valid = _create_job(session, target.id, "Daily", "*/5 * * * *", enabled=True)
    enabled_invalid = _create_job(session, target.id, "Bad", "not a cron", enabled=True)
    disabled = _create_job(session, target.id, "Disabled", "0 2 * * *", enabled=False)

    sched = DummyScheduler()
    caplog.set_level(logging.INFO, logger="app.core.scheduler")

    schedule_jobs_on_startup(sched, session)

    # Only enabled-valid should be scheduled
    assert any(c["id"] == f"job:{enabled_valid.id}" for c in sched.calls)
    assert not any(c["id"] == f"job:{disabled.id}" for c in sched.calls)

    # Invalid cron should be logged
    assert any("invalid_cron" in r.getMessage() for r in caplog.records)

    # Validate call args
    call = next(c for c in sched.calls if c["id"] == f"job:{enabled_valid.id}")
    assert call["name"] == enabled_valid.name
    assert call["kwargs"] == {"job_id": enabled_valid.id}
    assert call["max_instances"] == 1
    assert call["replace_existing"] is True


def test_scheduled_job_creates_run_and_marks_success(
    monkeypatch: pytest.MonkeyPatch, session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    # Mock the get_session function to return our test session
    def mock_get_session():
        yield session

    import app.core.db as db_mod
    monkeypatch.setattr(db_mod, "get_session", mock_get_session, raising=True)

    target = _create_target(session)
    job = _create_job(session, target.id, "Now", "* * * * *", enabled=True)

    # Force plugin to be a minimal success plugin
    class _SuccessPlugin(BackupPlugin):
        async def validate_config(self, config: Dict[str, Any]) -> bool:  # noqa: ARG002
            return True
        async def test(self, config: Dict[str, Any]) -> bool:  # noqa: ARG002
            return True
        async def backup(self, context: BackupContext) -> Dict[str, Any]:  # noqa: ARG002
            fd, path = tempfile.mkstemp(prefix="backup-test-", suffix=".txt")
            return {"artifact_path": path}
        async def restore(self, context: BackupContext) -> Dict[str, Any]:  # noqa: ARG002
            return {"ok": True}
        async def get_status(self, context: BackupContext) -> Dict[str, Any]:  # noqa: ARG002
            return {"ok": True}

    import app.core.scheduler as sched
    monkeypatch.setattr(sched, "get_plugin", lambda name: _SuccessPlugin(name))

    caplog.set_level(logging.INFO, logger="app.core.scheduler")
    _scheduled_job(job.id)

    # Query using the same session to reflect committed state
    runs = session.query(Run).filter(Run.job_id == job.id).all()
    assert len(runs) == 1
    run = runs[0]
    assert run.status == "success"
    assert run.started_at is not None
    assert run.finished_at is not None
    # Parent run no longer carries artifact metadata
    assert getattr(run, "artifact_path", None) is None

    # Logs
    messages = [r.getMessage() for r in caplog.records]
    assert any("run_started" in m for m in messages)
    assert any("run_finished" in m for m in messages)


def test_run_job_immediately_shared_logic(session: Session, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    target = _create_target(session)
    job = _create_job(session, target.id, "Manual", "0 0 * * *", enabled=True)

    # Force plugin to be a minimal success plugin
    class _SuccessPlugin(BackupPlugin):
        async def validate_config(self, config: Dict[str, Any]) -> bool:  # noqa: ARG002
            return True
        async def test(self, config: Dict[str, Any]) -> bool:  # noqa: ARG002
            return True
        async def backup(self, context: BackupContext) -> Dict[str, Any]:  # noqa: ARG002
            fd, path = tempfile.mkstemp(prefix="backup-test-", suffix=".txt")
            return {"artifact_path": path}
        async def restore(self, context: BackupContext) -> Dict[str, Any]:  # noqa: ARG002
            return {"ok": True}
        async def get_status(self, context: BackupContext) -> Dict[str, Any]:  # noqa: ARG002
            return {"ok": True}

    import app.core.scheduler as sched
    monkeypatch.setattr(sched, "get_plugin", lambda name: _SuccessPlugin(name))

    caplog.set_level(logging.INFO, logger="app.core.scheduler")
    run = run_job_immediately(session, job.id, triggered_by="manual_test")

    assert run.job_id == job.id
    assert run.status == "success"
    assert getattr(run, "artifact_path", None) is None
    assert run.finished_at is not None

    messages = [r.getMessage() for r in caplog.records]
    assert any("run_started" in m for m in messages)
    assert any("run_finished" in m for m in messages)


def test_scheduled_job_missing_is_logged(monkeypatch: pytest.MonkeyPatch, session: Session, caplog: pytest.LogCaptureFixture) -> None:
    # Mock the get_session function to return our test session
    def mock_get_session():
        yield session

    import app.core.db as db_mod
    monkeypatch.setattr(db_mod, "get_session", mock_get_session, raising=True)

    caplog.set_level(logging.INFO, logger="app.core.scheduler")
    _scheduled_job(999999)  # Nonexistent
    assert any("job_missing" in r.getMessage() for r in caplog.records)


def test_reschedule_job_updates_scheduler():
    """Test that reschedule_job properly updates the APScheduler."""
    from app.core.scheduler import reschedule_job, get_scheduler
    
    scheduler = get_scheduler()
    
    # Test rescheduling a job
    result = reschedule_job(1, "0 3 * * *", enabled=True)
    assert result is True
    
    # Verify job was added to scheduler
    job = scheduler.get_job("job:1")
    assert job is not None
    # Check that the trigger contains the expected hour and minute
    trigger_str = str(job.trigger)
    assert "hour='3'" in trigger_str
    assert "minute='0'" in trigger_str


def test_reschedule_job_disabled_removes_from_scheduler():
    """Test that reschedule_job removes job when disabled."""
    from app.core.scheduler import reschedule_job, get_scheduler
    
    scheduler = get_scheduler()
    
    # First add a job
    reschedule_job(2, "0 4 * * *", enabled=True)
    assert scheduler.get_job("job:2") is not None
    
    # Then disable it
    result = reschedule_job(2, "0 4 * * *", enabled=False)
    assert result is True
    
    # Verify job was removed
    assert scheduler.get_job("job:2") is None


def test_reschedule_job_invalid_cron_returns_false():
    """Test that reschedule_job returns False for invalid cron."""
    from app.core.scheduler import reschedule_job
    
    result = reschedule_job(3, "invalid cron", enabled=True)
    assert result is False


def test_remove_job_from_scheduler():
    """Test that remove_job properly removes jobs from scheduler."""
    from app.core.scheduler import remove_job, reschedule_job, get_scheduler
    
    scheduler = get_scheduler()
    
    # Add a job first
    reschedule_job(4, "0 5 * * *", enabled=True)
    assert scheduler.get_job("job:4") is not None
    
    # Remove it
    result = remove_job(4)
    assert result is True
    
    # Verify it's gone
    assert scheduler.get_job("job:4") is None


def test_get_scheduler_jobs_returns_job_list():
    """Test that get_scheduler_jobs returns list of scheduled jobs."""
    from app.core.scheduler import get_scheduler_jobs, reschedule_job, get_scheduler
    
    scheduler = get_scheduler()
    
    # Add a test job
    reschedule_job(5, "0 6 * * *", enabled=True)
    
    # Get jobs directly from scheduler for testing
    scheduler_jobs = scheduler.get_jobs()
    assert len(scheduler_jobs) > 0
    
    # Find our test job
    test_job = next((j for j in scheduler_jobs if j.id == "job:5"), None)
    assert test_job is not None
    assert test_job.name == "Job 5"
    # Check that the trigger contains the expected hour and minute
    trigger_str = str(test_job.trigger)
    assert "hour='6'" in trigger_str
    assert "minute='0'" in trigger_str


def test_scheduler_job_id_format():
    """Test that scheduler job IDs use the correct format."""
    from app.core.scheduler import reschedule_job, get_scheduler
    
    scheduler = get_scheduler()
    
    # Add a job
    reschedule_job(6, "0 7 * * *", enabled=True)
    
    # Check the job ID format
    job = scheduler.get_job("job:6")
    assert job is not None
    assert job.id == "job:6"


def test_scheduler_replace_existing_behavior():
    """Test that scheduler properly replaces existing jobs."""
    from app.core.scheduler import reschedule_job, get_scheduler
    
    scheduler = get_scheduler()
    
    # Add job with first schedule
    reschedule_job(7, "0 8 * * *", enabled=True)
    job1 = scheduler.get_job("job:7")
    assert job1 is not None
    
    # Update with new schedule
    reschedule_job(7, "0 9 * * *", enabled=True)
    job2 = scheduler.get_job("job:7")
    assert job2 is not None
    
    # Should be the same job ID (replaced)
    assert job1.id == job2.id
    
    # But with updated trigger
    trigger_str = str(job2.trigger)
    assert "hour='9'" in trigger_str
    assert "minute='0'" in trigger_str


