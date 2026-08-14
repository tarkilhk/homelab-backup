from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.db import Base
from app.core.plugins.base import BackupContext, BackupPlugin, RestoreContext
from app.core.plugins.sidecar import write_backup_sidecar
from app.core.scheduler import scheduled_tick_with_session
from app.domain.enums import RunStatus, TargetRunStatus
from app.models import Job, Run, Tag, Target, TargetRun, TargetTag
from app.services.jobs import _get_job_lock


class _ArtifactPlugin(BackupPlugin):
    def __init__(self, artifact_root: Path) -> None:
        super().__init__(name="artifact-test")
        self.artifact_root = artifact_root

    async def validate_config(self, config: dict[str, Any]) -> bool:
        return True

    async def test(self, config: dict[str, Any]) -> bool:
        return True

    async def backup(self, context: BackupContext) -> dict[str, Any]:
        artifact_path = self.artifact_root / f"target-{context.target_id}.backup"
        artifact_path.write_bytes(f"backup:{context.target_id}".encode())
        write_backup_sidecar(str(artifact_path), self, context)
        return {"artifact_path": str(artifact_path)}

    async def restore(self, context: RestoreContext) -> dict[str, Any]:
        raise NotImplementedError

    async def get_status(self, context: BackupContext) -> dict[str, Any]:
        return {"status": "ready"}


def test_scheduled_multi_target_run_persists_every_result(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Concurrent targets must not share a SQLAlchemy Session."""

    engine = create_engine(
        f"sqlite:///{tmp_path / 'scheduler.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    session_factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)

    with session_factory() as setup:
        tag = Tag(display_name="concurrent")
        setup.add(tag)
        setup.flush()
        job = Job(
            tag_id=tag.id,
            name="Concurrent backup",
            schedule_cron="* * * * *",
            enabled=True,
        )
        setup.add(job)
        setup.flush()
        for index in range(5):
            target = Target(
                name=f"Target {index}",
                slug=f"target-{index}",
                plugin_name="artifact-test",
                plugin_config_json="{}",
            )
            setup.add(target)
            setup.flush()
            setup.add(TargetTag(target_id=target.id, tag_id=tag.id, origin="DIRECT"))
        setup.commit()
        job_id = int(job.id)

    plugin = _ArtifactPlugin(tmp_path / "artifacts")
    plugin.artifact_root.mkdir()
    monkeypatch.setattr("app.core.scheduler.get_plugin", lambda _name: plugin)

    with session_factory() as scheduler_session:
        summary = scheduled_tick_with_session(scheduler_session, job_id)

    assert summary["started"] is True
    assert len(summary["results"]) == 5
    assert {result["status"] for result in summary["results"]} == {TargetRunStatus.SUCCESS.value}

    with session_factory() as audit:
        runs = audit.query(Run).all()
        target_runs = audit.query(TargetRun).all()
        assert len(runs) == 1
        assert runs[0].status == RunStatus.SUCCESS.value
        assert len(target_runs) == 5
        assert len({target_run.id for target_run in target_runs}) == 5
        assert all(target_run.status == TargetRunStatus.SUCCESS.value for target_run in target_runs)


def test_overlapping_schedule_persists_a_skipped_run(tmp_path: Path) -> None:
    """An overlapping scheduler dispatch must remain visible in the audit ledger."""

    engine = create_engine(f"sqlite:///{tmp_path / 'overlap.db'}")
    session_factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    with session_factory() as setup:
        tag = Tag(display_name="overlap")
        setup.add(tag)
        setup.flush()
        job = Job(
            tag_id=tag.id,
            name="Overlap",
            schedule_cron="* * * * *",
            enabled=True,
        )
        setup.add(job)
        setup.commit()
        job_id = int(job.id)

    lock = _get_job_lock(job_id)
    assert lock.acquire(blocking=False) is True
    try:
        with session_factory() as scheduler_session:
            summary = scheduled_tick_with_session(scheduler_session, job_id)
    finally:
        lock.release()

    assert summary == {"started": False, "reason": "overlap", "results": []}
    with session_factory() as audit:
        runs = audit.query(Run).all()
        assert len(runs) == 1
        assert runs[0].status == RunStatus.SKIPPED.value
        assert runs[0].finished_at is not None
        assert runs[0].message == "Skipped: previous run is still in progress"


def test_scheduled_job_with_no_targets_persists_a_failed_run(tmp_path: Path) -> None:
    """An empty schedule must be a durable failure rather than a green no-op."""

    engine = create_engine(f"sqlite:///{tmp_path / 'no-targets.db'}")
    session_factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    with session_factory() as setup:
        tag = Tag(display_name="empty")
        setup.add(tag)
        setup.flush()
        job = Job(
            tag_id=tag.id,
            name="Empty backup",
            schedule_cron="* * * * *",
            enabled=True,
        )
        setup.add(job)
        setup.commit()
        job_id = int(job.id)

    with session_factory() as scheduler_session:
        summary = scheduled_tick_with_session(scheduler_session, job_id)

    assert summary == {"started": True, "reason": "no_targets", "results": []}
    with session_factory() as audit:
        run = audit.query(Run).one()
        assert run.status == RunStatus.FAILED.value
        assert run.finished_at is not None
        assert run.message == "Failed: no targets resolved for this job"


def test_scheduled_retry_persists_each_target_attempt(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A retry must preserve both the failed attempt and the eventual success."""

    engine = create_engine(
        f"sqlite:///{tmp_path / 'retry.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    session_factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)

    with session_factory() as setup:
        tag = Tag(display_name="retry")
        setup.add(tag)
        setup.flush()
        job = Job(
            tag_id=tag.id,
            name="Retry backup",
            schedule_cron="* * * * *",
            enabled=True,
        )
        target = Target(
            name="Retry target",
            slug="retry-target",
            plugin_name="artifact-test",
            plugin_config_json="{}",
        )
        setup.add_all([job, target])
        setup.flush()
        setup.add(TargetTag(target_id=target.id, tag_id=tag.id, origin="DIRECT"))
        setup.commit()
        job_id = int(job.id)

    plugin = _ArtifactPlugin(tmp_path / "retry-artifacts")
    plugin.artifact_root.mkdir()
    attempts = 0
    original_backup = plugin.backup

    async def fail_once(context: BackupContext) -> dict[str, Any]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("transient failure")
        return await original_backup(context)

    monkeypatch.setattr(plugin, "backup", fail_once)
    monkeypatch.setattr("app.core.scheduler.get_plugin", lambda _name: plugin)

    with session_factory() as scheduler_session:
        summary = scheduled_tick_with_session(scheduler_session, job_id)

    assert summary["results"][0]["status"] == TargetRunStatus.SUCCESS.value
    with session_factory() as audit:
        run = audit.query(Run).one()
        attempts_ledger = audit.query(TargetRun).order_by(TargetRun.id).all()
        assert run.status == RunStatus.SUCCESS.value
        assert [attempt.status for attempt in attempts_ledger] == [
            TargetRunStatus.FAILED.value,
            TargetRunStatus.SUCCESS.value,
        ]
