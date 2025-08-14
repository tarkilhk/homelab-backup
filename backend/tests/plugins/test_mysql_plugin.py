import asyncio
import os
import sys
import types
import pytest

from app.core.plugins.base import BackupContext
from app.plugins.mysql import MySQLPlugin


class DummyProcess:
    def __init__(self, returncode=0, stdout=b"", stderr=b""):
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self):
        return self._stdout, self._stderr


@pytest.mark.asyncio
async def test_test_returns_true(monkeypatch):
    class DummyCursor:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def execute(self, query):
            assert query.strip().upper() == "SELECT 1"
            return None

        async def fetchone(self):
            return (1,)

    class DummyConn:
        def cursor(self):
            return DummyCursor()

        def close(self):
            return None

    async def dummy_connect(**kwargs):
        assert "host" in kwargs and "user" in kwargs and "db" in kwargs
        return DummyConn()

    dummy_aiomysql = types.SimpleNamespace(connect=dummy_connect)
    monkeypatch.setitem(sys.modules, "aiomysql", dummy_aiomysql)
    plugin = MySQLPlugin(name="mysql")
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
        assert "mysqldump" in args
        return DummyProcess(returncode=0, stdout=b"dump data")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr("app.plugins.mysql.plugin.BACKUP_BASE_PATH", str(tmp_path))
    plugin = MySQLPlugin(name="mysql")
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
