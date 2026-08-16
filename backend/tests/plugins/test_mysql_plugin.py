import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.plugins.base import BackupContext, RestoreContext
from app.main import app
from app.plugins.mysql import MySQLPlugin
from app.services.targets import TargetService


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


def _source_config() -> dict[str, object]:
    return {
        "mode": "source",
        "host": "mysql-source.internal",
        "port": 3306,
        "database": "application_production",
        "user": "backup_reader",
        "password": "synthetic-password",
        "ssl_mode": "REQUIRED",
    }


def _restore_config() -> dict[str, object]:
    return {
        **_source_config(),
        "mode": "restore_destination",
        "host": "mysql-restore.internal",
        "user": "restore_owner",
    }


def test_mysql_discovery_exposes_the_clean_breaking_partial_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The public API must advertise the exact strict MySQL 8.4 schema."""

    @asynccontextmanager
    async def route_only_lifespan(_app):  # type: ignore[no-untyped-def]
        yield

    monkeypatch.setattr(app.router, "lifespan_context", route_only_lifespan)
    with TestClient(app, backend_options={"use_uvloop": True}) as client:
        plugins_response = client.get("/api/v1/plugins/")
        schema_response = client.get("/api/v1/plugins/mysql/schema")

    assert plugins_response.status_code == 200
    assert next(item for item in plugins_response.json() if item["key"] == "mysql") == {
        "key": "mysql",
        "name": "mysql",
        "version": "0.2.1",
        "restore_capability": "partial",
    }
    assert schema_response.status_code == 200
    schema = schema_response.json()
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert schema["required"] == [
        "mode",
        "host",
        "port",
        "database",
        "user",
        "password",
        "ssl_mode",
    ]
    assert schema["properties"]["mode"] == {
        "type": "string",
        "title": "Mode",
        "enum": ["source", "restore_destination"],
    }
    assert schema["properties"]["port"]["minimum"] == 1
    assert schema["properties"]["port"]["maximum"] == 65535
    assert schema["properties"]["ssl_mode"]["enum"] == ["REQUIRED", "DISABLED"]
    for credential in ("user", "password"):
        assert "default" not in schema["properties"][credential]


@pytest.mark.asyncio
async def test_mysql_configuration_is_strict_mode_explicit_and_system_schema_free() -> None:
    """Only the exact flat source and restore-destination shapes are valid."""
    plugin = MySQLPlugin(name="mysql")
    source = _source_config()

    assert await plugin.validate_config(source) is True
    assert await plugin.validate_config(_restore_config()) is True

    invalid_configs: tuple[object, ...] = (
        None,
        {},
        {key: value for key, value in source.items() if key != "mode"},
        {**source, "mode": "legacy"},
        {**source, "host": "mysql://backup_reader@mysql-source/db"},
        {**source, "host": "mysql-source/path"},
        {**source, "host": "mysql-source\ninvalid"},
        {**source, "host": "mysql-source\x7finvalid"},
        {**source, "host": "mysql-source\x85invalid"},
        {**source, "port": True},
        {**source, "port": "3306"},
        {**source, "port": 0},
        {**source, "port": 65536},
        {**source, "database": ""},
        {**source, "database": "unsafe/name"},
        {**source, "database": "information_schema"},
        {**source, "database": "MYSQL"},
        {**source, "user": "  "},
        {**source, "user": "backup reader"},
        {**source, "password": ""},
        {**source, "password": "synthetic\tpassword"},
        {**source, "ssl_mode": "PREFERRED"},
        {**source, "ssl_mode": 1},
        {**source, "unexpected": "compatibility-fallback"},
    )
    for config in invalid_configs:
        assert await plugin.validate_config(config) is False  # type: ignore[arg-type]


def test_mysql_target_persistence_enforces_the_runtime_contract(
    db_session: Session,
) -> None:
    """Persisted targets must pass the same clean-breaking public shape."""
    service = TargetService(db_session)
    source = _source_config()
    serialized = json.dumps(source, sort_keys=True)

    target = service.create(
        name="MySQL exact source",
        plugin_name="mysql",
        plugin_config_json=serialized,
    )
    assert target.plugin_config_json == serialized

    invalid_configs = (
        {key: value for key, value in source.items() if key != "mode"},
        {**source, "host": "mysql://backup_reader@mysql-source/db"},
        {**source, "port": "3306"},
        {**source, "database": "mysql"},
        {**source, "ssl_mode": "PREFERRED"},
        {**source, "unexpected": "compatibility-fallback"},
    )
    for index, invalid in enumerate(invalid_configs):
        with pytest.raises(ValueError, match="Invalid plugin_config_json"):
            service.create(
                name=f"MySQL invalid target {index}",
                plugin_name="mysql",
                plugin_config_json=json.dumps(invalid),
            )


def test_mysql_public_test_api_rejects_legacy_shape_without_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The public connectivity seam must fail invalid configuration before I/O."""

    @asynccontextmanager
    async def route_only_lifespan(_app):  # type: ignore[no-untyped-def]
        yield

    async def forbidden_exec(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("invalid MySQL configuration reached the network")

    monkeypatch.setattr(app.router, "lifespan_context", route_only_lifespan)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden_exec)
    with TestClient(app, backend_options={"use_uvloop": True}) as client:
        response = client.post(
            "/api/v1/plugins/mysql/test",
            json={**_source_config(), "ssl_mode": "PREFERRED"},
        )

    assert response.status_code == 200
    assert response.json() == {
        "ok": False,
        "error": "Invalid MySQL source or restore-destination configuration",
    }
    assert str(_source_config()["password"]) not in response.text


def test_frontend_mock_does_not_claim_automatic_mysql_restore() -> None:
    """Frontend recovery affordances must reflect the partial MySQL boundary."""
    handlers = (
        Path(__file__).resolve().parents[3] / "frontend" / "src" / "mocks" / "handlers.ts"
    ).read_text(encoding="utf-8")
    assert (
        "key: 'mysql', name: 'MySQL', version: '1.0.0', restore_capability: 'partial'" in handlers
    )
    assert (
        "key: 'mysql', name: 'MySQL', version: '1.0.0', restore_capability: 'automatic'"
        not in handlers
    )


@pytest.mark.asyncio
async def test_test_returns_true(monkeypatch):
    async def fake_exec(*args, **kwargs):
        assert args[0] == "mysql"
        assert args[-2:] == ("-e", "SELECT 1")
        assert kwargs["env"]["MYSQL_PWD"] == "synthetic-password"
        return DummyProcess(returncode=0, stdout=b"1\n")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    plugin = MySQLPlugin(name="mysql")
    ok = await plugin.test(_source_config())
    assert ok is True


@pytest.mark.asyncio
async def test_test_raises_when_mysql_returns_unexpected_result(monkeypatch):
    async def fake_exec(*args, **kwargs):
        return DummyProcess(returncode=0, stdout=b"0\n")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    with pytest.raises(ConnectionError, match="validate MySQL connection"):
        await MySQLPlugin(name="mysql").test(_source_config())


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
        assert "-h" in args and "mysql-source.internal" in args
        assert "-u" in args and "backup_reader" in args
        assert "application_production" in args
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
        assert kwargs["env"]["MYSQL_PWD"] == "synthetic-password"
        return DummyProcess(returncode=0, stdout_stream=DummyStream(b"dump data"))

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr("app.plugins.mysql.plugin.BACKUP_BASE_PATH", str(tmp_path))
    plugin = MySQLPlugin(name="mysql")
    ctx = BackupContext(
        job_id="1",
        target_id="1",
        config=_source_config(),
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
            config=_source_config(),
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
            config=_restore_config(),
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
                config=_restore_config(),
                artifact_path=str(artifact),
            )
        )
