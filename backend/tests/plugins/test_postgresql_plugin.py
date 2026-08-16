import asyncio
import hashlib
import json
import logging
import os
import stat
import tempfile
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.plugins import postgresql as postgresql_core
from app.core.plugins.artifacts import create_backup_artifact
from app.core.plugins.base import BackupContext, RestoreContext
from app.core.plugins.postgresql import publish_postgresql_artifact
from app.core.plugins.sidecar import write_backup_sidecar
from app.main import app
from app.models import Run, Target, TargetRun
from app.plugins.postgresql import PostgreSQLPlugin
from app.services.restores import RestoreService
from app.services.targets import TargetService

PG16_BIN = "/usr/local/lib/postgresql/16/bin"
PSQL16 = f"{PG16_BIN}/psql"
PG_DUMP16 = f"{PG16_BIN}/pg_dump"
PG_RESTORE16 = f"{PG16_BIN}/pg_restore"
PG_DUMP_PREFIX = (
    postgresql_core.PRLIMIT,
    f"--fsize={postgresql_core.MAX_ARCHIVE_BYTES}:{postgresql_core.MAX_ARCHIVE_BYTES}",
    "--",
    PG_DUMP16,
)


class DummyProcess:
    def __init__(self, returncode=0, stdout=b"", stderr=b"", stdout_stream=None):
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self.stdout = stdout_stream or DummyStream(stdout)
        self.stderr = DummyStream(stderr)

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


class StreamOnlyProcess:
    """A subprocess double that forbids unbounded communicate()."""

    def __init__(
        self,
        *,
        stdout: bytes,
        stderr: bytes = b"",
        returncode: int = 0,
    ) -> None:
        self.returncode: int | None = None
        self._desired_returncode = returncode
        self.stdout = DummyStream(stdout)
        self.stderr = DummyStream(stderr)

    async def communicate(self) -> tuple[bytes, bytes]:
        raise AssertionError("archive inspection used unbounded communicate()")

    async def wait(self) -> int:
        self.returncode = self._desired_returncode
        return self._desired_returncode

    def terminate(self) -> None:
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9


def _safe_source_identity() -> dict[str, object]:
    """Return independent literal evidence for a small safe PG16 source."""
    return {
        "server_version_num": 160014,
        "server_version": "16.14",
        "database": "application_production",
        "server_encoding": "UTF8",
        "lc_collate": "C.UTF-8",
        "lc_ctype": "C.UTF-8",
        "schemas": ["public"],
        "extensions": [{"name": "plpgsql", "schema": "pg_catalog", "version": "1.0"}],
        "relations": [{"schema": "public", "name": "items", "kind": "r"}],
        "sequences": [{"schema": "public", "name": "items_id_seq"}],
        "rls_tables": [],
        "large_objects": [],
        "indexes": [],
        "constraints": [],
        "routines": [],
        "types": [],
        "invalid_indexes": [],
        "invalid_constraints": [],
        "event_triggers": [],
        "system_namespace_user_objects": [],
        "unsupported_database_objects": [],
        "security_definer_routines": [],
        "role_superuser": False,
        "role_bypassrls": False,
        "role_createdb": False,
        "role_createrole": False,
        "role_replication": False,
        "database_create": False,
        "database_temporary": False,
        "schema_create": [],
        "unusable_schemas": [],
        "unreadable_relations": [],
        "writable_relations": [],
        "unusable_sequences": [],
        "writable_sequences": [],
        "unreadable_large_objects": [],
        "dangerous_role_memberships": [],
        "unrelated_database_privileges": [],
    }


def test_postgresql_discovery_exposes_the_exact_mode_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The public API must advertise the clean-breaking PostgreSQL contract."""

    @asynccontextmanager
    async def route_only_lifespan(_app):  # type: ignore[no-untyped-def]
        yield

    monkeypatch.setattr(app.router, "lifespan_context", route_only_lifespan)
    with TestClient(app, backend_options={"use_uvloop": True}) as client:
        plugins_response = client.get("/api/v1/plugins/")
        schema_response = client.get("/api/v1/plugins/postgresql/schema")

    assert plugins_response.status_code == 200
    assert next(item for item in plugins_response.json() if item["key"] == "postgresql") == {
        "key": "postgresql",
        "name": "postgresql",
        "version": "0.2.1",
        "restore_capability": "automatic",
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
    ]
    assert schema["properties"]["mode"] == {
        "type": "string",
        "title": "Mode",
        "enum": ["source", "restore_destination"],
    }
    assert schema["properties"]["port"]["minimum"] == 1
    assert schema["properties"]["port"]["maximum"] == 65535
    assert "default" not in schema["properties"]["user"]
    assert "default" not in schema["properties"]["password"]


def test_postgresql_public_connectivity_api_is_real_and_secret_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The HTTP test seam must run the real probe and return useful safe failures."""
    secret = "public-api-password-must-not-leak"
    config = {
        "mode": "source",
        "host": "postgres-source.internal",
        "port": 5432,
        "database": "application_production",
        "user": "backup_reader",
        "password": secret,
    }

    @asynccontextmanager
    async def route_only_lifespan(_app):  # type: ignore[no-untyped-def]
        yield

    async def fake_exec(*_args: str, **_kwargs: object) -> DummyProcess:
        return StreamOnlyProcess(
            stdout=(json.dumps(_safe_source_identity()) + "\n").encode()
        )  # type: ignore[return-value]

    monkeypatch.setattr(app.router, "lifespan_context", route_only_lifespan)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    with TestClient(app, backend_options={"use_uvloop": True}) as client:
        success = client.post("/api/v1/plugins/postgresql/test", json=config)
        failure = client.post(
            "/api/v1/plugins/postgresql/test",
            json={**config, "password": ""},
        )

    assert success.status_code == 200
    assert success.json() == {"ok": True}
    assert failure.status_code == 200
    assert failure.json() == {
        "ok": False,
        "error": "Invalid PostgreSQL source or restore-destination configuration",
    }
    assert secret not in success.text + failure.text


@pytest.mark.asyncio
async def test_postgresql_configuration_is_strict_and_mode_explicit() -> None:
    """Only the exact flat source and restore-destination shapes are valid."""
    plugin = PostgreSQLPlugin(name="postgresql")
    source = {
        "mode": "source",
        "host": "postgres-source.internal",
        "port": 5432,
        "database": "application_production",
        "user": "backup_reader",
        "password": "synthetic-password",
    }
    destination = {**source, "mode": "restore_destination", "host": "postgres-restore"}

    assert await plugin.validate_config(source) is True
    assert await plugin.validate_config(destination) is True

    invalid_configs: tuple[object, ...] = (
        None,
        {},
        {key: value for key, value in source.items() if key != "mode"},
        {**source, "mode": "legacy"},
        {**source, "host": "postgresql://backup_reader@postgres-source/db"},
        {**source, "host": "postgres-source\ninvalid"},
        {**source, "host": "postgres-source\x7finvalid"},
        {**source, "host": "postgres-source\x85invalid"},
        {**source, "port": True},
        {**source, "port": "5432"},
        {**source, "port": 0},
        {**source, "port": 65536},
        {**source, "database": ""},
        {**source, "database": "unsafe/name"},
        {**source, "user": "  "},
        {**source, "user": "backup\x1breader"},
        {**source, "user": "backup\x9freader"},
        {**source, "password": ""},
        {**source, "password": "synthetic\tpassword"},
        {**source, "password": "synthetic\x80password"},
        {**source, "unexpected": "compatibility-fallback"},
    )
    for config in invalid_configs:
        assert await plugin.validate_config(config) is False


def test_postgresql_target_persistence_enforces_the_runtime_contract(
    db_session: Session,
) -> None:
    """Persisted targets must pass the same strict shape as plugin execution."""
    service = TargetService(db_session)
    source = {
        "mode": "source",
        "host": "postgres-source.internal",
        "port": 5432,
        "database": "application_production",
        "user": "backup_reader",
        "password": "synthetic-password",
    }
    serialized = json.dumps(source, sort_keys=True)

    target = service.create(
        name="PostgreSQL exact source",
        plugin_name="postgresql",
        plugin_config_json=serialized,
    )
    assert target.plugin_config_json == serialized

    invalid_configs = (
        {key: value for key, value in source.items() if key != "mode"},
        {**source, "host": "postgresql://backup_reader@postgres-source/db"},
        {**source, "database": "unsafe/name"},
        {**source, "user": "backup reader"},
        {**source, "unexpected": "compatibility-fallback"},
    )
    for index, invalid in enumerate(invalid_configs):
        with pytest.raises(ValueError, match="Invalid plugin_config_json"):
            service.create(
                name=f"PostgreSQL invalid target {index}",
                plugin_name="postgresql",
                plugin_config_json=json.dumps(invalid),
            )


@pytest.mark.asyncio
async def test_postgresql_test_uses_private_auth_and_exact_readonly_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Connectivity must prove the configured PG16 database without ambient secrets."""
    config = {
        "mode": "source",
        "host": "postgres-source.internal",
        "port": 5432,
        "database": "application_production",
        "user": "backup_reader",
        "password": "synthetic-password",
    }
    observed_password_files: list[Path] = []
    monkeypatch.setenv("PGPASSWORD", "ambient-password-must-be-removed")
    monkeypatch.setenv("PGSERVICE", "ambient-service-must-be-removed")
    monkeypatch.setenv("PGSSLMODE", "ambient-sslmode-must-be-removed")
    monkeypatch.setenv("PGSSLKEY", "/ambient/private/key-must-be-removed")
    monkeypatch.setenv("PGGSSENCMODE", "ambient-gss-mode-must-be-removed")

    async def fake_exec(*args: str, **kwargs: object) -> DummyProcess:
        assert args[:12] == (
            PSQL16,
            "-X",
            "-h",
            config["host"],
            "-p",
            str(config["port"]),
            "-U",
            config["user"],
            "--dbname",
            config["database"],
            "--set",
            "ON_ERROR_STOP=on",
        )
        assert args[-3:-1] == ("-tA", "-c")
        assert "server_version_num" in args[-1]
        assert "current_database" in args[-1]
        assert "server_encoding" in args[-1]
        environment = kwargs["env"]
        assert isinstance(environment, dict)
        assert "PGPASSWORD" not in environment
        assert "PGSERVICE" not in environment
        assert "PGSSLMODE" not in environment
        assert "PGSSLKEY" not in environment
        assert "PGGSSENCMODE" not in environment
        assert environment["PGCONNECT_TIMEOUT"] == "30"
        assert environment["PGOPTIONS"] == "-c statement_timeout=30000"
        password_file = Path(environment["PGPASSFILE"])
        observed_password_files.append(password_file)
        assert stat.S_IMODE(password_file.stat().st_mode) == 0o600
        assert password_file.read_text(encoding="utf-8") == (
            "postgres-source.internal:5432:application_production:"
            "backup_reader:synthetic-password\n"
        )
        return StreamOnlyProcess(
            stdout=(
                json.dumps(
                    {
                        "server_version_num": 160014,
                        "server_version": "16.14",
                        "database": "application_production",
                        "server_encoding": "UTF8",
                        "lc_collate": "C.UTF-8",
                        "lc_ctype": "C.UTF-8",
                        "schemas": ["public"],
                        "extensions": [
                            {"name": "plpgsql", "schema": "pg_catalog", "version": "1.0"}
                        ],
                        "relations": [{"schema": "public", "name": "items", "kind": "r"}],
                        "sequences": [{"schema": "public", "name": "items_id_seq"}],
                        "rls_tables": [],
                        "large_objects": [
                            {"oid": 16384, "owner": "application_owner", "readable": True}
                        ],
                        "indexes": [],
                        "constraints": [],
                        "routines": [],
                        "types": [],
                        "invalid_indexes": [],
                        "invalid_constraints": [],
                        "event_triggers": [],
                        "system_namespace_user_objects": [],
                        "unsupported_database_objects": [],
                        "security_definer_routines": [],
                        "role_superuser": False,
                        "role_bypassrls": False,
                        "role_createdb": False,
                        "role_createrole": False,
                        "role_replication": False,
                        "database_create": False,
                        "database_temporary": False,
                        "schema_create": [],
                        "unusable_schemas": [],
                        "unreadable_relations": [],
                        "writable_relations": [],
                        "unusable_sequences": [],
                        "writable_sequences": [],
                        "unreadable_large_objects": [],
                        "dangerous_role_memberships": [],
                        "unrelated_database_privileges": [],
                    }
                )
                + "\n"
            ).encode()
        )

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    assert await PostgreSQLPlugin(name="postgresql").test(config) is True
    assert observed_password_files
    assert all(not path.exists() for path in observed_password_files)


@pytest.mark.asyncio
async def test_postgresql_status_reports_checked_identity_and_safe_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Status must derive from the real probe and never expose credentials."""
    secret = "status-password-must-not-leak"
    config = {
        "mode": "source",
        "host": "postgres-source.internal",
        "port": 5432,
        "database": "application_production",
        "user": "backup_reader",
        "password": secret,
    }
    context = BackupContext(
        job_id="status-job",
        target_id="status-target",
        config=config,
        metadata={"target_slug": "postgres-status"},
    )
    identity = {
        "server_version_num": 160014,
        "server_version": "16.14 (Debian 16.14-1.pgdg13+1)",
        "database": "application_production",
        "server_encoding": "UTF8",
        "lc_collate": "C.UTF-8",
        "lc_ctype": "C.UTF-8",
        "schemas": ["public"],
        "extensions": [{"name": "plpgsql", "schema": "pg_catalog", "version": "1.0"}],
        "relations": [{"schema": "public", "name": "items", "kind": "r"}],
        "sequences": [{"schema": "public", "name": "items_id_seq"}],
        "rls_tables": [],
        "large_objects": [],
        "indexes": [],
        "constraints": [],
        "routines": [],
        "types": [],
        "invalid_indexes": [],
        "invalid_constraints": [],
        "event_triggers": [],
        "system_namespace_user_objects": [],
        "unsupported_database_objects": [],
        "security_definer_routines": [],
        "role_superuser": False,
        "role_bypassrls": False,
        "role_createdb": False,
        "role_createrole": False,
        "role_replication": False,
        "database_create": False,
        "database_temporary": False,
        "schema_create": [],
        "unusable_schemas": [],
        "unreadable_relations": [],
        "writable_relations": [],
        "unusable_sequences": [],
        "writable_sequences": [],
        "unreadable_large_objects": [],
        "dangerous_role_memberships": [],
        "unrelated_database_privileges": [],
    }

    async def successful_exec(*_args: str, **_kwargs: object) -> DummyProcess:
        return DummyProcess(stdout=(json.dumps(identity) + "\n").encode())

    monkeypatch.setattr(asyncio, "create_subprocess_exec", successful_exec)
    plugin = PostgreSQLPlugin(name="postgresql")
    assert await plugin.get_status(context) == {
        "status": "ok",
        "server_version": "16.14",
        "database": "application_production",
        "server_encoding": "UTF8",
        "lc_collate": "C.UTF-8",
        "lc_ctype": "C.UTF-8",
    }

    async def failed_exec(*_args: str, **_kwargs: object) -> DummyProcess:
        return DummyProcess(returncode=2, stderr=secret.encode())

    monkeypatch.setattr(asyncio, "create_subprocess_exec", failed_exec)
    failure = await plugin.get_status(context)
    assert failure == {
        "status": "error",
        "error": "Unable to connect to the PostgreSQL database",
    }
    assert secret not in json.dumps(failure)


@pytest.mark.asyncio
async def test_postgresql_source_probe_rejects_unsafe_catalog_or_privileges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A source is usable only when the dump identity is complete and read-only."""
    secret = "catalog-probe-password-must-not-leak"
    config = {
        "mode": "source",
        "host": "postgres-source.internal",
        "port": 5432,
        "database": "application_production",
        "user": "backup_reader",
        "password": secret,
    }
    safe_identity: dict[str, object] = {
        "server_version_num": 160014,
        "server_version": "16.14",
        "database": "application_production",
        "server_encoding": "UTF8",
        "lc_collate": "C.UTF-8",
        "lc_ctype": "C.UTF-8",
        "schemas": ["public"],
        "extensions": [{"name": "plpgsql", "schema": "pg_catalog", "version": "1.0"}],
        "relations": [{"schema": "public", "name": "items", "kind": "r"}],
        "sequences": [{"schema": "public", "name": "items_id_seq"}],
        "rls_tables": [],
        "large_objects": [{"oid": 16384, "owner": "application_owner", "readable": True}],
        "indexes": [],
        "constraints": [],
        "routines": [],
        "types": [],
        "invalid_indexes": [],
        "invalid_constraints": [],
        "event_triggers": [],
        "system_namespace_user_objects": [],
        "unsupported_database_objects": [],
        "security_definer_routines": [],
        "role_superuser": False,
        "role_bypassrls": False,
        "role_createdb": False,
        "role_createrole": False,
        "role_replication": False,
        "database_create": False,
        "database_temporary": False,
        "schema_create": [],
        "unusable_schemas": [],
        "unreadable_relations": [],
        "writable_relations": [],
        "unusable_sequences": [],
        "writable_sequences": [],
        "unreadable_large_objects": [],
        "dangerous_role_memberships": [],
        "unrelated_database_privileges": [],
    }
    current_identity = safe_identity

    async def fake_exec(*_args: str, **_kwargs: object) -> DummyProcess:
        return DummyProcess(stdout=(json.dumps(current_identity) + "\n").encode())

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    plugin = PostgreSQLPlugin(name="postgresql")
    assert await plugin.test(config) is True

    unsafe_cases = (
        ({"role_superuser": True}, "privilege"),
        ({"role_createdb": True}, "privilege"),
        ({"rls_tables": ["public.private_items"]}, "RLS"),
        ({"invalid_indexes": ["public.items_bad_idx"]}, "invalid"),
        ({"unusable_schemas": ["public"]}, "schema"),
        ({"unreadable_relations": ["public.items"]}, "read"),
        ({"writable_relations": ["public.items"]}, "write"),
        ({"unusable_sequences": ["public.items_id_seq"]}, "sequence"),
        ({"writable_sequences": ["public.items_id_seq"]}, "sequence"),
        ({"unreadable_large_objects": [16384]}, "large object"),
        ({"event_triggers": ["unsafe_trigger"]}, "executable"),
        (
            {"system_namespace_user_objects": ["routine:pg_catalog.unsafe()"]},
            "executable",
        ),
        ({"unsupported_database_objects": ["server:unsafe_server"]}, "unsupported"),
        ({"security_definer_routines": ["public.unsafe()"]}, "definer"),
        ({"database_temporary": True}, "temporary"),
        ({"dangerous_role_memberships": ["pg_signal_backend"]}, "outside"),
        ({"dangerous_role_memberships": ["pg_read_all_data"]}, "outside"),
        ({"unrelated_database_privileges": ["postgres"]}, "outside"),
    )
    for replacement, error_pattern in unsafe_cases:
        current_identity = {**safe_identity, **replacement}
        with pytest.raises(RuntimeError, match=error_pattern):
            await plugin.test(config)
    assert secret not in repr(current_identity)


@pytest.mark.asyncio
async def test_backup_publishes_private_validated_archive_and_sidecar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A backup streams one exact custom archive and secret-free evidence."""
    secret = "backup-password-must-not-leak"
    config = {
        "mode": "source",
        "host": "postgres-source.internal",
        "port": 5432,
        "database": "application_production",
        "user": "backup_reader",
        "password": secret,
    }
    identity = {
        "server_version_num": 160014,
        "server_version": "16.14",
        "database": "application_production",
        "server_encoding": "UTF8",
        "lc_collate": "C.UTF-8",
        "lc_ctype": "C.UTF-8",
        "schemas": ["public"],
        "extensions": [{"name": "plpgsql", "schema": "pg_catalog", "version": "1.0"}],
        "relations": [{"schema": "public", "name": "items", "kind": "r"}],
        "sequences": [{"schema": "public", "name": "items_id_seq"}],
        "rls_tables": [],
        "large_objects": [],
        "indexes": [],
        "constraints": [],
        "routines": [],
        "types": [],
        "invalid_indexes": [],
        "invalid_constraints": [],
        "event_triggers": [],
        "system_namespace_user_objects": [],
        "unsupported_database_objects": [],
        "security_definer_routines": [],
        "role_superuser": False,
        "role_bypassrls": False,
        "role_createdb": False,
        "role_createrole": False,
        "role_replication": False,
        "database_create": False,
        "database_temporary": False,
        "schema_create": [],
        "unusable_schemas": [],
        "unreadable_relations": [],
        "writable_relations": [],
        "unusable_sequences": [],
        "writable_sequences": [],
        "unreadable_large_objects": [],
        "dangerous_role_memberships": [],
        "unrelated_database_privileges": [],
    }
    dump_bytes = b"PGDMP\x01\x0f synthetic PostgreSQL 16 custom archive"
    toc = b"\n".join(
        (
            b"; PostgreSQL database dump",
            b"; Dumped from database version: 16.14 (Debian 16.14-1)",
            b"; Dumped by pg_dump version: 16.14 (Debian 16.14-1)",
            b"1; 0 0 EXTENSION - plpgsql ",
            b"2; 1259 16384 TABLE public items application_owner",
            b"3; 0 16384 TABLE DATA public items application_owner",
            b"4; 1259 16385 SEQUENCE public items_id_seq application_owner",
            b"",
        )
    )
    password_files: list[Path] = []

    async def fake_exec(*args: str, **kwargs: object) -> DummyProcess:
        environment = kwargs.get("env")
        if isinstance(environment, dict) and "PGPASSFILE" in environment:
            assert "PGPASSWORD" not in environment
            password_file = Path(environment["PGPASSFILE"])
            password_files.append(password_file)
            assert stat.S_IMODE(password_file.stat().st_mode) == 0o600
        if args[0] == PSQL16:
            return DummyProcess(stdout=(json.dumps(identity) + "\n").encode())
        if args[:4] == PG_DUMP_PREFIX:
            assert args == PG_DUMP_PREFIX + (
                "-h",
                config["host"],
                "-p",
                str(config["port"]),
                "-U",
                config["user"],
                "--format=custom",
                "--no-owner",
                "--no-privileges",
                config["database"],
            )
            output_descriptor = kwargs["stdout"]
            assert isinstance(output_descriptor, int) and output_descriptor >= 0
            assert "preexec_fn" not in kwargs
            os.write(output_descriptor, dump_bytes)
            return DummyProcess()
        if args[0] == postgresql_core.SHA256SUM:
            assert args[1] == "--binary"
            return StreamOnlyProcess(
                stdout=f"{hashlib.sha256(dump_bytes).hexdigest()}  artifact\n".encode()
            )  # type: ignore[return-value]
        assert args[:2] == (PG_RESTORE16, "--list")
        assert args[-1].startswith("/proc/self/fd/")
        assert kwargs["stdout"] == asyncio.subprocess.PIPE
        assert kwargs["pass_fds"]
        return StreamOnlyProcess(stdout=toc)  # type: ignore[return-value]

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr("app.plugins.postgresql.plugin.BACKUP_BASE_PATH", str(tmp_path))
    plugin = PostgreSQLPlugin(name="postgresql")
    ctx = BackupContext(
        job_id="postgresql-backup-job",
        target_id="postgresql-source-target",
        config=config,
        metadata={"target_slug": "postgresql-source"},
    )

    real_fsync = os.fsync

    def reject_parent_archive_fsync(descriptor: int) -> None:
        try:
            descriptor_path = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
        except OSError:
            descriptor_path = Path("/")
        if descriptor_path.is_relative_to(tmp_path) and descriptor_path.suffix == ".tmp":
            raise AssertionError("archive fsync must run inside the bounded publication worker")
        real_fsync(descriptor)

    monkeypatch.setattr(postgresql_core.os, "fsync", reject_parent_archive_fsync)

    result = await plugin.backup(ctx)
    artifact_path = Path(result["artifact_path"])
    sidecar_path = Path(f"{artifact_path}.meta.json")
    assert artifact_path.is_absolute()
    assert artifact_path.read_bytes() == dump_bytes
    assert stat.S_IMODE(artifact_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(sidecar_path.stat().st_mode) == 0o600
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    assert sidecar["postgresql_server_version"] == "16.14"
    assert sidecar["postgresql_server_version_num"] == 160014
    assert sidecar["server_encoding"] == "UTF8"
    assert sidecar["lc_collate"] == "C.UTF-8"
    assert sidecar["lc_ctype"] == "C.UTF-8"
    assert sidecar["rls_table_count"] == 0
    assert sidecar["validation"] == "postgresql-custom-v1"
    assert sidecar["catalog_counts"] == {
        "extensions": 1,
        "indexes": 0,
        "constraints": 0,
        "routines": 0,
        "types": 0,
        "large_objects": 0,
        "relations": 1,
        "schemas": 1,
        "sequences": 1,
    }
    for key in (
        "source_identity_sha256",
        "source_catalog_sha256",
        "archive_catalog_sha256",
        "toc_sha256",
    ):
        assert isinstance(sidecar[key], str) and len(sidecar[key]) == 64
    assert secret not in json.dumps(sidecar)
    assert config["host"] not in json.dumps(sidecar)
    assert config["user"] not in json.dumps(sidecar)
    assert password_files and all(not path.exists() for path in password_files)


@pytest.mark.asyncio
async def test_backup_rejects_archive_whose_objects_do_not_match_the_source_catalog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid custom archive cannot substitute a different relation inventory."""
    config = {
        "mode": "source",
        "host": "postgres-source.internal",
        "port": 5432,
        "database": "application_production",
        "user": "backup_reader",
        "password": "synthetic-password",
    }
    mismatched_toc = b"\n".join(
        (
            b"; PostgreSQL database dump",
            b"; Dumped from database version: 16.14 (Debian 16.14-1)",
            b"; Dumped by pg_dump version: 16.14 (Debian 16.14-1)",
            b"1; 0 0 EXTENSION - plpgsql ",
            b"2; 1259 16384 TABLE public substituted_items application_owner",
            b"3; 0 16384 TABLE DATA public substituted_items application_owner",
            b"4; 1259 16385 SEQUENCE public items_id_seq application_owner",
            b"",
        )
    )

    dump_bytes = b"PGDMP\x01\x0f archive"

    async def fake_exec(*args: str, **kwargs: object) -> DummyProcess:
        if args[0] == PSQL16:
            return DummyProcess(stdout=(json.dumps(_safe_source_identity()) + "\n").encode())
        if args[:4] == PG_DUMP_PREFIX:
            output_descriptor = kwargs["stdout"]
            assert isinstance(output_descriptor, int)
            os.write(output_descriptor, dump_bytes)
            return DummyProcess()
        if args[0] == postgresql_core.SHA256SUM:
            return StreamOnlyProcess(
                stdout=f"{hashlib.sha256(dump_bytes).hexdigest()}  artifact\n".encode()
            )  # type: ignore[return-value]
        return StreamOnlyProcess(stdout=mismatched_toc)  # type: ignore[return-value]

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr("app.plugins.postgresql.plugin.BACKUP_BASE_PATH", str(tmp_path))
    context = BackupContext(
        job_id="catalog-mismatch-job",
        target_id="postgresql-source-target",
        config=config,
        metadata={"target_slug": "postgresql-source"},
    )

    with pytest.raises(RuntimeError, match="catalog"):
        await PostgreSQLPlugin(name="postgresql").backup(context)
    assert not [path for path in tmp_path.rglob("*") if path.is_file()]


@pytest.mark.asyncio
async def test_backup_rejects_ambiguous_duplicate_archive_catalog_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A custom archive cannot repeat one authority object under another TOC id."""
    config = {
        "mode": "source",
        "host": "postgres-source.internal",
        "port": 5432,
        "database": "application_production",
        "user": "backup_reader",
        "password": "synthetic-password",
    }
    ambiguous_toc = b"\n".join(
        (
            b"; PostgreSQL database dump",
            b"; Dumped from database version: 16.14 (Debian 16.14-1)",
            b"; Dumped by pg_dump version: 16.14 (Debian 16.14-1)",
            b"1; 0 0 EXTENSION - plpgsql ",
            b"2; 1259 16384 TABLE public items application_owner",
            b"3; 1259 16384 TABLE public items application_owner",
            b"4; 1259 16385 SEQUENCE public items_id_seq application_owner",
            b"",
        )
    )

    dump_bytes = b"PGDMP\x01\x0f archive"

    async def fake_exec(*args: str, **kwargs: object) -> DummyProcess:
        if args[0] == PSQL16:
            return DummyProcess(stdout=(json.dumps(_safe_source_identity()) + "\n").encode())
        if args[:4] == PG_DUMP_PREFIX:
            output_descriptor = kwargs["stdout"]
            assert isinstance(output_descriptor, int)
            os.write(output_descriptor, dump_bytes)
            return DummyProcess()
        if args[0] == postgresql_core.SHA256SUM:
            return StreamOnlyProcess(
                stdout=f"{hashlib.sha256(dump_bytes).hexdigest()}  artifact\n".encode()
            )  # type: ignore[return-value]
        return StreamOnlyProcess(stdout=ambiguous_toc)  # type: ignore[return-value]

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr("app.plugins.postgresql.plugin.BACKUP_BASE_PATH", str(tmp_path))
    context = BackupContext(
        job_id="ambiguous-catalog-job",
        target_id="postgresql-source-target",
        config=config,
        metadata={"target_slug": "postgresql-source"},
    )

    with pytest.raises(RuntimeError, match="ambiguous TOC"):
        await PostgreSQLPlugin(name="postgresql").backup(context)
    assert not [path for path in tmp_path.rglob("*") if path.is_file()]


@pytest.mark.parametrize(
    "entry",
    (
        "1; 0 0 SERVER - unsafe_server application_owner",
        "1; 0 0 USER MAPPING application_owner unsafe_server application_owner",
        "1; 0 0 PUBLICATION - unsafe_publication application_owner",
        "1; 0 0 OPERATOR public ## application_owner",
        "1; 0 0 COLLATION public unsafe_collation application_owner",
    ),
)
def test_archive_catalog_rejects_every_unclassified_toc_descriptor(entry: str) -> None:
    """Unknown database object classes cannot disappear from archive evidence."""
    with pytest.raises(RuntimeError, match="unsafe TOC"):
        postgresql_core._archive_catalog_sha256([entry])


def test_archive_catalog_refuses_identifiers_the_toc_cannot_bind_unambiguously() -> None:
    """Whitespace-delimited TOC authority must fail closed for quoted names."""
    payload = _safe_source_identity()
    payload["schemas"] = ["public", "schema with space"]
    payload["relations"] = [
        {"schema": "schema with space", "name": "table with space", "kind": "r"}
    ]
    payload["sequences"] = []
    target = postgresql_core.PostgreSQLTarget.from_config(
        {
            "mode": "source",
            "host": "postgres-source.internal",
            "port": 5432,
            "database": "application_production",
            "user": "backup_reader",
            "password": "synthetic-password",
        }
    )
    identity = postgresql_core._parse_identity(
        json.dumps(payload).encode(),
        target,
        expected_state="source",
    )
    toc = b"\n".join(
        (
            b"; Dumped from database version: 16.14",
            b"; Dumped by pg_dump version: 16.14",
            b"1; 0 0 EXTENSION - plpgsql ",
            b"2; 2615 16384 SCHEMA - schema with space owner with space",
            b"3; 1259 16385 TABLE schema with space table with space owner with space",
            b"4; 0 16385 TABLE DATA schema with space table with space owner with space",
        )
    )

    with pytest.raises(RuntimeError, match="unambiguously"):
        postgresql_core._validate_archive_toc(toc, identity)


def test_archive_catalog_rejects_prefix_extension_and_accepts_exact_owner_boundary() -> None:
    """An object name cannot be confused with a longer whitespace-delimited name."""
    target = postgresql_core.PostgreSQLTarget.from_config(
        {
            "mode": "source",
            "host": "postgres-source.internal",
            "port": 5432,
            "database": "application_production",
            "user": "backup_reader",
            "password": "synthetic-password",
        }
    )
    identity = postgresql_core._parse_identity(
        json.dumps(_safe_source_identity()).encode(),
        target,
        expected_state="source",
    )
    exact = b"\n".join(
        (
            b"; Dumped from database version: 16.14",
            b"; Dumped by pg_dump version: 16.14",
            b"1; 0 0 EXTENSION - plpgsql ",
            b"2; 1259 16384 TABLE public items application_owner",
            b"3; 1259 16385 SEQUENCE public items_id_seq application_owner",
        )
    )

    postgresql_core._validate_archive_toc(exact, identity)

    with pytest.raises(RuntimeError, match="source catalog"):
        postgresql_core._validate_archive_toc(
            exact.replace(
                b"TABLE public items application_owner",
                b"TABLE public items substituted application_owner",
            ),
            identity,
        )


def test_source_catalog_digest_binds_constraint_definitions_and_validation() -> None:
    """FK and CHECK semantics, not only their names, must survive recovery."""
    payload = _safe_source_identity()
    payload["relations"] = [
        {"schema": "public", "name": "accounts", "kind": "r"},
        {"schema": "public", "name": "entries", "kind": "r"},
    ]
    payload["sequences"] = []
    payload["constraints"] = [
        {
            "schema": "public",
            "table": "entries",
            "name": "entries_account_id_fkey",
            "type": "f",
            "definition": "FOREIGN KEY (account_id) REFERENCES accounts(id)",
            "validated": True,
        },
        {
            "schema": "public",
            "table": "accounts",
            "name": "accounts_id_check",
            "type": "c",
            "definition": "CHECK (id > 0)",
            "validated": True,
        },
    ]
    baseline = postgresql_core._expected_archive_catalog_sha256(payload)
    changed = json.loads(json.dumps(payload))
    changed["constraints"][0]["definition"] = "FOREIGN KEY (account_id) REFERENCES entries(id)"
    unvalidated = json.loads(json.dumps(payload))
    unvalidated["constraints"][1]["validated"] = False

    assert postgresql_core._expected_archive_catalog_sha256(changed) != baseline
    assert postgresql_core._expected_archive_catalog_sha256(unvalidated) != baseline


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    (
        "dump-nonzero",
        "dump-diagnostic",
        "empty-archive",
        "oversized-archive",
        "inspector-nonzero",
        "inspector-diagnostic",
        "malformed-toc",
        "oversized-toc",
    ),
)
async def test_backup_fails_closed_for_process_and_archive_boundaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """Every dump/inspection failure must remove private state and publish nothing."""
    config = {
        "mode": "source",
        "host": "postgres-source.internal",
        "port": 5432,
        "database": "application_production",
        "user": "backup_reader",
        "password": "boundary-secret-must-not-leak",
    }
    dump_bytes = b"PGDMP\x01\x0f bounded archive"
    valid_toc = b"\n".join(
        (
            b"; PostgreSQL database dump",
            b"; Dumped from database version: 16.14 (Debian 16.14-1)",
            b"; Dumped by pg_dump version: 16.14 (Debian 16.14-1)",
            b"1; 0 0 EXTENSION - plpgsql ",
            b"2; 1259 16384 TABLE public items application_owner",
            b"3; 1259 16385 SEQUENCE public items_id_seq application_owner",
            b"",
        )
    )

    if failure == "oversized-archive":
        monkeypatch.setattr(
            "app.core.plugins.postgresql.MAX_ARCHIVE_BYTES",
            len(dump_bytes) - 1,
        )
    if failure == "oversized-toc":
        monkeypatch.setattr("app.core.plugins.postgresql.MAX_TOC_BYTES", 16)

    async def fake_exec(*args: str, **kwargs: object) -> DummyProcess:
        if args[0] == PSQL16:
            return StreamOnlyProcess(
                stdout=(json.dumps(_safe_source_identity()) + "\n").encode()
            )  # type: ignore[return-value]
        if args[:4] == PG_DUMP_PREFIX:
            if failure == "dump-diagnostic":
                diagnostics = kwargs["stderr"]
                assert hasattr(diagnostics, "write")
                diagnostics.write(b"synthetic warning")  # type: ignore[union-attr]
            if failure != "empty-archive":
                output_descriptor = kwargs["stdout"]
                assert isinstance(output_descriptor, int)
                os.write(output_descriptor, dump_bytes)
            return DummyProcess(returncode=1 if failure == "dump-nonzero" else 0)
        if args[0] == postgresql_core.SHA256SUM:
            return StreamOnlyProcess(
                stdout=f"{hashlib.sha256(dump_bytes).hexdigest()}  artifact\n".encode()
            )  # type: ignore[return-value]
        return StreamOnlyProcess(
            stdout=b"malformed" if failure == "malformed-toc" else valid_toc,
            stderr=b"synthetic inspector warning" if failure == "inspector-diagnostic" else b"",
            returncode=1 if failure == "inspector-nonzero" else 0,
        )  # type: ignore[return-value]

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr("app.plugins.postgresql.plugin.BACKUP_BASE_PATH", str(tmp_path))
    context = BackupContext(
        job_id=f"postgresql-{failure}",
        target_id="postgresql-source-target",
        config=config,
        metadata={"target_slug": "postgresql-source"},
    )

    with pytest.raises(RuntimeError):
        await PostgreSQLPlugin(name="postgresql").backup(context)
    assert not [path for path in tmp_path.rglob("*") if path.is_file()]


@pytest.mark.asyncio
async def test_backup_cancellation_reaps_dump_and_cleans_private_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelling the public operation must reap pg_dump before cleanup returns."""

    class BlockingDump:
        returncode: int | None = None

        def __init__(self) -> None:
            self.entered = asyncio.Event()
            self.terminated = False
            self.reaped = False

        def terminate(self) -> None:
            self.terminated = True
            self.returncode = -15

        def kill(self) -> None:
            self.returncode = -9

        async def wait(self) -> int:
            self.entered.set()
            while self.returncode is None:
                await asyncio.sleep(0)
            self.reaped = True
            return self.returncode

    config = {
        "mode": "source",
        "host": "postgres-source.internal",
        "port": 5432,
        "database": "application_production",
        "user": "backup_reader",
        "password": "cancel-secret-must-not-leak",
    }
    dump = BlockingDump()

    async def fake_exec(*args: str, **kwargs: object) -> object:
        if args[0] == PSQL16:
            return StreamOnlyProcess(stdout=(json.dumps(_safe_source_identity()) + "\n").encode())
        assert args[:4] == PG_DUMP_PREFIX
        output_descriptor = kwargs["stdout"]
        assert isinstance(output_descriptor, int)
        os.write(output_descriptor, b"PGDMP partial archive")
        return dump

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr("app.plugins.postgresql.plugin.BACKUP_BASE_PATH", str(tmp_path))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    context = BackupContext(
        job_id="postgresql-cancel",
        target_id="postgresql-source",
        config=config,
        metadata={"target_slug": "postgresql-cancel"},
    )
    task = asyncio.create_task(PostgreSQLPlugin(name="postgresql").backup(context))
    await dump.entered.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert dump.terminated is True
    assert dump.reaped is True
    assert not [path for path in tmp_path.rglob("*") if path.is_file()]


@pytest.mark.asyncio
async def test_backup_cumulative_timeout_reaps_dump_and_publishes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The public deadline must stop/reap pg_dump before artifact cleanup returns."""

    class BlockingDump:
        returncode: int | None = None

        def __init__(self) -> None:
            self.terminated = False
            self.reaped = False

        def terminate(self) -> None:
            self.terminated = True
            self.returncode = -15

        def kill(self) -> None:
            self.returncode = -9

        async def wait(self) -> int:
            while self.returncode is None:
                await asyncio.sleep(0)
            self.reaped = True
            return self.returncode

    config = {
        "mode": "source",
        "host": "postgres-source.internal",
        "port": 5432,
        "database": "application_production",
        "user": "backup_reader",
        "password": "timeout-secret-must-not-leak",
    }
    dump = BlockingDump()
    calls = 0

    async def fake_exec(*args: str, **_kwargs: object) -> object:
        nonlocal calls
        calls += 1
        if calls == 1:
            return StreamOnlyProcess(stdout=(json.dumps(_safe_source_identity()) + "\n").encode())
        assert args[:4] == PG_DUMP_PREFIX
        return dump

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr("app.plugins.postgresql.plugin.BACKUP_BASE_PATH", str(tmp_path))
    monkeypatch.setattr("app.plugins.postgresql.plugin.BACKUP_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    context = BackupContext(
        job_id="postgresql-timeout",
        target_id="postgresql-source",
        config=config,
        metadata={"target_slug": "postgresql-timeout"},
    )

    with (
        caplog.at_level(logging.INFO),
        pytest.raises(RuntimeError, match="PostgreSQL backup timed out"),
    ):
        await PostgreSQLPlugin(name="postgresql").backup(context)

    assert dump.terminated is True
    assert dump.reaped is True
    assert not [path for path in tmp_path.rglob("*") if path.is_file()]
    assert config["password"] not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ("timeout", "cancel"))
async def test_bounded_publication_stops_worker_and_cleans_owned_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    """Publication timeout/cancellation must reap its worker before owned cleanup."""

    class BlockingPublicationProcess:
        exitcode: int | None = None

        def __init__(self) -> None:
            self.alive = True
            self.terminated = False
            self.reaped = False

        def is_alive(self) -> bool:
            return self.alive

        def terminate(self) -> None:
            self.terminated = True
            self.alive = False
            self.exitcode = -15

        def kill(self) -> None:
            self.alive = False
            self.exitcode = -9

        def join(self, _timeout: float) -> None:
            self.reaped = not self.alive

    class EmptyConnection:
        def __init__(self) -> None:
            self.closed = False

        def poll(self) -> bool:
            return False

        def recv(self) -> object:
            raise AssertionError("blocking publication returned a result")

        def close(self) -> None:
            self.closed = True

    process = BlockingPublicationProcess()
    connection = EmptyConnection()

    def fake_start(_request: object) -> tuple[object, object]:
        return process, connection

    monkeypatch.setattr(
        "app.core.plugins.postgresql._start_publication_process",
        fake_start,
    )
    monkeypatch.setattr(
        "app.core.plugins.postgresql.PUBLICATION_TIMEOUT_SECONDS",
        0.01,
    )
    plugin = PostgreSQLPlugin(name="postgresql")
    context = BackupContext(
        job_id=f"publication-{operation}",
        target_id="postgresql-source",
        config={},
        metadata={"target_slug": "postgresql-publication"},
    )

    async def publish() -> None:
        with create_backup_artifact(
            plugin,
            context,
            prefix="postgresql-publication",
            suffix=".dump",
            backup_root=tmp_path,
        ) as artifact:
            payload = b"PGDMP bounded publication"
            artifact.publication_fd = os.open(
                artifact.temporary_path,
                os.O_RDWR | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            os.write(artifact.publication_fd, payload)
            artifact.publication_sha256 = hashlib.sha256(payload).hexdigest()
            await publish_postgresql_artifact(artifact, plugin, context)

    task = asyncio.create_task(publish())
    if operation == "cancel":
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(RuntimeError, match="publication timed out"):
            await task

    assert process.terminated is True
    assert process.reaped is True
    assert connection.closed is True
    assert not [path for path in tmp_path.rglob("*") if path.is_file()]


@pytest.mark.asyncio
async def test_backup_rejects_same_size_replacement_before_descriptor_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Publication must bind the bytes streamed by pg_dump, not a path replacement."""
    config = {
        "mode": "source",
        "host": "postgres-source.internal",
        "port": 5432,
        "database": "application_production",
        "user": "backup_reader",
        "password": "synthetic-password",
    }
    original = b"PGDMP\x01\x0f original-streamed-archive"
    replacement = b"PGDMP\x01\x0f substituted-path-archive!"
    assert len(original) == len(replacement)
    toc = b"\n".join(
        (
            b"; PostgreSQL database dump",
            b"; Dumped from database version: 16.14 (Debian 16.14-1)",
            b"; Dumped by pg_dump version: 16.14 (Debian 16.14-1)",
            b"1; 0 0 EXTENSION - plpgsql ",
            b"2; 1259 16384 TABLE public items application_owner",
            b"3; 0 16384 TABLE DATA public items application_owner",
            b"4; 1259 16385 SEQUENCE public items_id_seq application_owner",
            b"",
        )
    )

    replaced = False

    async def fake_exec(*args: str, **kwargs: object) -> DummyProcess:
        nonlocal replaced
        if args[0] == PSQL16:
            return DummyProcess(stdout=(json.dumps(_safe_source_identity()) + "\n").encode())
        if args[:4] == PG_DUMP_PREFIX:
            output_descriptor = kwargs["stdout"]
            assert isinstance(output_descriptor, int)
            os.write(output_descriptor, original)
            return DummyProcess()
        if args[0] == postgresql_core.SHA256SUM:
            descriptor = next(iter(kwargs["pass_fds"]))  # type: ignore[arg-type]
            artifact_path = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
            artifact_path.write_bytes(replacement)
            replaced = True
            return StreamOnlyProcess(
                stdout=f"{hashlib.sha256(original).hexdigest()}  artifact\n".encode()
            )  # type: ignore[return-value]
        return StreamOnlyProcess(stdout=toc)  # type: ignore[return-value]

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr("app.plugins.postgresql.plugin.BACKUP_BASE_PATH", str(tmp_path))
    context = BackupContext(
        job_id="replacement-race-job",
        target_id="postgresql-source-target",
        config=config,
        metadata={"target_slug": "postgresql-source"},
    )

    with pytest.raises(RuntimeError, match="identity|changed"):
        await PostgreSQLPlugin(name="postgresql").backup(context)
    assert replaced is True
    assert not [path for path in tmp_path.rglob("*") if path.is_file()]


@pytest.mark.asyncio
async def test_restore_requires_local_allowlist_and_distinct_database_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Restore authorization failures must occur before any PostgreSQL I/O."""
    artifact_path = tmp_path / "staged-postgresql.dump"
    artifact_path.write_bytes(b"PGDMP\x01\x0f staged archive")
    destination_config = {
        "mode": "restore_destination",
        "host": "postgres-restore.internal",
        "port": 5432,
        "database": "application_restore_a",
        "user": "restore_owner",
        "password": "synthetic-restore-password",
    }
    metadata = {
        "source_database_identity": {
            "host": "postgres-source.internal",
            "port": 5432,
            "database": "application_production",
            "user": "backup_reader",
        }
    }

    async def forbidden_exec(*_args: str, **_kwargs: object) -> DummyProcess:
        raise AssertionError("restore refusal performed PostgreSQL I/O")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden_exec)
    plugin = PostgreSQLPlugin(name="postgresql")

    def context(*, config: dict[str, object], source_metadata: dict[str, object]) -> RestoreContext:
        return RestoreContext(
            job_id="postgresql-restore-job",
            source_target_id="postgresql-source-target",
            destination_target_id="postgresql-destination-target",
            artifact_path=str(artifact_path),
            config=config,
            metadata=source_metadata,
        )

    monkeypatch.delenv("HOMELAB_BACKUP_ALLOW_ISOLATED_RESTORE", raising=False)
    monkeypatch.delenv("HOMELAB_BACKUP_ISOLATED_POSTGRESQL_RESTORE_DESTINATIONS", raising=False)
    with pytest.raises(ValueError, match="disabled"):
        await plugin.restore(context(config=destination_config, source_metadata=metadata))

    monkeypatch.setenv("HOMELAB_BACKUP_ALLOW_ISOLATED_RESTORE", "1")
    with pytest.raises(ValueError, match="allowlist"):
        await plugin.restore(context(config=destination_config, source_metadata=metadata))

    monkeypatch.setenv(
        "HOMELAB_BACKUP_ISOLATED_POSTGRESQL_RESTORE_DESTINATIONS",
        "POSTGRES-RESTORE.INTERNAL:5432/Application_Restore_A",
    )
    with pytest.raises(ValueError, match="allowlist"):
        await plugin.restore(context(config=destination_config, source_metadata=metadata))

    monkeypatch.setenv(
        "HOMELAB_BACKUP_ISOLATED_POSTGRESQL_RESTORE_DESTINATIONS",
        "postgres-restore.internal:5432/application_restore_a",
    )
    same_database_metadata = {
        "source_database_identity": {
            "host": "postgres-restore.internal",
            "port": 5432,
            "database": "application_restore_a",
            "user": "different_user_does_not_make_it_safe",
        }
    }
    with pytest.raises(ValueError, match="distinct"):
        await plugin.restore(
            context(config=destination_config, source_metadata=same_database_metadata)
        )


@pytest.mark.asyncio
async def test_restore_refuses_destination_without_exact_fresh_database_sentinel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed fresh-database sentinel must stop restore before mutation."""
    artifact_path = tmp_path / "staged-postgresql.dump"
    artifact_path.write_bytes(b"PGDMP\x01\x0f staged archive")
    config = {
        "mode": "restore_destination",
        "host": "postgres-restore.internal",
        "port": 5432,
        "database": "application_restore_a",
        "user": "restore_owner",
        "password": "synthetic-restore-password",
    }
    destination_identity = {
        "server_version_num": 160014,
        "server_version": "16.14",
        "database": "application_restore_a",
        "server_encoding": "UTF8",
        "lc_collate": "C.UTF-8",
        "lc_ctype": "C.UTF-8",
        "database_comment": "wrong-sentinel",
        "database_owner": "restore_owner",
        "current_user": "restore_owner",
        "other_connections": 0,
        "schemas": ["public"],
        "extensions": [{"name": "plpgsql", "schema": "pg_catalog", "version": "1.0"}],
        "relations": [],
        "sequences": [],
        "rls_tables": [],
        "large_objects": [],
        "indexes": [],
        "constraints": [],
        "routines": [],
        "types": [],
        "invalid_indexes": [],
        "invalid_constraints": [],
        "event_triggers": [],
        "system_namespace_user_objects": [],
        "unsupported_database_objects": [],
        "security_definer_routines": [],
        "role_superuser": False,
        "role_bypassrls": False,
        "role_createdb": False,
        "role_createrole": False,
        "role_replication": False,
        "database_create": True,
        "database_temporary": True,
        "schema_create": ["public"],
        "unusable_schemas": [],
        "unreadable_relations": [],
        "writable_relations": [],
        "unusable_sequences": [],
        "writable_sequences": [],
        "unreadable_large_objects": [],
        "dangerous_role_memberships": [],
        "unrelated_database_privileges": [],
    }
    calls: list[str] = []

    async def fake_exec(*args: str, **_kwargs: object) -> DummyProcess:
        calls.append(args[0])
        if args[0] != PSQL16:
            raise AssertionError("unsafe destination reached artifact mutation")
        return DummyProcess(stdout=(json.dumps(destination_identity) + "\n").encode())

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setenv("HOMELAB_BACKUP_ALLOW_ISOLATED_RESTORE", "1")
    monkeypatch.setenv(
        "HOMELAB_BACKUP_ISOLATED_POSTGRESQL_RESTORE_DESTINATIONS",
        "postgres-restore.internal:5432/application_restore_a",
    )
    context = RestoreContext(
        job_id="freshness-restore-job",
        source_target_id="postgresql-source-target",
        destination_target_id="postgresql-destination-target",
        artifact_path=str(artifact_path),
        config=config,
        metadata={
            "source_database_identity": {
                "host": "postgres-source.internal",
                "port": 5432,
                "database": "application_production",
                "user": "backup_reader",
            }
        },
    )

    with pytest.raises(RuntimeError, match="sentinel"):
        await PostgreSQLPlugin(name="postgresql").restore(context)
    assert calls == [PSQL16]

    destination_identity["database_comment"] = "homelab-backup:postgresql-restore:v1"
    destination_identity["event_triggers"] = ["unsafe_trigger"]
    calls.clear()
    with pytest.raises(RuntimeError, match="fresh and empty"):
        await PostgreSQLPlugin(name="postgresql").restore(context)
    assert calls == [PSQL16]

    destination_identity["event_triggers"] = []
    destination_identity["system_namespace_user_objects"] = ["routine:pg_catalog.unsafe_restore()"]
    calls.clear()
    with pytest.raises(RuntimeError, match="fresh and empty"):
        await PostgreSQLPlugin(name="postgresql").restore(context)
    assert calls == [PSQL16]


@pytest.mark.asyncio
async def test_restore_uses_one_verified_descriptor_and_transactional_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Authorized restore must inspect, restore and verify one staged artifact inode."""
    artifact_bytes = b"PGDMP\x01\x0f exact staged PostgreSQL archive"
    artifact_path = tmp_path / "postgresql-staged.dump"
    artifact_path.write_bytes(artifact_bytes)
    source_identity = _safe_source_identity()
    toc = b"\n".join(
        (
            b"; PostgreSQL database dump",
            b"; Dumped from database version: 16.14 (Debian 16.14-1)",
            b"; Dumped by pg_dump version: 16.14 (Debian 16.14-1)",
            b"1; 0 0 EXTENSION - plpgsql ",
            b"2; 1259 16384 TABLE public items application_owner",
            b"3; 0 16384 TABLE DATA public items application_owner",
            b"4; 1259 16385 SEQUENCE public items_id_seq application_owner",
            b"",
        )
    )
    archive_catalog_sha256 = postgresql_core._archive_catalog_sha256(
        [line for line in toc.decode().splitlines() if line and not line.startswith(";")]
    )
    destination_config = {
        "mode": "restore_destination",
        "host": "postgres-restore.internal",
        "port": 5432,
        "database": "application_restore_a",
        "user": "restore_owner",
        "password": "synthetic-restore-password",
    }
    destination_base = {
        **source_identity,
        "database": "application_restore_a",
        "database_comment": "homelab-backup:postgresql-restore:v1",
        "database_owner": "restore_owner",
        "current_user": "restore_owner",
        "other_connections": 0,
        "role_superuser": False,
        "role_bypassrls": False,
        "role_createdb": False,
        "role_createrole": False,
        "role_replication": False,
        "database_create": True,
        "schema_create": ["public"],
    }
    fresh_identity = {
        **destination_base,
        "relations": [],
        "sequences": [],
        "large_objects": [],
    }
    restored_identity = {
        **destination_base,
        "writable_relations": ["public.items"],
    }
    probe_results = [fresh_identity, restored_identity]
    archive_descriptor_paths: list[str] = []
    password_files: list[Path] = []

    async def fake_exec(*args: str, **kwargs: object) -> DummyProcess:
        if args[0] == PSQL16:
            return DummyProcess(stdout=(json.dumps(probe_results.pop(0)) + "\n").encode())
        if args[0] == postgresql_core.SHA256SUM:
            return StreamOnlyProcess(
                stdout=f"{hashlib.sha256(artifact_bytes).hexdigest()}  artifact\n".encode()
            )  # type: ignore[return-value]
        assert args[0] == PG_RESTORE16
        archive_descriptor_paths.append(args[-1])
        assert args[-1].startswith("/proc/self/fd/")
        assert kwargs["pass_fds"]
        if "--list" in args:
            assert args == (PG_RESTORE16, "--list", args[-1])
            return StreamOnlyProcess(stdout=toc)  # type: ignore[return-value]
        assert args == (
            PG_RESTORE16,
            "-h",
            destination_config["host"],
            "-p",
            str(destination_config["port"]),
            "-U",
            destination_config["user"],
            "--dbname",
            destination_config["database"],
            "--exit-on-error",
            "--single-transaction",
            "--no-owner",
            "--no-privileges",
            args[-1],
        )
        assert "--clean" not in args
        assert "--create" not in args
        assert "--if-exists" not in args
        environment = kwargs["env"]
        assert isinstance(environment, dict)
        assert "PGPASSWORD" not in environment
        password_file = Path(environment["PGPASSFILE"])
        password_files.append(password_file)
        assert stat.S_IMODE(password_file.stat().st_mode) == 0o600
        return DummyProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setenv("HOMELAB_BACKUP_ALLOW_ISOLATED_RESTORE", "1")
    monkeypatch.setenv(
        "HOMELAB_BACKUP_ISOLATED_POSTGRESQL_RESTORE_DESTINATIONS",
        "postgres-restore.internal:5432/application_restore_a",
    )
    source_database_identity = {
        "host": "postgres-source.internal",
        "port": 5432,
        "database": "application_production",
        "user": "backup_reader",
    }
    context = RestoreContext(
        job_id="postgresql-restore-job",
        source_target_id="postgresql-source-target",
        destination_target_id="postgresql-destination-target",
        artifact_path=str(artifact_path),
        config=destination_config,
        metadata={
            "source_database_identity": source_database_identity,
            "artifact_bytes": len(artifact_bytes),
            "artifact_sha256": hashlib.sha256(artifact_bytes).hexdigest(),
            "staged_artifact_device": artifact_path.stat().st_dev,
            "staged_artifact_inode": artifact_path.stat().st_ino,
            "artifact_sidecar": {
                "postgresql_server_version": "16.14",
                "postgresql_server_version_num": 160014,
                "server_encoding": "UTF8",
                "lc_collate": "C.UTF-8",
                "lc_ctype": "C.UTF-8",
                "rls_table_count": 0,
                "catalog_counts": {
                    "schemas": 1,
                    "extensions": 1,
                    "relations": 1,
                    "sequences": 1,
                    "indexes": 0,
                    "constraints": 0,
                    "routines": 0,
                    "types": 0,
                    "large_objects": 0,
                },
                "source_identity_sha256": hashlib.sha256(
                    json.dumps(
                        source_database_identity, sort_keys=True, separators=(",", ":")
                    ).encode()
                ).hexdigest(),
                "source_catalog_sha256": postgresql_core._expected_archive_catalog_sha256(
                    source_identity
                ),
                "archive_catalog_sha256": archive_catalog_sha256,
                "toc_sha256": hashlib.sha256(toc).hexdigest(),
                "validation": "postgresql-custom-v1",
            },
        },
    )

    result = await PostgreSQLPlugin(name="postgresql").restore(context)

    assert result == {
        "status": "success",
        "artifact_path": str(artifact_path),
        "artifact_bytes": len(artifact_bytes),
    }
    assert archive_descriptor_paths[0] == archive_descriptor_paths[1]
    assert probe_results == []
    assert artifact_path.read_bytes() == artifact_bytes
    assert password_files and all(not path.exists() for path in password_files)


@pytest.mark.asyncio
async def test_restore_cumulative_timeout_reaps_pg_restore_and_preserves_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A timed-out transactional restore must stop its child and clean private auth."""

    class BlockingRestore:
        returncode: int | None = None

        def __init__(self) -> None:
            self.terminated = False
            self.reaped = False
            self.stopped = asyncio.Event()

        def terminate(self) -> None:
            self.terminated = True
            self.returncode = -15
            self.stopped.set()

        def kill(self) -> None:
            self.returncode = -9
            self.stopped.set()

        async def wait(self) -> int:
            await self.stopped.wait()
            self.reaped = True
            return self.returncode or -15

    artifact_bytes = b"PGDMP\x01\x0f timeout restore archive"
    artifact = tmp_path / "postgresql-timeout.dump"
    artifact.write_bytes(artifact_bytes)
    artifact.chmod(0o600)
    artifact_status = artifact.stat()
    source_identity = _safe_source_identity()
    destination_config = {
        "mode": "restore_destination",
        "host": "postgres-restore.internal",
        "port": 5432,
        "database": "application_restore_a",
        "user": "restore_owner",
        "password": "restore-timeout-secret-must-not-leak",
    }
    fresh_identity = {
        **source_identity,
        "database": destination_config["database"],
        "database_comment": "homelab-backup:postgresql-restore:v1",
        "database_owner": destination_config["user"],
        "current_user": destination_config["user"],
        "other_connections": 0,
        "relations": [],
        "sequences": [],
        "large_objects": [],
        "database_create": True,
        "schema_create": ["public"],
    }
    toc = b"\n".join(
        (
            b"; PostgreSQL database dump",
            b"; Dumped from database version: 16.14 (Debian 16.14-1)",
            b"; Dumped by pg_dump version: 16.14 (Debian 16.14-1)",
            b"1; 0 0 EXTENSION - plpgsql ",
            b"2; 1259 16384 TABLE public items application_owner",
            b"3; 0 16384 TABLE DATA public items application_owner",
            b"4; 1259 16385 SEQUENCE public items_id_seq application_owner",
            b"",
        )
    )
    archive_catalog_sha256 = postgresql_core._archive_catalog_sha256(
        [line for line in toc.decode().splitlines() if line and not line.startswith(";")]
    )
    blocking_restore = BlockingRestore()

    async def fake_exec(*args: str, **_kwargs: object) -> object:
        if args[0] == PSQL16:
            return StreamOnlyProcess(stdout=(json.dumps(fresh_identity) + "\n").encode())
        if args[0] == postgresql_core.SHA256SUM:
            return StreamOnlyProcess(
                stdout=f"{hashlib.sha256(artifact_bytes).hexdigest()}  artifact\n".encode()
            )
        if "--list" in args:
            assert args[:2] == (PG_RESTORE16, "--list")
            return StreamOnlyProcess(stdout=toc)
        assert args[0] == PG_RESTORE16
        return blocking_restore

    source_database_identity = {
        "host": "postgres-source.internal",
        "port": 5432,
        "database": "application_production",
        "user": "backup_reader",
    }
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr("app.plugins.postgresql.plugin.RESTORE_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setenv("HOMELAB_BACKUP_ALLOW_ISOLATED_RESTORE", "1")
    monkeypatch.setenv(
        "HOMELAB_BACKUP_ISOLATED_POSTGRESQL_RESTORE_DESTINATIONS",
        "postgres-restore.internal:5432/application_restore_a",
    )
    context = RestoreContext(
        job_id="postgresql-restore-timeout",
        source_target_id="postgresql-source",
        destination_target_id="postgresql-destination",
        config=destination_config,
        artifact_path=str(artifact),
        metadata={
            "source_database_identity": source_database_identity,
            "artifact_bytes": len(artifact_bytes),
            "artifact_sha256": hashlib.sha256(artifact_bytes).hexdigest(),
            "staged_artifact_device": artifact_status.st_dev,
            "staged_artifact_inode": artifact_status.st_ino,
            "artifact_sidecar": {
                "postgresql_server_version": "16.14",
                "postgresql_server_version_num": 160014,
                "server_encoding": "UTF8",
                "lc_collate": "C.UTF-8",
                "lc_ctype": "C.UTF-8",
                "rls_table_count": 0,
                "catalog_counts": {
                    "schemas": 1,
                    "extensions": 1,
                    "relations": 1,
                    "sequences": 1,
                    "indexes": 0,
                    "constraints": 0,
                    "routines": 0,
                    "types": 0,
                    "large_objects": 0,
                },
                "source_identity_sha256": hashlib.sha256(
                    json.dumps(
                        source_database_identity,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest(),
                "source_catalog_sha256": "3" * 64,
                "archive_catalog_sha256": archive_catalog_sha256,
                "toc_sha256": hashlib.sha256(toc).hexdigest(),
                "validation": "postgresql-custom-v1",
            },
        },
    )

    with (
        caplog.at_level(logging.INFO),
        pytest.raises(RuntimeError, match="PostgreSQL restore timed out"),
    ):
        await PostgreSQLPlugin(name="postgresql").restore(context)

    assert blocking_restore.terminated is True
    assert blocking_restore.reaped is True
    assert artifact.read_bytes() == artifact_bytes
    assert not list(tmp_path.glob("homelab-backup-postgresql-pgpass-*"))
    assert destination_config["password"] not in caplog.text


@pytest.mark.asyncio
async def test_restore_rejects_byte_identical_staged_inode_substitution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A verified staged pathname cannot be replaced, even with identical bytes."""
    payload = b"PGDMP\x01\x0f staged archive identity"
    artifact = tmp_path / "postgresql-staged.dump"
    artifact.write_bytes(payload)
    original_stat = artifact.stat()
    config = {
        "mode": "restore_destination",
        "host": "postgres-restore.internal",
        "port": 5432,
        "database": "application_restore_a",
        "user": "restore_owner",
        "password": "synthetic-restore-password",
    }
    fresh_identity = {
        **_safe_source_identity(),
        "database": "application_restore_a",
        "database_comment": "homelab-backup:postgresql-restore:v1",
        "database_owner": "restore_owner",
        "current_user": "restore_owner",
        "other_connections": 0,
        "relations": [],
        "sequences": [],
        "large_objects": [],
        "database_create": True,
        "schema_create": ["public"],
    }

    async def fake_exec(*args: str, **_kwargs: object) -> DummyProcess:
        if args[0] == PSQL16:
            return DummyProcess(stdout=(json.dumps(fresh_identity) + "\n").encode())
        raise AssertionError("substituted staged artifact reached pg_restore")

    real_open = os.open
    replaced = False

    def substitute_before_open(
        path: str | os.PathLike[str],
        flags: int,
        mode: int = 0o777,
    ) -> int:
        nonlocal replaced
        if not replaced and Path(path) == artifact and flags & os.O_ACCMODE == os.O_RDONLY:
            replacement = tmp_path / "replacement.dump"
            replacement.write_bytes(payload)
            replacement.chmod(0o600)
            os.replace(replacement, artifact)
            replaced = True
        return real_open(path, flags, mode)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr("app.core.plugins.postgresql.os.open", substitute_before_open)
    monkeypatch.setenv("HOMELAB_BACKUP_ALLOW_ISOLATED_RESTORE", "1")
    monkeypatch.setenv(
        "HOMELAB_BACKUP_ISOLATED_POSTGRESQL_RESTORE_DESTINATIONS",
        "postgres-restore.internal:5432/application_restore_a",
    )
    context = RestoreContext(
        job_id="staged-substitution-job",
        source_target_id="postgresql-source-target",
        destination_target_id="postgresql-destination-target",
        artifact_path=str(artifact),
        config=config,
        metadata={
            "source_database_identity": {
                "host": "postgres-source.internal",
                "port": 5432,
                "database": "application_production",
                "user": "backup_reader",
            },
            "artifact_bytes": len(payload),
            "artifact_sha256": hashlib.sha256(payload).hexdigest(),
            "staged_artifact_device": original_stat.st_dev,
            "staged_artifact_inode": original_stat.st_ino,
            "artifact_sidecar": {
                "postgresql_server_version": "16.14",
                "postgresql_server_version_num": 160014,
                "server_encoding": "UTF8",
                "lc_collate": "C.UTF-8",
                "lc_ctype": "C.UTF-8",
                "rls_table_count": 0,
                "catalog_counts": {
                    "schemas": 1,
                    "extensions": 1,
                    "relations": 1,
                    "sequences": 1,
                    "indexes": 0,
                    "constraints": 0,
                    "routines": 0,
                    "types": 0,
                    "large_objects": 0,
                },
                "source_identity_sha256": hashlib.sha256(
                    json.dumps(
                        {
                            "host": "postgres-source.internal",
                            "port": 5432,
                            "database": "application_production",
                            "user": "backup_reader",
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest(),
                "source_catalog_sha256": "3" * 64,
                "archive_catalog_sha256": "2" * 64,
                "toc_sha256": "3" * 64,
                "validation": "postgresql-custom-v1",
            },
        },
    )

    with pytest.raises(ValueError, match="staging identity"):
        await PostgreSQLPlugin(name="postgresql").restore(context)
    assert replaced is True


def test_restore_service_stages_postgresql_sidecar_provenance(
    db_session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RestoreService must pass validated PostgreSQL evidence with its staged copy."""
    source_config = {
        "mode": "source",
        "host": "postgres-source.internal",
        "port": 5432,
        "database": "application_production",
        "user": "backup_reader",
        "password": "source-password-must-not-leak",
    }
    destination_config = {
        "mode": "restore_destination",
        "host": "postgres-restore.internal",
        "port": 5432,
        "database": "application_restore_a",
        "user": "restore_owner",
        "password": "destination-password-must-not-leak",
    }
    source = Target(
        name="PostgreSQL source",
        slug="postgresql-source",
        plugin_name="postgresql",
        plugin_config_json=json.dumps(source_config),
    )
    destination = Target(
        name="PostgreSQL destination",
        slug="postgresql-destination",
        plugin_name="postgresql",
        plugin_config_json=json.dumps(destination_config),
    )
    db_session.add_all((source, destination))
    db_session.flush()
    artifact_dir = tmp_path / "backups" / source.slug / "2026-08-16"
    artifact_dir.mkdir(parents=True)
    artifact = artifact_dir / "postgresql.dump"
    payload = b"PGDMP\x01\x0f restore-service archive"
    artifact.write_bytes(payload)
    artifact.chmod(0o600)
    sidecar_evidence = {
        "postgresql_server_version": "16.14",
        "postgresql_server_version_num": 160014,
        "server_encoding": "UTF8",
        "lc_collate": "C.UTF-8",
        "lc_ctype": "C.UTF-8",
        "rls_table_count": 0,
        "catalog_counts": {
            "schemas": 1,
            "extensions": 1,
            "relations": 1,
            "sequences": 1,
            "indexes": 0,
            "constraints": 0,
            "routines": 0,
            "types": 0,
            "large_objects": 0,
        },
        "source_identity_sha256": "1" * 64,
        "source_catalog_sha256": "3" * 64,
        "archive_catalog_sha256": "2" * 64,
        "toc_sha256": "3" * 64,
        "validation": "postgresql-custom-v1",
    }
    plugin = PostgreSQLPlugin("postgresql")
    write_backup_sidecar(
        str(artifact),
        plugin,
        BackupContext(
            job_id="source-backup",
            target_id=str(source.id),
            config=source_config,
            metadata={"target_slug": source.slug},
        ),
        extra_metadata=sidecar_evidence,
    )
    now = datetime.now(timezone.utc)
    source_run = Run(
        status="success",
        operation="backup",
        started_at=now,
        finished_at=now,
    )
    db_session.add(source_run)
    db_session.flush()
    source_database_identity = {
        "host": source_config["host"],
        "port": source_config["port"],
        "database": source_config["database"],
        "user": source_config["user"],
    }
    source_target_run = TargetRun(
        run_id=source_run.id,
        target_id=source.id,
        status="success",
        operation="backup",
        artifact_path=str(artifact),
        artifact_bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        source_identity_json=json.dumps(source_database_identity),
        started_at=now,
        finished_at=now,
    )
    db_session.add(source_target_run)
    db_session.commit()
    observed: dict[str, object] = {}

    async def observe_restore(context: RestoreContext) -> dict[str, Any]:
        staged = Path(context.artifact_path)
        observed["staged_path"] = staged
        observed["staged_mode"] = stat.S_IMODE(staged.stat().st_mode)
        observed["metadata"] = dict(context.metadata or {})
        return {
            "status": "success",
            "artifact_path": str(staged),
            "artifact_bytes": staged.stat().st_size,
        }

    monkeypatch.setattr(plugin, "restore", observe_restore)
    monkeypatch.setattr("app.services.restores.get_plugin", lambda _name: plugin)
    monkeypatch.setenv("BACKUP_BASE_PATH", str(tmp_path / "backups"))

    restored = RestoreService(db_session).restore(
        source_target_run_id=int(source_target_run.id),
        destination_target_id=int(destination.id),
    )

    metadata = observed["metadata"]
    assert isinstance(metadata, dict)
    assert metadata["artifact_sidecar"] == {
        **sidecar_evidence,
        "plugin_name": "postgresql",
        "plugin_version": "0.2.1",
        "target_slug": source.slug,
        "artifact_bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }
    staged_path = observed["staged_path"]
    assert isinstance(staged_path, Path) and not staged_path.exists()
    assert observed["staged_mode"] == 0o600
    assert artifact.read_bytes() == payload
    assert restored.status == "success"
