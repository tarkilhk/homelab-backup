from __future__ import annotations

import asyncio
import importlib
import json
import stat
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.core.plugins.base import BackupContext, RestoreContext
from app.core.plugins.loader import get_plugin, get_plugin_schema_path, list_plugins
from app.core.plugins.sidecar import read_backup_sidecar
from app.main import app

HINDSIGHT_VERSION = "0.8.6"
POSTGRES_SERVER_VERSION_NUM = 180006
VECTOR_VERSION = "0.8.6"
PG_TRGM_VERSION = "1.6"
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


@pytest.fixture
def anyio_backend() -> tuple[str, dict[str, bool]]:
    return ("asyncio", {"use_uvloop": True})


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


def _write_process_output(kwargs: dict[str, Any], stream: str, payload: bytes) -> None:
    target = kwargs.get(stream)
    assert target is not None and hasattr(target, "write")
    target.write(payload)


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
        self.started.set()
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
                "pg_trgm_version": PG_TRGM_VERSION,
                "alembic_heads": [ALEMBIC_HEAD],
                "tables": sorted(REQUIRED_TABLES),
                "rls_tables": [],
                "invalid_indexes": [],
                "invalid_constraints": [],
            }
        )
        + "\n"
    ).encode()


def _toc_bytes(*, missing: str | None = None, unexpected: str | None = None) -> bytes:
    lines = [
        "; PostgreSQL database dump",
        "; Dumped from database version: 18.6",
        "; Dumped by pg_dump version: 18.6",
        "1; 3079 16385 EXTENSION - vector",
        "2; 0 0 COMMENT - EXTENSION vector",
        "3; 3079 16386 EXTENSION - pg_trgm",
        "4; 0 0 COMMENT - EXTENSION pg_trgm",
        "5; 2615 2200 SCHEMA - public hindsight",
    ]
    object_id = 100
    for table in sorted(REQUIRED_TABLES - ({missing} if missing else set())):
        lines.append(f"{object_id}; 1259 {object_id} TABLE public {table} hindsight")
        lines.append(f"{object_id + 1}; 0 {object_id} TABLE DATA public {table} hindsight")
        object_id += 2
    if unexpected is not None:
        lines.append(f"{object_id}; 1259 {object_id} TABLE public {unexpected} hindsight")
        lines.append(f"{object_id + 1}; 0 {object_id} TABLE DATA public {unexpected} hindsight")
    for index_kind in ("expr", "obsv", "worl"):
        object_id += 2
        lines.append(
            f"{object_id}; 1259 {object_id} "
            f"INDEX public idx_mu_emb_{index_kind}_0123456789abcdef hindsight"
        )
    return ("\n".join(lines) + "\n").encode()


def _destination_toc_bytes(*, unexpected: str | None = None) -> bytes:
    lines = [
        "; PostgreSQL database dump",
        "; Dumped from database version: 18.6",
        "; Dumped by pg_dump version: 18.6",
        "1; 3079 16385 EXTENSION - vector",
        "2; 0 0 COMMENT - EXTENSION vector",
    ]
    if unexpected is not None:
        lines.append(f"3; 1255 16386 FUNCTION public {unexpected}() synthetic_owner")
    return ("\n".join(lines) + "\n").encode()


@pytest.fixture(autouse=True)
def _use_synthetic_toc_fingerprint(monkeypatch: pytest.MonkeyPatch) -> None:
    hindsight_module = _plugin_module()
    monkeypatch.setattr(
        hindsight_module,
        "EXPECTED_TOC_FINGERPRINT",
        hindsight_module._archive_toc_fingerprint(_toc_bytes()),
    )
    monkeypatch.setattr(
        hindsight_module,
        "EXPECTED_EMPTY_DESTINATION_TOC_FINGERPRINT",
        hindsight_module._archive_toc_fingerprint(_destination_toc_bytes()),
    )


def test_toc_fingerprint_normalizes_only_exact_migration_index_suffixes() -> None:
    hindsight_module = _plugin_module()
    first = _toc_bytes()
    second = first.replace(b"0123456789abcdef", b"fedcba9876543210")

    assert hindsight_module._archive_toc_fingerprint(first) == (
        hindsight_module._archive_toc_fingerprint(second)
    )

    second_bank = first + b"".join(
        f"{900 + index}; 1259 {900 + index} INDEX public "
        f"idx_mu_emb_{kind}_fedcba9876543210 hindsight\n".encode()
        for index, kind in enumerate(("expr", "obsv", "worl"))
    )
    assert hindsight_module._archive_toc_fingerprint(first) == (
        hindsight_module._archive_toc_fingerprint(second_bank)
    )

    incomplete_bank = first + b"".join(
        f"{910 + index}; 1259 {910 + index} INDEX public "
        f"idx_mu_emb_{kind}_aaaaaaaaaaaaaaaa hindsight\n".encode()
        for index, kind in enumerate(("expr", "obsv"))
    )
    with pytest.raises(RuntimeError, match="malformed"):
        hindsight_module._archive_toc_fingerprint(incomplete_bank)

    malformed = first.replace(b"0123456789abcdef", b"not-a-migration-id")
    with pytest.raises(RuntimeError, match="malformed"):
        hindsight_module._archive_toc_fingerprint(malformed)


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
            _write_process_output(kwargs, "stdout", _source_fingerprint_bytes())
            return _CompletedProcess()

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
        _write_process_output(kwargs, "stdout", self.toc)
        return _CompletedProcess(returncode=self.toc_returncode)


def _destination_fingerprint_bytes(
    *,
    database_comment: str = "homelab-backup:hindsight-restore:v1",
    tables: list[str] | None = None,
    vector_version: str = VECTOR_VERSION,
) -> bytes:
    return (
        json.dumps(
            {
                "server_version_num": POSTGRES_SERVER_VERSION_NUM,
                "database": DESTINATION_CONFIG["database"],
                "database_comment": database_comment,
                "vector_version": vector_version,
                "pg_trgm_version": None,
                "tables": [] if tables is None else tables,
                "views": [],
                "sequences": [],
            }
        )
        + "\n"
    ).encode()


def _restore_context(
    artifact_path: Path,
    *,
    config: dict[str, object] | None = None,
    source_target_id: str = "hindsight-source-id",
    destination_target_id: str = "hindsight-restore-id",
) -> RestoreContext:
    return RestoreContext(
        job_id="hindsight-restore-job",
        source_target_id=source_target_id,
        destination_target_id=destination_target_id,
        config=dict(config or DESTINATION_CONFIG),
        artifact_path=str(artifact_path),
        metadata={"source_target_slug": "hindsight-source"},
    )


def _write_restore_artifact(path: Path, payload: bytes | None = None) -> Path:
    path.write_bytes(payload or b"PGDMP\x01\x0f synthetic Hindsight restore archive")
    path.chmod(0o600)
    return path


class _RestoreBoundary:
    def __init__(
        self,
        *,
        toc: bytes | None = None,
        destination_toc: bytes | None = None,
        destination_fingerprint: bytes | None = None,
        restore_returncode: int = 0,
        restore_stderr: bytes = b"",
        block_restore: bool = False,
    ) -> None:
        self.toc = _toc_bytes() if toc is None else toc
        self.destination_toc = (
            _destination_toc_bytes() if destination_toc is None else destination_toc
        )
        self.destination_fingerprint = (
            _destination_fingerprint_bytes()
            if destination_fingerprint is None
            else destination_fingerprint
        )
        self.restore_returncode = restore_returncode
        self.restore_stderr = restore_stderr
        self.blocking_process = _BlockingProcess() if block_restore else None
        self.calls: list[tuple[str, ...]] = []
        self.password_files: list[Path] = []
        self.allowlist_paths: list[Path] = []
        self.allowlist_contents: list[str] = []
        self.restore_sql_paths: list[Path] = []
        self.restore_sql_contents: list[str] = []
        self.destination_schema_paths: list[Path] = []
        self.restore_attempts = 0
        self.restored = False

    def _assert_private_credentials(self, args: tuple[str, ...], kwargs: dict[str, Any]) -> None:
        password = str(DESTINATION_CONFIG["password"])
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

    def _capture_allowlist(self, args: tuple[str, ...]) -> None:
        assert "--use-list" in args
        allowlist = Path(args[args.index("--use-list") + 1])
        self.allowlist_paths.append(allowlist)
        assert allowlist.is_file()
        assert stat.S_IMODE(allowlist.stat().st_mode) == 0o600
        contents = allowlist.read_text(encoding="utf-8")
        self.allowlist_contents.append(contents)
        assert " EXTENSION - vector" not in contents
        assert " COMMENT - EXTENSION vector" not in contents
        assert " EXTENSION - pg_trgm" in contents
        assert " COMMENT - EXTENSION pg_trgm" in contents
        for table in REQUIRED_TABLES:
            assert f" TABLE public {table} " in contents

    async def exec(self, *args: str, **kwargs: Any) -> Any:
        argv = tuple(args)
        self.calls.append(argv)
        assert "shell" not in kwargs
        assert args[0] not in {"bash", "sh", "/bin/bash", "/bin/sh"}

        if args[0] == "psql":
            self._assert_private_credentials(argv, kwargs)
            if "--single-transaction" in argv:
                self.restore_attempts += 1
                assert "--file" in argv
                restore_sql = Path(argv[argv.index("--file") + 1])
                self.restore_sql_paths.append(restore_sql)
                assert restore_sql.is_file()
                assert stat.S_IMODE(restore_sql.stat().st_mode) == 0o600
                restore_sql_text = restore_sql.read_text(encoding="utf-8")
                self.restore_sql_contents.append(restore_sql_text)
                assert "DO $hindsight_validation$" in restore_sql_text
                assert "FROM public.alembic_version" in restore_sql_text
                assert "ANALYZE\n" in restore_sql_text
                assert "public.memory_units_bm25" in restore_sql_text
                assert argv[argv.index("-h") + 1] == str(DESTINATION_CONFIG["host"])
                assert argv[argv.index("-p") + 1] == str(DESTINATION_CONFIG["port"])
                assert argv[argv.index("-U") + 1] == str(DESTINATION_CONFIG["user"])
                assert argv[argv.index("--dbname") + 1] == str(DESTINATION_CONFIG["database"])
                self._write_stderr(kwargs, self.restore_stderr)
                if self.blocking_process is not None:
                    return self.blocking_process
                if self.restore_returncode == 0:
                    self.restored = True
                return _CompletedProcess(returncode=self.restore_returncode)
            sql = " ".join(args).upper()
            forbidden = ("DROP ", "TRUNCATE ", "DELETE ", "CREATE ", "ALTER ")
            assert not any(keyword in sql for keyword in forbidden)
            output = self.destination_fingerprint
            _write_process_output(kwargs, "stdout", output)
            return _CompletedProcess()

        if args[0] == "pg_dump":
            self._assert_private_credentials(argv, kwargs)
            assert "--schema-only" in argv
            assert "--file" in argv
            schema_dump = Path(argv[argv.index("--file") + 1])
            self.destination_schema_paths.append(schema_dump)
            assert schema_dump.is_file()
            assert stat.S_IMODE(schema_dump.stat().st_mode) == 0o600
            schema_dump.write_bytes(b"PGDMP\x01\x10 synthetic empty destination")
            schema_dump.chmod(0o600)
            return _CompletedProcess()

        assert args[0] == "pg_restore"
        if "--list" in args:
            inspected_path = Path(args[-1])
            output = (
                self.destination_toc
                if inspected_path.name.startswith("hindsight-destination-schema-")
                else self.toc
            )
            _write_process_output(kwargs, "stdout", output)
            return _CompletedProcess()

        self._capture_allowlist(argv)
        required_arguments = {
            "--exit-on-error",
            "--no-owner",
            "--no-privileges",
            "--use-list",
            "--file",
        }
        assert required_arguments.issubset(argv)
        assert not {
            "--clean",
            "--create",
            "--if-exists",
            "--disable-triggers",
            "--dbname",
        }.intersection(argv)
        assert argv[-1].endswith(".dump")
        restore_sql = Path(argv[argv.index("--file") + 1])
        assert restore_sql.is_file()
        assert stat.S_IMODE(restore_sql.stat().st_mode) == 0o600
        restore_sql.write_text("-- synthetic pg_restore output\n", encoding="utf-8")
        restore_sql.chmod(0o600)
        return _CompletedProcess()


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


@pytest.mark.anyio
async def test_hindsight_api_exposes_schema_and_secret_safe_connectivity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supplied_password = "synthetic-api-password-must-not-leak"

    async def successful_test(self: Any, config: dict[str, Any]) -> bool:
        if not await self.validate_config(config):
            raise ValueError("Invalid Hindsight configuration")
        assert config["password"] == supplied_password
        return True

    monkeypatch.setattr(_plugin_class(), "test", successful_test)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        plugins_response = await client.get("/api/v1/plugins/")
        schema_response = await client.get("/api/v1/plugins/hindsight/schema")
        test_response = await client.post(
            "/api/v1/plugins/hindsight/test",
            json={**SOURCE_CONFIG, "password": supplied_password},
        )
        invalid_response = await client.post(
            "/api/v1/plugins/hindsight/test",
            json={**SOURCE_CONFIG, "password": supplied_password, "legacy": True},
        )

    assert plugins_response.status_code == 200
    assert any(
        item["key"] == "hindsight" and item["restore_capability"] == "partial"
        for item in plugins_response.json()
    )
    assert schema_response.status_code == 200
    assert set(schema_response.json()["required"]) == {
        "mode",
        "host",
        "database",
        "user",
        "password",
    }
    assert test_response.json() == {"ok": True}
    assert invalid_response.json()["ok"] is False
    assert supplied_password not in invalid_response.text


@pytest.mark.asyncio
async def test_source_connectivity_requires_the_exact_read_only_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    password = str(SOURCE_CONFIG["password"])
    fingerprint = {
        "server_version_num": POSTGRES_SERVER_VERSION_NUM,
        "database": SOURCE_CONFIG["database"],
        "vector_version": VECTOR_VERSION,
        "pg_trgm_version": PG_TRGM_VERSION,
        "alembic_heads": [ALEMBIC_HEAD],
        "tables": sorted(REQUIRED_TABLES),
        "rls_tables": [],
        "invalid_indexes": [],
        "invalid_constraints": [],
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
        _write_process_output(
            kwargs,
            "stdout",
            (json.dumps(fingerprint) + "\n").encode(),
        )
        return _CompletedProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    result = await _plugin_class()(name="hindsight").test(dict(SOURCE_CONFIG))

    assert result is True
    assert calls
    assert password_files
    assert all(not path.exists() for path in password_files)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "message"),
    (
        ("invalid_indexes", "invalid indexes"),
        ("invalid_constraints", "invalid constraints"),
    ),
)
async def test_source_refuses_invalid_schema_objects(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    message: str,
) -> None:
    fingerprint = json.loads(_source_fingerprint_bytes())
    fingerprint[field] = ["synthetic_invalid_object"]

    async def fake_exec(*args: str, **kwargs: Any) -> _CompletedProcess:
        _write_process_output(
            kwargs,
            "stdout",
            (json.dumps(fingerprint) + "\n").encode(),
        )
        return _CompletedProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    with pytest.raises(RuntimeError, match=message):
        await _plugin_class()(name="hindsight").test(dict(SOURCE_CONFIG))


@pytest.mark.asyncio
async def test_source_fingerprint_output_is_bounded_before_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hindsight_module = _plugin_module()
    monkeypatch.setattr(hindsight_module, "MAX_FINGERPRINT_BYTES", 32, raising=False)

    async def fake_exec(*args: str, **kwargs: Any) -> _CompletedProcess:
        _write_process_output(kwargs, "stdout", b"x" * 33)
        return _CompletedProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    with pytest.raises(RuntimeError, match="safety limit"):
        await _plugin_class()(name="hindsight").test(dict(SOURCE_CONFIG))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("returncode", "expected_exception"),
    ((2, ConnectionError), (1, RuntimeError), (3, RuntimeError)),
)
async def test_source_maps_connection_and_command_failures_separately(
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
    expected_exception: type[Exception],
) -> None:
    async def fake_exec(*args: str, **kwargs: Any) -> _CompletedProcess:
        return _CompletedProcess(returncode=returncode)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    with pytest.raises(expected_exception):
        await _plugin_class()(name="hindsight").test(dict(SOURCE_CONFIG))


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
        (
            {
                "toc": _toc_bytes()
                + b"999; 1255 999 FUNCTION public malicious_restore() synthetic_owner\n"
            },
            "schema|exact|version",
        ),
        ({"toc": b"; oversized TOC\n" * 70_000}, "safety|limit|TOC"),
    ),
    ids=(
        "warning",
        "nonzero-dump",
        "nonzero-inspector",
        "malformed-toc",
        "missing-table",
        "unexpected-table",
        "unexpected-function",
        "oversized-toc",
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


@pytest.mark.asyncio
async def test_restore_accepts_only_destination_mode_and_distinct_targets(
    tmp_path: Path,
) -> None:
    artifact = _write_restore_artifact(tmp_path / "hindsight.dump")
    plugin = _plugin_class()(name="hindsight")

    with pytest.raises(ValueError, match="restore.destination|destination mode"):
        await plugin.restore(_restore_context(artifact, config=SOURCE_CONFIG))

    with pytest.raises(ValueError, match="distinct|same|source.*destination"):
        await plugin.restore(
            _restore_context(
                artifact,
                source_target_id="same-target",
                destination_target_id="same-target",
            )
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("destination_fingerprint", "message"),
    (
        (
            _destination_fingerprint_bytes(database_comment="wrong-sentinel"),
            "sentinel|comment",
        ),
        (
            _destination_fingerprint_bytes(tables=["existing_state"]),
            "empty|existing|table",
        ),
        (
            _destination_fingerprint_bytes(vector_version="0.8.5"),
            "vector|version",
        ),
    ),
    ids=("wrong-sentinel", "nonempty", "wrong-vector"),
)
async def test_restore_requires_exact_empty_sentinel_destination_before_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    destination_fingerprint: bytes,
    message: str,
) -> None:
    artifact = _write_restore_artifact(tmp_path / "hindsight.dump")
    boundary = _RestoreBoundary(destination_fingerprint=destination_fingerprint)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", boundary.exec)

    with pytest.raises((RuntimeError, ValueError), match=message):
        await _plugin_class()(name="hindsight").restore(_restore_context(artifact))

    assert boundary.restore_attempts == 0
    assert artifact.read_bytes().startswith(b"PGDMP")
    assert boundary.password_files
    assert all(not path.exists() for path in boundary.password_files)


@pytest.mark.asyncio
async def test_restore_refuses_non_vector_destination_objects_before_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = _write_restore_artifact(tmp_path / "hindsight.dump")
    boundary = _RestoreBoundary(
        destination_toc=_destination_toc_bytes(unexpected="foreign_function")
    )
    monkeypatch.setattr(asyncio, "create_subprocess_exec", boundary.exec)

    with pytest.raises(RuntimeError, match="only pgvector"):
        await _plugin_class()(name="hindsight").restore(_restore_context(artifact))

    assert boundary.restore_attempts == 0
    assert boundary.destination_schema_paths
    assert all(not path.exists() for path in boundary.destination_schema_paths)
    assert all(not path.exists() for path in boundary.password_files)


@pytest.mark.asyncio
async def test_restore_uses_vector_only_allowlist_and_returns_verified_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"PGDMP\x01\x0f immutable Hindsight restore archive"
    artifact = _write_restore_artifact(tmp_path / "hindsight.dump", payload)
    boundary = _RestoreBoundary()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", boundary.exec)
    plugin = _plugin_class()(name="hindsight")

    result = await plugin.restore(_restore_context(artifact))

    assert result["status"] == "success"
    message = str(result["message"]).lower()
    assert "boot" in message
    assert "config" in message or "oauth" in message
    assert plugin.restore_capability == "partial"
    assert boundary.restore_attempts == 1
    assert boundary.restored
    assert artifact.read_bytes() == payload

    inspect_index = next(
        index for index, call in enumerate(boundary.calls) if call[:2] == ("pg_restore", "--list")
    )
    preflight_index = next(index for index, call in enumerate(boundary.calls) if call[0] == "psql")
    render_index = next(
        index
        for index, call in enumerate(boundary.calls)
        if call[0] == "pg_restore" and "--list" not in call
    )
    restore_index = next(
        index
        for index, call in enumerate(boundary.calls)
        if call[0] == "psql" and "--single-transaction" in call
    )
    assert inspect_index < preflight_index < render_index < restore_index
    assert boundary.allowlist_contents
    assert boundary.allowlist_paths
    assert all(not path.exists() for path in boundary.allowlist_paths)
    assert boundary.restore_sql_paths
    assert all(not path.exists() for path in boundary.restore_sql_paths)
    assert boundary.destination_schema_paths
    assert all(not path.exists() for path in boundary.destination_schema_paths)
    assert boundary.password_files
    assert all(not path.exists() for path in boundary.password_files)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("variant", "message"),
    (
        ("missing-file", "not found|missing|artifact"),
        ("corrupt-header", "malformed|archive|header"),
        ("missing-table", "file_storage|missing|schema"),
        ("unexpected-table", "unexpected|schema|table"),
    ),
)
async def test_restore_refuses_untrusted_archive_before_destination_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    variant: str,
    message: str,
) -> None:
    artifact = tmp_path / "hindsight.dump"
    toc = _toc_bytes()
    if variant == "missing-file":
        pass
    elif variant == "corrupt-header":
        _write_restore_artifact(artifact, b"not a PostgreSQL archive")
    else:
        _write_restore_artifact(artifact)
        if variant == "missing-table":
            toc = _toc_bytes(missing="file_storage")
        else:
            toc = _toc_bytes(unexpected="unresearched_state")
    boundary = _RestoreBoundary(toc=toc)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", boundary.exec)

    with pytest.raises((FileNotFoundError, RuntimeError, ValueError), match=message):
        await _plugin_class()(name="hindsight").restore(_restore_context(artifact))

    assert boundary.restore_attempts == 0
    assert not boundary.restored
    assert all(not path.exists() for path in boundary.password_files)


@pytest.mark.asyncio
async def test_restore_transaction_failure_preserves_destination_and_cleans_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"PGDMP\x01\x0f immutable failed-restore archive"
    artifact = _write_restore_artifact(tmp_path / "hindsight.dump", payload)
    secret = str(DESTINATION_CONFIG["password"])
    boundary = _RestoreBoundary(
        restore_returncode=1,
        restore_stderr=f"transaction aborted near {secret}".encode(),
    )
    monkeypatch.setattr(asyncio, "create_subprocess_exec", boundary.exec)

    with pytest.raises(RuntimeError, match="restore|transaction") as error:
        await _plugin_class()(name="hindsight").restore(_restore_context(artifact))

    assert secret not in str(error.value)
    assert boundary.restore_attempts == 1
    assert not boundary.restored
    assert artifact.read_bytes() == payload
    assert all(
        not {"--clean", "--create", "--if-exists", "--disable-triggers"}.intersection(call)
        for call in boundary.calls
    )
    assert boundary.allowlist_paths
    assert all(not path.exists() for path in boundary.allowlist_paths)
    assert boundary.restore_sql_contents
    assert "DO $hindsight_validation$" in boundary.restore_sql_contents[0]
    assert boundary.restore_sql_paths
    assert all(not path.exists() for path in boundary.restore_sql_paths)
    assert boundary.password_files
    assert all(not path.exists() for path in boundary.password_files)


@pytest.mark.asyncio
async def test_restore_timeout_reaps_child_and_cleans_ephemeral_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"PGDMP\x01\x0f immutable timeout archive"
    artifact = _write_restore_artifact(tmp_path / "hindsight.dump", payload)
    hindsight_module = _plugin_module()
    boundary = _RestoreBoundary(block_restore=True)
    monkeypatch.setattr(hindsight_module, "RESTORE_TIMEOUT_SECONDS", 0.01, raising=False)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", boundary.exec)

    with pytest.raises((RuntimeError, TimeoutError), match="timed out"):
        await _plugin_class()(name="hindsight").restore(_restore_context(artifact))

    process = boundary.blocking_process
    assert process is not None
    assert process.terminated or process.killed
    assert process.reaped
    assert not boundary.restored
    assert artifact.read_bytes() == payload
    assert boundary.allowlist_paths
    assert all(not path.exists() for path in boundary.allowlist_paths)
    assert boundary.restore_sql_paths
    assert all(not path.exists() for path in boundary.restore_sql_paths)
    assert boundary.password_files
    assert all(not path.exists() for path in boundary.password_files)


@pytest.mark.asyncio
async def test_restore_cancellation_reaps_child_and_cleans_ephemeral_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"PGDMP\x01\x0f immutable cancelled archive"
    artifact = _write_restore_artifact(tmp_path / "hindsight.dump", payload)
    boundary = _RestoreBoundary(block_restore=True)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", boundary.exec)
    task = asyncio.create_task(
        _plugin_class()(name="hindsight").restore(_restore_context(artifact))
    )
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
    assert not boundary.restored
    assert artifact.read_bytes() == payload
    assert boundary.allowlist_paths
    assert all(not path.exists() for path in boundary.allowlist_paths)
    assert boundary.restore_sql_paths
    assert all(not path.exists() for path in boundary.restore_sql_paths)
    assert boundary.password_files
    assert all(not path.exists() for path in boundary.password_files)
