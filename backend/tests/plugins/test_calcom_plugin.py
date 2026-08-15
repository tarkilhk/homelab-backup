import asyncio
from pathlib import Path

import pytest

from app.core.plugins.base import BackupContext, RestoreContext
from app.plugins.calcom import CalcomPlugin


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
async def test_test_uses_direct_url_without_exposing_credentials_in_argv(monkeypatch):
    monkeypatch.setenv("PGPASSWORD", "ambient-secret")
    monkeypatch.setenv("PGSERVICE", "ambient-service")

    async def fake_exec(*args, **kwargs):
        assert args == (
            "psql",
            "-X",
            "--set",
            "ON_ERROR_STOP=on",
            "-tA",
            "-c",
            "SELECT 1",
        )
        assert kwargs["env"]["PGHOST"] == "directdb"
        assert kwargs["env"]["PGUSER"] == "direct"
        assert kwargs["env"]["PGPASSWORD"] == "secret value"
        assert kwargs["env"]["PGDATABASE"] == "calcom"
        assert "PGSERVICE" not in kwargs["env"]
        assert "postgresql://" not in " ".join(args)
        return DummyProcess(returncode=0, stdout=b"1\n")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    assert await CalcomPlugin("calcom").test(
        {
            "database_url": "postgresql://pooled:pw@pool/calcom",
            "database_direct_url": "postgresql://direct:secret%20value@directdb/calcom",
        }
    )


@pytest.mark.asyncio
async def test_backup_creates_and_validates_custom_archive(tmp_path, monkeypatch):
    calls: list[str] = []

    async def fake_exec(*args, **kwargs):
        calls.append(args[0])
        if args[0] == "pg_dump":
            assert "--format=custom" in args
            assert "--file" not in args
            assert kwargs["stdout"] == asyncio.subprocess.PIPE
            return DummyProcess(returncode=0, stdout_stream=DummyStream(b"PGDMP fixture"))
        assert args[0] == "pg_restore"
        assert "--list" in args
        return DummyProcess(returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    plugin = CalcomPlugin("calcom", base_dir=str(tmp_path))

    result = await plugin.backup(
        BackupContext(
            job_id="1",
            target_id="1",
            config={"database_url": "postgresql://user:pw@db/calcom"},
            metadata={"target_slug": "calcom"},
        )
    )

    artifact = Path(result["artifact_path"])
    assert artifact.suffix == ".dump"
    assert artifact.read_bytes() == b"PGDMP fixture"
    assert Path(f"{artifact}.meta.json").is_file()
    assert calls == ["pg_dump", "pg_restore"]


@pytest.mark.asyncio
async def test_restore_is_transactional_and_stops_on_error(tmp_path, monkeypatch):
    artifact = tmp_path / "calcom-db.dump"
    artifact.write_bytes(b"PGDMP fixture")

    async def fake_exec(*args, **kwargs):
        assert args[0] == "pg_restore"
        for option in (
            "--exit-on-error",
            "--single-transaction",
            "--clean",
            "--if-exists",
            "--no-owner",
            "--no-privileges",
        ):
            assert option in args
        assert args[-1] == str(artifact)
        assert kwargs["env"]["PGDATABASE"] == "calcom_restore"
        return DummyProcess(returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    result = await CalcomPlugin("calcom").restore(
        RestoreContext(
            job_id="2",
            source_target_id="1",
            destination_target_id="2",
            config={"database_url": "postgresql://user:pw@db/calcom_restore"},
            artifact_path=str(artifact),
        )
    )

    assert result["status"] == "success"
    assert result["artifact_bytes"] == artifact.stat().st_size
