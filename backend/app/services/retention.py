"""Retention service: policy parsing, keep-set computation, and destructive cleanup.

This service implements tiered retention (daily/weekly/monthly) with configurable windows.
Retention is evaluated per (job_id, target_id) pair.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from sqlalchemy.orm import Session
from zoneinfo import ZoneInfo

from app.models import Run as RunModel, Settings as SettingsModel, Job as JobModel
from app.models.runs import TargetRun as TargetRunModel
from app.domain.enums import TargetRunStatus, TargetRunOperation


logger = logging.getLogger(__name__)

# Server timezone for bucket computation (matches scheduler)
SERVER_TZ = ZoneInfo("Asia/Singapore")


def _parse_retention_policy(policy_json: Optional[str]) -> Optional[Dict[str, Any]]:
    """Parse a retention policy JSON string into a dict.
    
    Returns None if policy_json is None/empty or invalid.
    """
    if not policy_json:
        return None
    try:
        policy = json.loads(policy_json)
        if isinstance(policy, dict) and "rules" in policy:
            return policy
        return None
    except (json.JSONDecodeError, TypeError):
        return None


def _get_effective_policy(db: Session, job_id: int) -> Optional[Dict[str, Any]]:
    """Get the effective retention policy for a job (job override or global).
    
    Returns None if no retention policy is configured (retention disabled).
    """
    # Check job-level override first
    job = db.get(JobModel, job_id)
    if job and job.retention_policy_json:
        policy = _parse_retention_policy(job.retention_policy_json)
        if policy:
            return policy
    
    # Fall back to global settings
    settings = db.query(SettingsModel).filter(SettingsModel.id == 1).first()
    if settings and settings.global_retention_policy_json:
        return _parse_retention_policy(settings.global_retention_policy_json)
    
    return None


def _get_bucket_key(dt: datetime, unit: str) -> Tuple[int, ...]:
    """Compute bucket key for a datetime based on unit (day/week/month).
    
    Returns a tuple that uniquely identifies the bucket.
    """
    dt_aware = _ensure_tz_aware(dt)
    local_dt = dt_aware.astimezone(SERVER_TZ)
    
    if unit == "day":
        return (local_dt.year, local_dt.month, local_dt.day)
    elif unit == "week":
        # ISO week: (year, week_number)
        iso_year, iso_week, _ = local_dt.isocalendar()
        return (iso_year, iso_week)
    elif unit == "month":
        return (local_dt.year, local_dt.month)
    else:
        # Default to day if unknown
        return (local_dt.year, local_dt.month, local_dt.day)


def _ensure_tz_aware(dt: datetime) -> datetime:
    """Ensure a datetime is timezone-aware, assuming UTC if naive."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _get_window_start(now: datetime, unit: str, window: int) -> datetime:
    """Compute the start of the retention window.
    
    For 'keep 1 per day for last 7 days', window=7 and we look back 7 days.
    """
    now_aware = _ensure_tz_aware(now)
    local_now = now_aware.astimezone(SERVER_TZ)
    
    if unit == "day":
        # Start of day N days ago
        start = local_now - timedelta(days=window)
    elif unit == "week":
        # Start of week N weeks ago
        start = local_now - timedelta(weeks=window)
    elif unit == "month":
        # Approximate: go back N*30 days
        start = local_now - timedelta(days=window * 30)
    else:
        start = local_now - timedelta(days=window)
    
    return start


def compute_keep_set(
    candidates: List[TargetRunModel],
    policy: Dict[str, Any],
    now: Optional[datetime] = None,
) -> Set[int]:
    """Compute which TargetRun IDs to keep based on retention policy.
    
    Uses union semantics: a backup is kept if it matches ANY rule.
    For each bucket, keeps the latest backup (by started_at).
    
    Args:
        candidates: List of TargetRun objects with artifact_path set
        policy: Parsed retention policy dict with 'rules' list
        now: Current time for window calculation (defaults to utcnow)
    
    Returns:
        Set of TargetRun IDs to keep
    """
    if now is None:
        now = datetime.now(SERVER_TZ)
    
    rules = policy.get("rules", [])
    if not rules:
        # No rules = keep everything (retention disabled)
        return {tr.id for tr in candidates}
    
    keep_ids: Set[int] = set()
    
    for rule in rules:
        unit = rule.get("unit", "day")
        window = rule.get("window", 1)
        keep_per_bucket = rule.get("keep", 1)
        
        window_start = _get_window_start(now, unit, window)
        
        # Group candidates by bucket (only those within window)
        buckets: Dict[Tuple[int, ...], List[TargetRunModel]] = {}
        for tr in candidates:
            if tr.started_at is None:
                continue
            # Ensure timezone-aware comparison
            tr_started = _ensure_tz_aware(tr.started_at)
            # Check if within window
            if tr_started < window_start:
                continue
            
            bucket_key = _get_bucket_key(tr.started_at, unit)
            if bucket_key not in buckets:
                buckets[bucket_key] = []
            buckets[bucket_key].append(tr)
        
        # For each bucket, keep the N latest
        for bucket_key, bucket_trs in buckets.items():
            # Sort by started_at descending (latest first)
            bucket_trs.sort(key=lambda x: _ensure_tz_aware(x.started_at) if x.started_at else datetime.min.replace(tzinfo=timezone.utc), reverse=True)
            for tr in bucket_trs[:keep_per_bucket]:
                keep_ids.add(tr.id)
    
    return keep_ids


def _delete_artifact(artifact_path: str) -> bool:
    """Delete an artifact file or directory and its sidecar metadata.
    
    Returns True if deletion was successful or file didn't exist.
    """
    success = True
    
    # Delete main artifact
    if os.path.exists(artifact_path):
        try:
            if os.path.isdir(artifact_path):
                shutil.rmtree(artifact_path)
            else:
                os.remove(artifact_path)
            logger.info("retention_artifact_deleted | path=%s", artifact_path)
        except Exception as exc:
            logger.error("retention_artifact_delete_failed | path=%s error=%s", artifact_path, exc)
            success = False
    
    # Delete sidecar metadata
    sidecar_path = f"{artifact_path}.meta.json"
    if os.path.exists(sidecar_path):
        try:
            os.remove(sidecar_path)
            logger.info("retention_sidecar_deleted | path=%s", sidecar_path)
        except Exception as exc:
            logger.error("retention_sidecar_delete_failed | path=%s error=%s", sidecar_path, exc)
            success = False
    
    return success


def apply_retention(
    db: Session,
    job_id: int,
    target_id: int,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Apply retention policy to backups for a specific (job_id, target_id) pair.
    
    Args:
        db: Database session
        job_id: Job ID to filter by
        target_id: Target ID to filter by
        dry_run: If True, compute but don't actually delete
    
    Returns:
        Dict with counts and paths:
        {
            "keep_count": int,
            "delete_count": int,
            "deleted_paths": List[str],
            "kept_paths": List[str],
        }
    """
    policy = _get_effective_policy(db, job_id)
    
    if policy is None:
        # No retention policy = keep everything
        logger.debug(
            "retention_skip_no_policy | job_id=%s target_id=%s",
            job_id, target_id
        )
        return {"keep_count": 0, "delete_count": 0, "deleted_paths": [], "kept_paths": []}
    
    # Query candidates: successful backup TargetRuns with artifact_path
    candidates = (
        db.query(TargetRunModel)
        .join(RunModel, TargetRunModel.run_id == RunModel.id)
        .filter(
            RunModel.job_id == job_id,
            TargetRunModel.target_id == target_id,
            TargetRunModel.operation == TargetRunOperation.BACKUP.value,
            TargetRunModel.status == TargetRunStatus.SUCCESS.value,
            TargetRunModel.artifact_path.isnot(None),
        )
        .all()
    )
    
    if not candidates:
        return {"keep_count": 0, "delete_count": 0, "deleted_paths": [], "kept_paths": []}
    
    # Compute keep set
    keep_ids = compute_keep_set(candidates, policy)
    
    # Partition into keep and delete
    to_keep = [tr for tr in candidates if tr.id in keep_ids]
    to_delete = [tr for tr in candidates if tr.id not in keep_ids]
    
    deleted_paths: List[str] = []
    kept_paths: List[str] = [tr.artifact_path for tr in to_keep if tr.artifact_path]
    
    if not dry_run:
        for tr in to_delete:
            if tr.artifact_path:
                _delete_artifact(tr.artifact_path)
                deleted_paths.append(tr.artifact_path)
            
            # Delete the TargetRun row
            run_id = tr.run_id
            db.delete(tr)
            db.flush()
            
            # Check if parent Run has any remaining TargetRuns
            remaining = db.query(TargetRunModel).filter(
                TargetRunModel.run_id == run_id
            ).count()
            
            if remaining == 0:
                # Delete the orphaned Run
                run = db.get(RunModel, run_id)
                if run:
                    db.delete(run)
                    logger.info(
                        "retention_run_deleted | run_id=%s (no remaining target_runs)",
                        run_id
                    )
            
            logger.info(
                "retention_targetrun_deleted | target_run_id=%s artifact_path=%s",
                tr.id, tr.artifact_path
            )
        
        db.commit()
    else:
        deleted_paths = [tr.artifact_path for tr in to_delete if tr.artifact_path]
    
    logger.info(
        "retention_applied | job_id=%s target_id=%s keep=%s delete=%s dry_run=%s",
        job_id, target_id, len(to_keep), len(to_delete), dry_run
    )
    
    return {
        "keep_count": len(to_keep),
        "delete_count": len(to_delete),
        "deleted_paths": deleted_paths,
        "kept_paths": kept_paths,
    }


def apply_retention_all(db: Session, dry_run: bool = False) -> Dict[str, Any]:
    """Apply retention to all distinct (job_id, target_id) pairs with backups.
    
    Used for nightly catch-up cleanup.
    
    Returns:
        Aggregate stats across all pairs, with targets_processed count.
    """
    # Get distinct (job_id, target_id) pairs
    pairs = (
        db.query(RunModel.job_id, TargetRunModel.target_id)
        .join(TargetRunModel, TargetRunModel.run_id == RunModel.id)
        .filter(
            RunModel.job_id.isnot(None),
            TargetRunModel.operation == TargetRunOperation.BACKUP.value,
            TargetRunModel.status == TargetRunStatus.SUCCESS.value,
            TargetRunModel.artifact_path.isnot(None),
        )
        .distinct()
        .all()
    )
    
    # Count distinct targets
    distinct_targets = {target_id for _, target_id in pairs if target_id is not None}
    
    total_keep = 0
    total_delete = 0
    all_deleted_paths: List[str] = []
    
    for job_id, target_id in pairs:
        if job_id is None:
            continue
        result = apply_retention(db, job_id, target_id, dry_run=dry_run)
        total_keep += result["keep_count"]
        total_delete += result["delete_count"]
        all_deleted_paths.extend(result["deleted_paths"])
    
    logger.info(
        "retention_all_applied | targets=%s pairs=%s keep=%s delete=%s dry_run=%s",
        len(distinct_targets), len(pairs), total_keep, total_delete, dry_run
    )
    
    return {
        "targets_processed": len(distinct_targets),
        "keep_count": total_keep,
        "delete_count": total_delete,
        "deleted_paths": all_deleted_paths,
    }


class RetentionService:
    """Service class for retention operations."""
    
    def __init__(self, db: Session) -> None:
        self.db = db
    
    def get_effective_policy(self, job_id: int) -> Optional[Dict[str, Any]]:
        """Get the effective retention policy for a job."""
        return _get_effective_policy(self.db, job_id)
    
    def apply_for_job_target(
        self,
        job_id: int,
        target_id: int,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """Apply retention for a specific job/target pair."""
        return apply_retention(self.db, job_id, target_id, dry_run=dry_run)
    
    def apply_all(self, dry_run: bool = False) -> Dict[str, Any]:
        """Apply retention to all job/target pairs (nightly cleanup)."""
        return apply_retention_all(self.db, dry_run=dry_run)
    
    def preview(self, job_id: int, target_id: int) -> Dict[str, Any]:
        """Preview what retention would delete (dry run)."""
        return apply_retention(self.db, job_id, target_id, dry_run=True)
