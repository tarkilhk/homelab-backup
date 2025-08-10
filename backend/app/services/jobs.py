from __future__ import annotations

from datetime import datetime
import logging
import threading
import queue
import time as _time
from typing import Callable, List, Optional, Any

from sqlalchemy.orm import Session

from zoneinfo import ZoneInfo
from apscheduler.triggers.cron import CronTrigger

from app.models import (
    Job as JobModel,
    Run as RunModel,
    Tag as TagModel,
    Target as TargetModel,
    TargetTag as TargetTagModel,
    validate_cron_expression,
)
from app.schemas import UpcomingJob


class JobService:
    """Business logic for Jobs.

    Provides CRUD convenience, upcoming-time computation, and manual run trigger.
    """

    def __init__(self, db: Session) -> None:
        self.db = db

    # CRUD
    def list(self) -> List[JobModel]:
        return list(self.db.query(JobModel).all())

    def get(self, job_id: int) -> Optional[JobModel]:
        return self.db.get(JobModel, job_id)

    def create(
        self,
        *,
        tag_id: int,
        name: str,
        schedule_cron: str,
        enabled: bool = True,
    ) -> JobModel:
        # Validate tag exists
        tag = self.db.get(TagModel, tag_id)
        if tag is None:
            raise KeyError("tag_not_found")
        # Validate cron
        validate_cron_expression(schedule_cron)
        job = JobModel(
            tag_id=tag_id,
            name=name,
            schedule_cron=schedule_cron,
            enabled=enabled,
        )
        self.db.add(job)
        self.db.commit()
        self.db.refresh(job)
        return job

    def update(self, job_id: int, **fields: object) -> JobModel:
        job = self.db.get(JobModel, job_id)
        if job is None:
            raise KeyError("job_not_found")
        # Field validations
        if "tag_id" in fields:
            tag_id_val = int(fields["tag_id"])  # type: ignore[call-arg]
            tag = self.db.get(TagModel, tag_id_val)
            if tag is None:
                raise KeyError("tag_not_found")
        if "schedule_cron" in fields:
            validate_cron_expression(str(fields["schedule_cron"]))
        for key, value in fields.items():
            setattr(job, key, value)
        self.db.add(job)
        self.db.commit()
        self.db.refresh(job)
        return job

    def delete(self, job_id: int) -> None:
        job = self.db.get(JobModel, job_id)
        if job is None:
            raise KeyError("job_not_found")
        # Preserve historical runs by reassigning them to an archived sentinel job
        archived_tag = self.db.query(TagModel).filter(TagModel.slug == "archived").first()
        if archived_tag is None:
            archived_tag = TagModel(display_name="Archived")
            self.db.add(archived_tag)
            self.db.commit()
            self.db.refresh(archived_tag)
        sentinel = (
            self.db.query(JobModel)
            .filter(JobModel.tag_id == archived_tag.id, JobModel.name == "Archived Job")
            .first()
        )
        if sentinel is None:
            sentinel = JobModel(tag_id=archived_tag.id, name="Archived Job", schedule_cron="0 0 1 1 *", enabled=False)
            self.db.add(sentinel)
            self.db.commit()
            self.db.refresh(sentinel)
        # Reassign runs to sentinel before deleting the job
        self.db.query(RunModel).filter(RunModel.job_id == job.id).update({RunModel.job_id: sentinel.id}, synchronize_session="fetch")
        self.db.commit()

        self.db.delete(job)
        self.db.commit()

    # Domain logic
    def upcoming(self, *, limit: int = 10, tz_name: str = "Asia/Singapore") -> List[UpcomingJob]:
        tz = ZoneInfo(tz_name)
        now = datetime.now(tz)
        rows = self.db.query(JobModel).filter(JobModel.enabled.is_(True)).all()
        results: list[UpcomingJob] = []
        for job in rows:
            try:
                trigger = CronTrigger.from_crontab(job.schedule_cron, timezone=tz)
                next_time = trigger.get_next_fire_time(previous_fire_time=None, now=now)
                if next_time is None:
                    continue
                results.append(
                    UpcomingJob(
                        job_id=job.id,
                        name=job.name,
                        next_run_at=next_time,
                    )
                )
            except Exception:
                continue
        results.sort(key=lambda r: r.next_run_at)
        return results[:limit]

    def run_now(self, job_id: int, *, triggered_by: str = "manual_api") -> RunModel:
        # Execute via scheduler path to ensure plugin behavior and failure handling are consistent.
        job = self.db.get(JobModel, job_id)
        if job is None:
            raise ValueError("Job not found")
        from app.core.scheduler import run_job_immediately
        return run_job_immediately(self.db, job_id=job.id, triggered_by=triggered_by)


_log = logging.getLogger(__name__)
_job_locks: dict[int, threading.Lock] = {}
_job_locks_guard = threading.Lock()


def _get_job_lock(job_id: int) -> threading.Lock:
    with _job_locks_guard:
        lk = _job_locks.get(job_id)
        if lk is None:
            lk = threading.Lock()
            _job_locks[job_id] = lk
        return lk


def resolve_tag_to_targets(db: Session, tag_id: int) -> List[TargetModel]:
    """Return distinct targets that currently have the given tag via any origin."""
    q = (
        db.query(TargetModel)
        .join(TargetTagModel, TargetTagModel.target_id == TargetModel.id)
        .filter(TargetTagModel.tag_id == tag_id)
    )
    seen: set[int] = set()
    result: list[TargetModel] = []
    for t in q.all():
        if t.id not in seen:
            seen.add(int(t.id))
            result.append(t)
    return result


def run_job_for_tag(
    db: Session,
    job_id: int,
    tag_id: int,
    *,
    runner: Callable[[TargetModel], Any],
    max_concurrency: int = 5,
    no_overlap: bool = True,
    max_retries: int = 1,
    sleep_fn: Callable[[float], None] = _time.sleep,
    backoff_base: float = 0.05,
) -> dict:
    """Execute a job for all current targets under a tag with bounded concurrency and retries."""
    lock = _get_job_lock(job_id)
    acquired = lock.acquire(blocking=False) if no_overlap else lock.acquire(blocking=True)
    if not acquired:
        _log.info("job_run_skip_overlap | job_id=%s", job_id, extra={"event": "job_run_skip_overlap", "job_id": job_id})
        return {"started": False, "results": []}
    try:
        _log.info("job_run_start | job_id=%s tag_id=%s", job_id, tag_id, extra={"event": "job_run_start", "job_id": job_id, "tag_id": tag_id})
        targets = resolve_tag_to_targets(db, tag_id)
        if max_concurrency < 1:
            max_concurrency = 1
        work: "queue.Queue[TargetModel]" = queue.Queue()
        for t in targets:
            work.put(t)
        results_lock = threading.Lock()
        results: list[dict] = []

        def worker() -> None:
            while True:
                try:
                    t = work.get_nowait()
                except queue.Empty:
                    break
                status = "failed"
                last_err: Optional[BaseException] = None
                artifact_path: Optional[str] = None
                for attempt in range(0, max_retries + 1):
                    try:
                        res = runner(t)
                        status = "success"
                        if isinstance(res, dict):
                            ap = res.get("artifact_path")
                            if isinstance(ap, str):
                                artifact_path = ap
                        last_err = None
                        break
                    except BaseException as exc:  # noqa: BLE001
                        last_err = exc
                        if attempt < max_retries:
                            sleep_fn(max(backoff_base * (2 ** attempt), 0.0))
                        else:
                            break
                with results_lock:
                    results.append({"target_id": t.id, "status": status, "error": str(last_err) if last_err else None, "artifact_path": artifact_path})
                work.task_done()

        threads: list[threading.Thread] = []
        for _ in range(min(len(targets), max_concurrency)):
            th = threading.Thread(target=worker, daemon=True)
            threads.append(th)
            th.start()
        for th in threads:
            th.join()
        _log.info(
            "job_run_done | job_id=%s target_count=%s",
            job_id,
            len(targets),
            extra={"event": "job_run_done", "job_id": job_id, "target_count": len(targets)},
        )
        return {"started": True, "results": results}
    finally:
        if acquired:
            try:
                lock.release()
            except Exception:
                pass
