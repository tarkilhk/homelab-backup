"""Tests for retention service: policy parsing, keep-set computation, and cleanup."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session

from app.models import Job as JobModel, Run as RunModel, Tag as TagModel, Target as TargetModel, Settings as SettingsModel
from app.models.runs import TargetRun as TargetRunModel
from app.domain.enums import RunStatus, TargetRunStatus, RunOperation, TargetRunOperation
from app.services.retention import (
    RetentionService,
    compute_keep_set,
    apply_retention,
    _parse_retention_policy,
    _get_effective_policy,
    SERVER_TZ,
)


def _create_tag(db: Session, name: str = "test-tag") -> TagModel:
    """Create a tag for testing."""
    tag = TagModel(display_name=name)
    db.add(tag)
    db.commit()
    db.refresh(tag)
    return tag


def _create_target(db: Session, name: str = "test-target") -> TargetModel:
    """Create a target for testing."""
    target = TargetModel(name=name, slug=name.lower().replace(" ", "-"), plugin_name="pihole", plugin_config_json="{}")
    db.add(target)
    db.commit()
    db.refresh(target)
    return target


def _create_job(db: Session, tag: TagModel, retention_json: str | None = None) -> JobModel:
    """Create a job for testing."""
    job = JobModel(
        tag_id=tag.id,
        name="Test Job",
        schedule_cron="0 2 * * *",
        enabled=True,
        retention_policy_json=retention_json,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def _create_run_with_target_run(
    db: Session,
    job: JobModel,
    target: TargetModel,
    started_at: datetime,
    artifact_path: str | None = None,
) -> tuple[RunModel, TargetRunModel]:
    """Create a run and target_run for testing."""
    run = RunModel(
        job_id=job.id,
        started_at=started_at,
        finished_at=started_at + timedelta(minutes=5),
        status=RunStatus.SUCCESS.value,
        operation=RunOperation.BACKUP.value,
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    
    target_run = TargetRunModel(
        run_id=run.id,
        target_id=target.id,
        started_at=started_at,
        finished_at=started_at + timedelta(minutes=5),
        status=TargetRunStatus.SUCCESS.value,
        operation=TargetRunOperation.BACKUP.value,
        artifact_path=artifact_path,
        artifact_bytes=1024 if artifact_path else None,
    )
    db.add(target_run)
    db.commit()
    db.refresh(target_run)
    
    return run, target_run


class TestParsePolicyJson:
    """Tests for _parse_retention_policy function."""
    
    def test_parse_valid_policy(self):
        """Valid policy JSON is parsed correctly."""
        policy_json = '{"rules": [{"unit": "day", "window": 7, "keep": 1}]}'
        result = _parse_retention_policy(policy_json)
        assert result is not None
        assert "rules" in result
        assert len(result["rules"]) == 1
    
    def test_parse_empty_string_returns_none(self):
        """Empty string returns None."""
        assert _parse_retention_policy("") is None
    
    def test_parse_none_returns_none(self):
        """None returns None."""
        assert _parse_retention_policy(None) is None
    
    def test_parse_invalid_json_returns_none(self):
        """Invalid JSON returns None."""
        assert _parse_retention_policy("not valid json") is None
    
    def test_parse_missing_rules_returns_none(self):
        """JSON without rules key returns None."""
        assert _parse_retention_policy('{"something": "else"}') is None


class TestGetEffectivePolicy:
    """Tests for _get_effective_policy function."""
    
    def test_job_override_takes_precedence(self, db_session: Session):
        """Job-level retention policy overrides global."""
        # Create global settings
        settings = SettingsModel(
            id=1,
            global_retention_policy_json='{"rules": [{"unit": "day", "window": 30, "keep": 1}]}',
        )
        db_session.add(settings)
        db_session.commit()
        
        # Create job with override
        tag = _create_tag(db_session)
        job = _create_job(
            db_session,
            tag,
            retention_json='{"rules": [{"unit": "day", "window": 7, "keep": 1}]}',
        )
        
        policy = _get_effective_policy(db_session, job.id)
        assert policy is not None
        assert policy["rules"][0]["window"] == 7  # Job override, not global 30
    
    def test_falls_back_to_global_when_no_job_override(self, db_session: Session):
        """Falls back to global settings when job has no override."""
        settings = SettingsModel(
            id=1,
            global_retention_policy_json='{"rules": [{"unit": "month", "window": 6, "keep": 1}]}',
        )
        db_session.add(settings)
        db_session.commit()
        
        tag = _create_tag(db_session)
        job = _create_job(db_session, tag, retention_json=None)
        
        policy = _get_effective_policy(db_session, job.id)
        assert policy is not None
        assert policy["rules"][0]["unit"] == "month"
        assert policy["rules"][0]["window"] == 6
    
    def test_returns_none_when_no_policy_configured(self, db_session: Session):
        """Returns None when neither job nor global policy exists."""
        tag = _create_tag(db_session)
        job = _create_job(db_session, tag, retention_json=None)
        
        policy = _get_effective_policy(db_session, job.id)
        assert policy is None


class TestComputeKeepSet:
    """Tests for compute_keep_set function."""
    
    def test_empty_candidates_returns_empty_set(self):
        """Empty candidate list returns empty keep set."""
        policy = {"rules": [{"unit": "day", "window": 7, "keep": 1}]}
        result = compute_keep_set([], policy)
        assert result == set()
    
    def test_no_rules_keeps_everything(self, db_session: Session):
        """Policy with no rules keeps all backups."""
        tag = _create_tag(db_session)
        target = _create_target(db_session)
        job = _create_job(db_session, tag)
        
        now = datetime.now(timezone.utc)
        _, tr1 = _create_run_with_target_run(db_session, job, target, now - timedelta(days=1), "/backup1")
        _, tr2 = _create_run_with_target_run(db_session, job, target, now - timedelta(days=2), "/backup2")
        
        policy = {"rules": []}
        result = compute_keep_set([tr1, tr2], policy)
        assert tr1.id in result
        assert tr2.id in result
    
    def test_daily_rule_keeps_latest_per_day(self, db_session: Session):
        """Daily rule keeps only the latest backup per day."""
        tag = _create_tag(db_session)
        target = _create_target(db_session)
        job = _create_job(db_session, tag)
        
        now = datetime.now(SERVER_TZ)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        
        # Two backups on same day - should keep only the later one
        _, tr1 = _create_run_with_target_run(db_session, job, target, today_start + timedelta(hours=2), "/backup1")
        _, tr2 = _create_run_with_target_run(db_session, job, target, today_start + timedelta(hours=8), "/backup2")
        
        policy = {"rules": [{"unit": "day", "window": 1, "keep": 1}]}
        result = compute_keep_set([tr1, tr2], policy, now=now)
        
        # Only the later backup (tr2) should be kept
        assert tr2.id in result
        assert tr1.id not in result
    
    def test_backups_outside_window_not_kept(self, db_session: Session):
        """Backups outside the retention window are not kept."""
        tag = _create_tag(db_session)
        target = _create_target(db_session)
        job = _create_job(db_session, tag)
        
        now = datetime.now(SERVER_TZ)
        
        # Backup from 10 days ago
        _, tr_old = _create_run_with_target_run(
            db_session, job, target,
            now - timedelta(days=10),
            "/backup_old",
        )
        # Backup from 2 days ago
        _, tr_recent = _create_run_with_target_run(
            db_session, job, target,
            now - timedelta(days=2),
            "/backup_recent",
        )
        
        policy = {"rules": [{"unit": "day", "window": 5, "keep": 1}]}
        result = compute_keep_set([tr_old, tr_recent], policy, now=now)
        
        assert tr_recent.id in result
        assert tr_old.id not in result
    
    def test_overlapping_rules_union(self, db_session: Session):
        """Multiple rules create union of kept backups."""
        tag = _create_tag(db_session)
        target = _create_target(db_session)
        job = _create_job(db_session, tag)
        
        now = datetime.now(SERVER_TZ)
        
        # Recent backup (within daily window)
        _, tr_daily = _create_run_with_target_run(
            db_session, job, target,
            now - timedelta(days=1),
            "/backup_daily",
        )
        # Older backup (only within monthly window)
        _, tr_monthly = _create_run_with_target_run(
            db_session, job, target,
            now - timedelta(days=20),
            "/backup_monthly",
        )
        
        policy = {
            "rules": [
                {"unit": "day", "window": 7, "keep": 1},
                {"unit": "month", "window": 1, "keep": 1},
            ]
        }
        result = compute_keep_set([tr_daily, tr_monthly], policy, now=now)
        
        # Both should be kept (union of daily and monthly rules)
        assert tr_daily.id in result
        assert tr_monthly.id in result

    def test_weekly_rule_keeps_one_per_week(self, db_session: Session):
        """Weekly rule keeps only the latest backup per ISO week."""
        tag = _create_tag(db_session)
        target = _create_target(db_session)
        job = _create_job(db_session, tag)
        
        now = datetime.now(SERVER_TZ)
        
        # Create two backups in the same week (should keep only latest)
        week_start = now - timedelta(days=now.weekday())  # Monday of current week
        _, tr_mon = _create_run_with_target_run(
            db_session, job, target,
            week_start + timedelta(hours=2),
            "/backup_monday",
        )
        _, tr_wed = _create_run_with_target_run(
            db_session, job, target,
            week_start + timedelta(days=2, hours=2),
            "/backup_wednesday",
        )
        
        policy = {"rules": [{"unit": "week", "window": 1, "keep": 1}]}
        result = compute_keep_set([tr_mon, tr_wed], policy, now=now)
        
        # Only the later backup (Wednesday) should be kept
        assert tr_wed.id in result
        assert tr_mon.id not in result

    def test_monthly_rule_keeps_one_per_month(self, db_session: Session):
        """Monthly rule keeps only the latest backup per month."""
        tag = _create_tag(db_session)
        target = _create_target(db_session)
        job = _create_job(db_session, tag)
        
        now = datetime.now(SERVER_TZ)
        
        # Create two backups in the same month (should keep only latest)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        _, tr_early = _create_run_with_target_run(
            db_session, job, target,
            month_start + timedelta(days=1),
            "/backup_early_month",
        )
        _, tr_late = _create_run_with_target_run(
            db_session, job, target,
            month_start + timedelta(days=10),
            "/backup_late_month",
        )
        
        policy = {"rules": [{"unit": "month", "window": 1, "keep": 1}]}
        result = compute_keep_set([tr_early, tr_late], policy, now=now)
        
        # Only the later backup should be kept
        assert tr_late.id in result
        assert tr_early.id not in result

    def test_full_tiered_retention(self, db_session: Session):
        """Full tiered retention: 7 daily, 4 weekly, 6 monthly."""
        tag = _create_tag(db_session)
        target = _create_target(db_session)
        job = _create_job(db_session, tag)
        
        now = datetime.now(SERVER_TZ)
        
        # Create backups spanning multiple tiers
        backups = []
        
        # Recent daily backups (last 7 days)
        for i in range(7):
            _, tr = _create_run_with_target_run(
                db_session, job, target,
                now - timedelta(days=i),
                f"/backup_day_{i}",
            )
            backups.append(tr)
        
        # Weekly backup (2 weeks ago - outside daily, inside weekly)
        _, tr_week2 = _create_run_with_target_run(
            db_session, job, target,
            now - timedelta(weeks=2),
            "/backup_week_2",
        )
        backups.append(tr_week2)
        
        # Monthly backup (2 months ago - outside weekly, inside monthly)
        _, tr_month2 = _create_run_with_target_run(
            db_session, job, target,
            now - timedelta(days=60),
            "/backup_month_2",
        )
        backups.append(tr_month2)
        
        # Very old backup (8 months ago - outside all windows)
        _, tr_old = _create_run_with_target_run(
            db_session, job, target,
            now - timedelta(days=240),
            "/backup_too_old",
        )
        backups.append(tr_old)
        
        policy = {
            "rules": [
                {"unit": "day", "window": 7, "keep": 1},
                {"unit": "week", "window": 4, "keep": 1},
                {"unit": "month", "window": 6, "keep": 1},
            ]
        }
        result = compute_keep_set(backups, policy, now=now)
        
        # All 7 daily backups should be kept
        for i in range(7):
            assert backups[i].id in result, f"Daily backup {i} should be kept"
        
        # Weekly backup should be kept
        assert tr_week2.id in result, "Weekly backup should be kept"
        
        # Monthly backup should be kept
        assert tr_month2.id in result, "Monthly backup should be kept"
        
        # Very old backup should NOT be kept
        assert tr_old.id not in result, "Old backup should be deleted"


class TestApplyRetention:
    """Tests for apply_retention function with actual file deletion."""
    
    def test_no_policy_keeps_everything(self, db_session: Session):
        """When no policy is configured, nothing is deleted."""
        tag = _create_tag(db_session)
        target = _create_target(db_session)
        job = _create_job(db_session, tag, retention_json=None)
        
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact = os.path.join(tmpdir, "backup.tar.gz")
            with open(artifact, "w") as f:
                f.write("test")
            
            _, tr = _create_run_with_target_run(
                db_session, job, target,
                datetime.now(timezone.utc) - timedelta(days=100),
                artifact,
            )
            
            result = apply_retention(db_session, job.id, target.id)
            
            # No policy = keep everything
            assert result["delete_count"] == 0
            assert os.path.exists(artifact)
    
    def test_deletes_artifact_and_sidecar(self, db_session: Session):
        """Retention deletes artifact file and sidecar metadata."""
        # Create settings with aggressive retention
        settings = SettingsModel(
            id=1,
            global_retention_policy_json='{"rules": [{"unit": "day", "window": 1, "keep": 1}]}',
        )
        db_session.add(settings)
        db_session.commit()
        
        tag = _create_tag(db_session)
        target = _create_target(db_session)
        job = _create_job(db_session, tag)
        
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create old artifact (outside retention window)
            old_artifact = os.path.join(tmpdir, "old_backup.tar.gz")
            old_sidecar = f"{old_artifact}.meta.json"
            with open(old_artifact, "w") as f:
                f.write("old backup data")
            with open(old_sidecar, "w") as f:
                f.write('{"plugin": "test"}')
            
            # Create recent artifact (within retention window)
            recent_artifact = os.path.join(tmpdir, "recent_backup.tar.gz")
            with open(recent_artifact, "w") as f:
                f.write("recent backup data")
            
            now = datetime.now(SERVER_TZ)
            _, tr_old = _create_run_with_target_run(
                db_session, job, target,
                now - timedelta(days=5),
                old_artifact,
            )
            _, tr_recent = _create_run_with_target_run(
                db_session, job, target,
                now - timedelta(hours=1),
                recent_artifact,
            )
            
            result = apply_retention(db_session, job.id, target.id)
            
            # Old artifact should be deleted
            assert result["delete_count"] == 1
            assert not os.path.exists(old_artifact)
            assert not os.path.exists(old_sidecar)
            
            # Recent artifact should remain
            assert result["keep_count"] == 1
            assert os.path.exists(recent_artifact)
    
    def test_deletes_db_rows(self, db_session: Session):
        """Retention deletes TargetRun and orphaned Run from DB."""
        settings = SettingsModel(
            id=1,
            global_retention_policy_json='{"rules": [{"unit": "day", "window": 1, "keep": 1}]}',
        )
        db_session.add(settings)
        db_session.commit()
        
        tag = _create_tag(db_session)
        target = _create_target(db_session)
        job = _create_job(db_session, tag)
        
        with tempfile.TemporaryDirectory() as tmpdir:
            old_artifact = os.path.join(tmpdir, "old.tar.gz")
            with open(old_artifact, "w") as f:
                f.write("data")
            
            now = datetime.now(SERVER_TZ)
            run_old, tr_old = _create_run_with_target_run(
                db_session, job, target,
                now - timedelta(days=10),
                old_artifact,
            )
            run_old_id = run_old.id
            tr_old_id = tr_old.id
            
            _, tr_recent = _create_run_with_target_run(
                db_session, job, target,
                now - timedelta(hours=1),
                os.path.join(tmpdir, "recent.tar.gz"),
            )
            with open(tr_recent.artifact_path, "w") as f:
                f.write("recent")
            
            apply_retention(db_session, job.id, target.id)
            
            # Old TargetRun should be deleted
            assert db_session.get(TargetRunModel, tr_old_id) is None
            # Old Run should be deleted (no remaining TargetRuns)
            assert db_session.get(RunModel, run_old_id) is None
            # Recent should remain
            assert db_session.get(TargetRunModel, tr_recent.id) is not None
    
    def test_dry_run_does_not_delete(self, db_session: Session):
        """Dry run computes but does not delete."""
        settings = SettingsModel(
            id=1,
            global_retention_policy_json='{"rules": [{"unit": "day", "window": 1, "keep": 1}]}',
        )
        db_session.add(settings)
        db_session.commit()
        
        tag = _create_tag(db_session)
        target = _create_target(db_session)
        job = _create_job(db_session, tag)
        
        with tempfile.TemporaryDirectory() as tmpdir:
            old_artifact = os.path.join(tmpdir, "old.tar.gz")
            with open(old_artifact, "w") as f:
                f.write("data")
            
            now = datetime.now(SERVER_TZ)
            _, tr_old = _create_run_with_target_run(
                db_session, job, target,
                now - timedelta(days=10),
                old_artifact,
            )
            
            result = apply_retention(db_session, job.id, target.id, dry_run=True)
            
            # Should report deletion but not actually delete
            assert result["delete_count"] == 1
            assert os.path.exists(old_artifact)  # File still exists
            assert db_session.get(TargetRunModel, tr_old.id) is not None  # DB row still exists


class TestRetentionService:
    """Tests for RetentionService class."""
    
    def test_preview_returns_dry_run_result(self, db_session: Session):
        """preview() returns dry-run result without deleting."""
        settings = SettingsModel(
            id=1,
            global_retention_policy_json='{"rules": [{"unit": "day", "window": 1, "keep": 1}]}',
        )
        db_session.add(settings)
        db_session.commit()
        
        tag = _create_tag(db_session)
        target = _create_target(db_session)
        job = _create_job(db_session, tag)
        
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact = os.path.join(tmpdir, "backup.tar.gz")
            with open(artifact, "w") as f:
                f.write("data")
            
            now = datetime.now(SERVER_TZ)
            _create_run_with_target_run(db_session, job, target, now - timedelta(days=10), artifact)
            
            svc = RetentionService(db_session)
            result = svc.preview(job.id, target.id)
            
            assert result["delete_count"] == 1
            assert os.path.exists(artifact)  # Not actually deleted
