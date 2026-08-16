import asyncio
import hashlib
import json
import os
import stat
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.plugins import postgresql as postgresql_core
from app.core.plugins.artifacts import PendingBackupArtifact
from app.core.plugins.base import BackupContext, RestoreContext
from app.core.plugins.postgresql import (
    PostgreSQLArchiveEvidence,
    PostgreSQLIdentity,
    PostgreSQLTarget,
)
from app.core.plugins.sidecar import read_backup_sidecar, write_backup_sidecar
from app.core.scheduler import _perform_target_run
from app.main import app
from app.models import Job, Run, Tag, Target, TargetRun
from app.plugins.calcom import CalcomPlugin
from app.plugins.calcom import plugin as calcom_module
from app.services.restores import RestoreService
from app.services.targets import TargetService


class DummyProcess:
    def __init__(
        self,
        returncode: int = 0,
        stdout: bytes = b"",
        stderr: bytes = b"",
        stdout_stream: "DummyStream | None" = None,
    ) -> None:
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self.stdout = stdout_stream or DummyStream(stdout)
        self.stderr = DummyStream(stderr)

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr

    async def wait(self) -> int:
        return self.returncode


class DummyStream:
    def __init__(self, *chunks: bytes) -> None:
        self.chunks = list(chunks)

    async def read(self, size: int) -> bytes:
        assert 0 < size <= 1024 * 1024
        return self.chunks.pop(0) if self.chunks else b""


_EXACT_TRIGGER_OBJECTS = [
    "trigger:public.assignment_reason_delete_trigger_for_routing_form",
    "trigger:public.assignment_reason_insert_trigger_for_routing_form",
    "trigger:public.assignment_reason_update_trigger_for_routing_form",
    "trigger:public.booking_delete_trigger_for_routing_form",
    "trigger:public.booking_denorm_booking_delete_trigger",
    "trigger:public.booking_denorm_booking_insert_update_trigger",
    "trigger:public.booking_denorm_event_type_length_update_trigger",
    "trigger:public.booking_denorm_event_type_parent_id_update_trigger",
    "trigger:public.booking_denorm_event_type_team_id_update_trigger",
    "trigger:public.booking_denorm_user_update_trigger",
    "trigger:public.booking_insert_trigger_for_routing_form",
    "trigger:public.booking_update_trigger_for_routing_form",
    "trigger:public.event_type_update_trigger_for_routing_form",
    "trigger:public.membership_role_change_trigger",
    "trigger:public.routing_form_delete_trigger",
    "trigger:public.routing_form_name_update_trigger",
    "trigger:public.routing_form_response_delete_trigger",
    "trigger:public.routing_form_response_denormalized_insert_trigger",
    "trigger:public.routing_form_response_insert_update_trigger",
    "trigger:public.routing_form_response_update_trigger",
    "trigger:public.routing_form_team_update_trigger",
    "trigger:public.routing_form_user_update_trigger",
    "trigger:public.tracking_delete_trigger_for_routing_form",
    "trigger:public.tracking_insert_trigger_for_routing_form",
    "trigger:public.tracking_update_trigger_for_routing_form",
    "trigger:public.trigger_nullify_routing_form_response_denormalized_event_type",
    "trigger:public.user_delete_trigger_for_routing_form",
    "trigger:public.user_update_trigger_for_routing_form",
]
_EXACT_MIGRATION_PROFILE = {
    "migration_count": 588,
    "migration_head": "20260219000000_add_fallback_action_to_queued_form_response",
    "migration_sha256": "4bab1776d3e03cdd18d6c36a8a57d5fb1243759f43717f0a3d7fa7f1561016f8",
    "unfinished_count": 0,
    "rolled_back_count": 0,
    "incomplete_step_count": 0,
}
_EXACT_CATALOG_SHA256 = "6f04bc45e021dac638c80dacca4384ebc43c7d5c0073e4a46595438733d1dc33"
_EXACT_SCHEMA_SHA256 = "f1112b98123f36ae502f39173e523545f7a41959c35351ef200a3f2b7fd66e52"
_EMPTY_MARKER_SHA256 = "01ba4719c80b6fe911b091a7c05124b64eeece964e09c058ef8f9805daca546b"
_EXACT_MARKER_PROFILE = {
    category: {"count": 0, "sha256": _EMPTY_MARKER_SHA256}
    for category in (
        "api_keys",
        "attendees",
        "bookings",
        "credentials",
        "destination_calendars",
        "event_types",
        "schedules",
        "selected_calendars",
        "users",
        "webhooks",
        "workflow_steps",
        "workflows",
    )
}


async def _async_value(value: str) -> str:
    return value


def _valid_calcom_restore_sidecar() -> dict[str, object]:
    marker_counts = {
        category: evidence["count"] for category, evidence in _EXACT_MARKER_PROFILE.items()
    }
    return {
        "application_version": "6.2.0",
        "migration_head": _EXACT_MIGRATION_PROFILE["migration_head"],
        "migration_sha256": _EXACT_MIGRATION_PROFILE["migration_sha256"],
        "schema_sha256": _EXACT_SCHEMA_SHA256,
        "marker_profile_sha256": hashlib.sha256(
            json.dumps(
                _EXACT_MARKER_PROFILE,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest(),
        "marker_counts": marker_counts,
        "postgresql_server_version": "16.14",
        "postgresql_server_version_num": 160014,
        "server_encoding": "UTF8",
        "lc_collate": "en_US.utf8",
        "lc_ctype": "en_US.utf8",
        "rls_table_count": 0,
        "source_identity_sha256": "1" * 64,
        "source_catalog_sha256": "2" * 64,
        "archive_catalog_sha256": "3" * 64,
        "toc_sha256": "4" * 64,
        "catalog_counts": {
            "schemas": 1,
            "extensions": 1,
            "relations": 125,
            "sequences": 57,
            "indexes": 349,
            "constraints": 346,
            "routines": 30,
            "types": 54,
            "large_objects": 0,
        },
        "validation": "calcom-postgresql-v1",
    }


def _safe_calcom_identity() -> dict[str, object]:
    return {
        "server_version_num": 160014,
        "server_version": "16.14",
        "database": "calendso",
        "server_encoding": "UTF8",
        "lc_collate": "en_US.utf8",
        "lc_ctype": "en_US.utf8",
        "schemas": ["public"],
        "extensions": [{"name": "plpgsql", "schema": "pg_catalog", "version": "1.0"}],
        "relations": [{"schema": "public", "name": "users", "kind": "r"}],
        "sequences": [{"schema": "public", "name": "users_id_seq"}],
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
        "unsupported_database_objects": list(_EXACT_TRIGGER_OBJECTS),
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


def test_calcom_discovery_exposes_the_strict_partial_postgresql_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The public API must expose the clean-breaking Cal.com mode schema."""

    @asynccontextmanager
    async def route_only_lifespan(_app):  # type: ignore[no-untyped-def]
        yield

    monkeypatch.setattr(app.router, "lifespan_context", route_only_lifespan)
    with TestClient(app, backend_options={"use_uvloop": True}) as client:
        plugins_response = client.get("/api/v1/plugins/")
        schema_response = client.get("/api/v1/plugins/calcom/schema")

    assert plugins_response.status_code == 200
    assert next(item for item in plugins_response.json() if item["key"] == "calcom") == {
        "key": "calcom",
        "name": "calcom",
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


def test_calcom_public_test_api_and_target_persistence_use_the_exact_contract(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HTTP connectivity and persisted targets must share the strict public schema."""
    secret = "public-calcom-secret-must-not-leak"
    config = {
        "mode": "source",
        "host": "calcom-postgres.internal",
        "port": 5432,
        "database": "calendso",
        "user": "calcom_backup",
        "password": secret,
    }
    responses = [
        _safe_calcom_identity(),
        _EXACT_MIGRATION_PROFILE,
        _EXACT_MARKER_PROFILE,
    ]

    @asynccontextmanager
    async def route_only_lifespan(_app):  # type: ignore[no-untyped-def]
        yield

    async def fake_exec(*_args: str, **_kwargs: object) -> DummyProcess:
        return DummyProcess(stdout=(json.dumps(responses.pop(0)) + "\n").encode())

    monkeypatch.setattr(app.router, "lifespan_context", route_only_lifespan)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(
        "app.plugins.calcom.plugin.postgresql_catalog_sha256",
        lambda _identity: _EXACT_CATALOG_SHA256,
    )
    with TestClient(app, backend_options={"use_uvloop": True}) as client:
        success = client.post("/api/v1/plugins/calcom/test", json=config)
        failure = client.post(
            "/api/v1/plugins/calcom/test",
            json={**config, "database_url": "postgresql://legacy.invalid/calendso"},
        )

    assert success.json() == {"ok": True}
    assert failure.json() == {
        "ok": False,
        "error": "Invalid Cal.com source or restore-destination configuration",
    }
    assert secret not in success.text + failure.text
    assert responses == []

    service = TargetService(db_session)
    target = service.create(
        name="Cal.com source",
        plugin_name="calcom",
        plugin_config_json=json.dumps(config),
    )
    assert json.loads(target.plugin_config_json or "{}") == config
    with pytest.raises(ValueError, match="Invalid plugin_config_json"):
        service.create(
            name="Cal.com legacy source",
            plugin_name="calcom",
            plugin_config_json=json.dumps({"database_url": "postgresql://legacy.invalid/calendso"}),
        )


def test_calcom_public_test_accepts_only_a_fresh_restore_destination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The HTTP test seam must validate the empty Cal.com restore sentinel state."""
    config = {
        "mode": "restore_destination",
        "host": "calcom-restore.internal",
        "port": 5432,
        "database": "hlb_calcom_restore_a",
        "user": "restore_owner",
        "password": "public-restore-secret-must-not-leak",
    }
    observed: list[tuple[PostgreSQLTarget, dict[str, object]]] = []

    @asynccontextmanager
    async def route_only_lifespan(_app):  # type: ignore[no-untyped-def]
        yield

    async def fake_probe(
        target: PostgreSQLTarget,
        **kwargs: object,
    ) -> PostgreSQLIdentity:
        observed.append((target, kwargs))
        payload = _safe_calcom_identity()
        return PostgreSQLIdentity(
            server_version_num=160014,
            server_version="16.14",
            database="hlb_calcom_restore_a",
            server_encoding="UTF8",
            lc_collate="en_US.utf8",
            lc_ctype="en_US.utf8",
            catalog=payload,
        )

    async def forbidden_profiles(
        _target: PostgreSQLTarget,
    ) -> tuple[Mapping[str, object], Mapping[str, object]]:
        raise AssertionError("fresh restore test must not query populated Cal.com tables")

    monkeypatch.setattr(app.router, "lifespan_context", route_only_lifespan)
    monkeypatch.setattr("app.plugins.calcom.plugin.probe_postgresql", fake_probe)
    monkeypatch.setattr("app.plugins.calcom.plugin._read_calcom_profiles", forbidden_profiles)

    with TestClient(app, backend_options={"use_uvloop": True}) as client:
        response = client.post("/api/v1/plugins/calcom/test", json=config)

    assert response.json() == {"ok": True}
    assert str(config["password"]) not in response.text
    assert len(observed) == 1
    target, kwargs = observed[0]
    assert target.mode == "restore_destination"
    assert kwargs == {
        "expected_state": "fresh_destination",
        "restore_sentinel": "homelab-backup:calcom-restore:v1",
    }


@pytest.mark.asyncio
async def test_calcom_configuration_is_strict_mode_explicit_and_url_free() -> None:
    """Only the exact flat source and restore-destination shapes are valid."""
    plugin = CalcomPlugin(name="calcom")
    source = {
        "mode": "source",
        "host": "calcom-postgres.internal",
        "port": 5432,
        "database": "calendso",
        "user": "calcom_backup",
        "password": "synthetic-password",
    }
    destination = {
        **source,
        "mode": "restore_destination",
        "host": "calcom-restore.internal",
        "database": "hlb_calcom_restore_a",
        "user": "restore_owner",
    }

    assert await plugin.validate_config(source) is True
    assert await plugin.validate_config(destination) is True

    invalid_configs: tuple[object, ...] = (
        None,
        {},
        {key: value for key, value in source.items() if key != "mode"},
        {**source, "mode": "legacy"},
        {**source, "host": "postgresql://calcom_backup@postgres/calendso"},
        {**source, "host": "postgres\ninvalid"},
        {**source, "port": True},
        {**source, "port": "5432"},
        {**source, "port": 0},
        {**source, "database": "unsafe/name"},
        {**source, "user": "backup reader"},
        {**source, "password": ""},
        {**source, "password": "synthetic\x80password"},
        {**source, "database_url": "postgresql://legacy.invalid/calendso"},
        {**source, "database_direct_url": "postgresql://legacy.invalid/calendso"},
        {**source, "unexpected": "compatibility-fallback"},
    )
    for config in invalid_configs:
        assert await plugin.validate_config(config) is False  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_calcom_test_uses_private_pg16_auth_and_exact_trigger_inventory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Connectivity admits only the exact ordinary triggers from Cal.com 6.2.0."""
    config = {
        "mode": "source",
        "host": "calcom-postgres.internal",
        "port": 5432,
        "database": "calendso",
        "user": "calcom_backup",
        "password": "synthetic-password",
    }
    observed_password_files: list[Path] = []
    observed_queries: list[str] = []
    calls = 0
    monkeypatch.setenv("PGPASSWORD", "ambient-password-must-not-propagate")
    monkeypatch.setenv("PGSERVICE", "ambient-service-must-not-propagate")

    async def fake_exec(*args: str, **kwargs: object) -> DummyProcess:
        nonlocal calls
        calls += 1
        assert args[0] == postgresql_core.PSQL16
        assert config["host"] in args
        assert config["database"] in args
        environment = kwargs["env"]
        assert isinstance(environment, dict)
        assert "PGPASSWORD" not in environment
        assert "PGSERVICE" not in environment
        password_file = Path(environment["PGPASSFILE"])
        observed_password_files.append(password_file)
        assert stat.S_IMODE(password_file.stat().st_mode) == 0o600
        query = args[args.index("-c") + 1]
        observed_queries.append(query)
        responses = (
            _safe_calcom_identity(),
            _EXACT_MIGRATION_PROFILE,
            _EXACT_MARKER_PROFILE,
        )
        response = responses[calls - 1]
        return DummyProcess(stdout=(json.dumps(response) + "\n").encode())

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(
        "app.plugins.calcom.plugin.postgresql_catalog_sha256",
        lambda _identity: _EXACT_CATALOG_SHA256,
    )

    assert await CalcomPlugin(name="calcom").test(config) is True
    assert calls == 3
    assert "_prisma_migrations" in observed_queries[1]
    assert all(
        table in observed_queries[2]
        for table in ("public.users", '"Booking"', '"Credential"', '"Webhook"', '"ApiKey"')
    )
    assert observed_password_files and all(not path.exists() for path in observed_password_files)


@pytest.mark.asyncio
async def test_calcom_test_rejects_migration_inventory_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid PG16 database is not Cal.com 6.2.0 without the exact migration set."""
    config = {
        "mode": "source",
        "host": "calcom-postgres.internal",
        "port": 5432,
        "database": "calendso",
        "user": "calcom_backup",
        "password": "synthetic-password",
    }
    calls = 0

    async def fake_exec(*_args: str, **_kwargs: object) -> DummyProcess:
        nonlocal calls
        calls += 1
        if calls == 1:
            return DummyProcess(stdout=(json.dumps(_safe_calcom_identity()) + "\n").encode())
        drifted = {**_EXACT_MIGRATION_PROFILE, "migration_count": 587}
        return DummyProcess(stdout=(json.dumps(drifted) + "\n").encode())

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(
        "app.plugins.calcom.plugin.postgresql_catalog_sha256",
        lambda _identity: _EXACT_CATALOG_SHA256,
    )

    with pytest.raises(RuntimeError, match="migration"):
        await CalcomPlugin(name="calcom").test(config)
    assert calls == 2


@pytest.mark.asyncio
async def test_calcom_test_rejects_catalog_inventory_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A migration-compatible database still needs the exact v6.2.0 object catalog."""
    config = {
        "mode": "source",
        "host": "calcom-postgres.internal",
        "port": 5432,
        "database": "calendso",
        "user": "calcom_backup",
        "password": "synthetic-password",
    }

    async def fake_exec(*_args: str, **_kwargs: object) -> DummyProcess:
        return DummyProcess(stdout=(json.dumps(_safe_calcom_identity()) + "\n").encode())

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    with pytest.raises(RuntimeError, match="catalog"):
        await CalcomPlugin(name="calcom").test(config)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("role_superuser", True, "excessive privileges"),
        ("rls_tables", ["public.User"], "unsupported RLS"),
        ("unreadable_relations", ["public.Booking"], "cannot read every relation"),
        (
            "unsupported_database_objects",
            _EXACT_TRIGGER_OBJECTS[:-1],
            "unsupported database objects",
        ),
    ),
)
async def test_calcom_test_rejects_incomplete_or_overprivileged_backup_identity(
    field: str,
    value: object,
    message: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cal.com inherits the complete generic PostgreSQL least-authority fence."""
    config = {
        "mode": "source",
        "host": "calcom-postgres.internal",
        "port": 5432,
        "database": "calendso",
        "user": "calcom_backup",
        "password": "least-privilege-secret-must-not-escape",
    }
    identity = {**_safe_calcom_identity(), field: value}

    async def fake_exec(*_args: str, **_kwargs: object) -> DummyProcess:
        return DummyProcess(stdout=(json.dumps(identity) + "\n").encode())

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    with pytest.raises(RuntimeError, match=message) as raised:
        await CalcomPlugin(name="calcom").test(config)
    assert str(config["password"]) not in str(raised.value)


@pytest.mark.asyncio
async def test_calcom_backup_publishes_stable_exact_profile_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The public backup seam must reuse PG16 capture and bind Cal.com evidence."""
    config = {
        "mode": "source",
        "host": "calcom-postgres.internal",
        "port": 5432,
        "database": "calendso",
        "user": "calcom_backup",
        "password": "synthetic-password",
    }
    raw_identity = _safe_calcom_identity()
    identity = PostgreSQLIdentity(
        server_version_num=160014,
        server_version="16.14",
        database="calendso",
        server_encoding="UTF8",
        lc_collate="en_US.utf8",
        lc_ctype="en_US.utf8",
        catalog=raw_identity,
    )
    query_results: list[dict[str, object]] = [
        dict(_EXACT_MIGRATION_PROFILE),
        dict(_EXACT_MARKER_PROFILE),
        dict(_EXACT_MIGRATION_PROFILE),
        dict(_EXACT_MARKER_PROFILE),
    ]
    observed_allowed_objects: list[frozenset[str]] = []

    async def fake_probe(
        _target: object,
        *,
        allowed_unsupported_database_objects: frozenset[str] = frozenset(),
        **_kwargs: object,
    ) -> PostgreSQLIdentity:
        observed_allowed_objects.append(allowed_unsupported_database_objects)
        return identity

    async def fake_query(*_args: object, **_kwargs: object) -> dict[str, object]:
        return query_results.pop(0)

    async def fake_write(
        _target: object,
        _identity: object,
        artifact: PendingBackupArtifact,
        **kwargs: object,
    ) -> PostgreSQLArchiveEvidence:
        assert kwargs["allowed_unsupported_database_objects"] == frozenset(_EXACT_TRIGGER_OBJECTS)
        payload = b"PGDMP synthetic Cal.com v6.2.0 archive"
        descriptor = os.open(artifact.temporary_path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        os.write(descriptor, payload)
        artifact.publication_fd = descriptor
        artifact.publication_sha256 = hashlib.sha256(payload).hexdigest()
        return PostgreSQLArchiveEvidence(
            source_identity_sha256="1" * 64,
            source_catalog_sha256="2" * 64,
            archive_catalog_sha256="3" * 64,
            toc_sha256="4" * 64,
            catalog_counts={
                "schemas": 1,
                "extensions": 1,
                "relations": 125,
                "sequences": 57,
                "indexes": 349,
                "constraints": 346,
                "routines": 30,
                "types": 54,
                "large_objects": 0,
            },
        )

    async def defer_publication(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr("app.plugins.calcom.plugin.probe_postgresql", fake_probe)
    monkeypatch.setattr(
        "app.plugins.calcom.plugin.postgresql_catalog_sha256",
        lambda _identity: _EXACT_CATALOG_SHA256,
    )
    monkeypatch.setattr("app.plugins.calcom.plugin.query_postgresql_json", fake_query)
    monkeypatch.setattr("app.plugins.calcom.plugin.write_postgresql_archive", fake_write)
    monkeypatch.setattr(
        "app.plugins.calcom.plugin.postgresql_archive_schema_sha256",
        lambda _descriptor: _async_value(_EXACT_SCHEMA_SHA256),
    )
    monkeypatch.setattr("app.plugins.calcom.plugin.publish_postgresql_artifact", defer_publication)

    result = await CalcomPlugin(name="calcom", base_dir=str(tmp_path)).backup(
        BackupContext(
            job_id="calcom-job",
            target_id="calcom-source",
            config=config,
            metadata={"target_slug": "calcom-source"},
        )
    )

    artifact = Path(result["artifact_path"])
    sidecar = read_backup_sidecar(str(artifact))
    assert artifact.is_file() and stat.S_IMODE(artifact.stat().st_mode) == 0o600
    assert sidecar is not None
    assert sidecar["plugin_name"] == "calcom"
    assert sidecar["validation"] == "calcom-postgresql-v1"
    assert sidecar["application_version"] == "6.2.0"
    assert sidecar["migration_head"] == _EXACT_MIGRATION_PROFILE["migration_head"]
    assert sidecar["migration_sha256"] == _EXACT_MIGRATION_PROFILE["migration_sha256"]
    assert sidecar["schema_sha256"] == _EXACT_SCHEMA_SHA256
    assert (
        sidecar["marker_profile_sha256"]
        == hashlib.sha256(
            json.dumps(
                _EXACT_MARKER_PROFILE,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
    )
    assert "password" not in json.dumps(sidecar)
    assert query_results == []
    assert observed_allowed_objects == [frozenset(_EXACT_TRIGGER_OBJECTS)]


@pytest.mark.asyncio
async def test_calcom_backup_retries_one_profile_drift_without_publishing_first_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One changing application fence retries the complete dump exactly once."""
    config = {
        "mode": "source",
        "host": "calcom-postgres.internal",
        "port": 5432,
        "database": "calendso",
        "user": "calcom_backup",
        "password": "synthetic-password",
    }
    identity = PostgreSQLIdentity(
        server_version_num=160014,
        server_version="16.14",
        database="calendso",
        server_encoding="UTF8",
        lc_collate="en_US.utf8",
        lc_ctype="en_US.utf8",
        catalog=_safe_calcom_identity(),
    )
    phase_b = json.loads(json.dumps(_EXACT_MARKER_PROFILE))
    phase_b["users"] = {"count": 1, "sha256": "a" * 64}
    query_results: list[dict[str, object]] = [
        dict(_EXACT_MIGRATION_PROFILE),
        dict(_EXACT_MARKER_PROFILE),
        dict(_EXACT_MIGRATION_PROFILE),
        phase_b,
        dict(_EXACT_MIGRATION_PROFILE),
        phase_b,
        dict(_EXACT_MIGRATION_PROFILE),
        phase_b,
    ]
    writes = 0

    async def fake_probe(*_args: object, **_kwargs: object) -> PostgreSQLIdentity:
        return identity

    async def fake_query(*_args: object, **_kwargs: object) -> dict[str, object]:
        return query_results.pop(0)

    async def fake_write(
        _target: object,
        _identity: object,
        artifact: PendingBackupArtifact,
        **_kwargs: object,
    ) -> PostgreSQLArchiveEvidence:
        nonlocal writes
        writes += 1
        payload = f"PGDMP Cal.com attempt {writes}".encode()
        descriptor = os.open(artifact.temporary_path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        os.write(descriptor, payload)
        artifact.publication_fd = descriptor
        artifact.publication_sha256 = hashlib.sha256(payload).hexdigest()
        return PostgreSQLArchiveEvidence(
            source_identity_sha256="1" * 64,
            source_catalog_sha256="2" * 64,
            archive_catalog_sha256="3" * 64,
            toc_sha256="4" * 64,
            catalog_counts={
                "schemas": 1,
                "extensions": 1,
                "relations": 125,
                "sequences": 57,
                "indexes": 349,
                "constraints": 346,
                "routines": 30,
                "types": 54,
                "large_objects": 0,
            },
        )

    async def defer_publication(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr("app.plugins.calcom.plugin.probe_postgresql", fake_probe)
    monkeypatch.setattr("app.plugins.calcom.plugin.query_postgresql_json", fake_query)
    monkeypatch.setattr("app.plugins.calcom.plugin.write_postgresql_archive", fake_write)
    monkeypatch.setattr(
        "app.plugins.calcom.plugin.postgresql_archive_schema_sha256",
        lambda _descriptor: _async_value(_EXACT_SCHEMA_SHA256),
    )
    monkeypatch.setattr(
        "app.plugins.calcom.plugin.postgresql_catalog_sha256",
        lambda _identity: _EXACT_CATALOG_SHA256,
    )
    monkeypatch.setattr("app.plugins.calcom.plugin.publish_postgresql_artifact", defer_publication)

    result = await CalcomPlugin(name="calcom", base_dir=str(tmp_path)).backup(
        BackupContext(
            job_id="retry",
            target_id="calcom-source",
            config=config,
            metadata={"target_slug": "calcom-source"},
        )
    )

    artifact = Path(result["artifact_path"])
    assert writes == 2
    assert artifact.read_bytes() == b"PGDMP Cal.com attempt 2"
    assert query_results == []
    assert len(list(tmp_path.rglob("*.dump"))) == 1
    assert not list(tmp_path.rglob("*.tmp"))


@pytest.mark.asyncio
async def test_calcom_backup_bounds_repeated_drift_timeout_and_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Every public exit is bounded and leaves no Cal.com artifact residue."""
    config = {
        "mode": "source",
        "host": "calcom-postgres.internal",
        "port": 5432,
        "database": "calendso",
        "user": "calcom_backup",
        "password": "lifecycle-secret-must-not-escape",
    }
    context = BackupContext(
        job_id="lifecycle",
        target_id="calcom-source",
        config=config,
        metadata={"target_slug": "calcom-source"},
    )
    plugin = CalcomPlugin(name="calcom", base_dir=str(tmp_path))
    attempts = 0

    async def always_drifts(*_args: object, **_kwargs: object) -> str:
        nonlocal attempts
        attempts += 1
        raise calcom_module._CalcomProfileDrift("Cal.com source profile changed during capture")

    monkeypatch.setattr(plugin, "_capture_backup_attempt", always_drifts)
    with pytest.raises(RuntimeError, match="did not stabilize"):
        await plugin.backup(context)
    assert attempts == 2

    started = asyncio.Event()
    never_release = asyncio.Event()

    async def blocks(*_args: object, **_kwargs: object) -> str:
        started.set()
        await never_release.wait()
        raise AssertionError("unreachable")

    monkeypatch.setattr(plugin, "_capture_backup_attempt", blocks)
    monkeypatch.setattr("app.plugins.calcom.plugin.BACKUP_TIMEOUT_SECONDS", 0.01)
    with pytest.raises(RuntimeError, match="timed out"):
        await plugin.backup(context)

    monkeypatch.setattr("app.plugins.calcom.plugin.BACKUP_TIMEOUT_SECONDS", 60.0)
    started.clear()
    task = asyncio.create_task(plugin.backup(context))
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not list(tmp_path.rglob("*.dump"))
    assert not list(tmp_path.rglob("*.tmp"))
    assert str(config["password"]) not in caplog.text


def test_calcom_scheduled_backup_records_private_artifact_and_source_provenance(
    db_session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A scheduled Cal.com run persists its artifact without its database password."""
    config = {
        "mode": "source",
        "host": "calcom-postgres.internal",
        "port": 5432,
        "database": "calendso",
        "user": "calcom_backup",
        "password": "scheduled-secret-must-not-escape",
    }
    identity = PostgreSQLIdentity(
        server_version_num=160014,
        server_version="16.14",
        database="calendso",
        server_encoding="UTF8",
        lc_collate="en_US.utf8",
        lc_ctype="en_US.utf8",
        catalog=_safe_calcom_identity(),
    )
    query_results: list[dict[str, object]] = [
        dict(_EXACT_MIGRATION_PROFILE),
        dict(_EXACT_MARKER_PROFILE),
        dict(_EXACT_MIGRATION_PROFILE),
        dict(_EXACT_MARKER_PROFILE),
    ]

    async def fake_probe(*_args: object, **_kwargs: object) -> PostgreSQLIdentity:
        return identity

    async def fake_query(*_args: object, **_kwargs: object) -> dict[str, object]:
        return query_results.pop(0)

    async def fake_write(
        _target: object,
        _identity: object,
        artifact: PendingBackupArtifact,
        **_kwargs: object,
    ) -> PostgreSQLArchiveEvidence:
        payload = b"PGDMP scheduled Cal.com v6.2.0 archive"
        descriptor = os.open(artifact.temporary_path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        os.write(descriptor, payload)
        artifact.publication_fd = descriptor
        artifact.publication_sha256 = hashlib.sha256(payload).hexdigest()
        return PostgreSQLArchiveEvidence(
            source_identity_sha256="1" * 64,
            source_catalog_sha256="2" * 64,
            archive_catalog_sha256="3" * 64,
            toc_sha256="4" * 64,
            catalog_counts={
                "schemas": 1,
                "extensions": 1,
                "relations": 125,
                "sequences": 57,
                "indexes": 349,
                "constraints": 346,
                "routines": 30,
                "types": 54,
                "large_objects": 0,
            },
        )

    async def defer_publication(*_args: object, **_kwargs: object) -> None:
        return None

    plugin = CalcomPlugin(name="calcom", base_dir=str(tmp_path / "backups"))
    monkeypatch.setattr("app.plugins.calcom.plugin.probe_postgresql", fake_probe)
    monkeypatch.setattr("app.plugins.calcom.plugin.query_postgresql_json", fake_query)
    monkeypatch.setattr("app.plugins.calcom.plugin.write_postgresql_archive", fake_write)
    monkeypatch.setattr(
        "app.plugins.calcom.plugin.postgresql_archive_schema_sha256",
        lambda _descriptor: _async_value(_EXACT_SCHEMA_SHA256),
    )
    monkeypatch.setattr("app.plugins.calcom.plugin.publish_postgresql_artifact", defer_publication)
    monkeypatch.setattr(
        "app.plugins.calcom.plugin.postgresql_catalog_sha256",
        lambda _identity: _EXACT_CATALOG_SHA256,
    )
    monkeypatch.setattr("app.core.scheduler.get_plugin", lambda _name: plugin)

    tag = Tag(display_name="Cal.com scheduled recovery")
    db_session.add(tag)
    db_session.flush()
    job = Job(
        tag_id=tag.id,
        name="Cal.com backup",
        schedule_cron="* * * * *",
        enabled=True,
    )
    target = Target(
        name="Cal.com source",
        slug="calcom-source",
        plugin_name="calcom",
        plugin_config_json=json.dumps(config),
    )
    run = Run(status="running", operation="backup")
    db_session.add_all((job, target, run))
    db_session.commit()

    result = _perform_target_run(db_session, job, run, target_id=int(target.id))

    assert result["status"] == "success"
    target_run = db_session.query(TargetRun).one()
    assert target_run.status == "success"
    assert target_run.artifact_path is not None
    assert Path(target_run.artifact_path).is_file()
    assert target_run.artifact_bytes and target_run.artifact_bytes > 0
    assert target_run.sha256 and len(target_run.sha256) == 64
    assert json.loads(target_run.source_identity_json or "{}") == {
        "database": "calendso",
        "host": "calcom-postgres.internal",
        "port": 5432,
        "user": "calcom_backup",
    }
    audit = "\n".join(
        filter(None, (target_run.message, target_run.logs_text, target_run.source_identity_json))
    )
    assert str(config["password"]) not in audit


@pytest.mark.asyncio
async def test_calcom_get_status_is_observed_and_secret_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Status must reflect the real adapter probe instead of manufacturing health."""
    config = {
        "mode": "source",
        "host": "calcom-postgres.internal",
        "port": 5432,
        "database": "calendso",
        "user": "calcom_backup",
        "password": "status-secret-must-not-escape",
    }
    plugin = CalcomPlugin(name="calcom")

    async def succeeds(_config: object) -> bool:
        return True

    monkeypatch.setattr(plugin, "test", succeeds)
    assert await plugin.get_status(
        BackupContext(job_id="status", target_id="source", config=config)
    ) == {"status": "ok", "application_version": "6.2.0"}

    async def fails(_config: object) -> bool:
        raise ConnectionError("Cal.com PostgreSQL probe failed")

    monkeypatch.setattr(plugin, "test", fails)
    result = await plugin.get_status(
        BackupContext(job_id="status", target_id="source", config=config)
    )
    assert result == {"status": "error", "error": "Cal.com PostgreSQL probe failed"}
    assert str(config["password"]) not in json.dumps(result)


@pytest.mark.asyncio
async def test_calcom_restore_destination_status_is_fresh_and_not_source_versioned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fresh-destination status must report observed state without claiming app identity."""
    config = {
        "mode": "restore_destination",
        "host": "calcom-restore.internal",
        "port": 5432,
        "database": "hlb_calcom_restore_a",
        "user": "restore_owner",
        "password": "status-restore-secret-must-not-escape",
    }
    plugin = CalcomPlugin(name="calcom")

    async def succeeds(_config: object) -> bool:
        return True

    monkeypatch.setattr(plugin, "test", succeeds)
    result = await plugin.get_status(
        BackupContext(job_id="status", target_id="destination", config=config)
    )

    assert result == {"status": "ok", "database_state": "fresh_destination"}
    assert str(config["password"]) not in json.dumps(result)


@pytest.mark.asyncio
async def test_calcom_restore_is_isolated_descriptor_bound_and_partial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The adapter restores only a verified staged archive into a fresh local DB."""
    artifact = tmp_path / "calcom-staged.dump"
    artifact.write_bytes(b"PGDMP verified Cal.com archive")
    status = artifact.stat()
    artifact_sha256 = hashlib.sha256(artifact.read_bytes()).hexdigest()
    source_identity = {
        "host": "calcom-source.internal",
        "port": 5432,
        "database": "calendso",
        "user": "calcom_backup",
    }
    config = {
        "mode": "restore_destination",
        "host": "calcom-restore.internal",
        "port": 5432,
        "database": "hlb_calcom_restore_a",
        "user": "restore_owner",
        "password": "synthetic-restore-password",
    }
    catalog_counts = {
        "schemas": 1,
        "extensions": 1,
        "relations": 125,
        "sequences": 57,
        "indexes": 349,
        "constraints": 346,
        "routines": 30,
        "types": 54,
        "large_objects": 0,
    }
    marker_counts = {
        category: evidence["count"] for category, evidence in _EXACT_MARKER_PROFILE.items()
    }
    context = RestoreContext(
        job_id="restore-job",
        source_target_id="source-target",
        destination_target_id="destination-target",
        config=config,
        artifact_path=str(artifact),
        metadata={
            "source_database_identity": source_identity,
            "artifact_bytes": status.st_size,
            "artifact_sha256": artifact_sha256,
            "staged_artifact_device": status.st_dev,
            "staged_artifact_inode": status.st_ino,
            "artifact_sidecar": {
                "application_version": "6.2.0",
                "migration_head": _EXACT_MIGRATION_PROFILE["migration_head"],
                "migration_sha256": _EXACT_MIGRATION_PROFILE["migration_sha256"],
                "schema_sha256": _EXACT_SCHEMA_SHA256,
                "marker_profile_sha256": hashlib.sha256(
                    json.dumps(
                        _EXACT_MARKER_PROFILE,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest(),
                "marker_counts": marker_counts,
                "postgresql_server_version": "16.14",
                "postgresql_server_version_num": 160014,
                "server_encoding": "UTF8",
                "lc_collate": "en_US.utf8",
                "lc_ctype": "en_US.utf8",
                "rls_table_count": 0,
                "source_identity_sha256": "1" * 64,
                "source_catalog_sha256": "2" * 64,
                "archive_catalog_sha256": "3" * 64,
                "toc_sha256": "4" * 64,
                "catalog_counts": catalog_counts,
                "validation": "calcom-postgresql-v1",
            },
        },
    )
    restored_identity = PostgreSQLIdentity(
        server_version_num=160014,
        server_version="16.14",
        database=str(config["database"]),
        server_encoding="UTF8",
        lc_collate="en_US.utf8",
        lc_ctype="en_US.utf8",
        catalog=_safe_calcom_identity(),
    )
    observed_restore_kwargs: dict[str, object] = {}

    async def fake_probe(*_args: object, **_kwargs: object) -> PostgreSQLIdentity:
        return restored_identity

    async def fake_restore(*_args: object, **kwargs: object) -> PostgreSQLIdentity:
        observed_restore_kwargs.update(kwargs)
        return restored_identity

    query_results: list[dict[str, object]] = [
        dict(_EXACT_MIGRATION_PROFILE),
        dict(_EXACT_MARKER_PROFILE),
    ]

    async def fake_query(*_args: object, **_kwargs: object) -> dict[str, object]:
        return query_results.pop(0)

    monkeypatch.setenv("HOMELAB_BACKUP_ALLOW_ISOLATED_RESTORE", "1")
    monkeypatch.setenv(
        "HOMELAB_BACKUP_ISOLATED_CALCOM_RESTORE_DESTINATIONS",
        "calcom-restore.internal:5432/hlb_calcom_restore_a",
    )
    monkeypatch.setattr("app.plugins.calcom.plugin.probe_postgresql", fake_probe)
    monkeypatch.setattr("app.plugins.calcom.plugin.restore_postgresql_archive", fake_restore)
    monkeypatch.setattr("app.plugins.calcom.plugin.query_postgresql_json", fake_query)
    monkeypatch.setattr(
        "app.plugins.calcom.plugin.postgresql_catalog_sha256",
        lambda _identity: _EXACT_CATALOG_SHA256,
    )

    result = await CalcomPlugin(name="calcom").restore(context)

    assert result == {
        "status": "partial",
        "message": (
            "Cal.com database restored; exact-image boot and external deployment "
            "configuration remain required"
        ),
        "restored_path": str(artifact),
        "artifact_bytes": status.st_size,
        "sha256": artifact_sha256,
    }
    assert observed_restore_kwargs["validation"] == "calcom-postgresql-v1"
    assert observed_restore_kwargs["expected_schema_sha256"] == _EXACT_SCHEMA_SHA256
    assert observed_restore_kwargs["restore_sentinel"] == "homelab-backup:calcom-restore:v1"
    assert observed_restore_kwargs["allowed_unsupported_database_objects"] == frozenset(
        _EXACT_TRIGGER_OBJECTS
    )
    assert query_results == []
    assert artifact.read_bytes() == b"PGDMP verified Cal.com archive"


def test_calcom_restore_service_stages_and_audits_partial_recovery(
    db_session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RestoreService must privately stage Cal.com evidence and audit partial recovery."""
    source_config = {
        "mode": "source",
        "host": "calcom-source.internal",
        "port": 5432,
        "database": "calendso",
        "user": "calcom_backup",
        "password": "source-secret-must-not-escape",
    }
    destination_config = {
        "mode": "restore_destination",
        "host": "calcom-restore.internal",
        "port": 5432,
        "database": "hlb_calcom_restore_service",
        "user": "restore_owner",
        "password": "destination-secret-must-not-escape",
    }
    source = Target(
        name="Cal.com source",
        slug="calcom-source",
        plugin_name="calcom",
        plugin_config_json=json.dumps(source_config),
    )
    destination = Target(
        name="Cal.com destination",
        slug="calcom-destination",
        plugin_name="calcom",
        plugin_config_json=json.dumps(destination_config),
    )
    db_session.add_all((source, destination))
    db_session.flush()
    artifact_dir = tmp_path / "backups" / source.slug / "2026-08-16"
    artifact_dir.mkdir(parents=True)
    artifact = artifact_dir / "calcom.dump"
    payload = b"PGDMP Cal.com RestoreService artifact"
    artifact.write_bytes(payload)
    artifact.chmod(0o600)
    marker_counts = {
        category: evidence["count"] for category, evidence in _EXACT_MARKER_PROFILE.items()
    }
    sidecar_evidence = {
        "application_version": "6.2.0",
        "migration_head": _EXACT_MIGRATION_PROFILE["migration_head"],
        "migration_sha256": _EXACT_MIGRATION_PROFILE["migration_sha256"],
        "schema_sha256": _EXACT_SCHEMA_SHA256,
        "marker_profile_sha256": hashlib.sha256(
            json.dumps(
                _EXACT_MARKER_PROFILE,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest(),
        "marker_counts": marker_counts,
        "postgresql_server_version": "16.14",
        "postgresql_server_version_num": 160014,
        "server_encoding": "UTF8",
        "lc_collate": "en_US.utf8",
        "lc_ctype": "en_US.utf8",
        "rls_table_count": 0,
        "source_identity_sha256": "1" * 64,
        "source_catalog_sha256": "2" * 64,
        "archive_catalog_sha256": "3" * 64,
        "toc_sha256": "4" * 64,
        "catalog_counts": {
            "schemas": 1,
            "extensions": 1,
            "relations": 125,
            "sequences": 57,
            "indexes": 349,
            "constraints": 346,
            "routines": 30,
            "types": 54,
            "large_objects": 0,
        },
        "validation": "calcom-postgresql-v1",
    }
    plugin = CalcomPlugin(name="calcom")
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
    sidecar_path = Path(f"{artifact}.meta.json")
    source_sidecar_bytes = sidecar_path.read_bytes()
    now = datetime.now(timezone.utc)
    source_run = Run(
        status="success",
        operation="backup",
        started_at=now,
        finished_at=now,
    )
    db_session.add(source_run)
    db_session.flush()
    source_target_run = TargetRun(
        run_id=source_run.id,
        target_id=source.id,
        status="success",
        operation="backup",
        artifact_path=str(artifact),
        artifact_bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        source_identity_json=json.dumps(
            {
                "host": source_config["host"],
                "port": source_config["port"],
                "database": source_config["database"],
                "user": source_config["user"],
            }
        ),
        started_at=now,
        finished_at=now,
    )
    db_session.add(source_target_run)
    db_session.commit()
    observed: dict[str, object] = {}

    async def observe_restore(context: RestoreContext) -> dict[str, object]:
        staged = Path(context.artifact_path)
        observed["staged_path"] = staged
        observed["staged_mode"] = stat.S_IMODE(staged.stat().st_mode)
        observed["metadata"] = dict(context.metadata or {})
        return {
            "status": "partial",
            "message": "Cal.com database restored; exact-image boot remains required",
            "restored_path": str(staged),
            "artifact_bytes": staged.stat().st_size,
            "sha256": hashlib.sha256(staged.read_bytes()).hexdigest(),
        }

    monkeypatch.setattr(plugin, "restore", observe_restore)
    monkeypatch.setattr("app.services.restores.get_plugin", lambda _name: plugin)
    monkeypatch.setenv("BACKUP_BASE_PATH", str(tmp_path / "backups"))

    restored_run = RestoreService(db_session).restore(
        source_target_run_id=int(source_target_run.id),
        destination_target_id=int(destination.id),
    )

    staged_path = observed["staged_path"]
    assert isinstance(staged_path, Path) and not staged_path.exists()
    assert observed["staged_mode"] == 0o600
    metadata = observed["metadata"]
    assert isinstance(metadata, dict)
    assert metadata["artifact_sidecar"]["validation"] == "calcom-postgresql-v1"
    assert metadata["source_database_identity"]["database"] == "calendso"
    assert artifact.read_bytes() == payload
    assert sidecar_path.read_bytes() == source_sidecar_bytes
    assert restored_run.status == "partial"
    restored_target_run = db_session.query(TargetRun).filter(TargetRun.operation == "restore").one()
    assert restored_target_run.status == "partial"


@pytest.mark.asyncio
async def test_calcom_restore_timeout_and_cancellation_propagate_before_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The whole restore is deadline-bound and cancellation remains observable."""
    artifact = tmp_path / "restore-lifecycle.dump"
    artifact.write_bytes(b"PGDMP restore lifecycle")
    config = {
        "mode": "restore_destination",
        "host": "calcom-restore.internal",
        "port": 5432,
        "database": "hlb_calcom_restore_lifecycle",
        "user": "restore_owner",
        "password": "restore-lifecycle-secret-must-not-escape",
    }
    context = RestoreContext(
        job_id="restore-lifecycle",
        source_target_id="source",
        destination_target_id="destination",
        config=config,
        artifact_path=str(artifact),
        metadata={"artifact_sidecar": {}},
    )
    started = asyncio.Event()
    never_release = asyncio.Event()
    restore_started = False

    async def blocks(*_args: object, **_kwargs: object) -> PostgreSQLIdentity:
        started.set()
        await never_release.wait()
        raise AssertionError("unreachable")

    async def forbidden_restore(*_args: object, **_kwargs: object) -> PostgreSQLIdentity:
        nonlocal restore_started
        restore_started = True
        raise AssertionError("restore must not start")

    monkeypatch.setattr(
        "app.plugins.calcom.plugin._require_calcom_restore_sidecar", lambda _metadata: {}
    )
    monkeypatch.setattr(
        "app.plugins.calcom.plugin.authorize_postgresql_restore", lambda *a, **k: None
    )
    monkeypatch.setattr("app.plugins.calcom.plugin.probe_postgresql", blocks)
    monkeypatch.setattr("app.plugins.calcom.plugin.restore_postgresql_archive", forbidden_restore)
    monkeypatch.setattr("app.plugins.calcom.plugin.RESTORE_TIMEOUT_SECONDS", 0.01)
    plugin = CalcomPlugin(name="calcom")

    with pytest.raises(RuntimeError, match="restore timed out"):
        await plugin.restore(context)
    assert restore_started is False

    monkeypatch.setattr("app.plugins.calcom.plugin.RESTORE_TIMEOUT_SECONDS", 60.0)
    started.clear()
    task = asyncio.create_task(plugin.restore(context))
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert restore_started is False
    assert artifact.is_file()
    assert str(config["password"]) not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "message"),
    (
        ("disabled", "disabled outside an isolated local drill"),
        ("unapproved", "not in the exact allowlist"),
        ("same_target", "distinct source and destination targets"),
        ("same_database", "distinct database identity"),
        ("wrong_mode", "restore-destination configuration"),
        ("incomplete_sidecar", "archive provenance"),
    ),
)
async def test_calcom_restore_refuses_unsafe_or_incomplete_destinations_before_io(
    case: str,
    message: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every authorization/provenance failure occurs before destination probing."""
    artifact = tmp_path / f"{case}.dump"
    artifact.write_bytes(b"PGDMP refusal fixture")
    status = artifact.stat()
    config: dict[str, object] = {
        "mode": "restore_destination",
        "host": "calcom-restore.internal",
        "port": 5432,
        "database": "hlb_calcom_restore_refusal",
        "user": "restore_owner",
        "password": "refusal-secret-must-not-escape",
    }
    source_identity: dict[str, object] = {
        "host": "calcom-source.internal",
        "port": 5432,
        "database": "calendso",
        "user": "calcom_backup",
    }
    source_target_id = "source"
    destination_target_id = "destination"
    sidecar = _valid_calcom_restore_sidecar()
    monkeypatch.setenv("HOMELAB_BACKUP_ALLOW_ISOLATED_RESTORE", "1")
    monkeypatch.setenv(
        "HOMELAB_BACKUP_ISOLATED_CALCOM_RESTORE_DESTINATIONS",
        "calcom-restore.internal:5432/hlb_calcom_restore_refusal",
    )
    if case == "disabled":
        monkeypatch.delenv("HOMELAB_BACKUP_ALLOW_ISOLATED_RESTORE")
    elif case == "unapproved":
        monkeypatch.setenv(
            "HOMELAB_BACKUP_ISOLATED_CALCOM_RESTORE_DESTINATIONS",
            "other.internal:5432/other_restore",
        )
    elif case == "same_target":
        destination_target_id = source_target_id
    elif case == "same_database":
        source_identity.update(
            {
                "host": config["host"],
                "port": config["port"],
                "database": config["database"],
            }
        )
    elif case == "wrong_mode":
        config["mode"] = "source"
    elif case == "incomplete_sidecar":
        sidecar.pop("schema_sha256")

    async def forbidden_probe(*_args: object, **_kwargs: object) -> PostgreSQLIdentity:
        raise AssertionError("destination I/O must not start")

    monkeypatch.setattr("app.plugins.calcom.plugin.probe_postgresql", forbidden_probe)
    metadata = {
        "source_database_identity": source_identity,
        "artifact_bytes": status.st_size,
        "artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
        "staged_artifact_device": status.st_dev,
        "staged_artifact_inode": status.st_ino,
        "artifact_sidecar": sidecar,
    }
    with pytest.raises((ValueError, RuntimeError), match=message) as raised:
        await CalcomPlugin(name="calcom").restore(
            RestoreContext(
                job_id="refusal",
                source_target_id=source_target_id,
                destination_target_id=destination_target_id,
                config=config,  # type: ignore[arg-type]
                artifact_path=str(artifact),
                metadata=metadata,
            )
        )
    assert str(config["password"]) not in str(raised.value)


@pytest.mark.asyncio
async def test_schema_fingerprint_removes_only_pg_restore_session_guards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Random pg_restore guard tokens normalize, while executable SQL remains bound."""
    payloads = [
        b"-- schema\n\\restrict Alpha123\nCREATE TABLE public.x(id int);\n"
        b"\\unrestrict Alpha123\n",
        b"-- schema\n\\restrict Beta456\nCREATE TABLE public.x(id int);\n"
        b"\\unrestrict Beta456\n",
    ]
    observed: list[tuple[object, ...]] = []

    async def fake_exec(*args: object, **kwargs: object) -> DummyProcess:
        observed.append(args)
        assert kwargs["pass_fds"] == (37,)
        return DummyProcess(stdout=payloads.pop(0))

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    first = await postgresql_core.postgresql_archive_schema_sha256(37)
    second = await postgresql_core.postgresql_archive_schema_sha256(37)

    expected = hashlib.sha256(b"-- schema\nCREATE TABLE public.x(id int);\n").hexdigest()
    assert first == second == expected
    assert all(
        call
        == (
            postgresql_core.PG_RESTORE16,
            "--schema-only",
            "--no-owner",
            "--no-privileges",
            "--file=-",
            "/proc/self/fd/37",
        )
        for call in observed
    )


@pytest.mark.asyncio
async def test_schema_fingerprint_rejects_missing_or_ambiguous_session_guards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Normalization fails closed when the exact pg_restore guard shape drifts."""

    async def fake_exec(*_args: object, **_kwargs: object) -> DummyProcess:
        return DummyProcess(stdout=b"CREATE TABLE public.x(id int);\n")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    with pytest.raises(RuntimeError, match="schema was malformed"):
        await postgresql_core.postgresql_archive_schema_sha256(37)
