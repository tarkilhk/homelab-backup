"""Tests for BackupsFromDiskService."""

import os
import tempfile
from pathlib import Path
from datetime import datetime

import pytest
from sqlalchemy.orm import Session

from app.services.backups_from_disk import BackupsFromDiskService, BackupFromDisk
from app.models import TargetRun, Run, Target, Job, Tag
from app.core.plugins.sidecar import write_backup_sidecar
from app.core.plugins.base import BackupContext, BackupPlugin


class MockBackupPlugin(BackupPlugin):
    """Mock plugin for service tests."""
    
    def __init__(self, name="test_plugin"):
        super().__init__(name=name, version="1.0.0")
    
    async def validate_config(self, config):
        return True
    
    async def test(self, config):
        return True
    
    async def backup(self, context):
        return {"artifact_path": "/tmp/test"}
    
    async def restore(self, context):
        return {"status": "ok"}
    
    async def get_status(self, context):
        return {"status": "ok"}


@pytest.fixture
def backup_dir(tmp_path):
    """Create a temporary backup directory structure."""
    base = tmp_path / "backups"
    base.mkdir()
    
    # Create structure: backups/target1/2025-01-15/artifact1.tar.gz
    target1_dir = base / "target1" / "2025-01-15"
    target1_dir.mkdir(parents=True)
    artifact1 = target1_dir / "pihole-backup-20250115T120000.zip"
    artifact1.write_bytes(b"test backup content")
    
    # Create structure: backups/target2/2025-01-16/artifact2.sql
    target2_dir = base / "target2" / "2025-01-16"
    target2_dir.mkdir(parents=True)
    artifact2 = target2_dir / "postgresql-dump-20250116T130000.sql"
    artifact2.write_bytes(b"SQL dump content")
    
    # Create a sidecar for artifact2
    sidecar2 = artifact2.with_suffix(".sql.meta.json")
    import json
    sidecar2.write_text(json.dumps({
        "plugin_name": "postgresql",
        "plugin_version": "1.0.0",
        "target_slug": "target2",
        "created_at": "2025-01-16T13:00:00+00:00",
        "artifact_path": str(artifact2),
    }))
    
    return base


def test_scan_backups_basic(backup_dir, db_session: Session):
    """Test basic scanning of backup directory."""
    svc = BackupsFromDiskService(db_session)
    backups = svc.scan_backups(backup_base_path=str(backup_dir))
    
    assert len(backups) == 2
    
    # Check artifact1 (inferred)
    backup1 = next((b for b in backups if "pihole-backup" in b.artifact_path), None)
    assert backup1 is not None
    assert backup1.target_slug == "target1"
    assert backup1.date == "2025-01-15"
    assert backup1.plugin_name == "pihole"  # Inferred from filename
    assert backup1.metadata_source == "inferred"
    
    # Check artifact2 (sidecar)
    backup2 = next((b for b in backups if "postgresql-dump" in b.artifact_path), None)
    assert backup2 is not None
    assert backup2.target_slug == "target2"
    assert backup2.date == "2025-01-16"
    assert backup2.plugin_name == "postgresql"
    assert backup2.metadata_source == "sidecar"


def test_scan_backups_excludes_tracked(backup_dir, db_session: Session):
    """Test that tracked artifacts are excluded from results."""
    # Create a tracked TargetRun for artifact1
    tag = Tag(display_name="test")
    db_session.add(tag)
    db_session.flush()
    
    job = Job(name="test-job", tag_id=tag.id, enabled=True, schedule_cron="0 0 * * *")
    db_session.add(job)
    db_session.flush()
    
    target = Target(name="test-target", plugin_name="test_plugin", plugin_config_json="{}")
    db_session.add(target)
    db_session.flush()
    
    run = Run(job_id=job.id, status="success", operation="backup")
    db_session.add(run)
    db_session.flush()
    
    artifact1_path = str(backup_dir / "target1" / "2025-01-15" / "pihole-backup-20250115T120000.zip")
    target_run = TargetRun(
        run_id=run.id,
        target_id=target.id,
        artifact_path=artifact1_path,
        status="success",
    )
    db_session.add(target_run)
    db_session.commit()
    
    svc = BackupsFromDiskService(db_session)
    backups = svc.scan_backups(backup_base_path=str(backup_dir))
    
    # Should only find artifact2 (artifact1 is tracked)
    assert len(backups) == 1
    assert "postgresql-dump" in backups[0].artifact_path


def test_scan_backups_missing_directory(db_session: Session):
    """Test scanning when backup directory doesn't exist."""
    svc = BackupsFromDiskService(db_session)
    backups = svc.scan_backups(backup_base_path="/nonexistent/path")
    assert backups == []


def test_scan_backups_permission_error(db_session: Session, monkeypatch):
    """Test handling of permission errors."""
    def mock_iterdir(self):
        raise PermissionError("Permission denied")
    
    monkeypatch.setattr(Path, "iterdir", mock_iterdir)
    
    svc = BackupsFromDiskService(db_session)
    backups = svc.scan_backups(backup_base_path="/tmp")
    # Should return empty list on permission error
    assert backups == []


def test_infer_plugin_from_filename():
    """Test plugin inference from filename patterns."""
    svc = BackupsFromDiskService(None)
    
    assert svc._infer_plugin_from_filename("pihole-backup-20250115.zip") == "pihole"
    assert svc._infer_plugin_from_filename("postgresql-dump-20250115.sql") == "postgresql"
    assert svc._infer_plugin_from_filename("mysql-dump-20250115.sql") == "mysql"
    assert svc._infer_plugin_from_filename("vaultwarden-backup-20250115.tar.gz") == "vaultwarden"
    assert svc._infer_plugin_from_filename("jellyfin-backup-20250115.zip") == "jellyfin"
    assert svc._infer_plugin_from_filename("wordpress-backup-20250115.tar.gz") == "wordpress"
    assert svc._infer_plugin_from_filename("calcom-db-20250115.sql") == "calcom"
    assert svc._infer_plugin_from_filename("sonarr-backup-20250115.zip") == "sonarr"
    assert svc._infer_plugin_from_filename("lidarr-backup-20250115.zip") == "lidarr"
    assert svc._infer_plugin_from_filename("radarr-backup-20250115.zip") == "radarr"
    assert svc._infer_plugin_from_filename("invoiceninja-export-20250115.zip") == "invoiceninja"
    
    # Unknown patterns
    assert svc._infer_plugin_from_filename("unknown-file.txt") is None
    assert svc._infer_plugin_from_filename("random-backup.tar.gz") is None


