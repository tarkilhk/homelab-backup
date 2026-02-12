import asyncio
from pathlib import Path

import pytest

from app.core.plugins.base import BackupContext, RestoreContext
from app.plugins.calcom import CalcomPlugin


class DummyProcess:
    def __init__(self, returncode=0, stdout=b"", stderr=b""):
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self):
        return self._stdout, self._stderr


@pytest.mark.asyncio
async def test_test_success(monkeypatch):
    async def fake_exec(*args, **kwargs):
        assert args[0] == "psql"
        assert "--set" in args and "ON_ERROR_STOP=on" in args
        assert "-c" in args and "SELECT 1" in args
        return DummyProcess(returncode=0, stdout=b"1\n", stderr=b"")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    plugin = CalcomPlugin(name="calcom")
    ok = await plugin.test({"database_url": "postgresql://user:pass@host/db"})
    assert ok is True


@pytest.mark.asyncio
async def test_test_uses_direct_url_when_provided(monkeypatch):
    async def fake_exec(*args, **kwargs):
        assert args[1] == "postgresql://direct:pw@db/calcom"
        return DummyProcess(returncode=0, stdout=b"1\n", stderr=b"")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    plugin = CalcomPlugin(name="calcom")
    ok = await plugin.test(
        {
            "database_url": "postgresql://pooled:pw@db-pool/calcom",
            "database_direct_url": "postgresql://direct:pw@db/calcom",
        }
    )
    assert ok is True


@pytest.mark.asyncio
async def test_test_raises_connection_error(monkeypatch):
    async def fake_exec(*args, **kwargs):
        return DummyProcess(returncode=2, stdout=b"", stderr=b"authentication failed")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    plugin = CalcomPlugin(name="calcom")
    with pytest.raises(ConnectionError, match="authentication failed"):
        await plugin.test({"database_url": "postgresql://user:pass@host/db"})


@pytest.mark.asyncio
async def test_backup_writes_artifact(tmp_path, monkeypatch):
    async def fake_exec(*args, **kwargs):
        assert args[0] == "pg_dump"
        assert "--no-owner" in args
        assert "--no-privileges" in args
        return DummyProcess(returncode=0, stdout=b"dump", stderr=b"")

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


@pytest.mark.asyncio
async def test_restore_sets_on_error_stop(tmp_path, monkeypatch):
    artifact = tmp_path / "calcom-db-20250101T120000.sql"
    artifact.write_text("SELECT 1;")

    async def fake_exec(*args, **kwargs):
        assert args[0] == "psql"
        assert "--set" in args and "ON_ERROR_STOP=on" in args
        assert "-f" in args and str(artifact) in args
        return DummyProcess(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    plugin = CalcomPlugin(name="calcom", base_dir=str(tmp_path))
    ctx = RestoreContext(
        job_id="1",
        source_target_id="1",
        destination_target_id="2",
        config={"database_url": "postgresql://user:pass@host/db"},
        artifact_path=str(artifact),
        metadata={"target_slug": "calcom"},
    )
    result = await plugin.restore(ctx)
    assert result["status"] == "success"
    assert result["artifact_path"] == str(artifact)
    assert result["artifact_bytes"] == len("SELECT 1;")


@pytest.mark.asyncio
async def test_restore_retries_without_unsupported_settings(tmp_path, monkeypatch):
    artifact = tmp_path / "calcom-db-20250101T120000.sql"
    artifact.write_text(
        "SET transaction_timeout = 0;\n"
        "SET search_path = public;\n"
        "SELECT 1;\n"
    )

    seen_paths: list[str] = []

    async def fake_exec(*args, **kwargs):
        assert args[0] == "psql"
        assert "-f" in args
        sql_path = str(args[args.index("-f") + 1])
        seen_paths.append(sql_path)

        if len(seen_paths) == 1:
            assert sql_path == str(artifact)
            return DummyProcess(
                returncode=1,
                stdout=b"",
                stderr=(
                    b'psql:/backups/calcom.sql:13: ERROR:  '
                    b'unrecognized configuration parameter "transaction_timeout"'
                ),
            )

        # Retry should use a sanitized temporary SQL file.
        assert sql_path != str(artifact)
        sql = Path(sql_path).read_text()
        assert "transaction_timeout" not in sql
        assert "SET search_path = public;" in sql
        return DummyProcess(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    plugin = CalcomPlugin(name="calcom", base_dir=str(tmp_path))
    ctx = RestoreContext(
        job_id="1",
        source_target_id="1",
        destination_target_id="2",
        config={"database_url": "postgresql://user:pass@host/db"},
        artifact_path=str(artifact),
        metadata={"target_slug": "calcom"},
    )

    result = await plugin.restore(ctx)
    assert result["status"] == "success"
    assert seen_paths[0] == str(artifact)
    assert len(seen_paths) == 2
    assert not Path(seen_paths[1]).exists()


@pytest.mark.asyncio
async def test_restore_retries_after_schema_reset_when_objects_exist(tmp_path, monkeypatch):
    artifact = tmp_path / "calcom-db-20250101T120000.sql"
    artifact.write_text("SELECT 1;\n")

    calls: list[tuple[str, ...]] = []

    async def fake_exec(*args, **kwargs):
        assert args[0] == "psql"
        calls.append(tuple(str(a) for a in args))

        # First restore attempt fails on existing object.
        if "-f" in args and len([c for c in calls if "-f" in c]) == 1:
            return DummyProcess(
                returncode=1,
                stdout=b"",
                stderr=b'psql:/tmp/in.sql:28: ERROR:  type "AccessScope" already exists',
            )

        # Schema reset command should run next.
        if "-c" in args:
            sql_cmd = str(args[args.index("-c") + 1])
            assert "DROP SCHEMA IF EXISTS public CASCADE" in sql_cmd
            assert "CREATE SCHEMA public" in sql_cmd
            return DummyProcess(returncode=0, stdout=b"", stderr=b"")

        # Final restore attempt should succeed.
        if "-f" in args:
            return DummyProcess(returncode=0, stdout=b"", stderr=b"")

        raise AssertionError(f"unexpected command: {args}")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    plugin = CalcomPlugin(name="calcom", base_dir=str(tmp_path))
    ctx = RestoreContext(
        job_id="1",
        source_target_id="1",
        destination_target_id="2",
        config={"database_url": "postgresql://user:pass@host/db"},
        artifact_path=str(artifact),
        metadata={"target_slug": "calcom"},
    )

    result = await plugin.restore(ctx)
    assert result["status"] == "success"
    # restore -> schema reset -> restore
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_restore_retries_after_schema_reset_on_drop_dependency_error(tmp_path, monkeypatch):
    artifact = tmp_path / "calcom-db-20250101T120000.sql"
    artifact.write_text("SELECT 1;\n")

    calls: list[tuple[str, ...]] = []

    async def fake_exec(*args, **kwargs):
        assert args[0] == "psql"
        calls.append(tuple(str(a) for a in args))

        # First restore attempt fails with dependency/drop conflict.
        if "-f" in args and len([c for c in calls if "-f" in c]) == 1:
            return DummyProcess(
                returncode=1,
                stdout=b"",
                stderr=(
                    b"ERROR:  cannot drop constraint users_pkey on table public.users "
                    b"because other objects depend on it\n"
                    b"HINT:  Use DROP ... CASCADE to drop the dependent objects too."
                ),
            )

        # Schema reset command should run next.
        if "-c" in args:
            sql_cmd = str(args[args.index("-c") + 1])
            assert "DROP SCHEMA IF EXISTS public CASCADE" in sql_cmd
            assert "CREATE SCHEMA public" in sql_cmd
            return DummyProcess(returncode=0, stdout=b"", stderr=b"")

        # Final restore attempt should succeed.
        if "-f" in args:
            return DummyProcess(returncode=0, stdout=b"", stderr=b"")

        raise AssertionError(f"unexpected command: {args}")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    plugin = CalcomPlugin(name="calcom", base_dir=str(tmp_path))
    ctx = RestoreContext(
        job_id="1",
        source_target_id="1",
        destination_target_id="2",
        config={"database_url": "postgresql://user:pass@host/db"},
        artifact_path=str(artifact),
        metadata={"target_slug": "calcom"},
    )

    result = await plugin.restore(ctx)
    assert result["status"] == "success"
    # restore -> schema reset -> restore
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_restore_grants_permissions_to_explicit_role_across_dump_schemas(tmp_path, monkeypatch):
    artifact = tmp_path / "calcom-db-20250101T120000.sql"
    artifact.write_text(
        "CREATE TABLE public.users (id int);\n"
        'CREATE TABLE "workspace"."member" (id int);\n'
        'CREATE INDEX idx_booking_id ON public."Booking" ("id");\n'
        'SELECT "Booking"."id" FROM public."Booking";\n'
        "SELECT 1;\n"
    )

    calls: list[tuple[str, ...]] = []

    async def fake_exec(*args, **kwargs):
        assert args[0] == "psql"
        calls.append(tuple(str(a) for a in args))

        if "-f" in args:
            return DummyProcess(returncode=0, stdout=b"", stderr=b"")

        if "-c" in args:
            sql_cmd = str(args[args.index("-c") + 1])
            assert 'TO "calcom_app"' in sql_cmd
            assert 'GRANT USAGE, CREATE ON SCHEMA "public"' in sql_cmd
            assert 'GRANT USAGE, CREATE ON SCHEMA "workspace"' in sql_cmd
            assert 'SCHEMA "Booking"' not in sql_cmd
            assert 'ALTER DEFAULT PRIVILEGES IN SCHEMA "public"' in sql_cmd
            assert 'ALTER DEFAULT PRIVILEGES IN SCHEMA "workspace"' in sql_cmd
            return DummyProcess(returncode=0, stdout=b"", stderr=b"")

        raise AssertionError(f"unexpected command: {args}")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    plugin = CalcomPlugin(name="calcom", base_dir=str(tmp_path))
    ctx = RestoreContext(
        job_id="1",
        source_target_id="1",
        destination_target_id="2",
        config={
            "database_url": "postgresql://appuser:pass@appdb/calcom",
            "database_direct_url": "postgresql://admin:pass@directdb/calcom",
            "restore_grant_role": "calcom_app",
        },
        artifact_path=str(artifact),
        metadata={"target_slug": "calcom"},
    )

    result = await plugin.restore(ctx)
    assert result["status"] == "success"
    # restore + grants
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_restore_does_not_grant_when_role_not_configured(tmp_path, monkeypatch):
    artifact = tmp_path / "calcom-db-20250101T120000.sql"
    artifact.write_text("SELECT 1;\n")

    calls: list[tuple[str, ...]] = []

    async def fake_exec(*args, **kwargs):
        assert args[0] == "psql"
        calls.append(tuple(str(a) for a in args))
        return DummyProcess(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    plugin = CalcomPlugin(name="calcom", base_dir=str(tmp_path))
    ctx = RestoreContext(
        job_id="1",
        source_target_id="1",
        destination_target_id="2",
        config={
            "database_url": "postgresql://appuser:pass@appdb/calcom",
            "database_direct_url": "postgresql://admin:pass@directdb/calcom",
        },
        artifact_path=str(artifact),
        metadata={"target_slug": "calcom"},
    )

    result = await plugin.restore(ctx)
    assert result["status"] == "success"
    # only restore command (no grant command)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_restore_grant_works_after_sanitized_retry(tmp_path, monkeypatch):
    artifact = tmp_path / "calcom-db-20250101T120000.sql"
    artifact.write_text(
        "SET transaction_timeout = 0;\n"
        "CREATE TABLE public.users (id int);\n"
    )

    calls: list[tuple[str, ...]] = []

    async def fake_exec(*args, **kwargs):
        assert args[0] == "psql"
        calls.append(tuple(str(a) for a in args))

        if "-f" in args:
            # First restore call fails for unsupported setting, second succeeds.
            if len([c for c in calls if "-f" in c]) == 1:
                return DummyProcess(
                    returncode=1,
                    stdout=b"",
                    stderr=(
                        b'psql:/tmp/in.sql:13: ERROR:  '
                        b'unrecognized configuration parameter "transaction_timeout"'
                    ),
                )
            return DummyProcess(returncode=0, stdout=b"", stderr=b"")

        if "-c" in args:
            sql_cmd = str(args[args.index("-c") + 1])
            assert 'TO "calcom_app"' in sql_cmd
            assert 'SCHEMA "public"' in sql_cmd
            return DummyProcess(returncode=0, stdout=b"", stderr=b"")

        raise AssertionError(f"unexpected command: {args}")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    plugin = CalcomPlugin(name="calcom", base_dir=str(tmp_path))
    ctx = RestoreContext(
        job_id="1",
        source_target_id="1",
        destination_target_id="2",
        config={
            "database_url": "postgresql://appuser:pass@appdb/calcom",
            "database_direct_url": "postgresql://admin:pass@directdb/calcom",
            "restore_grant_role": "calcom_app",
        },
        artifact_path=str(artifact),
        metadata={"target_slug": "calcom"},
    )

    result = await plugin.restore(ctx)
    assert result["status"] == "success"
    # first restore, retry restore, then grant
    assert len(calls) == 3
