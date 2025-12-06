"""Tests for backups from disk API endpoint."""

import os
import tempfile
from pathlib import Path
import json

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def backup_dir(tmp_path):
    """Create a temporary backup directory structure."""
    base = tmp_path / "backups"
    base.mkdir()
    
    # Create structure: backups/target1/2025-01-15/artifact1.zip
    target1_dir = base / "target1" / "2025-01-15"
    target1_dir.mkdir(parents=True)
    artifact1 = target1_dir / "pihole-backup-20250115T120000.zip"
    artifact1.write_bytes(b"test backup content")
    
    # Create structure with sidecar
    target2_dir = base / "target2" / "2025-01-16"
    target2_dir.mkdir(parents=True)
    artifact2 = target2_dir / "postgresql-dump-20250116T130000.sql"
    artifact2.write_bytes(b"SQL dump content")
    
    sidecar2 = artifact2.with_suffix(".sql.meta.json")
    sidecar2.write_text(json.dumps({
        "plugin_name": "postgresql",
        "plugin_version": "1.0.0",
        "target_slug": "target2",
        "created_at": "2025-01-16T13:00:00+00:00",
        "artifact_path": str(artifact2),
    }))
    
    return base


def test_list_backups_from_disk(client: TestClient, backup_dir, monkeypatch):
    """Test GET /api/v1/backups/from-disk endpoint."""
    monkeypatch.setenv("BACKUP_BASE_PATH", str(backup_dir))
    
    resp = client.get("/api/v1/backups/from-disk")
    assert resp.status_code == 200
    
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) == 2
    
    # Check first backup (inferred)
    backup1 = next((b for b in data if "pihole-backup" in b["artifact_path"]), None)
    assert backup1 is not None
    assert backup1["target_slug"] == "target1"
    assert backup1["date"] == "2025-01-15"
    assert backup1["plugin_name"] == "pihole"
    assert backup1["metadata_source"] == "inferred"
    assert backup1["file_size"] > 0
    
    # Check second backup (sidecar)
    backup2 = next((b for b in data if "postgresql-dump" in b["artifact_path"]), None)
    assert backup2 is not None
    assert backup2["target_slug"] == "target2"
    assert backup2["date"] == "2025-01-16"
    assert backup2["plugin_name"] == "postgresql"
    assert backup2["metadata_source"] == "sidecar"
    assert backup2["file_size"] > 0


def test_list_backups_from_disk_empty(client: TestClient, tmp_path, monkeypatch):
    """Test endpoint when no backups found."""
    empty_dir = tmp_path / "empty_backups"
    empty_dir.mkdir()
    monkeypatch.setenv("BACKUP_BASE_PATH", str(empty_dir))
    
    resp = client.get("/api/v1/backups/from-disk")
    assert resp.status_code == 200
    
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) == 0


def test_list_backups_from_disk_missing_directory(client: TestClient, monkeypatch):
    """Test endpoint when backup directory doesn't exist."""
    monkeypatch.setenv("BACKUP_BASE_PATH", "/nonexistent/path")
    
    resp = client.get("/api/v1/backups/from-disk")
    assert resp.status_code == 200
    
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) == 0


