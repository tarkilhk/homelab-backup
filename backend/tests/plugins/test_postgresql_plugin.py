import asyncio
import os
import pytest

from app.core.plugins.base import BackupContext
from app.plugins.postgresql import PostgreSQLPlugin


class DummyProcess:
    def __init__(self, returncode=0, stdout=b"", stderr=b""):
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self):
        return self._stdout, self._stderr


@pytest.mark.asyncio
async def test_test_returns_true(monkeypatch):
    async def fake_exec(*args, **kwargs):
        # Ensure docker is used to invoke pg_dump --schema-only for connectivity
        assert args[0] == "docker"
        assert "pg_dump" in args
        assert "--schema-only" in args
        return DummyProcess(returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    plugin = PostgreSQLPlugin(name="postgresql")
    ok = await plugin.test(
        {
            "host": "localhost",
            "user": "user",
            "password": "pw",
            "database": "db",
        }
    )
    assert ok is True


@pytest.mark.asyncio
async def test_backup_writes_artifact(tmp_path, monkeypatch):
    async def fake_exec(*args, **kwargs):
        assert args[0] == "docker"
        assert "pg_dump" in args
        return DummyProcess(returncode=0, stdout=b"dump data")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr("app.plugins.postgresql.plugin.BACKUP_BASE_PATH", str(tmp_path))
    plugin = PostgreSQLPlugin(name="postgresql")
    ctx = BackupContext(
        job_id="1",
        target_id="1",
        config={
            "host": "localhost",
            "user": "user",
            "password": "pw",
            "database": "db",
        },
        metadata={"target_slug": "slug"},
    )
    result = await plugin.backup(ctx)
    artifact_path = result.get("artifact_path")
    assert artifact_path and os.path.isabs(artifact_path)
    assert os.path.exists(artifact_path)
    with open(artifact_path, "rb") as fh:
        assert fh.read() == b"dump data"
