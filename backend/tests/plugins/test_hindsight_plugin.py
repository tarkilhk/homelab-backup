from __future__ import annotations

import asyncio
import importlib
import json
import stat
from pathlib import Path
from typing import Any

import pytest

from app.core.plugins.base import BackupContext
from app.core.plugins.loader import get_plugin, get_plugin_schema_path, list_plugins
from app.core.plugins.sidecar import read_backup_sidecar

HINDSIGHT_VERSION = "0.8.6"
POSTGRES_SERVER_VERSION_NUM = 180006
VECTOR_VERSION = "0.8.6"
ALEMBIC_HEAD = "c7d1e9a4b3f2"
REQUIRED_TABLES = frozenset(
    {
        "alembic_version",
        "async_operations",
        "audit_log",
        "bank_stats_cache",
        "banks",
        "chunks",
        "directives",
        "documents",
        "entities",
        "entity_cooccurrences",
        "file_storage",
        "graph_maintenance_queue",
        "invalidated_memory_units",
        "llm_requests",
        "memory_links",
        "memory_units",
        "mental_model_history",
        "mental_models",
        "observation_history",
        "unit_entities",
        "webhooks",
    }
)

SOURCE_CONFIG: dict[str, object] = {
    "mode": "source",
    "host": "hindsight-db.local",
    "port": 5432,
    "database": "hindsight_local",
    "user": "hindsight_backup",
    "password": "synthetic-source-password",
}

DESTINATION_CONFIG: dict[str, object] = {
    "mode": "restore_destination",
    "host": "postgres-restore.local",
    "port": 5432,
    "database": "hlb_hindsight_restore_alpha",
    "user": "hindsight_restore_owner",
    "password": "synthetic-destination-password",
}


def _plugin_module() -> Any:
    return importlib.import_module("app.plugins.hindsight.plugin")


def _plugin_class() -> type[Any]:
    return _plugin_module().HindsightPlugin


class _CompletedProcess:
    def __init__(
        self,
        *,
        returncode: int = 0,
        stdout: bytes = b"",
        stderr: bytes = b"",
        stdout_stream: Any = None,
    ) -> None:
        self.returncode: int | None = returncode
        self._stdout = stdout
        self._stderr = stderr
        self.stdout = stdout_stream
        self.killed = False

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr

    async def wait(self) -> int:
        assert self.returncode is not None
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


class _ArtifactStream:
    def __init__(self, backup_root: Path, *chunks: bytes) -> None:
        self._backup_root = backup_root
        self._chunks = list(chunks)
        self.observed_private_first_byte = False

    async def read(self, size: int) -> bytes:
        assert 0 < size <= 1024 * 1024
        if self._chunks and not self.observed_private_first_byte:
            pending = [
                path
                for path in self._backup_root.rglob("*.tmp")
                if path.is_file() and path.name.startswith(".")
            ]
            assert len(pending) == 1
            assert stat.S_IMODE(pending[0].stat().st_mode) == 0o600
            self.observed_private_first_byte = True
        return self._chunks.pop(0) if self._chunks else b""


class _BlockingProcess:
    def __init__(self) -> None:
        self.returncode: int | None = None
        self.stdout = self
        self.started = asyncio.Event()
        self.released = asyncio.Event()
        self.terminated = False
        self.killed = False
        self.reaped = False

    async def read(self, size: int) -> bytes:
        assert 0 < size <= 1024 * 1024
        self.started.set()
        await self.released.wait()
        return b""

    async def communicate(self) -> tuple[bytes, bytes]:
        self.started.set()
        await self.released.wait()
        return b"", b""

    async def wait(self) -> int:
        if self.returncode is None:
            await self.released.wait()
        self.reaped = True
        assert self.returncode is not None
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15
        self.released.set()

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9
        self.released.set()


def _source_fingerprint_bytes() -> bytes:
    return (
        json.dumps(
            {
                "server_version_num": POSTGRES_SERVER_VERSION_NUM,
                "database": SOURCE_CONFIG["database"],
                "vector_version": VECTOR_VERSION,
                "alembic_heads": [ALEMBIC_HEAD],
                "tables": sorted(REQUIRED_TABLES),
                "rls_tables": [],
            }
        )
        + "\n"
    ).encode()


def _toc_bytes(*, missing: str | None = None, unexpected: str | None = None) -> bytes:
    lines = [
        "; PostgreSQL database dump",
        "; Dumped from database version 18.6",
        "1; 3079 16385 EXTENSION - vector",
        "2; 0 0 COMMENT - EXTENSION vector",
        "3; 2615 2200 SCHEMA - public hindsight",
    ]
    object_id = 100
    for table in sorted(REQUIRED_TABLES - ({missing} if missing else set())):
        lines.append(f"{object_id}; 1259 {object_id} TABLE public {table} hindsight")
        lines.append(f"{object_id + 1}; 0 {object_id} TABLE DATA public {table} hindsight")
        object_id += 2
    if unexpected is not None:
        lines.append(f"{object_id}; 1259 {object_id} TABLE public {unexpected} hindsight")
        lines.append(f"{object_id + 1}; 0 {object_id} TABLE DATA public {unexpected} hindsight")
    return ("\n".join(lines) + "\n").encode()


def _backup_context(config: dict[str, object] | None = None) -> BackupContext:
    return BackupContext(
        job_id="hindsight-backup-job",
        target_id="hindsight-source-id",
        config=dict(config or SOURCE_CONFIG),
        metadata={"target_slug": "hindsight-source"},
    )


class _BackupBoundary:
    def __init__(
        self,
        backup_root: Path,
        *,
        dump_returncode: int = 0,
        dump_stderr: bytes = b"",
        toc_returncode: int = 0,
        toc: bytes | None = None,
        block_dump: bool = False,
    ) -> None:
        self.backup_root = backup_root
        self.dump_returncode = dump_returncode
        self.dump_stderr = dump_stderr
        self.toc_returncode = toc_returncode
        self.toc = _toc_bytes() if toc is None else toc
        self.blocking_process = _BlockingProcess() if block_dump else None
        self.calls: list[tuple[str, ...]] = []
        self.password_files: list[Path] = []
        self.artifact_streams: list[_ArtifactStream] = []

    def _assert_private_credentials(self, args: tuple[str, ...], kwargs: dict[str, Any]) -> None:
        password = str(SOURCE_CONFIG["password"])
        assert password not in args
        env = kwargs["env"]
        assert "PGPASSWORD" not in env
        password_file = Path(env["PGPASSFILE"])
        self.password_files.append(password_file)
        assert password_file.is_file()
        assert stat.S_IMODE(password_file.stat().st_mode) == 0o600
        assert password in password_file.read_text(encoding="utf-8")

    @staticmethod
    def _write_stderr(kwargs: dict[str, Any], payload: bytes) -> None:
        target = kwargs.get("stderr")
        if payload and target is not None and hasattr(target, "write"):
            target.write(payload)

    async def exec(self, *args: str, **kwargs: Any) -> Any:
        argv = tuple(args)
        self.calls.append(argv)
        assert "shell" not in kwargs
        assert args[0] not in {"bash", "sh", "/bin/bash", "/bin/sh"}

        if args[0] == "psql":
            self._assert_private_credentials(argv, kwargs)
            return _CompletedProcess(stdout=_source_fingerprint_bytes())

        if args[0] == "pg_dump":
            self._assert_private_credentials(argv, kwargs)
            assert argv == (
                "pg_dump",
                "-h",
                str(SOURCE_CONFIG["host"]),
                "-p",
                str(SOURCE_CONFIG["port"]),
                "-U",
                str(SOURCE_CONFIG["user"]),
                "--format=custom",
                "--no-owner",
                "--no-privileges",
                str(SOURCE_CONFIG["database"]),
            )
            self._write_stderr(kwargs, self.dump_stderr)
            if self.blocking_process is not None:
                return self.blocking_process
            stream = _ArtifactStream(
                self.backup_root,
                b"PGDMP\x01\x0f synthetic Hindsight archive ",
                b"with native file bytes",
            )
            self.artifact_streams.append(stream)
            return _CompletedProcess(
                returncode=self.dump_returncode,
                stderr=self.dump_stderr,
                stdout_stream=stream,
            )

        assert args[0] == "pg_restore"
        assert "--list" in args
        assert str(SOURCE_CONFIG["password"]) not in args
        self._write_stderr(kwargs, b"invalid archive" if self.toc_returncode else b"")
        return _CompletedProcess(
            returncode=self.toc_returncode,
            stdout=self.toc,
            stderr=b"invalid archive" if self.toc_returncode else b"",
        )


@pytest.mark.asyncio
async def test_hindsight_discovery_schema_and_partial_restore_contract() -> None:
    plugin_class = _plugin_class()
    plugin = get_plugin("hindsight")

    assert isinstance(plugin, plugin_class)
    assert plugin.restore_capability == "partial"
    assert any(
        item["key"] == "hindsight" and item["restore_capability"] == "partial"
        for item in list_plugins()
    )

    schema_path = get_plugin_schema_path("hindsight")
    assert schema_path is not None
    schema = json.loads(Path(schema_path).read_text(encoding="utf-8"))
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {
        "mode",
        "host",
        "database",
        "user",
        "password",
    }
    assert set(schema["properties"]) == {
        "mode",
        "host",
        "port",
        "database",
        "user",
        "password",
    }
    assert schema["properties"]["mode"]["enum"] == [
        "source",
        "restore_destination",
    ]
    assert schema["properties"]["mode"]["default"] == "source"
    assert schema["properties"]["port"] == {
        "type": "integer",
        "title": "Port",
        "default": 5432,
        "minimum": 1,
        "maximum": 65535,
    }
    assert "default" not in schema["properties"]["password"]


@pytest.mark.asyncio
async def test_hindsight_configuration_is_strict_and_mode_aware() -> None:
    plugin = _plugin_class()(name="hindsight")

    assert await plugin.validate_config(dict(SOURCE_CONFIG)) is True
    assert await plugin.validate_config(dict(DESTINATION_CONFIG)) is True

    invalid_configs: tuple[object, ...] = (
        None,
        {},
        {**SOURCE_CONFIG, "mode": "legacy"},
        {**SOURCE_CONFIG, "host": "postgresql://user:secret@db.local/hindsight"},
        {**SOURCE_CONFIG, "host": "https://db.local"},
        {**SOURCE_CONFIG, "host": "db.local\nmalicious"},
        {**SOURCE_CONFIG, "port": True},
        {**SOURCE_CONFIG, "port": 0},
        {**SOURCE_CONFIG, "port": 65536},
        {**SOURCE_CONFIG, "database": ""},
        {**SOURCE_CONFIG, "database": "unsafe/name"},
        {**SOURCE_CONFIG, "user": "  "},
        {**SOURCE_CONFIG, "password": ""},
        {**SOURCE_CONFIG, "unexpected": "compatibility-fallback"},
    )
    for config in invalid_configs:
        assert await plugin.validate_config(config) is False


@pytest.mark.asyncio
async def test_source_connectivity_requires_the_exact_read_only_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    password = str(SOURCE_CONFIG["password"])
    fingerprint = {
        "server_version_num": POSTGRES_SERVER_VERSION_NUM,
        "database": SOURCE_CONFIG["database"],
        "vector_version": VECTOR_VERSION,
        "alembic_heads": [ALEMBIC_HEAD],
        "tables": sorted(REQUIRED_TABLES),
        "rls_tables": [],
    }
    calls: list[tuple[str, ...]] = []
    password_files: list[Path] = []

    async def fake_exec(*args: str, **kwargs: Any) -> _CompletedProcess:
        calls.append(tuple(args))
        assert args[0] == "psql"
        assert "-X" in args
        assert "-tA" in args
        assert password not in args
        assert not any(
            keyword in " ".join(args).upper()
            for keyword in ("INSERT ", "UPDATE ", "DELETE ", "CREATE ", "ALTER ", "DROP ")
        )

        env = kwargs["env"]
        assert "PGPASSWORD" not in env
        password_file = Path(env["PGPASSFILE"])
        password_files.append(password_file)
        assert password_file.is_file()
        assert stat.S_IMODE(password_file.stat().st_mode) == 0o600
        assert password in password_file.read_text(encoding="utf-8")
        return _CompletedProcess(stdout=(json.dumps(fingerprint) + "\n").encode())

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    result = await _plugin_class()(name="hindsight").test(dict(SOURCE_CONFIG))

    assert result is True
    assert calls
    assert password_files
    assert all(not path.exists() for path in password_files)


@pytest.mark.asyncio
async def test_backup_accepts_only_source_mode() -> None:
    plugin = _plugin_class()(name="hindsight")

    with pytest.raises(ValueError, match="source"):
        await plugin.backup(_backup_context(DESTINATION_CONFIG))


@pytest.mark.asyncio
async def test_backup_publishes_unique_private_strict_archives_and_sidecars(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backup_root = tmp_path / "backups"
    hindsight_module = _plugin_module()
    boundary = _BackupBoundary(backup_root)
    monkeypatch.setattr(hindsight_module, "BACKUP_BASE_PATH", str(backup_root), raising=False)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", boundary.exec)
    plugin = _plugin_class()(name="hindsight")

    first_result = await plugin.backup(_backup_context())
    second_result = await plugin.backup(_backup_context())

    first_path = Path(first_result["artifact_path"])
    second_path = Path(second_result["artifact_path"])
    assert first_path != second_path
    assert first_path.name.startswith("hindsight-postgresql-")
    assert first_path.suffix == ".dump"
    for artifact_path in (first_path, second_path):
        assert artifact_path.is_file()
        assert not artifact_path.is_symlink()
        assert stat.S_IMODE(artifact_path.stat().st_mode) == 0o600
        assert artifact_path.read_bytes() == (
            b"PGDMP\x01\x0f synthetic Hindsight archive with native file bytes"
        )
        sidecar = read_backup_sidecar(str(artifact_path))
        assert sidecar is not None
        assert sidecar["plugin_name"] == "hindsight"
        assert sidecar["target_slug"] == "hindsight-source"
        assert Path(sidecar["artifact_path"]) == artifact_path

    assert len([call for call in boundary.calls if call[0] == "pg_dump"]) == 2
    assert len([call for call in boundary.calls if call[:2] == ("pg_restore", "--list")]) == 2
    assert boundary.artifact_streams
    assert all(stream.observed_private_first_byte for stream in boundary.artifact_streams)
    assert boundary.password_files
    assert all(not path.exists() for path in boundary.password_files)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("boundary_options", "message"),
    (
        ({"dump_stderr": b"pg_dump: warning: synthetic warning"}, "warning|stderr"),
        ({"dump_returncode": 1, "dump_stderr": b"synthetic failure"}, "pg_dump|dump"),
        ({"toc_returncode": 1}, "archive|TOC|pg_restore"),
        ({"toc": b"not a PostgreSQL TOC\x00"}, "archive|TOC|malformed"),
        ({"toc": _toc_bytes(missing="file_storage")}, "file_storage|missing|schema"),
        ({"toc": _toc_bytes(unexpected="unresearched_state")}, "unexpected|schema|table"),
    ),
    ids=(
        "warning",
        "nonzero-dump",
        "nonzero-inspector",
        "malformed-toc",
        "missing-table",
        "unexpected-table",
    ),
)
async def test_backup_refuses_untrustworthy_dump_or_toc_without_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary_options: dict[str, Any],
    message: str,
) -> None:
    backup_root = tmp_path / "backups"
    hindsight_module = _plugin_module()
    boundary = _BackupBoundary(backup_root, **boundary_options)
    monkeypatch.setattr(hindsight_module, "BACKUP_BASE_PATH", str(backup_root), raising=False)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", boundary.exec)

    with pytest.raises(RuntimeError, match=message):
        await _plugin_class()(name="hindsight").backup(_backup_context())

    assert not [path for path in backup_root.rglob("*") if path.is_file()]
    assert boundary.password_files
    assert all(not path.exists() for path in boundary.password_files)


@pytest.mark.asyncio
async def test_backup_timeout_reaps_child_and_removes_every_partial_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backup_root = tmp_path / "backups"
    hindsight_module = _plugin_module()
    boundary = _BackupBoundary(backup_root, block_dump=True)
    monkeypatch.setattr(hindsight_module, "BACKUP_BASE_PATH", str(backup_root), raising=False)
    monkeypatch.setattr(hindsight_module, "BACKUP_TIMEOUT_SECONDS", 0.01, raising=False)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", boundary.exec)

    with pytest.raises((RuntimeError, TimeoutError), match="timed out"):
        await _plugin_class()(name="hindsight").backup(_backup_context())

    process = boundary.blocking_process
    assert process is not None
    assert process.terminated or process.killed
    assert process.reaped
    assert not [path for path in backup_root.rglob("*") if path.is_file()]
    assert boundary.password_files
    assert all(not path.exists() for path in boundary.password_files)


@pytest.mark.asyncio
async def test_backup_cancellation_reaps_child_and_removes_every_partial_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backup_root = tmp_path / "backups"
    hindsight_module = _plugin_module()
    boundary = _BackupBoundary(backup_root, block_dump=True)
    monkeypatch.setattr(hindsight_module, "BACKUP_BASE_PATH", str(backup_root), raising=False)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", boundary.exec)
    task = asyncio.create_task(_plugin_class()(name="hindsight").backup(_backup_context()))
    process = boundary.blocking_process
    assert process is not None
    started_task = asyncio.create_task(process.started.wait())
    completed, _ = await asyncio.wait(
        {task, started_task},
        timeout=1.0,
        return_when=asyncio.FIRST_COMPLETED,
    )
    if task in completed:
        started_task.cancel()
        await task
    assert started_task in completed

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert process.terminated or process.killed
    assert process.reaped
    assert not [path for path in backup_root.rglob("*") if path.is_file()]
    assert boundary.password_files
    assert all(not path.exists() for path in boundary.password_files)
