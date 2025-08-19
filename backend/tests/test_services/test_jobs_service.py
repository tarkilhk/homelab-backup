from __future__ import annotations

import threading
import time
from typing import Generator, List, Dict, Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import Session, sessionmaker

from app.core.db import Base
from app.models import Target, Tag, TargetTag, Job
from app.services.jobs import JobService, resolve_tag_to_targets, run_job_for_tag


@pytest.fixture
def session() -> Generator[Session, None, None]:
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


# Factories

def make_target(db: Session, name: str) -> Target:
    t = Target(name=name, slug=name.lower(), plugin_name="dummy", plugin_config_json="{}")
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def make_tag(db: Session, name: str) -> Tag:
    tag = Tag(display_name=name)
    db.add(tag)
    db.commit()
    db.refresh(tag)
    return tag


def attach(db: Session, target: Target, tag: Tag, origin: str, source_group_id: int | None = None, is_auto: bool = False) -> TargetTag:
    tt = TargetTag(target_id=target.id, tag_id=tag.id, origin=origin, source_group_id=source_group_id, is_auto_tag=is_auto)
    db.add(tt)
    db.commit()
    db.refresh(tt)
    return tt


def make_job(db: Session, tag: Tag, name: str = "J", cron: str = "* * * * *", enabled: bool = True) -> Job:
    job = Job(tag_id=tag.id, name=name, schedule_cron=cron, enabled=enabled)
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


# Tests

def test_job_create_requires_existing_tag_and_valid_cron(session: Session) -> None:
    svc = JobService(session)
    # Unknown tag id
    with pytest.raises(KeyError):
        svc.create(tag_id=9999, name="X", schedule_cron="* * * * *", enabled=True)
    # Create tag and bad cron
    t = make_tag(session, "Prod")
    with pytest.raises(Exception):
        svc.create(tag_id=t.id, name="X", schedule_cron="invalid CRON BAD", enabled=True)
    # Good
    job = svc.create(tag_id=t.id, name="Y", schedule_cron="*/5 * * * *", enabled=True)
    assert job.id > 0 and job.tag_id == t.id


def test_job_update_validates_fields(session: Session) -> None:
    svc = JobService(session)
    tg = make_tag(session, "Tag1")
    job = svc.create(tag_id=tg.id, name="A", schedule_cron="* * * * *", enabled=True)

    # Update to bad cron
    with pytest.raises(Exception):
        svc.update(job.id, schedule_cron="BAD expr")

    # Update to unknown tag
    with pytest.raises(KeyError):
        svc.update(job.id, tag_id=999999)

    # Valid update
    job2 = svc.update(job.id, name="B", schedule_cron="0 2 * * *")
    assert job2.name == "B"


def test_resolve_tag_to_targets_dedupes_origins(session: Session) -> None:
    tag = make_tag(session, "Prod")
    a = make_target(session, "A")
    b = make_target(session, "B")
    # Attach same tag to A via AUTO and DIRECT -> should dedupe
    attach(session, a, tag, origin="AUTO", is_auto=True)
    attach(session, a, tag, origin="DIRECT")
    # Attach to B via GROUP
    attach(session, b, tag, origin="GROUP", source_group_id=1)

    targets = resolve_tag_to_targets(session, tag.id)
    ids = {t.id for t in targets}
    assert ids == {a.id, b.id}


def test_no_overlap_skip_when_running(session: Session) -> None:
    tag = make_tag(session, "T")
    targets = [make_target(session, f"T{i}") for i in range(2)]
    for t in targets:
        attach(session, t, tag, origin="DIRECT")
    job = make_job(session, tag, name="Overlap")

    # Runner that blocks for a moment
    def runner(_t: Target) -> dict:
        time.sleep(0.2)
        return {"ok": True}

    # Start first run in background
    results_holder: dict[str, Any] = {}

    def bg() -> None:
        results_holder["first"] = run_job_for_tag(session, job.id, tag.id, runner=runner, max_concurrency=1, no_overlap=True)

    th = threading.Thread(target=bg)
    th.start()
    time.sleep(0.05)

    # Second run should skip
    second = run_job_for_tag(session, job.id, tag.id, runner=runner, max_concurrency=1, no_overlap=True)
    th.join()

    assert results_holder["first"]["started"] is True
    assert second["started"] is False and second["results"] == []


def test_bounded_concurrency(session: Session) -> None:
    tag = make_tag(session, "P")
    targets = [make_target(session, f"N{i}") for i in range(10)]
    for t in targets:
        attach(session, t, tag, origin="DIRECT")
    job = make_job(session, tag, name="Conc")

    concurrent = 0
    max_seen = 0
    lock = threading.Lock()

    def runner(_t: Target) -> dict:
        nonlocal concurrent, max_seen
        with lock:
            concurrent += 1
            if concurrent > max_seen:
                max_seen = concurrent
        time.sleep(0.05)
        with lock:
            concurrent -= 1
        return {"ok": True}

    out = run_job_for_tag(session, job.id, tag.id, runner=runner, max_concurrency=3, no_overlap=True)
    assert out["started"] is True
    assert max_seen <= 3


def test_per_target_retry_with_backoff(session: Session) -> None:
    tag = make_tag(session, "R")
    targets = [make_target(session, f"X{i}") for i in range(3)]
    for t in targets:
        attach(session, t, tag, origin="DIRECT")
    job = make_job(session, tag, name="Retry")

    attempts: Dict[int, int] = {}
    sleeps: list[float] = []

    def fake_sleep(d: float) -> None:
        sleeps.append(d)

    def runner(t: Target) -> dict:
        cnt = attempts.get(t.id, 0)
        attempts[t.id] = cnt + 1
        if cnt == 0:
            raise RuntimeError("fail-once")
        return {"ok": True}

    out = run_job_for_tag(
        session,
        job.id,
        tag.id,
        runner=runner,
        max_concurrency=2,
        no_overlap=True,
        max_retries=1,
        sleep_fn=fake_sleep,
        backoff_base=0.001,
    )
    assert out["started"] is True
    # Each target should have attempted twice total (1 fail + 1 success)
    assert all(attempts[t.id] == 2 for t in targets)
    # Backoff called for each initial failure
    assert len(sleeps) == len(targets)


def test_job_create_adds_to_scheduler(monkeypatch, session):
    """Test that creating a job automatically adds it to the scheduler."""
    from app.core.scheduler import reschedule_job
    
    # Mock the reschedule_job function
    mock_calls = []
    def mock_reschedule_job(job_id, schedule_cron, enabled):
        mock_calls.append({"job_id": job_id, "schedule_cron": schedule_cron, "enabled": enabled})
        return True
    
    monkeypatch.setattr("app.core.scheduler.reschedule_job", mock_reschedule_job)
    
    svc = JobService(session)
    tag = make_tag(session, "TestTag")
    
    # Create enabled job
    job = svc.create(tag_id=tag.id, name="TestJob", schedule_cron="0 2 * * *", enabled=True)
    
    # Verify scheduler was called
    assert len(mock_calls) == 1
    assert mock_calls[0]["job_id"] == job.id
    assert mock_calls[0]["schedule_cron"] == "0 2 * * *"
    assert mock_calls[0]["enabled"] is True


def test_job_create_disabled_does_not_add_to_scheduler(monkeypatch, session):
    """Test that creating a disabled job does not add it to scheduler."""
    from app.core.scheduler import reschedule_job
    
    # Mock the reschedule_job function
    mock_calls = []
    def mock_reschedule_job(job_id, schedule_cron, enabled):
        mock_calls.append({"job_id": job_id, "schedule_cron": schedule_cron, "enabled": enabled})
        return True
    
    monkeypatch.setattr("app.core.scheduler.reschedule_job", mock_reschedule_job)
    
    svc = JobService(session)
    tag = make_tag(session, "TestTag")
    
    # Create disabled job
    job = svc.create(tag_id=tag.id, name="TestJob", schedule_cron="0 2 * * *", enabled=False)
    
    # Verify scheduler was not called
    assert len(mock_calls) == 0


def test_job_update_schedule_cron_updates_scheduler(monkeypatch, session):
    """Test that updating job cron automatically updates scheduler."""
    from app.core.scheduler import reschedule_job
    
    # Mock the reschedule_job function
    mock_calls = []
    def mock_reschedule_job(job_id, schedule_cron, enabled):
        mock_calls.append({"job_id": job_id, "schedule_cron": schedule_cron, "enabled": enabled})
        return True
    
    monkeypatch.setattr("app.core.scheduler.reschedule_job", mock_reschedule_job)
    
    svc = JobService(session)
    tag = make_tag(session, "TestTag")
    job = svc.create(tag_id=tag.id, name="TestJob", schedule_cron="0 2 * * *", enabled=True)
    
    # Clear mock calls from create
    mock_calls.clear()
    
    # Update cron
    svc.update(job.id, schedule_cron="0 3 * * *")
    
    # Verify scheduler was called with new cron
    assert len(mock_calls) == 1
    assert mock_calls[0]["job_id"] == job.id
    assert mock_calls[0]["schedule_cron"] == "0 3 * * *"
    assert mock_calls[0]["enabled"] is True


def test_job_update_enabled_status_updates_scheduler(monkeypatch, session):
    """Test that updating job enabled status automatically updates scheduler."""
    from app.core.scheduler import reschedule_job
    
    # Mock the reschedule_job function
    mock_calls = []
    def mock_reschedule_job(job_id, schedule_cron, enabled):
        mock_calls.append({"job_id": job_id, "schedule_cron": schedule_cron, "enabled": enabled})
        return True
    
    monkeypatch.setattr("app.core.scheduler.reschedule_job", mock_reschedule_job)
    
    svc = JobService(session)
    tag = make_tag(session, "TestTag")
    job = svc.create(tag_id=tag.id, name="TestJob", schedule_cron="0 2 * * *", enabled=True)
    
    # Clear mock calls from create
    mock_calls.clear()
    
    # Disable job
    svc.update(job.id, enabled=False)
    
    # Verify scheduler was called with disabled status
    assert len(mock_calls) == 1
    assert mock_calls[0]["job_id"] == job.id
    assert mock_calls[0]["enabled"] is False


def test_job_update_other_fields_does_not_update_scheduler(monkeypatch, session):
    """Test that updating non-schedule fields doesn't trigger scheduler update."""
    from app.core.scheduler import reschedule_job
    
    # Mock the reschedule_job function
    mock_calls = []
    def mock_reschedule_job(job_id, schedule_cron, enabled):
        mock_calls.append({"job_id": job_id, "schedule_cron": schedule_cron, "enabled": enabled})
        return True
    
    monkeypatch.setattr("app.core.scheduler.reschedule_job", mock_reschedule_job)
    
    svc = JobService(session)
    tag = make_tag(session, "TestTag")
    job = svc.create(tag_id=tag.id, name="TestJob", schedule_cron="0 2 * * *", enabled=True)
    
    # Clear mock calls from create
    mock_calls.clear()
    
    # Update name only
    svc.update(job.id, name="NewName")
    
    # Verify scheduler was not called
    assert len(mock_calls) == 0


def test_job_delete_removes_from_scheduler(monkeypatch, session):
    """Test that deleting a job automatically removes it from scheduler."""
    from app.core.scheduler import remove_job
    
    # Mock the remove_job function
    mock_calls = []
    def mock_remove_job(job_id):
        mock_calls.append({"job_id": job_id})
        return True
    
    monkeypatch.setattr("app.core.scheduler.remove_job", mock_remove_job)
    
    svc = JobService(session)
    tag = make_tag(session, "TestTag")
    job = svc.create(tag_id=tag.id, name="TestJob", schedule_cron="0 2 * * *", enabled=True)
    
    # Delete job
    svc.delete(job.id)
    
    # Verify scheduler was called to remove job
    assert len(mock_calls) == 1
    assert mock_calls[0]["job_id"] == job.id


def test_scheduler_update_failure_does_not_fail_job_operation(monkeypatch, session):
    """Test that scheduler update failures don't cause job operations to fail."""
    from app.core.scheduler import reschedule_job
    
    # Mock reschedule_job to raise an exception
    def mock_reschedule_job(job_id, schedule_cron, enabled):
        raise RuntimeError("Scheduler error")
    
    monkeypatch.setattr("app.core.scheduler.reschedule_job", mock_reschedule_job)
    
    svc = JobService(session)
    tag = make_tag(session, "TestTag")
    
    # Job creation should still succeed even if scheduler update fails
    job = svc.create(tag_id=tag.id, name="TestJob", schedule_cron="0 2 * * *", enabled=True)
    assert job.id > 0
    
    # Job update should still succeed even if scheduler update fails
    updated_job = svc.update(job.id, schedule_cron="0 3 * * *")
    assert updated_job.schedule_cron == "0 3 * * *"
    
    # Job deletion should still succeed even if scheduler update fails
    svc.delete(job.id)
    # Verify job was deleted from DB
    assert session.query(Job).filter(Job.id == job.id).first() is None


def test_scheduled_job_execution_success(monkeypatch, session):
    """Test that _scheduled_job creates run and marks success."""
    from app.core.scheduler import _scheduled_job
    from app.core.db import get_session
    import tempfile
    
    # Mock get_session to return our test session
    def mock_get_session():
        yield session
    
    import app.core.db as db_mod
    monkeypatch.setattr(db_mod, "get_session", mock_get_session, raising=True)
    
    # Create target and job
    target = make_target(session, "ScheduleTest")
    tag = make_tag(session, "ScheduleTag")
    attach(session, target, tag, origin="DIRECT")
    job = make_job(session, tag, name="ScheduledJob", cron="* * * * *", enabled=True)
    
    # Mock successful plugin
    class SuccessPlugin:
        async def validate_config(self, config): return True
        async def test(self, config): return True
        async def backup(self, context):
            fd, path = tempfile.mkstemp(prefix="backup-test-", suffix=".txt")
            return {"artifact_path": path}
        async def restore(self, context): return {"ok": True}
        async def get_status(self, context): return {"ok": True}
    
    import app.core.scheduler as sched
    monkeypatch.setattr(sched, "get_plugin", lambda name: SuccessPlugin())
    
    # Execute scheduled job
    _scheduled_job(job.id)
    
    # Verify run was created and marked successful
    from app.models import Run as RunModel
    runs = session.query(RunModel).filter(RunModel.job_id == job.id).all()
    assert len(runs) == 1
    run = runs[0]
    assert run.status == "success"
    assert run.started_at is not None
    assert run.finished_at is not None


def test_scheduled_job_handles_plugin_errors(monkeypatch, session):
    """Test that _scheduled_job handles plugin errors gracefully."""
    from app.core.scheduler import _scheduled_job
    from app.core.db import get_session
    
    # Mock get_session to return our test session
    def mock_get_session():
        yield session
    
    import app.core.db as db_mod
    monkeypatch.setattr(db_mod, "get_session", mock_get_session, raising=True)
    
    # Create target and job
    target = make_target(session, "ErrorTest")
    tag = make_tag(session, "ErrorTag")
    attach(session, target, tag, origin="DIRECT")
    job = make_job(session, tag, name="ErrorJob", cron="* * * * *", enabled=True)
    
    # Mock failing plugin
    class FailingPlugin:
        async def validate_config(self, config): return True
        async def test(self, config): return True
        async def backup(self, context):
            raise RuntimeError("Plugin backup failed")
        async def restore(self, context): return {"ok": True}
        async def get_status(self, context): return {"ok": True}
    
    import app.core.scheduler as sched
    monkeypatch.setattr(sched, "get_plugin", lambda name: FailingPlugin())
    
    # Execute scheduled job
    _scheduled_job(job.id)
    
    # Verify run was created and marked failed
    from app.models import Run as RunModel
    runs = session.query(RunModel).filter(RunModel.job_id == job.id).all()
    assert len(runs) == 1
    run = runs[0]
    assert run.status == "failed"
    assert run.started_at is not None
    assert run.finished_at is not None
    assert "failed" in run.message.lower()


def test_run_job_immediately_shares_logic_with_scheduled_job(monkeypatch, session):
    """Test that run_job_immediately uses the same execution logic."""
    from app.core.scheduler import run_job_immediately
    import tempfile
    
    # Create target and job
    target = make_target(session, "ImmediateTest")
    tag = make_tag(session, "ImmediateTag")
    attach(session, target, tag, origin="DIRECT")
    job = make_job(session, tag, name="ImmediateJob", cron="0 0 * * *", enabled=True)
    
    # Mock successful plugin
    class SuccessPlugin:
        async def validate_config(self, config): return True
        async def test(self, config): return True
        async def backup(self, context):
            fd, path = tempfile.mkstemp(prefix="backup-test-", suffix=".txt")
            return {"artifact_path": path}
        async def restore(self, context): return {"ok": True}
        async def get_status(self, context): return {"ok": True}
    
    import app.core.scheduler as sched
    monkeypatch.setattr(sched, "get_plugin", lambda name: SuccessPlugin())
    
    # Execute job immediately
    run = run_job_immediately(session, job.id, triggered_by="manual_test")
    
    # Verify run was created successfully
    assert run.job_id == job.id
    assert run.status == "success"
    assert run.finished_at is not None
