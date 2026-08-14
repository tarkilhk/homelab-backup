"""Prometheus metrics derived from the target protection summary."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from app.core.db import get_session
from app.schemas.protection import TargetProtectionSummary
from app.services.protection import ProtectionSummaryService

router = APIRouter(tags=["metrics"])


def _sanitize_label_value(value: str) -> str:
    """Escape a value for Prometheus text-format labels."""
    return value.replace("\\", r"\\").replace('"', r"\"").replace("\n", r"\n")[:200]


def _labels(summary: TargetProtectionSummary) -> str:
    return (
        f'target_id="{summary.target_id}",'
        f'target_name="{_sanitize_label_value(summary.target_name)}",'
        f'target_slug="{_sanitize_label_value(summary.target_slug)}"'
    )


@router.get("/metrics", response_class=PlainTextResponse)
def metrics(db: Session = Depends(get_session)) -> str:
    """Serve the same target protection facts used by the Dashboard."""
    summaries = ProtectionSummaryService(db).list_targets()
    lines = [
        "# HELP homelab_backup_target_covering_jobs Enabled jobs currently covering the target",
        "# TYPE homelab_backup_target_covering_jobs gauge",
        "# HELP homelab_backup_target_latest_attempt_info Latest backup attempt outcome",
        "# TYPE homelab_backup_target_latest_attempt_info gauge",
        "# HELP homelab_backup_target_last_attempt_timestamp_seconds Unix timestamp of the latest backup attempt",
        "# TYPE homelab_backup_target_last_attempt_timestamp_seconds gauge",
        "# HELP homelab_backup_target_last_success_timestamp_seconds Unix timestamp of the latest validated backup",
        "# TYPE homelab_backup_target_last_success_timestamp_seconds gauge",
        "# HELP homelab_backup_target_artifact_age_seconds Age of the latest validated backup artifact",
        "# TYPE homelab_backup_target_artifact_age_seconds gauge",
        "# HELP homelab_backup_target_next_run_timestamp_seconds Unix timestamp of the next scheduled backup",
        "# TYPE homelab_backup_target_next_run_timestamp_seconds gauge",
        "# HELP homelab_backup_target_consecutive_failures Consecutive completed backup runs that failed for the target",
        "# TYPE homelab_backup_target_consecutive_failures gauge",
        "# HELP homelab_backup_target_gap_info Current protection gap reason",
        "# TYPE homelab_backup_target_gap_info gauge",
    ]

    for summary in summaries:
        labels = _labels(summary)
        lines.append(
            f"homelab_backup_target_covering_jobs{{{labels}}} {len(summary.covering_jobs)}"
        )
        lines.append(
            "homelab_backup_target_consecutive_failures"
            f"{{{labels}}} {summary.consecutive_failures}"
        )
        if summary.latest_attempt is not None:
            status = _sanitize_label_value(summary.latest_attempt.status)
            lines.append(
                f'homelab_backup_target_latest_attempt_info{{{labels},status="{status}"}} 1'
            )
            lines.append(
                "homelab_backup_target_last_attempt_timestamp_seconds"
                f"{{{labels}}} {summary.latest_attempt.started_at.timestamp():.6f}"
            )
        if summary.latest_success is not None:
            lines.append(
                "homelab_backup_target_last_success_timestamp_seconds"
                f"{{{labels}}} {summary.latest_success.finished_at.timestamp():.6f}"
            )
            lines.append(
                "homelab_backup_target_artifact_age_seconds"
                f"{{{labels}}} {summary.latest_success.age_seconds:.6f}"
            )
        if summary.next_run_at is not None:
            lines.append(
                "homelab_backup_target_next_run_timestamp_seconds"
                f"{{{labels}}} {summary.next_run_at.timestamp():.6f}"
            )
        if summary.gap_reason is not None:
            reason = _sanitize_label_value(summary.gap_reason.value)
            lines.append(f'homelab_backup_target_gap_info{{{labels},reason="{reason}"}} 1')

    return "\n".join(lines) + "\n"
