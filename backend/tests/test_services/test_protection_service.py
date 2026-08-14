from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.domain.enums import RunOperation, RunStatus, TargetRunOperation, TargetRunStatus
from app.models import Job, Run, Tag, Target, TargetRun, TargetTag
from app.services.protection import ProtectionSummaryService


def _target(db: Session, name: str) -> Target:
    target = Target(
        name=name,
        slug=name.lower().replace(" ", "-"),
        plugin_name="dummy",
        plugin_config_json="{}",
    )
    db.add(target)
    db.flush()
    return target


def _job_covering(db: Session, target: Target, *, cron: str, name: str) -> Job:
    tag = Tag(display_name=f"{name} tag")
    db.add(tag)
    db.flush()
    db.add(TargetTag(target_id=target.id, tag_id=tag.id, origin="DIRECT"))
    job = Job(
        tag_id=tag.id,
        name=name,
        schedule_cron=cron,
        enabled=True,
        created_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
    )
    db.add(job)
    db.flush()
    return job


def _attempt(
    db: Session,
    *,
    job: Job,
    target: Target,
    when: datetime,
    status: TargetRunStatus,
    valid_artifact: bool = False,
    run: Run | None = None,
) -> TargetRun:
    if run is None:
        run = Run(
            job_id=job.id,
            started_at=when,
            finished_at=when,
            status=status.value,
            operation=RunOperation.BACKUP.value,
        )
        db.add(run)
        db.flush()
    target_run = TargetRun(
        run_id=run.id,
        target_id=target.id,
        started_at=when,
        finished_at=when,
        status=status.value,
        operation=TargetRunOperation.BACKUP.value,
        message=None if status is TargetRunStatus.SUCCESS else "backup failed",
        artifact_path=f"/backups/{target.slug}/artifact.tar" if valid_artifact else None,
        artifact_bytes=128 if valid_artifact else None,
        sha256="a" * 64 if valid_artifact else None,
    )
    db.add(target_run)
    db.flush()
    return target_run


def test_summary_distinguishes_unscheduled_never_successful_and_missing(
    db_session: Session,
) -> None:
    now = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
    unscheduled = _target(db_session, "Unscheduled")
    never = _target(db_session, "Never")
    never_job = _job_covering(db_session, never, cron="0 0 * * *", name="Never job")
    missing = _target(db_session, "Missing")
    missing_job = _job_covering(db_session, missing, cron="* * * * *", name="Missing job")
    _attempt(
        db_session,
        job=missing_job,
        target=missing,
        when=now - timedelta(minutes=5),
        status=TargetRunStatus.SUCCESS,
        valid_artifact=True,
    )
    healthy = _target(db_session, "Healthy")
    healthy_job = _job_covering(db_session, healthy, cron="0 0 * * *", name="Healthy job")
    _attempt(
        db_session,
        job=healthy_job,
        target=healthy,
        when=now - timedelta(minutes=5),
        status=TargetRunStatus.SUCCESS,
        valid_artifact=True,
    )
    invalid = _target(db_session, "Invalid artifact")
    invalid_job = _job_covering(db_session, invalid, cron="0 0 * * *", name="Invalid job")
    _attempt(
        db_session,
        job=invalid_job,
        target=invalid,
        when=now - timedelta(minutes=5),
        status=TargetRunStatus.SUCCESS,
        valid_artifact=False,
    )
    db_session.commit()

    summaries = {
        row.target_id: row
        for row in ProtectionSummaryService(db_session).list_targets(now=now, tz_name="UTC")
    }

    assert summaries[unscheduled.id].gap_reason == "not_scheduled"
    assert summaries[never.id].gap_reason == "never_succeeded"
    assert summaries[missing.id].gap_reason == "scheduled_backup_missing"
    assert summaries[healthy.id].gap_reason is None
    assert summaries[invalid.id].gap_reason == "never_succeeded"
    assert summaries[healthy.id].latest_success is not None
    assert summaries[healthy.id].latest_success.age_seconds == 300
    assert summaries[healthy.id].next_run_at is not None
    assert summaries[never.id].covering_jobs[0].job_id == never_job.id


def test_summary_uses_final_attempt_per_run_for_consecutive_failures(
    db_session: Session,
) -> None:
    now = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
    target = _target(db_session, "Retry target")
    job = _job_covering(db_session, target, cron="0 0 * * *", name="Retry job")
    _attempt(
        db_session,
        job=job,
        target=target,
        when=now - timedelta(hours=3),
        status=TargetRunStatus.SUCCESS,
        valid_artifact=True,
    )
    _attempt(
        db_session,
        job=job,
        target=target,
        when=now - timedelta(hours=2),
        status=TargetRunStatus.FAILED,
    )
    retry_run = Run(
        job_id=job.id,
        started_at=now - timedelta(hours=1),
        finished_at=now - timedelta(hours=1),
        status=RunStatus.SUCCESS.value,
        operation=RunOperation.BACKUP.value,
    )
    db_session.add(retry_run)
    db_session.flush()
    _attempt(
        db_session,
        job=job,
        target=target,
        when=now - timedelta(hours=1, minutes=1),
        status=TargetRunStatus.FAILED,
        run=retry_run,
    )
    successful_retry = _attempt(
        db_session,
        job=job,
        target=target,
        when=now - timedelta(hours=1),
        status=TargetRunStatus.SUCCESS,
        valid_artifact=True,
        run=retry_run,
    )
    db_session.commit()

    summary = ProtectionSummaryService(db_session).list_targets(now=now, tz_name="UTC")[0]

    assert summary.latest_attempt is not None
    assert summary.latest_attempt.target_run_id == successful_retry.id
    assert summary.latest_attempt.status == TargetRunStatus.SUCCESS.value
    assert summary.consecutive_failures == 0


def test_missing_gap_waits_for_the_parent_run_to_finish(db_session: Session) -> None:
    now = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
    target = _target(db_session, "In progress target")
    job = _job_covering(db_session, target, cron="* * * * *", name="In progress job")
    _attempt(
        db_session,
        job=job,
        target=target,
        when=now - timedelta(minutes=5),
        status=TargetRunStatus.SUCCESS,
        valid_artifact=True,
    )
    active_run = Run(
        job_id=job.id,
        started_at=now - timedelta(seconds=30),
        status=RunStatus.RUNNING.value,
        operation=RunOperation.BACKUP.value,
    )
    db_session.add(active_run)
    db_session.flush()
    _attempt(
        db_session,
        job=job,
        target=target,
        when=now - timedelta(seconds=20),
        status=TargetRunStatus.FAILED,
        run=active_run,
    )
    db_session.commit()

    service = ProtectionSummaryService(db_session)
    summary = service.list_targets(now=now, tz_name="UTC")[0]
    assert summary.gap_reason is None

    active_run.status = RunStatus.FAILED.value
    active_run.finished_at = now
    db_session.add(active_run)
    db_session.commit()

    summary = service.list_targets(now=now, tz_name="UTC")[0]
    assert summary.gap_reason == "scheduled_backup_missing"
