import asyncio
from pathlib import Path

import pytest

from app.core.plugins.base import BackupContext
from app.plugins.calcom import CalcomPlugin


@pytest.mark.asyncio
async def test_test_success(monkeypatch):
    async def fake_exec(*args, **kwargs):
        class Proc:
            def __init__(self):
                self.returncode = 0
            async def communicate(self):
                return (b"", b"")
        return Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    plugin = CalcomPlugin(name="calcom")
    ok = await plugin.test({"database_url": "postgresql://user:pass@host/db"})
    assert ok is True


@pytest.mark.asyncio
async def test_backup_writes_artifact(tmp_path, monkeypatch):
    async def fake_exec(*args, **kwargs):
        class Proc:
            def __init__(self):
                self.returncode = 0
            async def communicate(self):
                return (b"dump", b"")
        return Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    plugin = CalcomPlugin(name="calcom", base_dir=str(tmp_path))
    ctx = BackupContext(
        job_id="1",
        target_id="1",
        config={"database_url": "postgresql://user:pass@host/db"},
        metadata={"target_slug": "calcom"},
    )
    result = await plugin.backup(ctx)
    artifact = result.get("artifact_path")
    assert artifact
    p = Path(artifact)
    assert p.exists()
    assert p.read_bytes() == b"dump"
