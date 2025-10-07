import asyncio
import os
import sys
import types
import pytest

from app.core.plugins.base import BackupContext, RestoreContext
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
    class DummyConn:
        async def fetchval(self, query):
            assert query.strip().upper() == "SELECT 1"
            return 1

        async def close(self):
            return None

    async def dummy_connect(**kwargs):
        # Ensure connection params are passed
        assert "host" in kwargs and "user" in kwargs and "database" in kwargs
        return DummyConn()

    dummy_asyncpg = types.SimpleNamespace(connect=dummy_connect)
    monkeypatch.setitem(sys.modules, "asyncpg", dummy_asyncpg)
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
    """Test should default to 'postgres' database when database field is empty."""
    class DummyConn:
        async def fetchval(self, query):
            assert query.strip().upper() == "SELECT 1"
            return 1

        async def close(self):
            return None

    async def dummy_connect(**kwargs):
        # Ensure connection defaults to 'postgres' database
        assert kwargs.get("database") == "postgres"
        return DummyConn()

    dummy_asyncpg = types.SimpleNamespace(connect=dummy_connect)
    monkeypatch.setitem(sys.modules, "asyncpg", dummy_asyncpg)
    plugin = PostgreSQLPlugin(name="postgresql")
    ok = await plugin.test(
        {
            "host": "localhost",
            "user": "user",
            "password": "pw",
            "database": "",  # Empty database field
        }
    )
    assert ok is True


@pytest.mark.asyncio
async def test_backup_writes_artifact(tmp_path, monkeypatch):
    async def fake_exec(*args, **kwargs):
        assert args[0] == "pg_dump"
        assert "docker" not in args
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


@pytest.mark.asyncio
async def test_backup_all_databases_uses_pg_dumpall(tmp_path, monkeypatch):
    """Backup should use pg_dumpall when database field is empty."""
    async def fake_exec(*args, **kwargs):
        assert args[0] == "pg_dumpall"
        assert "docker" not in args
        assert "pg_dump" not in args  # Ensure pg_dump is not used
        return DummyProcess(returncode=0, stdout=b"all databases dump data")

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
            "database": "",  # Empty database field
        },
        metadata={"target_slug": "slug"},
    )
    result = await plugin.backup(ctx)
    artifact_path = result.get("artifact_path")
    assert artifact_path and os.path.isabs(artifact_path)
    assert os.path.exists(artifact_path)
    assert "postgresql-dumpall-" in artifact_path  # Check filename contains dumpall
    with open(artifact_path, "rb") as fh:
        assert fh.read() == b"all databases dump data"


@pytest.mark.asyncio
async def test_restore_single_database(tmp_path, monkeypatch):
    """Restore should execute psql to restore a single database dump."""
    # Create a dummy artifact file
    artifact_path = tmp_path / "postgresql-dump-20250101T120000.sql"
    artifact_path.write_text("PostgreSQL backup data")
    
    async def fake_exec(*args, **kwargs):
        # Verify psql is called with correct arguments
        assert args[0] == "psql"
        assert "-h" in args and "localhost" in args
        assert "-U" in args and "user" in args
        assert "-d" in args and "db" in args
        assert "-f" in args and str(artifact_path) in args
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
async def test_restore_all_databases(tmp_path, monkeypatch):
    """Restore should execute psql against postgres database for dumpall dumps."""
    # Create a dummy dumpall artifact file
    artifact_path = tmp_path / "postgresql-dumpall-20250101T120000.sql"
    artifact_path.write_text("PostgreSQL dumpall data")
    
    async def fake_exec(*args, **kwargs):
        # Verify psql is called with correct arguments for dumpall
        assert args[0] == "psql"
        assert "-h" in args and "localhost" in args
        assert "-U" in args and "user" in args
        assert "-d" in args and "postgres" in args  # Should use postgres DB for dumpall
        assert "-f" in args and str(artifact_path) in args
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
            "database": "",  # Empty for dumpall
        },
        metadata={"target_slug": "postgres-restore"},
    )
    result = await plugin.restore(ctx)
    
    # Verify the result
    assert result["status"] == "success"
    assert result["artifact_path"] == str(artifact_path)
    assert result["artifact_bytes"] == len("PostgreSQL dumpall data")


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
    artifact_path = tmp_path / "postgresql-dump-20250101T120000.sql"
    artifact_path.write_text("SQL dump")
    
    async def fake_exec(*args, **kwargs):
        return DummyProcess(returncode=1, stdout=b"", stderr=b"connection refused")
    
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
    
    with pytest.raises(RuntimeError, match="psql restore failed"):
        await plugin.restore(ctx)
