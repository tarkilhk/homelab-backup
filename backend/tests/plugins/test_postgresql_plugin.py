import asyncio
import os
from pathlib import Path

import pytest

from app.core.plugins.base import BackupContext, RestoreContext
from app.plugins.postgresql import PostgreSQLPlugin


class DummyProcess:
    def __init__(self, returncode=0, stdout=b"", stderr=b"", stdout_stream=None):
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self.stdout = stdout_stream

    async def communicate(self):
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
        assert args[0] == "psql"
        assert args[-2:] == ("-c", "SELECT 1")
        assert kwargs["env"]["PGPASSWORD"] == "pw"
        return DummyProcess(returncode=0, stdout=b"1\n")

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
async def test_test_returns_true_without_database(monkeypatch):
    plugin = PostgreSQLPlugin(name="postgresql")
    with pytest.raises(ValueError, match="database"):
        await plugin.test(
            {
                "host": "localhost",
                "user": "user",
                "password": "pw",
                "database": "",
            }
        )


@pytest.mark.asyncio
async def test_backup_writes_artifact(tmp_path, monkeypatch):
    async def fake_exec(*args, **kwargs):
        if args[0] == "pg_dump":
            assert "--format=custom" in args
            assert "--no-owner" in args
            assert "--no-privileges" in args
            return DummyProcess(returncode=0, stdout_stream=DummyStream(b"dump data"))
        assert args[:2] == ("pg_restore", "--list")
        return DummyProcess(returncode=0)

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


@pytest.mark.asyncio
async def test_backup_streams_dump_in_bounded_chunks_to_pending_artifact(tmp_path, monkeypatch):
    dump_chunk = b"x" * (1024 * 1024)

    async def fake_exec(*args, **kwargs):
        if args[0] == "pg_restore":
            return DummyProcess(returncode=0)
        assert kwargs["stdout"] == asyncio.subprocess.PIPE
        return DummyProcess(
            returncode=0,
            stderr=b"",
            stdout_stream=DummyStream(*([dump_chunk] * 8)),
        )

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

    artifact_path = Path(result["artifact_path"])
    assert artifact_path.stat().st_size == 8 * 1024 * 1024
    assert Path(f"{artifact_path}.meta.json").is_file()


@pytest.mark.asyncio
async def test_failed_backup_cleans_partial_output_without_buffering_stderr(tmp_path, monkeypatch):
    async def fake_exec(*args, **kwargs):
        error_output = kwargs["stderr"]
        assert kwargs["stdout"] == asyncio.subprocess.PIPE
        assert error_output != asyncio.subprocess.PIPE
        error_output.write(b"connection refused" + b"x" * (128 * 1024))
        return DummyProcess(
            returncode=1,
            stdout_stream=DummyStream(b"partial dump"),
        )

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

    with pytest.raises(RuntimeError) as exc_info:
        await plugin.backup(ctx)

    error = str(exc_info.value)
    assert "connection refused" in error
    assert error.endswith("[truncated]")
    assert len(error.encode()) < 66 * 1024
    assert not [path for path in tmp_path.rglob("*") if path.is_file()]


@pytest.mark.asyncio
async def test_backup_rejects_missing_database():
    with pytest.raises(ValueError, match="database"):
        await PostgreSQLPlugin(name="postgresql").backup(
            BackupContext(
                job_id="1",
                target_id="1",
                config={"host": "localhost", "user": "user", "password": "pw"},
                metadata={},
            )
        )


@pytest.mark.asyncio
async def test_restore_single_database(tmp_path, monkeypatch):
    """Restore should execute pg_restore transactionally."""
    # Create a dummy artifact file
    artifact_path = tmp_path / "postgresql-dump-20250101T120000.dump"
    artifact_path.write_text("PostgreSQL backup data")

    async def fake_exec(*args, **kwargs):
        assert args[0] == "pg_restore"
        assert "-h" in args and "localhost" in args
        assert "-U" in args and "user" in args
        assert "--dbname" in args and "db" in args
        assert str(artifact_path) in args
        assert "--exit-on-error" in args
        assert "--single-transaction" in args
        assert "--clean" in args and "--if-exists" in args
        assert kwargs["stdout"] == asyncio.subprocess.DEVNULL
        assert kwargs["stderr"] != asyncio.subprocess.PIPE
        # Verify PGPASSWORD is set in environment
        assert kwargs.get("env", {}).get("PGPASSWORD") == "pw"
        return DummyProcess(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    plugin = PostgreSQLPlugin(name="postgresql")
    ctx = RestoreContext(
        job_id="1",
        source_target_id="1",
        destination_target_id="2",
        artifact_path=str(artifact_path),
        config={
            "host": "localhost",
            "user": "user",
            "password": "pw",
            "database": "db",
        },
        metadata={"target_slug": "postgres-restore"},
    )
    result = await plugin.restore(ctx)

    # Verify the result contains the expected fields
    assert result["status"] == "success"
    assert result["artifact_path"] == str(artifact_path)
    assert result["artifact_bytes"] == len("PostgreSQL backup data")


@pytest.mark.asyncio
async def test_restore_rejects_missing_database(tmp_path):
    artifact_path = tmp_path / "postgresql.dump"
    artifact_path.write_bytes(b"archive")
    with pytest.raises(ValueError, match="database"):
        await PostgreSQLPlugin(name="postgresql").restore(
            RestoreContext(
                job_id="1",
                source_target_id="1",
                destination_target_id="2",
                artifact_path=str(artifact_path),
                config={"host": "localhost", "user": "user", "password": "pw"},
                metadata={},
            )
        )


@pytest.mark.asyncio
async def test_restore_fails_when_artifact_missing(tmp_path):
    """Restore should raise FileNotFoundError when artifact doesn't exist."""
    plugin = PostgreSQLPlugin(name="postgresql")
    ctx = RestoreContext(
        job_id="1",
        source_target_id="1",
        destination_target_id="2",
        artifact_path="/nonexistent/path.sql",
        config={
            "host": "localhost",
            "user": "user",
            "password": "pw",
            "database": "db",
        },
        metadata={},
    )

    with pytest.raises(FileNotFoundError, match="Artifact not found"):
        await plugin.restore(ctx)


@pytest.mark.asyncio
async def test_restore_fails_when_psql_fails(tmp_path, monkeypatch):
    """Restore should raise RuntimeError when psql command fails."""
    artifact_path = tmp_path / "postgresql-dump-20250101T120000.dump"
    artifact_path.write_text("SQL dump")

    async def fake_exec(*args, **kwargs):
        kwargs["stderr"].write(b"connection refused")
        return DummyProcess(returncode=1)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    plugin = PostgreSQLPlugin(name="postgresql")
    ctx = RestoreContext(
        job_id="1",
        source_target_id="1",
        destination_target_id="2",
        artifact_path=str(artifact_path),
        config={
            "host": "localhost",
            "user": "user",
            "password": "pw",
            "database": "db",
        },
        metadata={},
    )

    with pytest.raises(RuntimeError, match="pg_restore failed"):
        await plugin.restore(ctx)
