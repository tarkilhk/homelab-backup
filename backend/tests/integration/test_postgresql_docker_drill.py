from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, cast

import pytest

from app.core.plugins.sidecar import read_backup_sidecar

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_POSTGRESQL_DOCKER_DRILL") != "1",
    reason="set RUN_POSTGRESQL_DOCKER_DRILL=1 for the isolated PostgreSQL 16 drill",
)

_POSTGRES_IMAGE = "postgres@sha256:670391653713782e51974845b217c56fed4dd8729142299c43c919a8d3e15e00"
_POSTGRES_18_NEGATIVE_IMAGE = (
    "pgvector/pgvector@" "sha256:ff8da7b0714e5efa413d77f43e24d93064dd66469d418d12608c1bbc91fcf045"
)
_LABEL = "asia.hollinger.homelab-backup.postgresql-drill"
_DATABASE = "application_source"
_BACKUP_USER = "backup_reader"
_SYNTHETIC_SECRETS: set[str] = set()
_BACKEND_ROOT = Path(__file__).resolve().parents[2]


def _redact(value: str) -> str:
    result = value
    for secret in _SYNTHETIC_SECRETS:
        result = result.replace(secret, "[REDACTED]")
    return result[-6000:]


def _docker(
    *arguments: str,
    check: bool = True,
    input_text: str | None = None,
    timeout: int = 300,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["docker", *arguments],
            check=check,
            capture_output=True,
            text=True,
            input=input_text,
            timeout=timeout,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(
            "Disposable PostgreSQL Docker command failed:\n"
            f"{_redact(str(exc.stderr or ''))}\n{_redact(str(exc.stdout or ''))}"
        ) from None


def _psql(container: str, database: str, sql: str) -> str:
    completed = _docker(
        "exec",
        "-i",
        container,
        "psql",
        "-X",
        "-U",
        "postgres",
        "--dbname",
        database,
        "--set",
        "ON_ERROR_STOP=on",
        "-tA",
        "-f",
        "-",
        input_text=sql,
    )
    return completed.stdout.strip()


def _wait_for_postgresql(container: str) -> None:
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        state = _docker(
            "inspect",
            "--format",
            "{{.State.Status}}",
            container,
            check=False,
        ).stdout.strip()
        logs = _docker("logs", container, check=False).stdout
        if state == "exited":
            raise RuntimeError(f"Disposable PostgreSQL exited before readiness: {_redact(logs)}")
        initialization_finished = "PostgreSQL init process complete" in logs
        ready = _docker(
            "exec",
            container,
            "pg_isready",
            "-U",
            "postgres",
            check=False,
        )
        if initialization_finished and ready.returncode == 0:
            return
        time.sleep(0.25)
    raise RuntimeError("Disposable PostgreSQL did not become ready")


@contextmanager
def _managed_drill_resources(
    *,
    suffix: str,
    network: str,
    volumes: list[str],
    backup_root: Path,
) -> Iterator[str]:
    """Build, own and remove every mutable Docker/test resource for one round."""
    runner_image = f"hlb-pg17-runner:{suffix}"
    try:
        _docker(
            "build",
            "--tag",
            runner_image,
            "--file",
            str(_BACKEND_ROOT / "Dockerfile"),
            str(_BACKEND_ROOT),
            timeout=600,
        )
        _docker("pull", _POSTGRES_IMAGE, timeout=600)
        _docker("pull", _POSTGRES_18_NEGATIVE_IMAGE, timeout=600)
        _docker("network", "create", "--internal", "--label", f"{_LABEL}=1", network)
        for volume in volumes:
            _docker("volume", "create", "--label", f"{_LABEL}=1", volume)
        yield runner_image
    finally:
        names = _docker("ps", "--all", "--format", "{{.Names}}", check=False).stdout.splitlines()
        owned_containers = [name for name in names if name.endswith(suffix)]
        if owned_containers:
            _docker("rm", "-f", *owned_containers, check=False)
        for volume in volumes:
            _docker("volume", "rm", "-f", volume, check=False)
        _docker("network", "rm", network, check=False)
        _docker("image", "rm", "-f", runner_image, check=False)
        shutil.rmtree(backup_root, ignore_errors=True)
        _SYNTHETIC_SECRETS.clear()
        assert not any(
            name.endswith(suffix)
            for name in _docker("ps", "--all", "--format", "{{.Names}}", check=False)
            .stdout.strip()
            .splitlines()
        )
        assert _docker("network", "inspect", network, check=False).returncode != 0
        assert all(
            _docker("volume", "inspect", volume, check=False).returncode != 0 for volume in volumes
        )
        assert _docker("image", "inspect", runner_image, check=False).returncode != 0
        assert not backup_root.exists()


def _seed_source(
    container: str,
    *,
    owner_password: str,
    backup_password: str,
) -> None:
    _psql(
        container,
        "postgres",
        f"""
CREATE ROLE application_owner LOGIN PASSWORD '{owner_password}'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
CREATE ROLE {_BACKUP_USER} LOGIN PASSWORD '{backup_password}'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
REVOKE CONNECT, TEMPORARY ON DATABASE postgres FROM PUBLIC;
REVOKE CONNECT, TEMPORARY ON DATABASE template1 FROM PUBLIC;
CREATE DATABASE {_DATABASE} WITH OWNER application_owner TEMPLATE template0 ENCODING 'UTF8';
REVOKE ALL ON DATABASE {_DATABASE} FROM PUBLIC;
GRANT CONNECT ON DATABASE {_DATABASE} TO {_BACKUP_USER};
""",
    )
    _psql(
        container,
        _DATABASE,
        f"""
SET ROLE application_owner;
CREATE EXTENSION pg_trgm;
CREATE TABLE accounts (
  id bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
  phase text NOT NULL UNIQUE,
  display_name text NOT NULL
);
CREATE TABLE entries (
  id bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
  account_id bigint NOT NULL REFERENCES accounts(id),
  payload text NOT NULL,
  attachment_oid oid NOT NULL
);
CREATE INDEX entries_payload_trgm_idx ON entries USING gin (payload gin_trgm_ops);
SELECT lo_from_bytea(91001, decode('70686173652d612d6c617267652d6f626a656374', 'hex'));
INSERT INTO accounts (phase, display_name) VALUES ('A', 'Synthetic account A');
INSERT INTO entries (account_id, payload, attachment_oid)
SELECT id, 'Synthetic relational payload A', 91001 FROM accounts WHERE phase = 'A';
RESET ROLE;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO {_BACKUP_USER};
GRANT SELECT ON ALL TABLES IN SCHEMA public TO {_BACKUP_USER};
GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO {_BACKUP_USER};
GRANT SELECT ON LARGE OBJECT 91001 TO {_BACKUP_USER};
""",
    )


def _mutate_source_for_phase_b(container: str) -> None:
    _psql(
        container,
        _DATABASE,
        f"""
SET ROLE application_owner;
SELECT lo_from_bytea(91002, decode('70686173652d622d6c617267652d6f626a656374', 'hex'));
INSERT INTO accounts (phase, display_name) VALUES ('B', 'Synthetic account B');
INSERT INTO entries (account_id, payload, attachment_oid)
SELECT id, 'Synthetic relational payload B', 91002 FROM accounts WHERE phase = 'B';
RESET ROLE;
GRANT SELECT ON LARGE OBJECT 91002 TO {_BACKUP_USER};
""",
    )


def _seed_transactional_restore_failure(container: str) -> None:
    _psql(
        container,
        _DATABASE,
        f"""
SET ROLE application_owner;
CREATE TABLE restore_failure_probe (
  id integer PRIMARY KEY,
  expected_database text NOT NULL,
  CONSTRAINT restore_failure_probe_database_check
    CHECK (current_database() = expected_database)
);
INSERT INTO restore_failure_probe VALUES (1, '{_DATABASE}');
RESET ROLE;
GRANT SELECT ON restore_failure_probe TO {_BACKUP_USER};
""",
    )


def _remove_transactional_restore_failure(container: str) -> None:
    _psql(
        container,
        _DATABASE,
        """
SET ROLE application_owner;
DROP TABLE restore_failure_probe;
""",
    )


def _seed_cancellable_restore(container: str) -> None:
    _psql(
        container,
        _DATABASE,
        f"""
SET ROLE application_owner;
CREATE TABLE slow_restore_probe (
  id integer PRIMARY KEY,
  expected_database text NOT NULL,
  CONSTRAINT slow_restore_probe_database_check
    CHECK (current_database() = expected_database OR pg_sleep(30) IS NULL)
);
INSERT INTO slow_restore_probe VALUES (1, '{_DATABASE}');
RESET ROLE;
GRANT SELECT ON slow_restore_probe TO {_BACKUP_USER};
""",
    )


def _remove_cancellable_restore(container: str) -> None:
    _psql(
        container,
        _DATABASE,
        """
SET ROLE application_owner;
DROP TABLE slow_restore_probe;
""",
    )


def _seed_wrong_major_source(container: str, backup_password: str) -> None:
    _psql(
        container,
        "postgres",
        f"""
CREATE ROLE {_BACKUP_USER} LOGIN PASSWORD '{backup_password}'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
REVOKE CONNECT, TEMPORARY ON DATABASE postgres FROM PUBLIC;
REVOKE CONNECT, TEMPORARY ON DATABASE template1 FROM PUBLIC;
CREATE DATABASE {_DATABASE} WITH TEMPLATE template0 ENCODING 'UTF8';
REVOKE ALL ON DATABASE {_DATABASE} FROM PUBLIC;
GRANT CONNECT ON DATABASE {_DATABASE} TO {_BACKUP_USER};
""",
    )


def _start_restore_destination(
    *,
    container: str,
    volume: str,
    network: str,
    admin_password: str,
    restore_password: str,
) -> None:
    _docker(
        "run",
        "--detach",
        "--pull",
        "never",
        "--name",
        container,
        "--label",
        f"{_LABEL}=destination",
        "--network",
        network,
        "--mount",
        f"source={volume},target=/var/lib/postgresql/data",
        "--tmpfs",
        "/var/run/postgresql:rw,nosuid,nodev,noexec",
        "--memory",
        "512m",
        "--memory-swap",
        "512m",
        "--pids-limit",
        "256",
        "--security-opt",
        "no-new-privileges:true",
        "-e",
        f"POSTGRES_PASSWORD={admin_password}",
        _POSTGRES_IMAGE,
    )
    _wait_for_postgresql(container)
    _psql(
        container,
        "postgres",
        f"""
CREATE ROLE restore_owner LOGIN PASSWORD '{restore_password}'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
REVOKE CONNECT, TEMPORARY ON DATABASE postgres FROM PUBLIC;
REVOKE CONNECT, TEMPORARY ON DATABASE template1 FROM PUBLIC;
CREATE DATABASE application_restore
  WITH OWNER restore_owner TEMPLATE template0 ENCODING 'UTF8';
REVOKE ALL ON DATABASE application_restore FROM PUBLIC;
COMMENT ON DATABASE application_restore IS 'homelab-backup:postgresql-restore:v1';
""",
    )


def _restored_state(container: str) -> dict[str, object]:
    payload = _psql(
        container,
        "application_restore",
        """
SELECT json_build_object(
  'accounts', (SELECT json_agg(row_to_json(a) ORDER BY a.id) FROM accounts AS a),
  'entries', (
    SELECT json_agg(
      json_build_object(
        'id', e.id,
        'account_id', e.account_id,
        'payload', e.payload,
        'attachment_oid', e.attachment_oid::bigint
      ) ORDER BY e.id
    ) FROM entries AS e
  ),
  'large_objects', (
    SELECT json_object_agg(loid::text, encode(lo_get(loid), 'hex') ORDER BY loid)
    FROM (SELECT oid AS loid FROM pg_largeobject_metadata) AS objects
  ),
  'sequence', (SELECT last_value FROM accounts_id_seq),
  'extension', (SELECT extversion FROM pg_extension WHERE extname = 'pg_trgm'),
  'foreign_key', (
    SELECT json_build_object(
      'validated', convalidated,
      'definition', pg_get_constraintdef(oid, true)
    )
    FROM pg_constraint
    WHERE conname = 'entries_account_id_fkey'
  )
);
""",
    )
    value = json.loads(payload)
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


def _user_relation_count(container: str) -> int:
    return int(
        _psql(
            container,
            "application_restore",
            """
SELECT count(*)
FROM pg_class AS relation
JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
WHERE namespace.nspname = 'public'
  AND relation.relkind IN ('r', 'p', 'v', 'm', 'f', 'S');
""",
        )
    )


_BACKUP_SCRIPT = r"""
import asyncio, json, sys
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app.core.db import Base
from app.core.scheduler import run_job_immediately
from app.core.plugins.base import BackupContext
from app.core.plugins import postgresql as postgresql_core
from app.plugins.postgresql.plugin import PostgreSQLPlugin
from app.models import Job, Tag, Target, TargetRun, TargetTag

request = json.load(sys.stdin)
if request.get("pg_dump_path"):
    postgresql_core.PG_DUMP16 = request["pg_dump_path"]

async def main():
    plugin = PostgreSQLPlugin(name="postgresql")
    config = request["config"]
    assert await plugin.test(config) is True
    status = await plugin.get_status(BackupContext(
        job_id="postgresql-exact-drill-status",
        target_id="postgresql-source",
        config=config,
        metadata={"target_slug": "postgresql-source"},
    ))
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    with Session() as session:
        tag = Tag(display_name="PostgreSQL exact drill")
        target = Target(
            name="PostgreSQL exact source",
            slug="postgresql-source",
            plugin_name="postgresql",
            plugin_config_json=json.dumps(config),
        )
        session.add_all([tag, target])
        session.flush()
        session.add(TargetTag(target_id=target.id, tag_id=tag.id, origin="DIRECT"))
        job = Job(
            tag_id=tag.id,
            name="PostgreSQL exact backup",
            schedule_cron="0 0 * * *",
            enabled=True,
        )
        session.add(job)
        session.commit()
        run = run_job_immediately(session, job.id, triggered_by="exact_postgresql_drill")
        target_run = session.query(TargetRun).filter(TargetRun.run_id == run.id).one()
        if run.status != "success" or target_run.status != "success":
            raise RuntimeError(target_run.message or run.message or "backup run failed")
        if not target_run.artifact_path:
            raise RuntimeError("backup run did not record an artifact")
        result = {
            "artifact_path": target_run.artifact_path,
            "run_status": run.status,
            "target_status": target_run.status,
            "artifact_bytes": target_run.artifact_bytes,
            "sha256": target_run.sha256,
            "source_identity": json.loads(target_run.source_identity_json),
        }
    sys.stdout.write(
        json.dumps({"status": status, **result}) + "\n"
    )

asyncio.run(main())
"""

_RESTORE_SCRIPT = r"""
import json, os, shutil, sys
from datetime import datetime, timezone
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app.core.db import Base
from app.models import Run, Target, TargetRun
from app.plugins.postgresql import plugin as postgresql_plugin
from app.services.restores import RestoreService

request = json.load(sys.stdin)
engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
Base.metadata.create_all(bind=engine)
Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
with Session() as session:
    source_config = (
        request["config"] if request.get("same_target") else {"mode": "source"}
    )
    source = Target(
        name="PostgreSQL drill source",
        slug="postgresql-source",
        plugin_name="postgresql",
        plugin_config_json=json.dumps(source_config),
    )
    destination = Target(
        name="PostgreSQL drill destination",
        slug="postgresql-destination",
        plugin_name="postgresql",
        plugin_config_json=json.dumps(request["config"]),
    )
    session.add_all([source, destination])
    session.flush()
    now = datetime.now(timezone.utc)
    source_run = Run(
        status="success",
        operation="backup",
        started_at=now,
        finished_at=now,
    )
    session.add(source_run)
    session.flush()
    source_target_run = TargetRun(
        run_id=source_run.id,
        target_id=source.id,
        status="success",
        operation="backup",
        artifact_path=request["artifact_path"],
        artifact_bytes=request["artifact_bytes"],
        sha256=request["artifact_sha256"],
        source_identity_json=json.dumps(request["source_database_identity"]),
        started_at=now,
        finished_at=now,
    )
    session.add(source_target_run)
    session.commit()

    if request.get("replace_staged_artifact"):
        original_restore = postgresql_plugin.restore_postgresql_archive

        async def substitute_staged_artifact(target, identity, artifact_path, metadata):
            staged = Path(artifact_path)
            replacement = staged.with_name(f"{staged.name}.replacement")
            shutil.copyfile(staged, replacement)
            replacement.chmod(0o600)
            os.replace(replacement, staged)
            return await original_restore(target, identity, staged, metadata)

        postgresql_plugin.restore_postgresql_archive = substitute_staged_artifact

    restored = RestoreService(session).restore(
        source_target_run_id=source_target_run.id,
        destination_target_id=(source.id if request.get("same_target") else destination.id),
        triggered_by="exact_postgresql_drill",
    )
    target_run = restored.target_runs[0]
    sys.stdout.write(json.dumps({
        "status": restored.status,
        "target_status": target_run.status,
        "artifact_path": target_run.artifact_path,
        "artifact_bytes": target_run.artifact_bytes,
        "sha256": target_run.sha256,
    }) + "\n")
"""

_CANCEL_RESTORE_SCRIPT = r"""
import asyncio, hashlib, json, os, shutil, sys, tempfile
from pathlib import Path
from app.core.plugins.base import RestoreContext
from app.core.plugins.postgresql import PG_RESTORE16
from app.core.plugins.sidecar import read_backup_sidecar
from app.plugins.postgresql.plugin import PostgreSQLPlugin

request = json.load(sys.stdin)

async def main():
    source = Path(request["artifact_path"])
    sidecar = read_backup_sidecar(str(source))
    assert isinstance(sidecar, dict)
    with tempfile.TemporaryDirectory(prefix="postgresql-cancel-stage-") as workspace:
        staged = Path(workspace) / "staged.dump"
        shutil.copyfile(source, staged)
        staged.chmod(0o600)
        opened = staged.stat()
        metadata = {
            "source_database_identity": request["source_database_identity"],
            "artifact_bytes": opened.st_size,
            "artifact_sha256": hashlib.sha256(staged.read_bytes()).hexdigest(),
            "staged_artifact_device": opened.st_dev,
            "staged_artifact_inode": opened.st_ino,
            "artifact_sidecar": sidecar,
        }
        context = RestoreContext(
            job_id="postgresql-exact-cancellation",
            source_target_id="postgresql-source",
            destination_target_id="postgresql-cancel-destination",
            artifact_path=str(staged),
            config=request["config"],
            metadata=metadata,
        )
        task = asyncio.create_task(PostgreSQLPlugin(name="postgresql").restore(context))
        await asyncio.sleep(1.0)
        assert not task.done(), "transactional restore did not reach the cancellation window"
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("cancelled PostgreSQL restore returned normally")
        assert not list(Path("/tmp").glob("homelab-backup-postgresql-pgpass-*"))
        child_paths = []
        for process_path in Path("/proc").iterdir():
            if not process_path.name.isdigit():
                continue
            try:
                argv = (process_path / "cmdline").read_bytes().split(b"\0")
            except OSError:
                continue
            if argv and os.fsdecode(argv[0]) == PG_RESTORE16:
                child_paths.append(process_path.name)
        assert child_paths == []
    sys.stdout.write(
        json.dumps({"cancelled": True, "child_count": 0, "pgpass_count": 0}) + "\n"
    )

asyncio.run(main())
"""


def _run_backup(
    *,
    name: str,
    network: str,
    source: str,
    backup_root: Path,
    password: str,
    runner_image: str,
    pg_dump_path: str | None = None,
) -> dict[str, object]:
    request = json.dumps(
        {
            "config": {
                "mode": "source",
                "host": source,
                "port": 5432,
                "database": _DATABASE,
                "user": _BACKUP_USER,
                "password": password,
            },
            "pg_dump_path": pg_dump_path,
        },
        separators=(",", ":"),
    )
    _docker(
        "create",
        "--name",
        name,
        "--interactive",
        "--label",
        f"{_LABEL}=runner",
        "--network",
        network,
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=128m",
        "--memory",
        "512m",
        "--memory-swap",
        "512m",
        "--pids-limit",
        "128",
        "--security-opt",
        "no-new-privileges:true",
        "--cap-drop",
        "ALL",
        "-v",
        f"{backup_root}:/backups:rw",
        runner_image,
        "python",
        "-c",
        _BACKUP_SCRIPT,
    )
    try:
        mounts = json.loads(_docker("inspect", "--format", "{{json .Mounts}}", name).stdout)
        assert mounts == [
            {
                **mounts[0],
                "Destination": "/backups",
                "RW": True,
            }
        ]
        assert _docker(
            "inspect", "--format", "{{json .HostConfig.PortBindings}}", name
        ).stdout.strip() in {"null", "{}"}
        completed = _docker(
            "start",
            "--attach",
            "--interactive",
            name,
            input_text=request,
            timeout=180,
        )
    finally:
        _docker("rm", "-f", name, check=False)
    for line in reversed(completed.stdout.splitlines()):
        if line.startswith("{"):
            value = json.loads(line)
            assert isinstance(value, dict)
            return cast(dict[str, object], value)
    raise AssertionError(f"PostgreSQL runner emitted no JSON: {_redact(completed.stderr)}")


def _run_restore(
    *,
    name: str,
    network: str,
    destination: str,
    backup_root: Path,
    artifact: Path,
    password: str,
    source: str,
    runner_image: str,
    allowlist_destination: str | None = None,
    same_target: bool = False,
    replace_staged_artifact: bool = False,
) -> dict[str, object]:
    container_artifact = "/backups/" + str(artifact.relative_to(backup_root))
    database = "application_restore"
    user = "restore_owner"
    request = json.dumps(
        {
            "artifact_path": container_artifact,
            "artifact_bytes": artifact.stat().st_size,
            "artifact_sha256": _sha256(artifact),
            "config": {
                "mode": "restore_destination",
                "host": destination,
                "port": 5432,
                "database": database,
                "user": user,
                "password": password,
            },
            "source_database_identity": {
                "host": source,
                "port": 5432,
                "database": _DATABASE,
                "user": _BACKUP_USER,
            },
            "same_target": same_target,
            "replace_staged_artifact": replace_staged_artifact,
        },
        separators=(",", ":"),
    )
    _docker(
        "create",
        "--name",
        name,
        "--interactive",
        "--label",
        f"{_LABEL}=runner",
        "--network",
        network,
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=128m",
        "--memory",
        "512m",
        "--memory-swap",
        "512m",
        "--pids-limit",
        "128",
        "--security-opt",
        "no-new-privileges:true",
        "--cap-drop",
        "ALL",
        "-e",
        "HOMELAB_BACKUP_ALLOW_ISOLATED_RESTORE=1",
        "-e",
        (
            "HOMELAB_BACKUP_ISOLATED_POSTGRESQL_RESTORE_DESTINATIONS="
            f"{allowlist_destination or destination}:5432/{database}"
        ),
        "-e",
        "BACKUP_BASE_PATH=/backups",
        "-v",
        f"{backup_root}:/backups:rw",
        runner_image,
        "python",
        "-c",
        _RESTORE_SCRIPT,
    )
    try:
        completed = _docker(
            "start",
            "--attach",
            "--interactive",
            name,
            input_text=request,
            timeout=180,
        )
    finally:
        _docker("rm", "-f", name, check=False)
    for line in reversed(completed.stdout.splitlines()):
        if line.startswith("{"):
            value = json.loads(line)
            assert isinstance(value, dict)
            return cast(dict[str, object], value)
    raise AssertionError(f"PostgreSQL restore runner emitted no JSON: {_redact(completed.stderr)}")


def _run_cancelled_restore(
    *,
    name: str,
    network: str,
    destination: str,
    backup_root: Path,
    artifact: Path,
    password: str,
    source: str,
    runner_image: str,
) -> dict[str, object]:
    database = "application_restore"
    request = json.dumps(
        {
            "artifact_path": "/backups/" + str(artifact.relative_to(backup_root)),
            "config": {
                "mode": "restore_destination",
                "host": destination,
                "port": 5432,
                "database": database,
                "user": "restore_owner",
                "password": password,
            },
            "source_database_identity": {
                "host": source,
                "port": 5432,
                "database": _DATABASE,
                "user": _BACKUP_USER,
            },
        },
        separators=(",", ":"),
    )
    _docker(
        "create",
        "--name",
        name,
        "--interactive",
        "--label",
        f"{_LABEL}=runner",
        "--network",
        network,
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=128m",
        "--memory",
        "512m",
        "--memory-swap",
        "512m",
        "--pids-limit",
        "128",
        "--security-opt",
        "no-new-privileges:true",
        "--cap-drop",
        "ALL",
        "-e",
        "HOMELAB_BACKUP_ALLOW_ISOLATED_RESTORE=1",
        "-e",
        (
            "HOMELAB_BACKUP_ISOLATED_POSTGRESQL_RESTORE_DESTINATIONS="
            f"{destination}:5432/{database}"
        ),
        "-v",
        f"{backup_root}:/backups:ro",
        runner_image,
        "python",
        "-c",
        _CANCEL_RESTORE_SCRIPT,
    )
    try:
        completed = _docker(
            "start",
            "--attach",
            "--interactive",
            name,
            input_text=request,
            timeout=180,
        )
    finally:
        _docker("rm", "-f", name, check=False)
    for line in reversed(completed.stdout.splitlines()):
        if line.startswith("{"):
            value = json.loads(line)
            assert isinstance(value, dict)
            return cast(dict[str, object], value)
    raise AssertionError(
        f"Cancelled PostgreSQL runner emitted no JSON: {_redact(completed.stderr)}"
    )


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


@pytest.mark.parametrize("drill_round", (1, 2))
def test_exact_postgresql_two_backups_and_two_fresh_restores(
    tmp_path: Path,
    drill_round: int,
) -> None:
    """Two exact A/B archives restore independently and survive server restart."""
    suffix = f"r{drill_round}-{uuid.uuid4().hex[:9]}"
    network = f"hlb-pg17-{suffix}"
    source = f"hlb-pg17-source-{suffix}"
    wrong_major_source = f"hlb-pg17-wrong-major-{suffix}"
    backup_runners = [f"hlb-pg17-backup-{phase}-{suffix}" for phase in ("a", "b")]
    negative_runners = [
        f"hlb-pg17-negative-{case}-{suffix}"
        for case in (
            "write",
            "rls",
            "underprivileged",
            "sequence-write",
            "dangerous-role",
            "other-database",
            "dump-failure",
            "wrong-major",
            "restore-transaction",
            "restore-cancellation",
            "temporary",
            "signal-role",
            "security-definer",
            "unsupported-object",
        )
    ]
    destinations = [f"hlb-pg17-destination-{phase}-{suffix}" for phase in ("a", "b")]
    restore_runners = [f"hlb-pg17-restore-{phase}-{suffix}" for phase in ("a", "b")]
    source_volume = f"hlb-pg17-source-{suffix}"
    wrong_major_volume = f"hlb-pg17-wrong-major-{suffix}"
    destination_volumes = [f"hlb-pg17-destination-{phase}-{suffix}" for phase in ("a", "b")]
    postgres_password = f"synthetic-postgres-{suffix}"
    wrong_major_admin_password = f"synthetic-wrong-major-admin-{suffix}"
    owner_password = f"synthetic-owner-{suffix}"
    backup_password = f"synthetic-reader-{suffix}"
    destination_admin_passwords = [
        f"synthetic-destination-admin-{phase}-{suffix}" for phase in ("a", "b")
    ]
    restore_passwords = [f"synthetic-restore-owner-{phase}-{suffix}" for phase in ("a", "b")]
    _SYNTHETIC_SECRETS.update(
        {
            postgres_password,
            wrong_major_admin_password,
            owner_password,
            backup_password,
            *destination_admin_passwords,
            *restore_passwords,
        }
    )

    volumes = [source_volume, wrong_major_volume, *destination_volumes]
    with _managed_drill_resources(
        suffix=suffix,
        network=network,
        volumes=volumes,
        backup_root=tmp_path,
    ) as runner_image:
        _docker(
            "run",
            "--detach",
            "--pull",
            "never",
            "--name",
            source,
            "--label",
            f"{_LABEL}=source",
            "--network",
            network,
            "--mount",
            f"source={source_volume},target=/var/lib/postgresql/data",
            "--tmpfs",
            "/var/run/postgresql:rw,nosuid,nodev,noexec",
            "--memory",
            "512m",
            "--memory-swap",
            "512m",
            "--pids-limit",
            "256",
            "--security-opt",
            "no-new-privileges:true",
            "-e",
            f"POSTGRES_PASSWORD={postgres_password}",
            _POSTGRES_IMAGE,
        )
        assert _docker(
            "inspect", "--format", "{{json .HostConfig.PortBindings}}", source
        ).stdout.strip() in {"null", "{}"}
        _wait_for_postgresql(source)
        _seed_source(
            source,
            owner_password=owner_password,
            backup_password=backup_password,
        )

        _psql(source, _DATABASE, f"GRANT INSERT ON accounts TO {_BACKUP_USER};")
        with pytest.raises(RuntimeError, match="relation write privileges"):
            _run_backup(
                name=negative_runners[0],
                network=network,
                source=source,
                backup_root=tmp_path,
                password=backup_password,
                runner_image=runner_image,
            )
        _psql(source, _DATABASE, f"REVOKE INSERT ON accounts FROM {_BACKUP_USER};")

        _psql(source, _DATABASE, "ALTER TABLE accounts ENABLE ROW LEVEL SECURITY;")
        with pytest.raises(RuntimeError, match="unsupported RLS"):
            _run_backup(
                name=negative_runners[1],
                network=network,
                source=source,
                backup_root=tmp_path,
                password=backup_password,
                runner_image=runner_image,
            )
        _psql(source, _DATABASE, "ALTER TABLE accounts DISABLE ROW LEVEL SECURITY;")

        _psql(source, _DATABASE, f"REVOKE SELECT ON entries FROM {_BACKUP_USER};")
        with pytest.raises(RuntimeError, match="cannot read every relation"):
            _run_backup(
                name=negative_runners[2],
                network=network,
                source=source,
                backup_root=tmp_path,
                password=backup_password,
                runner_image=runner_image,
            )
        _psql(source, _DATABASE, f"GRANT SELECT ON entries TO {_BACKUP_USER};")

        _psql(
            source,
            _DATABASE,
            f"GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO {_BACKUP_USER};",
        )
        with pytest.raises(RuntimeError, match="mutate sequence state"):
            _run_backup(
                name=negative_runners[3],
                network=network,
                source=source,
                backup_root=tmp_path,
                password=backup_password,
                runner_image=runner_image,
            )
        _psql(
            source,
            _DATABASE,
            f"REVOKE USAGE ON ALL SEQUENCES IN SCHEMA public FROM {_BACKUP_USER};",
        )

        _psql(source, _DATABASE, f"GRANT pg_read_all_data TO {_BACKUP_USER};")
        with pytest.raises(RuntimeError, match="authority outside the target database"):
            _run_backup(
                name=negative_runners[4],
                network=network,
                source=source,
                backup_root=tmp_path,
                password=backup_password,
                runner_image=runner_image,
            )
        _psql(source, _DATABASE, f"REVOKE pg_read_all_data FROM {_BACKUP_USER};")

        _psql(source, _DATABASE, f"GRANT CONNECT ON DATABASE postgres TO {_BACKUP_USER};")
        with pytest.raises(RuntimeError, match="authority outside the target database"):
            _run_backup(
                name=negative_runners[5],
                network=network,
                source=source,
                backup_root=tmp_path,
                password=backup_password,
                runner_image=runner_image,
            )
        _psql(source, _DATABASE, f"REVOKE CONNECT ON DATABASE postgres FROM {_BACKUP_USER};")

        _psql(
            source,
            _DATABASE,
            f"GRANT TEMPORARY ON DATABASE {_DATABASE} TO {_BACKUP_USER};",
        )
        with pytest.raises(RuntimeError, match="temporary database privilege"):
            _run_backup(
                name=negative_runners[10],
                network=network,
                source=source,
                backup_root=tmp_path,
                password=backup_password,
                runner_image=runner_image,
            )
        _psql(
            source,
            _DATABASE,
            f"REVOKE TEMPORARY ON DATABASE {_DATABASE} FROM {_BACKUP_USER};",
        )

        _psql(source, _DATABASE, f"GRANT pg_signal_backend TO {_BACKUP_USER};")
        with pytest.raises(RuntimeError, match="authority outside the target database"):
            _run_backup(
                name=negative_runners[11],
                network=network,
                source=source,
                backup_root=tmp_path,
                password=backup_password,
                runner_image=runner_image,
            )
        _psql(source, _DATABASE, f"REVOKE pg_signal_backend FROM {_BACKUP_USER};")

        _psql(
            source,
            _DATABASE,
            """
SET ROLE application_owner;
CREATE FUNCTION unsafe_security_definer() RETURNS integer
LANGUAGE sql SECURITY DEFINER AS 'SELECT 1';
RESET ROLE;
""",
        )
        with pytest.raises(RuntimeError, match="security definer"):
            _run_backup(
                name=negative_runners[12],
                network=network,
                source=source,
                backup_root=tmp_path,
                password=backup_password,
                runner_image=runner_image,
            )
        _psql(
            source,
            _DATABASE,
            "SET ROLE application_owner; DROP FUNCTION unsafe_security_definer(); RESET ROLE;",
        )

        _psql(
            source,
            _DATABASE,
            "CREATE EXTENSION postgres_fdw; "
            "CREATE SERVER unsafe_server FOREIGN DATA WRAPPER postgres_fdw;",
        )
        with pytest.raises(RuntimeError, match="unsupported database objects"):
            _run_backup(
                name=negative_runners[13],
                network=network,
                source=source,
                backup_root=tmp_path,
                password=backup_password,
                runner_image=runner_image,
            )
        _psql(source, _DATABASE, "DROP EXTENSION postgres_fdw CASCADE;")

        with pytest.raises(RuntimeError, match="pg_dump failed"):
            _run_backup(
                name=negative_runners[6],
                network=network,
                source=source,
                backup_root=tmp_path,
                password=backup_password,
                runner_image=runner_image,
                pg_dump_path="/bin/false",
            )

        _docker(
            "run",
            "--detach",
            "--pull",
            "never",
            "--name",
            wrong_major_source,
            "--label",
            f"{_LABEL}=wrong-major-source",
            "--network",
            network,
            "--mount",
            f"source={wrong_major_volume},target=/var/lib/postgresql",
            "--tmpfs",
            "/var/run/postgresql:rw,nosuid,nodev,noexec",
            "--memory",
            "512m",
            "--memory-swap",
            "512m",
            "--pids-limit",
            "256",
            "--security-opt",
            "no-new-privileges:true",
            "-e",
            f"POSTGRES_PASSWORD={wrong_major_admin_password}",
            _POSTGRES_18_NEGATIVE_IMAGE,
        )
        _wait_for_postgresql(wrong_major_source)
        _seed_wrong_major_source(wrong_major_source, backup_password)
        with pytest.raises(RuntimeError, match="server major version must be 16"):
            _run_backup(
                name=negative_runners[7],
                network=network,
                source=wrong_major_source,
                backup_root=tmp_path,
                password=backup_password,
                runner_image=runner_image,
            )
        assert not list(tmp_path.rglob("*.dump"))
        assert not list(tmp_path.rglob("*.meta.json"))

        artifacts: list[Path] = []
        sidecars: list[dict[str, Any]] = []
        for index, runner in enumerate(backup_runners):
            if index == 1:
                _mutate_source_for_phase_b(source)
            result = _run_backup(
                name=runner,
                network=network,
                source=source,
                backup_root=tmp_path,
                password=backup_password,
                runner_image=runner_image,
            )
            status = result["status"]
            assert isinstance(status, dict)
            assert status["status"] == "ok"
            assert status["server_version"] == "16.14"
            assert status["database"] == _DATABASE
            assert result["run_status"] == "success"
            assert result["target_status"] == "success"
            container_artifact = Path(cast(str, result["artifact_path"]))
            artifact = tmp_path / container_artifact.relative_to("/backups")
            assert result["artifact_bytes"] == artifact.stat().st_size
            assert result["sha256"] == _sha256(artifact)
            assert result["source_identity"] == {
                "database": _DATABASE,
                "host": source,
                "port": 5432,
                "user": _BACKUP_USER,
            }
            assert artifact.read_bytes()[:5] == b"PGDMP"
            assert stat.S_IMODE(artifact.stat().st_mode) == 0o600
            assert stat.S_IMODE(Path(f"{artifact}.meta.json").stat().st_mode) == 0o600
            sidecar = read_backup_sidecar(str(artifact))
            assert sidecar is not None
            assert sidecar["artifact_bytes"] == artifact.stat().st_size
            assert sidecar["sha256"] == _sha256(artifact)
            assert sidecar["postgresql_server_version"] == "16.14"
            assert sidecar["server_encoding"] == "UTF8"
            assert sidecar["lc_collate"]
            assert sidecar["lc_ctype"]
            assert sidecar["rls_table_count"] == 0
            assert sidecar["validation"] == "postgresql-custom-v1"
            assert len(sidecar["source_catalog_sha256"]) == 64
            assert sidecar["catalog_counts"] == {
                "schemas": 1,
                "extensions": 2,
                "relations": 2,
                "sequences": 2,
                "indexes": 1,
                "constraints": 4,
                "routines": 0,
                "types": 0,
                "large_objects": index + 1,
            }
            serialized = json.dumps(sidecar, sort_keys=True)
            assert all(secret not in serialized for secret in _SYNTHETIC_SECRETS)
            assert str(tmp_path) not in serialized
            artifacts.append(artifact)
            sidecars.append(sidecar)

        artifact_a_signature = (artifacts[0].stat().st_size, _sha256(artifacts[0]))
        assert artifacts[0] != artifacts[1]
        assert artifact_a_signature != (artifacts[1].stat().st_size, _sha256(artifacts[1]))
        assert sidecars[0]["archive_catalog_sha256"] != sidecars[1]["archive_catalog_sha256"]
        assert sidecars[0]["source_catalog_sha256"] != sidecars[1]["source_catalog_sha256"]
        assert sidecars[0]["toc_sha256"] != sidecars[1]["toc_sha256"]
        assert (artifacts[0].stat().st_size, _sha256(artifacts[0])) == artifact_a_signature

        _seed_transactional_restore_failure(source)
        failure_result = _run_backup(
            name=negative_runners[8],
            network=network,
            source=source,
            backup_root=tmp_path,
            password=backup_password,
            runner_image=runner_image,
        )
        failure_container_artifact = Path(cast(str, failure_result["artifact_path"]))
        failure_artifact = tmp_path / failure_container_artifact.relative_to("/backups")
        _remove_transactional_restore_failure(source)

        _seed_cancellable_restore(source)
        cancellation_result = _run_backup(
            name=negative_runners[9],
            network=network,
            source=source,
            backup_root=tmp_path,
            password=backup_password,
            runner_image=runner_image,
        )
        cancellation_container_artifact = Path(cast(str, cancellation_result["artifact_path"]))
        cancellation_artifact = tmp_path / cancellation_container_artifact.relative_to("/backups")
        _remove_cancellable_restore(source)

        for index, destination in enumerate(destinations):
            _start_restore_destination(
                container=destination,
                volume=destination_volumes[index],
                network=network,
                admin_password=destination_admin_passwords[index],
                restore_password=restore_passwords[index],
            )
            if index == 0:
                with pytest.raises(RuntimeError, match="distinct source and destination targets"):
                    _run_restore(
                        name=f"{restore_runners[index]}-same-target",
                        network=network,
                        destination=destination,
                        backup_root=tmp_path,
                        artifact=artifacts[index],
                        password=restore_passwords[index],
                        source=source,
                        runner_image=runner_image,
                        same_target=True,
                    )
                assert _user_relation_count(destination) == 0

                with pytest.raises(RuntimeError, match="staging identity"):
                    _run_restore(
                        name=f"{restore_runners[index]}-staged-substitution",
                        network=network,
                        destination=destination,
                        backup_root=tmp_path,
                        artifact=artifacts[index],
                        password=restore_passwords[index],
                        source=source,
                        runner_image=runner_image,
                        replace_staged_artifact=True,
                    )
                assert _user_relation_count(destination) == 0

                with pytest.raises(RuntimeError, match="not in the exact allowlist"):
                    _run_restore(
                        name=f"{restore_runners[index]}-external",
                        network=network,
                        destination=destination,
                        backup_root=tmp_path,
                        artifact=artifacts[index],
                        password=restore_passwords[index],
                        source=source,
                        runner_image=runner_image,
                        allowlist_destination="external.invalid",
                    )
                assert _user_relation_count(destination) == 0

                _psql(
                    destination,
                    "postgres",
                    "COMMENT ON DATABASE application_restore IS NULL;",
                )
                with pytest.raises(RuntimeError, match="sentinel"):
                    _run_restore(
                        name=f"{restore_runners[index]}-unsentinel",
                        network=network,
                        destination=destination,
                        backup_root=tmp_path,
                        artifact=artifacts[index],
                        password=restore_passwords[index],
                        source=source,
                        runner_image=runner_image,
                    )
                _psql(
                    destination,
                    "postgres",
                    """
COMMENT ON DATABASE application_restore
  IS 'homelab-backup:postgresql-restore:v1';
""",
                )
                assert _user_relation_count(destination) == 0

                _psql(
                    destination,
                    "application_restore",
                    "CREATE TABLE deliberately_nonfresh (id integer PRIMARY KEY);",
                )
                with pytest.raises(RuntimeError, match="fresh and empty"):
                    _run_restore(
                        name=f"{restore_runners[index]}-nonfresh",
                        network=network,
                        destination=destination,
                        backup_root=tmp_path,
                        artifact=artifacts[index],
                        password=restore_passwords[index],
                        source=source,
                        runner_image=runner_image,
                    )
                _psql(
                    destination,
                    "application_restore",
                    "DROP TABLE deliberately_nonfresh;",
                )
                assert _user_relation_count(destination) == 0

                original_artifact = artifacts[index].read_bytes()
                corrupted_artifact = bytearray(original_artifact)
                corrupted_artifact[-1] ^= 0xFF
                artifacts[index].write_bytes(corrupted_artifact)
                with pytest.raises(RuntimeError, match="artifact|sidecar"):
                    _run_restore(
                        name=f"{restore_runners[index]}-corrupt",
                        network=network,
                        destination=destination,
                        backup_root=tmp_path,
                        artifact=artifacts[index],
                        password=restore_passwords[index],
                        source=source,
                        runner_image=runner_image,
                    )
                artifacts[index].write_bytes(original_artifact)
                assert _user_relation_count(destination) == 0

                sidecar_path = Path(f"{artifacts[index]}.meta.json")
                original_sidecar = sidecar_path.read_bytes()
                altered_sidecar = json.loads(original_sidecar)
                altered_sidecar["plugin_name"] = "not-postgresql"
                sidecar_path.write_text(
                    json.dumps(altered_sidecar, sort_keys=True),
                    encoding="utf-8",
                )
                with pytest.raises(RuntimeError, match="plugin"):
                    _run_restore(
                        name=f"{restore_runners[index]}-wrong-plugin",
                        network=network,
                        destination=destination,
                        backup_root=tmp_path,
                        artifact=artifacts[index],
                        password=restore_passwords[index],
                        source=source,
                        runner_image=runner_image,
                    )
                sidecar_path.write_bytes(original_sidecar)
                assert _user_relation_count(destination) == 0

                altered_sidecar = json.loads(original_sidecar)
                altered_sidecar["archive_catalog_sha256"] = "0" * 64
                sidecar_path.write_text(
                    json.dumps(altered_sidecar, sort_keys=True),
                    encoding="utf-8",
                )
                with pytest.raises(RuntimeError, match="archive catalog"):
                    _run_restore(
                        name=f"{restore_runners[index]}-catalog",
                        network=network,
                        destination=destination,
                        backup_root=tmp_path,
                        artifact=artifacts[index],
                        password=restore_passwords[index],
                        source=source,
                        runner_image=runner_image,
                    )
                sidecar_path.write_bytes(original_sidecar)
                assert _user_relation_count(destination) == 0

                with pytest.raises(RuntimeError, match="pg_restore failed"):
                    _run_restore(
                        name=f"{restore_runners[index]}-transaction-failure",
                        network=network,
                        destination=destination,
                        backup_root=tmp_path,
                        artifact=failure_artifact,
                        password=restore_passwords[index],
                        source=source,
                        runner_image=runner_image,
                    )
                assert _user_relation_count(destination) == 0
                assert (
                    _psql(
                        destination,
                        "application_restore",
                        "SELECT string_agg(extname, ',' ORDER BY extname) FROM pg_extension;",
                    )
                    == "plpgsql"
                )

                cancellation = _run_cancelled_restore(
                    name=f"{restore_runners[index]}-cancelled",
                    network=network,
                    destination=destination,
                    backup_root=tmp_path,
                    artifact=cancellation_artifact,
                    password=restore_passwords[index],
                    source=source,
                    runner_image=runner_image,
                )
                assert cancellation == {
                    "cancelled": True,
                    "child_count": 0,
                    "pgpass_count": 0,
                }
                assert _user_relation_count(destination) == 0
                assert (
                    _psql(
                        destination,
                        "application_restore",
                        """
SELECT count(*) FROM pg_stat_activity
WHERE datname = current_database()
  AND usename = 'restore_owner'
  AND pid <> pg_backend_pid();
""",
                    )
                    == "0"
                )
                for negative_artifact in (failure_artifact, cancellation_artifact):
                    negative_artifact.unlink()
                    Path(f"{negative_artifact}.meta.json").unlink()

            restored = _run_restore(
                name=restore_runners[index],
                network=network,
                destination=destination,
                backup_root=tmp_path,
                artifact=artifacts[index],
                password=restore_passwords[index],
                source=source,
                runner_image=runner_image,
            )
            assert restored["status"] == "success"
            assert restored["target_status"] == "success"
            assert restored["artifact_bytes"] == artifacts[index].stat().st_size
            assert restored["sha256"] == _sha256(artifacts[index])
            assert restored["artifact_path"] == "/backups/" + str(
                artifacts[index].relative_to(tmp_path)
            )
            assert not list(artifacts[index].parent.glob(".homelab-backup-restore-*"))

        expected_a = {
            "accounts": [{"id": 1, "phase": "A", "display_name": "Synthetic account A"}],
            "entries": [
                {
                    "id": 1,
                    "account_id": 1,
                    "payload": "Synthetic relational payload A",
                    "attachment_oid": 91001,
                }
            ],
            "large_objects": {"91001": "70686173652d612d6c617267652d6f626a656374"},
            "sequence": 1,
            "extension": "1.6",
            "foreign_key": {
                "validated": True,
                "definition": "FOREIGN KEY (account_id) REFERENCES accounts(id)",
            },
        }
        expected_b = {
            "accounts": [
                {"id": 1, "phase": "A", "display_name": "Synthetic account A"},
                {"id": 2, "phase": "B", "display_name": "Synthetic account B"},
            ],
            "entries": [
                {
                    "id": 1,
                    "account_id": 1,
                    "payload": "Synthetic relational payload A",
                    "attachment_oid": 91001,
                },
                {
                    "id": 2,
                    "account_id": 2,
                    "payload": "Synthetic relational payload B",
                    "attachment_oid": 91002,
                },
            ],
            "large_objects": {
                "91001": "70686173652d612d6c617267652d6f626a656374",
                "91002": "70686173652d622d6c617267652d6f626a656374",
            },
            "sequence": 2,
            "extension": "1.6",
            "foreign_key": {
                "validated": True,
                "definition": "FOREIGN KEY (account_id) REFERENCES accounts(id)",
            },
        }
        assert _restored_state(destinations[0]) == expected_a
        assert _restored_state(destinations[1]) == expected_b
        for destination, expected in zip(destinations, (expected_a, expected_b), strict=True):
            _docker("restart", destination)
            _wait_for_postgresql(destination)
            assert _restored_state(destination) == expected
