"""Tests for the Prometheus protection metrics contract."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.api.metrics import _sanitize_label_value, metrics
from app.domain.enums import RunOperation, RunStatus, TargetRunOperation, TargetRunStatus
from app.models import Job, Run, Tag, Target, TargetRun, TargetTag


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


def _cover(db: Session, target: Target, name: str) -> Job:
    tag = Tag(display_name=f"{name} tag")
    db.add(tag)
    db.flush()
    db.add(TargetTag(target_id=target.id, tag_id=tag.id, origin="DIRECT"))
    job = Job(tag_id=tag.id, name=name, schedule_cron="0 0 * * *", enabled=True)
    db.add(job)
    db.flush()
    return job


def _attempt(
    db: Session,
    *,
    job: Job,
    target: Target,
    status: TargetRunStatus,
    valid_artifact: bool,
) -> None:
    when = datetime.now(timezone.utc) - timedelta(minutes=1)
    run = Run(
        job_id=job.id,
        started_at=when,
        finished_at=when,
        status=(
            RunStatus.SUCCESS.value if status is TargetRunStatus.SUCCESS else RunStatus.FAILED.value
        ),
        operation=RunOperation.BACKUP.value,
    )
    db.add(run)
    db.flush()
    db.add(
        TargetRun(
            run_id=run.id,
            target_id=target.id,
            started_at=when,
            finished_at=when,
            status=status.value,
            operation=TargetRunOperation.BACKUP.value,
            artifact_path=f"/backups/{target.slug}/artifact.tar" if valid_artifact else None,
            artifact_bytes=128 if valid_artifact else None,
            sha256="a" * 64 if valid_artifact else None,
        )
    )
    db.commit()


def test_sanitize_label_value() -> None:
    assert _sanitize_label_value("back\\slash") == "back\\\\slash"
    assert _sanitize_label_value('double"quote') == 'double\\"quote'
    assert len(_sanitize_label_value("a" * 300)) == 200


def test_metrics_empty_contract(db_session: Session) -> None:
    content = metrics(db_session)

    assert content.endswith("\n")
    assert "# HELP homelab_backup_target_covering_jobs" in content
    assert "# HELP homelab_backup_target_latest_attempt_info" in content
    assert "# HELP homelab_backup_target_last_success_timestamp_seconds" in content
    assert "# HELP homelab_backup_target_artifact_age_seconds" in content
    assert "# HELP homelab_backup_target_next_run_timestamp_seconds" in content
    assert "# HELP homelab_backup_target_consecutive_failures" in content
    assert "# HELP homelab_backup_target_gap_info" in content
    assert not any(line.startswith("homelab_backup_target_") for line in content.splitlines())


def test_metrics_export_the_same_target_protection_facts(db_session: Session) -> None:
    unscheduled = _target(db_session, 'Unscheduled "target"')
    healthy = _target(db_session, "Healthy target")
    healthy_job = _cover(db_session, healthy, "Healthy job")
    _attempt(
        db_session,
        job=healthy_job,
        target=healthy,
        status=TargetRunStatus.SUCCESS,
        valid_artifact=True,
    )
    failed = _target(db_session, "Failed target")
    failed_job = _cover(db_session, failed, "Failed job")
    _attempt(
        db_session,
        job=failed_job,
        target=failed,
        status=TargetRunStatus.FAILED,
        valid_artifact=False,
    )

    content = metrics(db_session)

    assert (
        f'homelab_backup_target_covering_jobs{{target_id="{unscheduled.id}",'
        'target_name="Unscheduled \\"target\\"",target_slug="unscheduled-\\"target\\""} 0'
        in content
    )
    assert (
        f'homelab_backup_target_gap_info{{target_id="{unscheduled.id}",'
        'target_name="Unscheduled \\"target\\"",target_slug="unscheduled-\\"target\\"",'
        'reason="not_scheduled"} 1' in content
    )
    assert (
        f'homelab_backup_target_latest_attempt_info{{target_id="{healthy.id}",'
        'target_name="Healthy target",target_slug="healthy-target",status="success"} 1' in content
    )
    assert (
        f'homelab_backup_target_last_success_timestamp_seconds{{target_id="{healthy.id}",'
        'target_name="Healthy target",target_slug="healthy-target"}' in content
    )
    assert (
        f'homelab_backup_target_artifact_age_seconds{{target_id="{healthy.id}",'
        'target_name="Healthy target",target_slug="healthy-target"}' in content
    )
    assert (
        f'homelab_backup_target_consecutive_failures{{target_id="{failed.id}",'
        'target_name="Failed target",target_slug="failed-target"} 1' in content
    )
    assert (
        f'homelab_backup_target_gap_info{{target_id="{failed.id}",'
        'target_name="Failed target",target_slug="failed-target",reason="never_succeeded"} 1'
        in content
    )
