"""Tests for scheduler behavior: scheduling and execution paths."""

from __future__ import annotations

import logging
from typing import Generator

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
from app.models import Target, Job, Run


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
        type="custom",
        config_json="{}",
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
    enabled: str = "true",
) -> Job:
    job = Job(
        target_id=target_id,
        name=name,
        schedule_cron=cron,
        enabled=enabled,
        plugin="dummy",
        plugin_version="1.0.0",
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def test_schedule_jobs_on_startup_filters_and_args(session: Session, caplog: pytest.LogCaptureFixture) -> None:
    target = _create_target(session)

    enabled_valid = _create_job(session, target.id, "Daily", "*/5 * * * *", enabled="true")
    enabled_invalid = _create_job(session, target.id, "Bad", "not a cron", enabled="true")
    disabled = _create_job(session, target.id, "Disabled", "0 2 * * *", enabled="false")

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
    # Back the scheduler's SessionLocal with the same engine/pool as this test session
    engine = session.get_bind()
    assert engine is not None
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    import app.core.scheduler as sched_mod

    monkeypatch.setattr(sched_mod, "SessionLocal", TestingSessionLocal, raising=True)

    target = _create_target(session)
    job = _create_job(session, target.id, "Now", "* * * * *", enabled="true")

    caplog.set_level(logging.INFO, logger="app.core.scheduler")
    _scheduled_job(job.id)

    # Query using a fresh session to reflect committed state
    check_db = TestingSessionLocal()
    try:
        runs = check_db.query(Run).filter(Run.job_id == job.id).all()
        assert len(runs) == 1
        run = runs[0]
        assert run.status == "success"
        assert run.started_at is not None
        assert run.finished_at is not None
        assert run.artifact_path is not None and run.artifact_path != ""
    finally:
        check_db.close()

    # Logs
    messages = [r.getMessage() for r in caplog.records]
    assert any("run_started" in m for m in messages)
    assert any("run_finished" in m for m in messages)


def test_run_job_immediately_shared_logic(session: Session, caplog: pytest.LogCaptureFixture) -> None:
    target = _create_target(session)
    job = _create_job(session, target.id, "Manual", "0 0 * * *", enabled="true")

    caplog.set_level(logging.INFO, logger="app.core.scheduler")
    run = run_job_immediately(session, job.id, triggered_by="manual_test")

    assert run.job_id == job.id
    assert run.status == "success"
    assert run.artifact_path is not None
    assert run.finished_at is not None

    messages = [r.getMessage() for r in caplog.records]
    assert any("run_started" in m for m in messages)
    assert any("run_finished" in m for m in messages)


def test_scheduled_job_missing_is_logged(monkeypatch: pytest.MonkeyPatch, session: Session, caplog: pytest.LogCaptureFixture) -> None:
    # Ensure scheduler uses the same DB
    engine = session.get_bind()
    assert engine is not None
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    import app.core.scheduler as sched_mod
    monkeypatch.setattr(sched_mod, "SessionLocal", TestingSessionLocal, raising=True)

    caplog.set_level(logging.INFO, logger="app.core.scheduler")
    _scheduled_job(999999)  # Nonexistent
    assert any("job_missing" in r.getMessage() for r in caplog.records)


