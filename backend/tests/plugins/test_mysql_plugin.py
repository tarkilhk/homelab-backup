import asyncio
import os
from pathlib import Path

import pytest

from app.core.plugins.base import BackupContext, RestoreContext
from app.plugins.mysql import MySQLPlugin


class DummyProcess:
    def __init__(self, returncode=0, stdout=b"", stderr=b"", stdout_stream=None):
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self.stdout = stdout_stream

    async def communicate(self, input=None):
        assert input is None
        return self._stdout, self._stderr

    async def wait(self):
        return self.returncode


class DummyStream:
    def __init__(self, *chunks):
        self.chunks = list(chunks)

    async def read(self, size):
        assert 0 < size <= 1024 * 1024
        return self.chunks.pop(0) if self.chunks else b""


@pytest.mark.asyncio
async def test_test_returns_true(monkeypatch):
    async def fake_exec(*args, **kwargs):
        assert args[0] == "mysql"
        assert args[-2:] == ("-e", "SELECT 1")
        assert kwargs["env"]["MYSQL_PWD"] == "pw"
        return DummyProcess(returncode=0, stdout=b"1\n")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
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
async def test_test_raises_when_mysql_returns_unexpected_result(monkeypatch):
    async def fake_exec(*args, **kwargs):
        return DummyProcess(returncode=0, stdout=b"0\n")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    with pytest.raises(ConnectionError, match="validate MySQL connection"):
        await MySQLPlugin(name="mysql").test(
            {
                "host": "localhost",
                "user": "user",
                "password": "pw",
                "database": "db",
            }
        )


@pytest.mark.asyncio
async def test_backup_rejects_missing_required_config():
    with pytest.raises(ValueError, match="host, user, password, database"):
        await MySQLPlugin(name="mysql").backup(
            BackupContext(job_id="1", target_id="1", config={}, metadata={})
        )


@pytest.mark.asyncio
async def test_backup_writes_artifact(tmp_path, monkeypatch):
    async def fake_exec(*args, **kwargs):
        # Updated to match new direct command approach (same as PostgreSQL)
        assert args[0] == "mysqldump"
        assert "-h" in args and "localhost" in args
        assert "-u" in args and "user" in args
        assert "db" in args
        for option in (
            "--single-transaction",
            "--quick",
            "--skip-lock-tables",
            "--routines",
            "--events",
            "--triggers",
            "--hex-blob",
            "--no-tablespaces",
            "--set-gtid-purged=OFF",
        ):
            assert option in args
        # Verify MYSQL_PWD is set in environment
        assert "env" in kwargs
        assert "MYSQL_PWD" in kwargs["env"]
        assert kwargs["env"]["MYSQL_PWD"] == "pw"
        return DummyProcess(returncode=0, stdout_stream=DummyStream(b"dump data"))

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


@pytest.mark.asyncio
async def test_backup_streams_large_dump_to_pending_artifact(tmp_path, monkeypatch):
    chunk = b"x" * (1024 * 1024)

    async def fake_exec(*args, **kwargs):
        assert kwargs["stdout"] == asyncio.subprocess.PIPE
        assert kwargs["stderr"] != asyncio.subprocess.PIPE
        return DummyProcess(returncode=0, stdout_stream=DummyStream(*([chunk] * 9)))

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr("app.plugins.mysql.plugin.BACKUP_BASE_PATH", str(tmp_path))

    result = await MySQLPlugin(name="mysql").backup(
        BackupContext(
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
    )

    artifact = Path(result["artifact_path"])
    assert artifact.stat().st_size == 9 * 1024 * 1024
    assert Path(f"{artifact}.meta.json").is_file()


@pytest.mark.asyncio
async def test_restore_streams_sql_file_to_mysql(tmp_path, monkeypatch):
    artifact = tmp_path / "mysql-dump.sql"
    artifact.write_bytes(b"CREATE TABLE proof (id INT); INSERT INTO proof VALUES (42);")

    calls = 0

    async def fake_exec(*args, **kwargs):
        nonlocal calls
        calls += 1
        assert args[0] in {"mysql", "mysqlcheck"}
        if "-e" in args:
            assert "information_schema.tables" in args[-1]
            assert "information_schema.routines" in args[-1]
            assert "information_schema.triggers" in args[-1]
            assert "information_schema.events" in args[-1]
            return DummyProcess(returncode=0, stdout=b"0\n")
        if args[0] == "mysqlcheck":
            return DummyProcess(returncode=0)
        assert kwargs["stdin"].read() == artifact.read_bytes()
        assert kwargs["stderr"] != asyncio.subprocess.PIPE
        return DummyProcess(returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    result = await MySQLPlugin(name="mysql").restore(
        RestoreContext(
            job_id="2",
            source_target_id="1",
            destination_target_id="2",
            config={
                "host": "localhost",
                "user": "user",
                "password": "pw",
                "database": "db",
            },
            artifact_path=str(artifact),
        )
    )

    assert result["status"] == "success"
    assert result["artifact_bytes"] == artifact.stat().st_size
    assert calls == 3


@pytest.mark.asyncio
async def test_restore_refuses_nonempty_destination(tmp_path, monkeypatch):
    artifact = tmp_path / "mysql-dump.sql"
    artifact.write_bytes(b"CREATE TABLE proof (id INT);")

    async def fake_exec(*args, **kwargs):
        return DummyProcess(returncode=0, stdout=b"3\n")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    with pytest.raises(ValueError, match="destination database must be empty"):
        await MySQLPlugin(name="mysql").restore(
            RestoreContext(
                job_id="2",
                source_target_id="1",
                destination_target_id="2",
                config={
                    "host": "localhost",
                    "user": "user",
                    "password": "pw",
                    "database": "db",
                },
                artifact_path=str(artifact),
            )
        )
