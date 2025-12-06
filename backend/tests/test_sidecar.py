"""Tests for backup sidecar metadata functionality."""

import json
import os
import tempfile
from pathlib import Path
from datetime import datetime, timezone

import pytest

from app.core.plugins.sidecar import write_backup_sidecar, read_backup_sidecar
from app.core.plugins.base import BackupContext, BackupPlugin


class TestPlugin(BackupPlugin):
    """Test plugin for sidecar tests."""
    
    def __init__(self):
        super().__init__(name="test_plugin", version="1.0.0")
    
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


def test_write_backup_sidecar(tmp_path):
    """Test writing sidecar metadata file."""
    artifact_path = str(tmp_path / "backup.tar.gz")
    
    # Create a dummy artifact file
    Path(artifact_path).touch()
    
    plugin = TestPlugin()
    context = BackupContext(
        job_id="1",
        target_id="1",
        config={},
        metadata={"target_slug": "test-target"},
    )
    
    write_backup_sidecar(artifact_path, plugin, context)
    
    sidecar_path = f"{artifact_path}.meta.json"
    assert os.path.exists(sidecar_path)
    
    with open(sidecar_path, "r") as f:
        data = json.load(f)
    
    assert data["plugin_name"] == "test_plugin"
    assert data["plugin_version"] == "1.0.0"
    assert data["target_slug"] == "test-target"
    assert data["artifact_path"] == artifact_path
    assert "created_at" in data


def test_write_backup_sidecar_fallback_target_id(tmp_path):
    """Test sidecar uses target_id when target_slug not in metadata."""
    artifact_path = str(tmp_path / "backup.tar.gz")
    Path(artifact_path).touch()
    
    plugin = TestPlugin()
    context = BackupContext(
        job_id="1",
        target_id="42",
        config={},
        metadata={},
    )
    
    write_backup_sidecar(artifact_path, plugin, context)
    
    sidecar_path = f"{artifact_path}.meta.json"
    with open(sidecar_path, "r") as f:
        data = json.load(f)
    
    assert data["target_slug"] == "42"


def test_read_backup_sidecar(tmp_path):
    """Test reading sidecar metadata."""
    artifact_path = str(tmp_path / "backup.tar.gz")
    Path(artifact_path).touch()
    
    sidecar_path = f"{artifact_path}.meta.json"
    sidecar_data = {
        "plugin_name": "test_plugin",
        "plugin_version": "1.0.0",
        "target_slug": "test-target",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "artifact_path": artifact_path,
    }
    
    with open(sidecar_path, "w") as f:
        json.dump(sidecar_data, f)
    
    result = read_backup_sidecar(artifact_path)
    assert result is not None
    assert result["plugin_name"] == "test_plugin"
    assert result["target_slug"] == "test-target"


def test_read_backup_sidecar_missing():
    """Test reading sidecar when file doesn't exist."""
    result = read_backup_sidecar("/nonexistent/path/backup.tar.gz")
    assert result is None


def test_read_backup_sidecar_invalid_json(tmp_path):
    """Test reading sidecar with invalid JSON."""
    artifact_path = str(tmp_path / "backup.tar.gz")
    Path(artifact_path).touch()
    
    sidecar_path = f"{artifact_path}.meta.json"
    with open(sidecar_path, "w") as f:
        f.write("invalid json")
    
    result = read_backup_sidecar(artifact_path)
    assert result is None


def test_read_backup_sidecar_missing_required_fields(tmp_path):
    """Test reading sidecar with missing required fields."""
    artifact_path = str(tmp_path / "backup.tar.gz")
    Path(artifact_path).touch()
    
    sidecar_path = f"{artifact_path}.meta.json"
    with open(sidecar_path, "w") as f:
        json.dump({"some_field": "value"}, f)
    
    result = read_backup_sidecar(artifact_path)
    assert result is None


