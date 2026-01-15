"""APScheduler configuration and job management.

Responsibilities:
- Provide a singleton `AsyncIOScheduler` instance
- Load enabled `Job`s from DB on startup and schedule them
- Execute a scheduled job by creating a `Run` row and invoking the configured plugin
- On errors (including missing plugin), mark the run as failed instead of creating dummy artifacts
- Provide helpers to trigger the same logic immediately (manual run)
"""

from __future__ import annotations

import json
import os
import hashlib
import logging
from datetime import datetime, timezone
import traceback
import asyncio
import threading
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.orm import Session

from app.core.db import SessionLocal
from app.models import Job as JobModel, Run as RunModel, Target as TargetModel
from app.models import MaintenanceJob as MaintenanceJobModel, MaintenanceRun as MaintenanceRunModel
from app.domain.enums import RunStatus, TargetRunStatus, RunOperation, TargetRunOperation, MaintenanceJobType
from app.core.plugins.base import BackupContext
from app.core.plugins.loader import get_plugin
from app.core.notifier import send_failure_email
from app.services.jobs import run_job_for_tag
from app.services.retention import apply_retention, apply_retention_all
from typing import Literal
from dataclasses import dataclass


logger = logging.getLogger(__name__)

# Global scheduler instance
_scheduler: Optional[AsyncIOScheduler] = None


# Unified scheduling abstraction (no DB inheritance)
@dataclass
class ScheduledItem:
    """DTO for items that can be scheduled by APScheduler."""
    kind: Literal["backup", "maintenance"]
    id: int
    name: str
    schedule_cron: str
    enabled: bool

    @staticmethod
    def from_backup_job(job: JobModel) -> "ScheduledItem":
        """Adapt a backup Job to ScheduledItem."""
        return ScheduledItem(
            kind="backup",
            id=job.id,
            name=job.name,
            schedule_cron=job.schedule_cron,
            enabled=job.enabled,
        )

    @staticmethod
    def from_maintenance_job(job: MaintenanceJobModel) -> "ScheduledItem":
        """Adapt a MaintenanceJob to ScheduledItem."""
        return ScheduledItem(
            kind="maintenance",
            id=job.id,
            name=job.name,
            schedule_cron=job.schedule_cron,
            enabled=job.enabled,
        )


def get_scheduler() -> AsyncIOScheduler:
    """Get the global scheduler instance."""
    global _scheduler
    if _scheduler is None:
        _scheduler = AsyncIOScheduler(
            timezone="Asia/Singapore",
            job_defaults={
                "coalesce": True,
                "max_instances": 1,
            },
        )
        # Log creation once to aid diagnostics in early startup
        _log_event(
            "scheduler_created",
            timezone="Asia/Singapore",
            coalesce=True,
            max_instances=1,
        )
    return _scheduler


def _log_event(event_name: str, **fields: object) -> None:
    """Emit a log line with text message and structured context via `extra`.

    The message is a concise 'event | k=v ...' line to keep parity with other
    modules, and the `extra` dict carries structured fields for future handlers.
    """
    # Build a readable message while still using lazy params
    if fields:
        keys = sorted(fields.keys())
        tmpl = " ".join(f"{k}=%s" for k in keys)
        values = tuple(fields[k] for k in keys)
        msg = "%s | " + tmpl
        args = (event_name, *values)

        # Avoid reserved LogRecord attribute collisions in `extra`
        reserved_keys = {
            "name",
            "msg",
            "message",
            "asctime",
            "levelname",
            "levelno",
            "pathname",
            "filename",
            "module",
            "exc_info",
            "exc_text",
            "stack_info",
            "lineno",
            "funcName",
            "created",
            "msecs",
            "relativeCreated",
            "thread",
            "threadName",
            "processName",
            "process",
            "args",
        }
        safe_extra: dict[str, object] = {"event": event_name}
        for k, v in fields.items():
            safe_key = k if k not in reserved_keys else f"field_{k}"
            safe_extra[safe_key] = v

        logger.info(msg, *args, extra=safe_extra)
    else:
        logger.info("%s", event_name, extra={"event": event_name})


def _create_run(db: Session, job: JobModel, triggered_by: str) -> RunModel:
    """Create and persist a Run row in running state for this job execution."""
    started_at = datetime.now(timezone.utc)
    run = RunModel(
        job_id=job.id,
        started_at=started_at,
        status=RunStatus.RUNNING.value,
        operation=RunOperation.BACKUP.value,
        message=f"Run started (triggered_by={triggered_by})",
        logs_text=f"Run started at {started_at.isoformat()} (triggered_by={triggered_by})",
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    _log_event(
        "run_started",
        job_id=job.id,
        run_id=run.id,
        triggered_by=triggered_by,
        started_at=run.started_at,
        job_name=job.name,
    )
    return run


def _perform_target_run(db: Session, job: JobModel, run: RunModel, *, target_id: int) -> dict:
    """Execute a plugin for a specific target and record a TargetRun; return summary dict."""
    from app.models import TargetRun as TargetRunModel

    started_at = datetime.now(timezone.utc)
    target_run = TargetRunModel(
        run_id=run.id,
        target_id=target_id,
        started_at=started_at,
        status=TargetRunStatus.RUNNING.value,
        operation=TargetRunOperation.BACKUP.value,
        message="Target run started",
        logs_text=f"Target run started at {started_at.isoformat()}",
    )
    db.add(target_run)
    db.commit()
    db.refresh(target_run)

    try:
        target = db.get(TargetModel, target_id)
        target_slug = target.slug if target is not None else f"target-{target_id}"
        config_dict = {}
        if target is not None:
            raw_cfg = target.plugin_config_json or "{}"
            try:
                config_dict = json.loads(raw_cfg)
            except Exception:
                config_dict = {}

        plugin_key = target.plugin_name if target is not None and target.plugin_name else None
        _log_event(
            "target_run_context",
            job_id=job.id,
            run_id=run.id,
            target_run_id=target_run.id,
            target_id=target_id,
            target_slug=target_slug,
            plugin=(plugin_key or "<missing>"),
            config_keys=sorted(list(config_dict.keys()))[:20],
            config_size=len(config_dict),
        )

        ctx = BackupContext(
            job_id=str(job.id),
            target_id=str(target_id),
            config=config_dict,
            metadata={"target_slug": target_slug},
        )
        if not plugin_key:
            raise KeyError("missing plugin on target")
        plugin = get_plugin(plugin_key)

        result_container: dict[str, object] = {}

        _log_event("plugin_start", job_id=job.id, run_id=run.id, target_run_id=target_run.id, plugin=plugin_key)

        def _runner() -> None:
            try:
                result_container["result"] = asyncio.run(plugin.backup(ctx))
            except Exception as exc:  # noqa: BLE001
                result_container["error"] = exc

        th = threading.Thread(target=_runner, daemon=True)
        th.start()
        th.join()
        if "error" in result_container:
            raise result_container["error"]  # type: ignore[misc]
        result = result_container.get("result")
        artifact_path = result.get("artifact_path") if isinstance(result, dict) else None  # type: ignore[assignment]
        if not artifact_path:
            raise RuntimeError("Plugin did not return artifact_path")

        finished_at = datetime.now(timezone.utc)
        target_run.finished_at = finished_at
        target_run.status = TargetRunStatus.SUCCESS.value
        target_run.message = "Run completed successfully"
        target_run.artifact_path = artifact_path
        # Populate artifact size and sha256 if file exists
        try:
            if artifact_path and os.path.exists(artifact_path):
                try:
                    target_run.artifact_bytes = int(os.path.getsize(artifact_path))
                except Exception:
                    target_run.artifact_bytes = None
                try:
                    sha256 = hashlib.sha256()
                    with open(artifact_path, "rb") as fobj:
                        for chunk in iter(lambda: fobj.read(1024 * 1024), b""):
                            sha256.update(chunk)
                    target_run.sha256 = sha256.hexdigest()
                except Exception:
                    target_run.sha256 = None
        except Exception:
            # Best-effort only; never fail run because of metadata
            pass
        target_run.logs_text = (target_run.logs_text or "") + f"\nCompleted at {finished_at.isoformat()}"
        db.add(target_run)
        db.commit()
        db.refresh(target_run)

        _log_event(
            "plugin_success",
            job_id=job.id,
            run_id=run.id,
            target_run_id=target_run.id,
            plugin=plugin_key,
            artifact_path=artifact_path,
        )
        
        # Apply retention cleanup for this job/target after successful backup
        try:
            retention_result = apply_retention(db, job.id, target_id)
            if retention_result.get("delete_count", 0) > 0:
                _log_event(
                    "retention_post_backup",
                    job_id=job.id,
                    target_id=target_id,
                    deleted=retention_result["delete_count"],
                    kept=retention_result["keep_count"],
                )
        except Exception as retention_exc:
            # Log but don't fail the backup due to retention errors
            _log_event(
                "retention_post_backup_error",
                job_id=job.id,
                target_id=target_id,
                error=str(retention_exc),
            )
        
        return {"target_id": target_id, "status": TargetRunStatus.SUCCESS.value, "error": None, "artifact_path": artifact_path}
    except KeyError as exc:
        finished_at = datetime.now(timezone.utc)
        target_run.finished_at = finished_at
        target_run.status = TargetRunStatus.FAILED.value
        target_run.message = "Run failed: missing plugin on target"
        # Do not set artifact fields for missing plugins
        target_run.logs_text = (target_run.logs_text or "") + f"\nFailed at {finished_at.isoformat()} with error: {exc}"
        db.add(target_run)
        db.commit()
        db.refresh(target_run)
        _log_event(
            "plugin_missing",
            job_id=job.id,
            run_id=run.id,
            target_run_id=target_run.id,
            plugin="<missing>",
            error=str(exc),
        )
        return {"target_id": target_id, "status": TargetRunStatus.FAILED.value, "error": "missing_plugin"}
    except Exception as exc:
        finished_at = datetime.now(timezone.utc)
        target_run.finished_at = finished_at
        target_run.status = TargetRunStatus.FAILED.value
        target_run.message = f"Run failed: {exc}"
        target_run.logs_text = (target_run.logs_text or "") + f"\nFailed at {finished_at.isoformat()} with error: {exc}"
        db.add(target_run)
        db.commit()
        db.refresh(target_run)
        try:
            subject = f"[Backup Failure] Job {job.id} — {job.name}"
            body = (
                f"Job ID: {job.id}\n"
                f"Run ID: {run.id}\n"
                f"Target ID: {target_id}\n"
                f"TargetRun ID: {target_run.id}\n"
                f"Started: {target_run.started_at}\n"
                f"Finished: {finished_at}\n"
                f"Error: {exc}\n"
            )
            send_failure_email(subject, body)
        except Exception:
            pass
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        _log_event(
            "plugin_error",
            job_id=job.id,
            run_id=run.id,
            target_run_id=target_run.id,
            error=str(exc),
            error_type=type(exc).__name__,
            traceback=tb,
        )
        logger.exception("Target run failed | job_id=%s run_id=%s target_id=%s", job.id, run.id, target_id)
        return {"target_id": target_id, "status": TargetRunStatus.FAILED.value, "error": str(exc)}
def _perform_run(db: Session, job: JobModel, triggered_by: str) -> RunModel:
    """Create a Run row, execute the job's plugin, and finalize status.

    This function is synchronous; scheduler will call it inside its own
    threadpool.
    """
    started_at = datetime.now(timezone.utc)

    run = RunModel(
        job_id=job.id,
        started_at=started_at,
        status=RunStatus.RUNNING.value,
        operation=RunOperation.BACKUP.value,
        message=f"Run started (triggered_by={triggered_by})",
        logs_text=f"Run started at {started_at.isoformat()} (triggered_by={triggered_by})",
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    _log_event(
        "run_started",
        job_id=job.id,
        run_id=run.id,
        triggered_by=triggered_by,
        started_at=run.started_at,
        job_name=job.name,
    )

    # Execute plugin if available; otherwise fall back to a dummy artifact.
    artifact_path: str
    try:
        # Load target for slug/config
        target = db.get(TargetModel, job.target_id)
        target_slug = target.slug if target is not None else f"target-{job.target_id}"
        # Use plugin-based target config only
        config_dict = {}
        if target is not None:
            raw_cfg = target.plugin_config_json or "{}"
            try:
                config_dict = json.loads(raw_cfg)
            except Exception:
                config_dict = {}

        # Emit context diagnostics (without dumping full config contents)
        plugin_key = target.plugin_name if target is not None and target.plugin_name else None
        _log_event(
            "run_context",
            job_id=job.id,
            run_id=run.id,
            target_id=job.target_id,
            target_slug=target_slug,
            plugin=(plugin_key or "<missing>"),
            config_keys=sorted(list(config_dict.keys()))[:20],
            config_size=len(config_dict),
        )

        # Build context per base class
        ctx = BackupContext(
            job_id=str(job.id),
            target_id=str(job.target_id),
            config=config_dict,
            metadata={"target_slug": target_slug},
        )

        # Resolve and invoke plugin strictly from target (jobs no longer carry plugin)
        if not plugin_key:
            # No plugin configured on target -> trigger legacy dummy branch below
            raise KeyError("missing plugin on target")
        plugin = get_plugin(plugin_key)

        # Plugin interface is async; execute in a dedicated thread with its own loop
        result_container: dict[str, object] = {}

        _log_event("plugin_start", job_id=job.id, run_id=run.id, plugin=plugin_key)

        def _runner() -> None:
            try:
                result_container["result"] = asyncio.run(plugin.backup(ctx))
            except Exception as exc:  # capture in-thread exceptions to re-raise in main thread
                result_container["error"] = exc

        th = threading.Thread(target=_runner, daemon=True)
        th.start()
        th.join()
        # Re-raise any error from the worker thread so our outer try/except handles it
        if "error" in result_container:
            raise result_container["error"]  # type: ignore[misc]
        result = result_container.get("result")
        artifact_path = result.get("artifact_path") if isinstance(result, dict) else None  # type: ignore[assignment]
        if not artifact_path:
            raise RuntimeError("Plugin did not return artifact_path")

        finished_at = datetime.now(timezone.utc)
        run.finished_at = finished_at
        run.status = RunStatus.SUCCESS.value
        run.message = "Run completed successfully"
        run.logs_text = (run.logs_text or "") + f"\nCompleted at {finished_at.isoformat()}"

        _log_event(
            "plugin_success",
            job_id=job.id,
            run_id=run.id,
            plugin=plugin_key,
        )
    except KeyError as exc:
        # Missing plugin — mark the run as failed; do not create dummy artifacts.
        finished_at = datetime.now(timezone.utc)
        run.finished_at = finished_at
        run.status = RunStatus.FAILED.value
        run.message = "Run failed: missing plugin on target"
        run.logs_text = (run.logs_text or "") + f"\nFailed at {finished_at.isoformat()} with error: {exc}"

        _log_event(
            "plugin_missing",
            job_id=job.id,
            run_id=run.id,
            plugin="<missing>",
            error=str(exc),
        )
    except Exception as exc:  # Catch-all failures → mark failed
        finished_at = datetime.now(timezone.utc)
        run.finished_at = finished_at
        run.status = RunStatus.FAILED.value
        run.message = f"Run failed: {exc}"
        run.logs_text = (run.logs_text or "") + f"\nFailed at {finished_at.isoformat()} with error: {exc}"

        # Fire-and-forget email notification (best-effort)
        try:
            subject = f"[Backup Failure] Job {job.id} — {job.name}"
            body = (
                f"Job ID: {job.id}\n"
                f"Job Name: {job.name}\n"
                f"Target ID: {job.target_id}\n"
                f"Started: {run.started_at}\n"
                f"Finished: {finished_at}\n"
                f"Error: {exc}\n"
            )
            send_failure_email(subject, body)
        except Exception:
            # Never let email issues affect job flow
            pass

        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        _log_event(
            "plugin_error",
            job_id=job.id,
            run_id=run.id,
            error=str(exc),
            error_type=type(exc).__name__,
            traceback=tb,
        )
        logger.exception("Run failed | job_id=%s run_id=%s", job.id, run.id)

    db.add(run)
    db.commit()
    db.refresh(run)

    duration_sec = None
    try:
        if run.started_at and run.finished_at:
            duration_sec = (run.finished_at - run.started_at).total_seconds()
    except Exception:
        duration_sec = None

        _log_event(
            "run_finished",
            job_id=job.id,
            run_id=run.id,
            triggered_by=triggered_by,
            finished_at=run.finished_at,
            status=run.status,
            duration_sec=duration_sec,
            message=run.message,
        )

    return run


def run_job_immediately(db: Session, job_id: int, triggered_by: str = "manual") -> RunModel:
    """Execute the job immediately and return the created Run row.

    This is used by the API "Run now" endpoint so both manual and scheduled
    runs share the exact same logic.
    """
    _log_event("manual_run_trigger", job_id=job_id, triggered_by=triggered_by)
    job = db.get(JobModel, job_id)
    if job is None:
        _log_event("manual_job_missing", job_id=job_id)
        raise ValueError("Job not found")
    # Create parent run and perform per-target runs
    run = _create_run(db, job, triggered_by)
    from app.services.jobs import resolve_tag_to_targets
    targets = resolve_tag_to_targets(db, job.tag_id)
    results: list[dict] = []
    for t in targets:
        results.append(_perform_target_run(db, job, run, target_id=int(t.id)))
    # Aggregate status to parent run
    try:
        run.finished_at = datetime.now(timezone.utc)
        total = len(results)
        success_count = sum(1 for r in results if r.get("status") == TargetRunStatus.SUCCESS.value)
        fail_count = sum(1 for r in results if r.get("status") == TargetRunStatus.FAILED.value)
        any_fail = fail_count > 0
        any_success = success_count > 0
        run.status = (
            RunStatus.PARTIAL.value if (any_fail and any_success) else (RunStatus.FAILED.value if any_fail else RunStatus.SUCCESS.value)
        )
        if run.status == RunStatus.SUCCESS.value:
            run.message = f"Completed successfully for {success_count}/{total} targets"
        elif run.status == RunStatus.PARTIAL.value:
            run.message = f"Partial: {success_count} succeeded, {fail_count} failed (of {total})"
        else:
            run.message = f"Failed: {fail_count}/{total} targets failed"
        db.add(run)
        db.commit()
        db.refresh(run)
    except Exception:
        pass
    # Always emit run_finished for manual runs regardless of aggregation errors
    try:
        _log_event(
            "run_finished",
            job_id=job.id,
            run_id=run.id,
            triggered_by=triggered_by,
            finished_at=run.finished_at,
            status=run.status,
            duration_sec=(
                (run.finished_at - run.started_at).total_seconds()
                if run.started_at and run.finished_at
                else None
            ),
            message=run.message,
        )
    except Exception:
        pass
    _log_event("manual_run_complete", job_id=job.id, run_id=run.id, status=run.status)
    return run


def _execute_job_for_target(db: Session, job: JobModel, run: RunModel, target_id: int) -> dict:
    """Execute a single job for a specific target and record a TargetRun."""
    return _perform_target_run(db, job, run, target_id=target_id)


def scheduled_tick_with_session(db: Session, job_id: int) -> dict:
    """Execute scheduler tick for a job using an existing DB session.

    Returns the summary dict from run_job_for_tag: {started: bool, results: [...]}.
    """
    _log_event("scheduled_tick_start", job_id=job_id)
    job = db.get(JobModel, job_id)
    if job is None:
        _log_event("job_missing", job_id=job_id)
        return {"started": False, "results": []}
    if not job.tag_id:
        _log_event("job_missing_tag", job_id=job_id)
        return {"started": False, "results": []}

    # Create parent run, then execute per-target runs and aggregate
    run = _create_run(db, job, triggered_by="scheduler")
    summary = run_job_for_tag(
        db,
        job_id=job.id,
        tag_id=job.tag_id,
        runner=lambda target: _perform_target_run(db, job, run, target_id=int(target.id)),
        max_concurrency=5,
        no_overlap=True,
    )
    results = summary.get("results", [])
    try:
        run.finished_at = datetime.now(timezone.utc)
        total = len(results)
        success_count = sum(1 for r in results if r.get("status") == TargetRunStatus.SUCCESS.value)
        fail_count = sum(1 for r in results if r.get("status") == TargetRunStatus.FAILED.value)
        any_fail = fail_count > 0
        any_success = success_count > 0
        run.status = (
            RunStatus.PARTIAL.value if (any_fail and any_success) else (RunStatus.FAILED.value if any_fail else RunStatus.SUCCESS.value)
        )
        if run.status == RunStatus.SUCCESS.value:
            run.message = f"Completed successfully for {success_count}/{total} targets"
        elif run.status == RunStatus.PARTIAL.value:
            run.message = f"Partial: {success_count} succeeded, {fail_count} failed (of {total})"
        else:
            run.message = f"Failed: {fail_count}/{total} targets failed"
        db.add(run)
        db.commit()
        db.refresh(run)
    except Exception:
        pass
    # Emit a run_finished event for scheduled runs as well
    try:
        duration_sec = None
        if run.started_at and run.finished_at:
            duration_sec = (run.finished_at - run.started_at).total_seconds()
        _log_event(
            "run_finished",
            job_id=job.id,
            run_id=run.id,
            triggered_by="scheduler",
            finished_at=run.finished_at,
            status=run.status,
            duration_sec=duration_sec,
            message=run.message,
        )
    except Exception:
        pass
    # Emit per-target outcomes for observability
    try:
        for r in results:
            _log_event(
                "scheduled_target_result",
                job_id=job.id,
                tag_id=job.tag_id,
                target_id=r.get("target_id"),
                status=r.get("status"),
                error=r.get("error"),
            )
    except Exception:
        pass
    _log_event(
        "scheduled_tick_done",
        job_id=job_id,
        tag_id=job.tag_id,
        started=summary.get("started"),
        resolved_count=len(results),
        deduped_count=len({r.get("target_id") for r in results}),
    )
    return summary


def scheduled_tick(job_id: int) -> None:
    """Entry point for APScheduler to execute tag-based jobs on tick."""
    from app.core.db import get_session
    db = next(get_session())
    try:
        scheduled_tick_with_session(db, job_id)
    finally:
        db.close()


def _scheduled_job(job_id: int) -> None:  # legacy-compatible shim for tests
    """Backward-compatible entry point used by existing tests.

    - If the job has a tag_id, execute the tag-based scheduled tick.
    - Otherwise, execute the legacy single-target _perform_run.
    """
    _log_event("scheduled_job_trigger", job_id=job_id)
    from app.core.db import get_session
    db = next(get_session())
    try:
        job = db.get(JobModel, job_id)
        if job is None:
            _log_event("job_missing", job_id=job_id)
            return
        if getattr(job, "tag_id", None):
            scheduled_tick_with_session(db, job_id)
            _log_event("scheduled_job_complete", job_id=job_id)
            return
        run = _perform_run(db, job, triggered_by="scheduler")
        _log_event(
            "scheduled_job_complete",
            job_id=job_id,
            run_id=getattr(run, "id", None),
            status=getattr(run, "status", None),
        )
    finally:
        db.close()


def execute_maintenance_job(maintenance_job_id: int) -> None:
    """Execute a maintenance job by ID.
    
    Creates a MaintenanceRun, executes the task based on job_type, and updates the run.
    """
    from app.core.db import get_session
    from app.services.maintenance import MaintenanceService
    import json
    
    db = next(get_session())
    try:
        svc = MaintenanceService(db)
        job = svc.get_job(maintenance_job_id)
        if job is None:
            _log_event("maintenance_job_missing", maintenance_job_id=maintenance_job_id)
            return
        
        # Create MaintenanceRun in running state
        started_at = datetime.now(timezone.utc)
        run = MaintenanceRunModel(
            maintenance_job_id=job.id,
            started_at=started_at,
            status=RunStatus.RUNNING.value,
            message=f"Maintenance job started: {job.name}",
        )
        db.add(run)
        db.commit()
        db.refresh(run)
        
        _log_event(
            "maintenance_run_started",
            maintenance_job_id=job.id,
            maintenance_run_id=run.id,
            job_type=job.job_type,
            job_name=job.name,
        )
        
        # Execute based on job_type
        try:
            if job.job_type == MaintenanceJobType.RETENTION_CLEANUP.value:
                result = apply_retention_all(db)
                # Update run with success
                run.finished_at = datetime.now(timezone.utc)
                run.status = RunStatus.SUCCESS.value
                run.message = f"Retention cleanup completed: {result.get('targets_processed', 0)} targets processed"
                run.result_json = json.dumps({
                    "targets_processed": result.get("targets_processed", 0),
                    "deleted_count": result.get("delete_count", 0),
                    "kept_count": result.get("keep_count", 0),
                    "deleted_paths": result.get("deleted_paths", []),
                })
                _log_event(
                    "maintenance_run_success",
                    maintenance_job_id=job.id,
                    maintenance_run_id=run.id,
                    targets_processed=result.get("targets_processed", 0),
                    deleted_count=result.get("delete_count", 0),
                )
            else:
                raise ValueError(f"Unknown maintenance job_type: {job.job_type}")
        except Exception as exc:
            # Update run with failure
            run.finished_at = datetime.now(timezone.utc)
            run.status = RunStatus.FAILED.value
            run.message = f"Maintenance job failed: {str(exc)}"
            run.result_json = json.dumps({
                "error": str(exc),
            })
            _log_event(
                "maintenance_run_failed",
                maintenance_job_id=job.id,
                maintenance_run_id=run.id,
                error=str(exc),
            )
        
        db.add(run)
        db.commit()
    except Exception as exc:
        _log_event("maintenance_job_execution_error", maintenance_job_id=maintenance_job_id, error=str(exc))
    finally:
        db.close()


def scheduled_dispatch(kind: str, job_id: int) -> None:
    """Unified dispatcher for scheduled jobs (backup or maintenance).
    
    Routes execution to the appropriate executor based on kind.
    """
    if kind == "backup":
        scheduled_tick(job_id)
    elif kind == "maintenance":
        execute_maintenance_job(job_id)
    else:
        _log_event("scheduled_dispatch_unknown_kind", kind=kind, job_id=job_id)


def nightly_retention_cleanup() -> None:
    """Legacy nightly retention cleanup (deprecated - use MaintenanceJob instead).
    
    This function is kept for backward compatibility but should be replaced
    by the MaintenanceJob with key='retention_cleanup_nightly'.
    """
    from app.core.db import get_session
    from app.services.maintenance import MaintenanceService
    
    db = next(get_session())
    try:
        # Try to find the nightly maintenance job and execute it
        svc = MaintenanceService(db)
        job = svc.get_job_by_key("retention_cleanup_nightly")
        if job and job.enabled:
            execute_maintenance_job(job.id)
        else:
            # Fallback to old behavior if job doesn't exist
            _log_event("nightly_retention_start")
            result = apply_retention_all(db)
            _log_event(
                "nightly_retention_done",
                targets=result.get("targets_processed", 0),
                deleted=result.get("delete_count", 0),
                kept=result.get("keep_count", 0),
            )
    except Exception as exc:
        _log_event("nightly_retention_error", error=str(exc))
    finally:
        db.close()


def schedule_jobs_on_startup(scheduler: AsyncIOScheduler, db: Session) -> None:
    """Load enabled jobs from DB (backup and maintenance) and schedule them with APScheduler.

    - Uses unified scheduling abstraction (ScheduledItem + dispatcher)
    - Uses cron in `jobs.schedule_cron` or `maintenance_jobs.schedule_cron`
    - Ensures `max_instances=1` via scheduler job_defaults or per-job arg
    """
    # In tests, a dummy scheduler may be provided without `add_job`.
    if not hasattr(scheduler, "add_job"):
        return

    # Load enabled backup jobs
    enabled_backup_jobs = db.query(JobModel).filter(JobModel.enabled.is_(True)).all()
    
    # Load enabled maintenance jobs
    enabled_maintenance_jobs = db.query(MaintenanceJobModel).filter(MaintenanceJobModel.enabled.is_(True)).all()

    _log_event(
        "scheduler_load_jobs_start",
        backup_jobs=len(enabled_backup_jobs),
        maintenance_jobs=len(enabled_maintenance_jobs),
    )

    # Adapt both types to ScheduledItem
    scheduled_items: list[ScheduledItem] = []
    for job in enabled_backup_jobs:
        scheduled_items.append(ScheduledItem.from_backup_job(job))
    for job in enabled_maintenance_jobs:
        scheduled_items.append(ScheduledItem.from_maintenance_job(job))

    scheduled_count: int = 0
    invalid_count: int = 0

    # Schedule all items using unified logic
    for item in scheduled_items:
        try:
            trigger = CronTrigger.from_crontab(item.schedule_cron)
        except Exception:
            _log_event("invalid_cron", kind=item.kind, job_id=item.id, schedule_cron=item.schedule_cron)
            invalid_count += 1
            continue

        # Use namespaced IDs to avoid collisions
        scheduler_id = f"{item.kind}:{item.id}"
        scheduler.add_job(
            func=scheduled_dispatch,
            trigger=trigger,
            id=scheduler_id,
            name=item.name,
            replace_existing=True,
            kwargs={"kind": item.kind, "job_id": item.id},
            max_instances=1,
        )
        scheduled_count += 1
        _log_event(
            "job_scheduled",
            kind=item.kind,
            job_id=item.id,
            scheduler_id=scheduler_id,
            name=item.name,
            schedule_cron=item.schedule_cron,
        )

    _log_event(
        "scheduler_load_jobs_done",
        backup_jobs=len(enabled_backup_jobs),
        maintenance_jobs=len(enabled_maintenance_jobs),
        scheduled=scheduled_count,
        invalid_cron=invalid_count,
    )


def reschedule_job(job_id: int, schedule_cron: str, enabled: bool = True) -> bool:
    """Reschedule a specific job with new cron expression.
    
    Returns True if successful, False if job not found or invalid cron.
    """
    scheduler = get_scheduler()
    if not hasattr(scheduler, "remove_job") or not hasattr(scheduler, "add_job"):
        return False
    
    try:
        # Remove existing job if it exists
        job_id_str = f"job:{job_id}"
        if scheduler.get_job(job_id_str):
            scheduler.remove_job(job_id_str)
            _log_event("job_removed", job_id=job_id)
        
        if not enabled:
            _log_event("job_disabled", job_id=job_id)
            return True
        
        # Parse and validate new cron
        trigger = CronTrigger.from_crontab(schedule_cron)
        
        # Add new job
        scheduler.add_job(
            func=scheduled_tick,
            trigger=trigger,
            id=job_id_str,
            name=f"Job {job_id}",
            replace_existing=True,
            kwargs={"job_id": job_id},
            max_instances=1,
        )
        
        _log_event("job_rescheduled", job_id=job_id, schedule_cron=schedule_cron)
        return True
        
    except Exception as exc:
        _log_event("job_reschedule_failed", job_id=job_id, schedule_cron=schedule_cron, error=str(exc))
        return False


def remove_job(job_id: int) -> bool:
    """Remove a job from the scheduler."""
    scheduler = get_scheduler()
    if not hasattr(scheduler, "remove_job"):
        return False
    
    try:
        job_id_str = f"job:{job_id}"
        if scheduler.get_job(job_id_str):
            scheduler.remove_job(job_id_str)
            _log_event("job_removed", job_id=job_id)
            return True
        return False
    except Exception as exc:
        _log_event("job_remove_failed", job_id=job_id, error=str(exc))
        return False


def get_scheduler_jobs() -> list[dict]:
    """Get list of currently scheduled jobs for debugging."""
    scheduler = get_scheduler()
    if not hasattr(scheduler, "get_jobs"):
        return []
    
    try:
        jobs = []
        for job in scheduler.get_jobs():
            jobs.append({
                "id": job.id,
                "name": job.name,
                "next_run_time": str(job.next_run_time) if job.next_run_time else None,
                "trigger": str(job.trigger),
            })
        return jobs
    except Exception:
        return []
