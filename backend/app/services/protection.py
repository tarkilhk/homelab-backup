from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import cast
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger  # type: ignore[import-untyped]
from sqlalchemy.orm import Session

from app.domain.enums import (
    ProtectionGapReason,
    RunOperation,
    RunStatus,
    TargetRunOperation,
    TargetRunStatus,
)
from app.models import Job, Run, Target, TargetRun, TargetTag
from app.schemas.protection import (
    BackupAttemptSummary,
    CoveringJobSummary,
    TargetProtectionSummary,
    ValidatedBackupSummary,
)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _next_fire(job: Job, *, after: datetime, tz: ZoneInfo) -> datetime | None:
    trigger = CronTrigger.from_crontab(cast(str, job.schedule_cron), timezone=tz)
    local_after = _aware_utc(after).astimezone(tz)
    return cast(
        datetime | None,
        trigger.get_next_fire_time(previous_fire_time=None, now=local_after),
    )


def _is_valid_success(attempt: TargetRun) -> bool:
    return bool(
        attempt.status == TargetRunStatus.SUCCESS.value
        and attempt.finished_at is not None
        and attempt.artifact_path
        and attempt.artifact_bytes is not None
        and attempt.artifact_bytes > 0
        and attempt.sha256
        and len(attempt.sha256) == 64
    )


class ProtectionSummaryService:
    """Derive target protection facts from schedules and the execution ledger."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def list_targets(
        self,
        *,
        now: datetime | None = None,
        tz_name: str = "Asia/Singapore",
    ) -> list[TargetProtectionSummary]:
        observed_at = _aware_utc(now or datetime.now(timezone.utc))
        tz = ZoneInfo(tz_name)
        targets = self.db.query(Target).order_by(Target.name, Target.id).all()
        return [self._summarize(target, now=observed_at, tz=tz) for target in targets]

    def _covering_jobs(self, target_id: int) -> list[Job]:
        return list(
            self.db.query(Job)
            .join(TargetTag, TargetTag.tag_id == Job.tag_id)
            .filter(TargetTag.target_id == target_id, Job.enabled.is_(True))
            .distinct()
            .order_by(Job.id)
            .all()
        )

    def _attempts(self, target_id: int) -> list[TargetRun]:
        return list(
            self.db.query(TargetRun)
            .filter(
                TargetRun.target_id == target_id,
                TargetRun.operation == TargetRunOperation.BACKUP.value,
            )
            .order_by(TargetRun.started_at.desc(), TargetRun.id.desc())
            .all()
        )

    def _summarize(
        self,
        target: Target,
        *,
        now: datetime,
        tz: ZoneInfo,
    ) -> TargetProtectionSummary:
        jobs = self._covering_jobs(int(target.id))
        attempts = self._attempts(int(target.id))
        latest_attempt = attempts[0] if attempts else None
        successful_attempt = next(
            (attempt for attempt in attempts if _is_valid_success(attempt)), None
        )

        job_summaries: list[CoveringJobSummary] = []
        for job in jobs:
            next_run = _next_fire(job, after=now, tz=tz)
            if next_run is None:
                continue
            job_summaries.append(
                CoveringJobSummary(
                    job_id=int(job.id),
                    name=cast(str, job.name),
                    schedule_cron=cast(str, job.schedule_cron),
                    next_run_at=next_run,
                )
            )
        next_run_at = min((job.next_run_at for job in job_summaries), default=None)

        latest_attempt_summary = None
        if latest_attempt is not None:
            latest_attempt_summary = BackupAttemptSummary(
                run_id=int(latest_attempt.run_id),
                target_run_id=int(latest_attempt.id),
                started_at=_aware_utc(cast(datetime, latest_attempt.started_at)),
                finished_at=(
                    _aware_utc(cast(datetime, latest_attempt.finished_at))
                    if latest_attempt.finished_at is not None
                    else None
                ),
                status=cast(str, latest_attempt.status),
                message=cast(str | None, latest_attempt.message),
            )

        latest_success_summary = None
        if successful_attempt is not None:
            finished_at = _aware_utc(cast(datetime, successful_attempt.finished_at))
            latest_success_summary = ValidatedBackupSummary(
                run_id=int(successful_attempt.run_id),
                target_run_id=int(successful_attempt.id),
                finished_at=finished_at,
                artifact_path=str(successful_attempt.artifact_path),
                artifact_bytes=int(successful_attempt.artifact_bytes),
                sha256=str(successful_attempt.sha256),
                age_seconds=max((now - finished_at).total_seconds(), 0.0),
            )

        gap_reason: ProtectionGapReason | None
        if not jobs:
            gap_reason = ProtectionGapReason.NOT_SCHEDULED
        elif successful_attempt is None:
            gap_reason = ProtectionGapReason.NEVER_SUCCEEDED
        elif self._scheduled_backup_is_missing(
            jobs,
            successful_attempt=successful_attempt,
            now=now,
            tz=tz,
        ):
            gap_reason = ProtectionGapReason.SCHEDULED_BACKUP_MISSING
        else:
            gap_reason = None

        return TargetProtectionSummary(
            target_id=int(target.id),
            target_name=cast(str, target.name),
            target_slug=cast(str, target.slug),
            plugin_name=cast(str | None, target.plugin_name),
            covering_jobs=job_summaries,
            latest_attempt=latest_attempt_summary,
            latest_success=latest_success_summary,
            next_run_at=next_run_at,
            consecutive_failures=self._consecutive_failures(attempts),
            gap_reason=gap_reason,
        )

    @staticmethod
    def _consecutive_failures(attempts: list[TargetRun]) -> int:
        final_attempts_by_run: list[TargetRun] = []
        seen_runs: set[int] = set()
        for attempt in attempts:
            run_id = int(attempt.run_id)
            if run_id in seen_runs:
                continue
            seen_runs.add(run_id)
            final_attempts_by_run.append(attempt)

        failures = 0
        for attempt in final_attempts_by_run:
            if attempt.status == TargetRunStatus.RUNNING.value:
                continue
            if attempt.status == TargetRunStatus.FAILED.value:
                failures += 1
                continue
            if attempt.status == TargetRunStatus.SUCCESS.value:
                break
        return failures

    def _scheduled_backup_is_missing(
        self,
        jobs: list[Job],
        *,
        successful_attempt: TargetRun,
        now: datetime,
        tz: ZoneInfo,
    ) -> bool:
        success_at = _aware_utc(cast(datetime, successful_attempt.finished_at))
        due_times: list[datetime] = []
        for job in jobs:
            created_at = _aware_utc(cast(datetime, job.created_at))
            anchor = max(success_at, created_at) + timedelta(microseconds=1)
            due_at = _next_fire(job, after=anchor, tz=tz)
            if due_at is not None and due_at <= now.astimezone(tz):
                due_times.append(_aware_utc(due_at))
        if not due_times:
            return False

        active_run = (
            self.db.query(Run)
            .filter(
                Run.job_id.in_([int(job.id) for job in jobs]),
                Run.operation == RunOperation.BACKUP.value,
                Run.status == RunStatus.RUNNING.value,
            )
            .first()
        )
        if active_run is not None:
            return False
        return True
