"""Prometheus metrics endpoint.

Exposes plain-text Prometheus metrics at `/metrics` without external deps.

Metrics per job:
- job_success_total{job_id, job_name}
- job_failure_total{job_id, job_name}
- last_run_timestamp{job_id, job_name}
"""

from __future__ import annotations

from typing import Dict, Tuple

from fastapi import APIRouter, Depends
from fastapi.responses import PlainTextResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.db import get_session
from app.models import Job as JobModel, Run as RunModel
from app.domain.enums import RunOperation


router = APIRouter(tags=["metrics"])


def _sanitize_label_value(value: str) -> str:
    """Sanitize label values for Prometheus exposition (very basic).

    - Escape backslashes and quotes
    - Trim to a reasonable length to avoid very large lines (optional)
    """
    safe = value.replace("\\", r"\\").replace("\"", r"\\\"")
    # Keep label values reasonably short
    return safe[:200]


@router.get("/metrics", response_class=PlainTextResponse)
def metrics(db: Session = Depends(get_session)) -> str:
    """Serve Prometheus metrics built from the database state."""
    # Preload jobs for labels
    jobs: Dict[int, str] = {j.id: j.name for j in db.query(JobModel).all()}

    # Success and failure counts per job
    success_counts: Dict[int, int] = {
        job_id: count
        for job_id, count in (
            db.query(RunModel.job_id, func.count())
            .filter(
                RunModel.status == "success",
                RunModel.operation == RunOperation.BACKUP.value,
            )
            .group_by(RunModel.job_id)
            .all()
        )
    }
    failure_counts: Dict[int, int] = {
        job_id: count
        for job_id, count in (
            db.query(RunModel.job_id, func.count())
            .filter(
                RunModel.status == "failed",
                RunModel.operation == RunOperation.BACKUP.value,
            )
            .group_by(RunModel.job_id)
            .all()
        )
    }

    # Last run timestamp per job (unix seconds). Use finished_at; fallback to started_at
    last_ts_rows: Tuple[int, float]
    last_ts: Dict[int, float] = {}
    for job_id, ts in (
        db.query(
            RunModel.job_id,
            func.max(func.coalesce(RunModel.finished_at, RunModel.started_at)),
        )
        .filter(RunModel.operation == RunOperation.BACKUP.value)
        .group_by(RunModel.job_id)
        .all()
    ):
        if ts is not None:
            try:
                last_ts[job_id] = ts.timestamp()  # type: ignore[assignment]
            except Exception:
                # In case of naive datetimes; SQLAlchemy/SQLite should give aware here
                last_ts[job_id] = float(0)

    # Build exposition text
    lines: list[str] = []

    lines.append("# HELP job_success_total Total number of successful job runs")
    lines.append("# TYPE job_success_total counter")
    for job_id, job_name in jobs.items():
        value = int(success_counts.get(job_id, 0))
        label_job = _sanitize_label_value(job_name)
        lines.append(
            f'job_success_total{{job_id="{job_id}",job_name="{label_job}"}} {value}'
        )

    lines.append("# HELP job_failure_total Total number of failed job runs")
    lines.append("# TYPE job_failure_total counter")
    for job_id, job_name in jobs.items():
        value = int(failure_counts.get(job_id, 0))
        label_job = _sanitize_label_value(job_name)
        lines.append(
            f'job_failure_total{{job_id="{job_id}",job_name="{label_job}"}} {value}'
        )

    lines.append("# HELP last_run_timestamp Unix timestamp of the last run per job")
    lines.append("# TYPE last_run_timestamp gauge")
    for job_id, job_name in jobs.items():
        value = float(last_ts.get(job_id, 0.0))
        label_job = _sanitize_label_value(job_name)
        lines.append(
            f'last_run_timestamp{{job_id="{job_id}",job_name="{label_job}"}} {value}'
        )

    body = "\n".join(lines) + "\n"
    return body

