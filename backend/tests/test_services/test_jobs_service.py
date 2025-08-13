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
