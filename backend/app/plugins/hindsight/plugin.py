"""Exact Hindsight 0.8.6 PostgreSQL backup contract."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Dict

from app.core.plugins.artifacts import create_backup_artifact, evict_file_cache
from app.core.plugins.base import BackupContext, BackupPlugin, RestoreContext
from app.core.subprocesses import run_process_with_timeout

POSTGRES_SERVER_MAJOR = 18
VECTOR_VERSION = "0.8.6"
PG_TRGM_VERSION = "1.6"
ALEMBIC_HEAD = "c7d1e9a4b3f2"
CONNECT_TIMEOUT_SECONDS = 30.0
BACKUP_TIMEOUT_SECONDS = 3600.0
BACKUP_BASE_PATH = "/backups"
STREAM_CHUNK_BYTES = 1024 * 1024
FILE_CACHE_FLUSH_BYTES = 8 * 1024 * 1024
MAX_TOC_BYTES = 1024 * 1024
MAX_FINGERPRINT_BYTES = 256 * 1024
MAX_TOC_ENTRIES = 1000
EXPECTED_TOC_FINGERPRINT = "598c1d07af2dbd181c1727deaaf9058f4a96c0ff9e741404dbfb91d05726d5f9"
EXPECTED_EMPTY_DESTINATION_TOC_FINGERPRINT = (
    "63eaeb9854682e86d1dab0093596621e4d747f9a1b8328b94b1aa7dac4cc8c00"
)
RESTORE_DATABASE_PREFIX = "hlb_hindsight_restore_"
RESTORE_SENTINEL = "homelab-backup:hindsight-restore:v1"
RESTORE_TIMEOUT_SECONDS = 3600.0
_SAFE_HOST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$-]*$")

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

_SOURCE_FINGERPRINT_SQL = """
SELECT json_build_object(
  'server_version_num', current_setting('server_version_num')::integer,
  'database', current_database(),
  'vector_version', (
    SELECT extversion FROM pg_extension WHERE extname = 'vector'
  ),
  'pg_trgm_version', (
    SELECT extversion FROM pg_extension WHERE extname = 'pg_trgm'
  ),
  'alembic_heads', (
    SELECT COALESCE(json_agg(version_num ORDER BY version_num), '[]'::json)
    FROM alembic_version
  ),
  'tables', (
    SELECT COALESCE(json_agg(tablename ORDER BY tablename), '[]'::json)
    FROM pg_tables WHERE schemaname = 'public'
  ),
  'rls_tables', (
    SELECT COALESCE(json_agg(c.relname ORDER BY c.relname), '[]'::json)
    FROM pg_class AS c
    JOIN pg_namespace AS n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public' AND c.relkind = 'r' AND c.relrowsecurity
  ),
  'invalid_indexes', (
    SELECT COALESCE(json_agg(index_class.relname ORDER BY index_class.relname), '[]'::json)
    FROM pg_index AS index_state
    JOIN pg_class AS index_class ON index_class.oid = index_state.indexrelid
    JOIN pg_class AS table_class ON table_class.oid = index_state.indrelid
    JOIN pg_namespace AS table_namespace ON table_namespace.oid = table_class.relnamespace
    WHERE table_namespace.nspname = 'public' AND NOT index_state.indisvalid
  ),
  'invalid_constraints', (
    SELECT COALESCE(json_agg(constraint_state.conname ORDER BY constraint_state.conname), '[]'::json)
    FROM pg_constraint AS constraint_state
    JOIN pg_namespace AS constraint_namespace
      ON constraint_namespace.oid = constraint_state.connamespace
    WHERE constraint_namespace.nspname = 'public' AND NOT constraint_state.convalidated
  )
);
""".strip()

_DESTINATION_FINGERPRINT_SQL = """
SELECT json_build_object(
  'server_version_num', current_setting('server_version_num')::integer,
  'database', current_database(),
  'database_comment', (
    SELECT shobj_description(oid, 'pg_database')
    FROM pg_database WHERE datname = current_database()
  ),
  'vector_version', (
    SELECT extversion FROM pg_extension WHERE extname = 'vector'
  ),
  'pg_trgm_version', (
    SELECT extversion FROM pg_extension WHERE extname = 'pg_trgm'
  ),
  'tables', (
    SELECT COALESCE(json_agg(tablename ORDER BY tablename), '[]'::json)
    FROM pg_tables WHERE schemaname = 'public'
  ),
  'views', (
    SELECT COALESCE(json_agg(viewname ORDER BY viewname), '[]'::json)
    FROM pg_views WHERE schemaname = 'public'
  ),
  'sequences', (
    SELECT COALESCE(json_agg(sequencename ORDER BY sequencename), '[]'::json)
    FROM pg_sequences WHERE schemaname = 'public'
  )
);
""".strip()

_POST_RESTORE_VALIDATION_SQL = """
DO $hindsight_validation$
DECLARE
  actual_tables text[];
  actual_extensions text[];
  actual_materialized_views text[];
  actual_sequences text[];
BEGIN
  SELECT array_agg(tablename ORDER BY tablename)
    INTO actual_tables
    FROM pg_tables
   WHERE schemaname = 'public';
  IF actual_tables IS DISTINCT FROM ARRAY[
    'alembic_version', 'async_operations', 'audit_log', 'bank_stats_cache',
    'banks', 'chunks', 'directives', 'documents', 'entities',
    'entity_cooccurrences', 'file_storage', 'graph_maintenance_queue',
    'invalidated_memory_units', 'llm_requests', 'memory_links', 'memory_units',
    'mental_model_history', 'mental_models', 'observation_history',
    'unit_entities', 'webhooks'
  ]::text[] THEN
    RAISE EXCEPTION 'Hindsight restored table set did not match 0.8.6';
  END IF;

  SELECT array_agg(extname ORDER BY extname)
    INTO actual_extensions
    FROM pg_extension
   WHERE extname <> 'plpgsql';
  IF actual_extensions IS DISTINCT FROM ARRAY['pg_trgm', 'vector']::text[] THEN
    RAISE EXCEPTION 'Hindsight restored extension set did not match 0.8.6';
  END IF;

  SELECT array_agg(matviewname ORDER BY matviewname)
    INTO actual_materialized_views
    FROM pg_matviews
   WHERE schemaname = 'public';
  IF actual_materialized_views IS DISTINCT FROM ARRAY['memory_units_bm25']::text[] THEN
    RAISE EXCEPTION 'Hindsight restored materialized views did not match 0.8.6';
  END IF;

  SELECT array_agg(sequencename ORDER BY sequencename)
    INTO actual_sequences
    FROM pg_sequences
   WHERE schemaname = 'public';
  IF actual_sequences IS DISTINCT FROM ARRAY[
    'mental_model_history_id_seq', 'observation_history_id_seq'
  ]::text[] THEN
    RAISE EXCEPTION 'Hindsight restored sequences did not match 0.8.6';
  END IF;

  IF (SELECT array_agg(version_num ORDER BY version_num) FROM public.alembic_version)
       IS DISTINCT FROM ARRAY['c7d1e9a4b3f2']::varchar[] THEN
    RAISE EXCEPTION 'Hindsight restored migration revision did not match 0.8.6';
  END IF;
  IF EXISTS (
    SELECT 1
      FROM pg_class AS table_state
      JOIN pg_namespace AS table_namespace ON table_namespace.oid = table_state.relnamespace
     WHERE table_namespace.nspname = 'public'
       AND table_state.relkind = 'r'
       AND table_state.relrowsecurity
  ) THEN
    RAISE EXCEPTION 'Hindsight restored tables unexpectedly enable RLS';
  END IF;
  IF EXISTS (SELECT 1 FROM pg_index WHERE NOT indisvalid) THEN
    RAISE EXCEPTION 'Hindsight restore created invalid indexes';
  END IF;
  IF EXISTS (
    SELECT 1
      FROM pg_constraint AS constraint_state
      JOIN pg_namespace AS constraint_namespace
        ON constraint_namespace.oid = constraint_state.connamespace
     WHERE constraint_namespace.nspname = 'public'
       AND NOT constraint_state.convalidated
  ) THEN
    RAISE EXCEPTION 'Hindsight restore created invalid constraints';
  END IF;
END
$hindsight_validation$;
ANALYZE
  public.alembic_version,
  public.async_operations,
  public.audit_log,
  public.bank_stats_cache,
  public.banks,
  public.chunks,
  public.directives,
  public.documents,
  public.entities,
  public.entity_cooccurrences,
  public.file_storage,
  public.graph_maintenance_queue,
  public.invalidated_memory_units,
  public.llm_requests,
  public.memory_links,
  public.memory_units,
  public.memory_units_bm25,
  public.mental_model_history,
  public.mental_models,
  public.observation_history,
  public.unit_entities,
  public.webhooks;
""".strip()


def _has_control_characters(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def _escape_pgpass(value: str) -> str:
    return value.replace("\\", "\\\\").replace(":", "\\:")


def _archive_toc_fingerprint(toc: bytes) -> str:
    try:
        text = toc.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError("Hindsight archive has a malformed TOC") from exc
    normalized: list[str] = []
    generated_vector_indexes: dict[str, set[str]] = {}
    for line in text.splitlines():
        if not re.match(r"^[0-9]+;", line):
            continue
        match = re.fullmatch(r"[0-9]+;\s+[0-9]+\s+[0-9]+\s+(.+?)\s*", line)
        if match is None:
            raise RuntimeError("Hindsight archive has a malformed TOC")
        descriptor = match.group(1).rstrip()
        if not descriptor.startswith(("EXTENSION - ", "COMMENT - EXTENSION ")):
            try:
                descriptor, owner = descriptor.rsplit(" ", 1)
            except ValueError as exc:
                raise RuntimeError("Hindsight archive has a malformed TOC") from exc
            if not owner or _has_control_characters(owner):
                raise RuntimeError("Hindsight archive has a malformed TOC")
        generated_index = re.fullmatch(
            r"INDEX public idx_mu_emb_(expr|obsv|worl)_([0-9a-f]{16})",
            descriptor,
        )
        if generated_index is not None:
            index_kind = generated_index.group(1)
            bank_suffix = generated_index.group(2)
            kinds = generated_vector_indexes.setdefault(bank_suffix, set())
            if index_kind in kinds:
                raise RuntimeError("Hindsight archive has a malformed TOC")
            kinds.add(index_kind)
            continue
        if descriptor.startswith("INDEX public idx_mu_emb_"):
            raise RuntimeError("Hindsight archive has a malformed TOC")
        normalized.append(descriptor)
    if (
        not normalized
        or len(normalized) != len(set(normalized))
        or any(kinds != {"expr", "obsv", "worl"} for kinds in generated_vector_indexes.values())
    ):
        raise RuntimeError("Hindsight archive has a malformed TOC")
    payload = "\n".join(sorted(normalized)).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class HindsightPlugin(BackupPlugin):
    """Back up exact Hindsight state through its single PostgreSQL boundary."""

    restore_capability = "partial"

    def __init__(self, name: str, version: str = "0.2.1") -> None:
        super().__init__(name=name, version=version)

    async def validate_config(self, config: Dict[str, Any]) -> bool:
        if not isinstance(config, dict):
            return False
        allowed = {"mode", "host", "port", "database", "user", "password"}
        if set(config) - allowed:
            return False
        if config.get("mode") not in {"source", "restore_destination"}:
            return False
        port = config.get("port", 5432)
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            return False
        host = config.get("host")
        database = config.get("database")
        user = config.get("user")
        password = config.get("password")
        if not isinstance(host, str) or not _SAFE_HOST.fullmatch(host):
            return False
        if not isinstance(database, str) or not _SAFE_IDENTIFIER.fullmatch(database):
            return False
        if not isinstance(user, str) or not _SAFE_IDENTIFIER.fullmatch(user):
            return False
        if not isinstance(password, str) or not password or _has_control_characters(password):
            return False
        return True

    def _password_file(self, config: Dict[str, Any]) -> Path:
        descriptor, raw_path = tempfile.mkstemp(prefix="hindsight-pgpass-")
        path = Path(raw_path)
        try:
            os.fchmod(descriptor, 0o600)
            line = ":".join(
                _escape_pgpass(str(value))
                for value in (
                    config["host"],
                    config.get("port", 5432),
                    config["database"],
                    config["user"],
                    config["password"],
                )
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as password_file:
                password_file.write(f"{line}\n")
                password_file.flush()
                os.fsync(password_file.fileno())
        except BaseException:
            try:
                os.close(descriptor)
            except OSError:
                pass
            path.unlink(missing_ok=True)
            raise
        return path

    def _environment(
        self,
        password_file: Path,
        *,
        statement_timeout_seconds: float | None = None,
    ) -> dict[str, str]:
        environment = os.environ.copy()
        for key in (
            "PGDATABASE",
            "PGHOST",
            "PGHOSTADDR",
            "PGOPTIONS",
            "PGPASSWORD",
            "PGPORT",
            "PGSERVICE",
            "PGSERVICEFILE",
            "PGUSER",
        ):
            environment.pop(key, None)
        environment["PGPASSFILE"] = str(password_file)
        environment["PGCONNECT_TIMEOUT"] = str(int(CONNECT_TIMEOUT_SECONDS))
        if statement_timeout_seconds is not None:
            environment["PGOPTIONS"] = (
                f"-c statement_timeout={int(statement_timeout_seconds * 1000)}"
            )
        return environment

    async def _fingerprint(self, config: Dict[str, Any]) -> dict[str, Any]:
        password_file = self._password_file(config)
        try:
            sql = (
                _SOURCE_FINGERPRINT_SQL
                if config["mode"] == "source"
                else _DESTINATION_FINGERPRINT_SQL
            )
            with (
                tempfile.TemporaryFile(mode="w+b") as stdout_file,
                tempfile.TemporaryFile(mode="w+b") as stderr_file,
            ):
                try:
                    process = await asyncio.create_subprocess_exec(
                        "psql",
                        "-X",
                        "-h",
                        str(config["host"]),
                        "-p",
                        str(config.get("port", 5432)),
                        "-U",
                        str(config["user"]),
                        "--dbname",
                        str(config["database"]),
                        "--set",
                        "ON_ERROR_STOP=on",
                        "-tA",
                        "-c",
                        sql,
                        stdout=stdout_file,
                        stderr=stderr_file,
                        env=self._environment(
                            password_file,
                            statement_timeout_seconds=CONNECT_TIMEOUT_SECONDS,
                        ),
                    )
                except FileNotFoundError as exc:
                    raise FileNotFoundError("PostgreSQL 18 psql client is unavailable") from exc
                except OSError as exc:
                    raise ConnectionError("Unable to connect to the Hindsight database") from exc
                await run_process_with_timeout(
                    process,
                    process.wait(),
                    operation="Hindsight compatibility check",
                    timeout_seconds=CONNECT_TIMEOUT_SECONDS,
                )
                stdout_file.seek(0)
                stdout = stdout_file.read(MAX_FINGERPRINT_BYTES + 1)
                stderr_file.seek(0)
                stderr = stderr_file.read(MAX_FINGERPRINT_BYTES + 1)
            if process.returncode == 2:
                raise ConnectionError("Unable to connect to the Hindsight database")
            if process.returncode != 0:
                raise RuntimeError("Hindsight database compatibility check failed")
            if stderr:
                raise RuntimeError("Hindsight database compatibility check emitted diagnostics")
            if len(stdout) > MAX_FINGERPRINT_BYTES:
                raise RuntimeError("Hindsight database fingerprint exceeds the safety limit")
            try:
                result = json.loads(stdout.decode("utf-8").strip())
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RuntimeError("Hindsight database returned an invalid fingerprint") from exc
            if not isinstance(result, dict):
                raise RuntimeError("Hindsight database returned an invalid fingerprint")
            return result
        finally:
            password_file.unlink(missing_ok=True)

    def _validate_fingerprint(self, config: Dict[str, Any], value: dict[str, Any]) -> None:
        server_version = value.get("server_version_num")
        if not isinstance(server_version, int) or server_version // 10000 != POSTGRES_SERVER_MAJOR:
            raise RuntimeError("Hindsight requires PostgreSQL server major 18")
        if value.get("database") != config["database"]:
            raise RuntimeError("Hindsight database identity did not match the target")
        if value.get("vector_version") != VECTOR_VERSION:
            raise RuntimeError("Hindsight pgvector version did not match 0.8.6")
        if config["mode"] == "source":
            if value.get("pg_trgm_version") != PG_TRGM_VERSION:
                raise RuntimeError("Hindsight pg_trgm version did not match 1.6")
            if value.get("alembic_heads") != [ALEMBIC_HEAD]:
                raise RuntimeError("Hindsight database migration revision did not match 0.8.6")
            if set(value.get("tables", [])) != REQUIRED_TABLES:
                raise RuntimeError("Hindsight database schema did not match 0.8.6")
            if value.get("rls_tables") != []:
                raise RuntimeError("Hindsight backup role cannot safely export RLS tables")
            if value.get("invalid_indexes") != []:
                raise RuntimeError("Hindsight database contains invalid indexes")
            if value.get("invalid_constraints") != []:
                raise RuntimeError("Hindsight database contains invalid constraints")
            return
        database = str(config["database"])
        if not database.startswith(RESTORE_DATABASE_PREFIX):
            raise ValueError("Hindsight restore destination has an unsafe database name")
        if value.get("database_comment") != RESTORE_SENTINEL:
            raise RuntimeError("Hindsight restore destination sentinel did not match")
        if value.get("pg_trgm_version") is not None:
            raise RuntimeError("Hindsight restore destination must not preinstall pg_trgm")
        if any(value.get(key) != [] for key in ("tables", "views", "sequences")):
            raise RuntimeError("Hindsight restore destination must be empty")

    async def test(self, config: Dict[str, Any]) -> bool:
        if not await self.validate_config(config):
            raise ValueError("Invalid Hindsight configuration")
        fingerprint = await self._fingerprint(config)
        self._validate_fingerprint(config, fingerprint)
        if config["mode"] == "restore_destination":
            await self._validate_empty_destination_schema(config)
        return True

    async def _validate_empty_destination_schema(self, config: Dict[str, Any]) -> None:
        password_file = self._password_file(config)
        descriptor, raw_path = tempfile.mkstemp(prefix="hindsight-destination-schema-")
        schema_dump = Path(raw_path)
        os.fchmod(descriptor, 0o600)
        os.close(descriptor)
        try:
            with tempfile.TemporaryFile(mode="w+b") as error_file:
                try:
                    process = await asyncio.create_subprocess_exec(
                        "pg_dump",
                        "-h",
                        str(config["host"]),
                        "-p",
                        str(config.get("port", 5432)),
                        "-U",
                        str(config["user"]),
                        "--format=custom",
                        "--schema-only",
                        "--no-owner",
                        "--no-privileges",
                        "--file",
                        str(schema_dump),
                        str(config["database"]),
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=error_file,
                        env=self._environment(
                            password_file,
                            statement_timeout_seconds=CONNECT_TIMEOUT_SECONDS,
                        ),
                    )
                except FileNotFoundError as exc:
                    raise FileNotFoundError("PostgreSQL 18 pg_dump client is unavailable") from exc
                await run_process_with_timeout(
                    process,
                    process.wait(),
                    operation="Hindsight destination schema inspection",
                    timeout_seconds=CONNECT_TIMEOUT_SECONDS,
                )
                error_file.seek(0)
                stderr = error_file.read(MAX_TOC_BYTES + 1)
            if process.returncode != 0:
                raise RuntimeError("Hindsight destination schema inspection failed")
            if stderr:
                raise RuntimeError("Hindsight destination schema inspection emitted diagnostics")
            toc = await self._inspect_archive(schema_dump)
            lines = toc.decode("utf-8").splitlines()
            if (
                not any("Dumped from database version: 18." in line for line in lines)
                or not any("Dumped by pg_dump version: 18." in line for line in lines)
                or _archive_toc_fingerprint(toc) != EXPECTED_EMPTY_DESTINATION_TOC_FINGERPRINT
            ):
                raise RuntimeError("Hindsight restore destination must contain only pgvector")
        finally:
            password_file.unlink(missing_ok=True)
            schema_dump.unlink(missing_ok=True)

    async def backup(self, context: BackupContext) -> Dict[str, Any]:
        config = context.config or {}
        if not await self.validate_config(config) or config.get("mode") != "source":
            raise ValueError("Hindsight backup requires a valid source configuration")
        await self.test(config)

        password_file = self._password_file(config)
        try:
            with create_backup_artifact(
                self,
                context,
                prefix="hindsight-postgresql",
                suffix=".dump",
                backup_root=BACKUP_BASE_PATH,
            ) as artifact:
                descriptor = os.open(
                    artifact.temporary_path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
                with (
                    os.fdopen(descriptor, "wb") as artifact_file,
                    tempfile.TemporaryFile(mode="w+b") as error_file,
                ):
                    try:
                        process = await asyncio.create_subprocess_exec(
                            "pg_dump",
                            "-h",
                            str(config["host"]),
                            "-p",
                            str(config.get("port", 5432)),
                            "-U",
                            str(config["user"]),
                            "--format=custom",
                            "--no-owner",
                            "--no-privileges",
                            str(config["database"]),
                            stdout=asyncio.subprocess.PIPE,
                            stderr=error_file,
                            env=self._environment(password_file),
                        )
                    except FileNotFoundError as exc:
                        raise FileNotFoundError(
                            "PostgreSQL 18 pg_dump client is unavailable"
                        ) from exc
                    if process.stdout is None:
                        raise RuntimeError("pg_dump did not provide an output stream")
                    dump_stdout = process.stdout

                    async def stream_dump() -> int:
                        eviction_offset = 0
                        pending_eviction = 0
                        while chunk := await dump_stdout.read(STREAM_CHUNK_BYTES):
                            artifact_file.write(chunk)
                            pending_eviction += len(chunk)
                            if pending_eviction >= FILE_CACHE_FLUSH_BYTES:
                                artifact_file.flush()
                                os.fsync(artifact_file.fileno())
                                evict_file_cache(
                                    artifact_file.fileno(),
                                    eviction_offset,
                                    pending_eviction,
                                )
                                eviction_offset += pending_eviction
                                pending_eviction = 0
                        artifact_file.flush()
                        os.fsync(artifact_file.fileno())
                        evict_file_cache(artifact_file.fileno(), eviction_offset, pending_eviction)
                        return await process.wait()

                    returncode = await run_process_with_timeout(
                        process,
                        stream_dump(),
                        operation="Hindsight pg_dump backup",
                        timeout_seconds=BACKUP_TIMEOUT_SECONDS,
                    )
                    error_file.seek(0)
                    stderr = error_file.read(MAX_TOC_BYTES + 1)
                if returncode != 0:
                    raise RuntimeError("Hindsight pg_dump failed")
                if stderr:
                    raise RuntimeError("Hindsight pg_dump emitted warning output")
                with artifact.temporary_path.open("rb") as archive_file:
                    archive_header = archive_file.read(5)
                if archive_header != b"PGDMP":
                    raise RuntimeError("Hindsight pg_dump produced a malformed archive")
                toc = await self._inspect_archive(artifact.temporary_path)
                self._validate_archive_toc(toc)
            return {"artifact_path": str(artifact.final_path)}
        finally:
            password_file.unlink(missing_ok=True)

    async def _inspect_archive(self, artifact_path: Path) -> bytes:
        with (
            tempfile.TemporaryFile(mode="w+b") as stdout_file,
            tempfile.TemporaryFile(mode="w+b") as stderr_file,
        ):
            try:
                process = await asyncio.create_subprocess_exec(
                    "pg_restore",
                    "--list",
                    str(artifact_path),
                    stdout=stdout_file,
                    stderr=stderr_file,
                )
            except FileNotFoundError as exc:
                raise FileNotFoundError("PostgreSQL 18 pg_restore client is unavailable") from exc
            await run_process_with_timeout(
                process,
                process.wait(),
                operation="Hindsight archive inspection",
                timeout_seconds=CONNECT_TIMEOUT_SECONDS,
            )
            stdout_file.seek(0)
            stdout = stdout_file.read(MAX_TOC_BYTES + 1)
            stderr_file.seek(0)
            stderr = stderr_file.read(MAX_TOC_BYTES + 1)
        if process.returncode != 0:
            raise RuntimeError("Hindsight archive inspection failed")
        if stderr:
            raise RuntimeError("Hindsight archive inspection emitted diagnostics")
        if len(stdout) > MAX_TOC_BYTES:
            raise RuntimeError("Hindsight archive TOC exceeds the safety limit")
        return stdout

    def _validate_archive_toc(self, toc: bytes) -> None:
        try:
            text = toc.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RuntimeError("Hindsight archive has a malformed TOC") from exc
        lines = text.splitlines()
        if (
            not text
            or "\x00" in text
            or len(lines) > MAX_TOC_ENTRIES
            or not any("Dumped from database version: 18." in line for line in lines)
            or not any("Dumped by pg_dump version: 18." in line for line in lines)
            or not any(" EXTENSION - vector" in line for line in lines)
            or not any(" EXTENSION - pg_trgm" in line for line in lines)
        ):
            raise RuntimeError("Hindsight archive has a malformed TOC")
        if _archive_toc_fingerprint(toc) != EXPECTED_TOC_FINGERPRINT:
            raise RuntimeError("Hindsight archive schema did not match exact version 0.8.6")

    async def restore(self, context: RestoreContext) -> Dict[str, Any]:
        config = context.config or {}
        if not await self.validate_config(config) or config.get("mode") != "restore_destination":
            raise ValueError("Hindsight restore requires a valid restore_destination mode")
        if context.source_target_id == context.destination_target_id:
            raise ValueError("Hindsight restore requires distinct source and destination targets")

        artifact = Path(context.artifact_path)
        if not artifact.exists():
            raise FileNotFoundError(f"Hindsight restore artifact not found: {artifact}")
        if not artifact.is_file() or artifact.is_symlink():
            raise ValueError("Hindsight restore artifact must be a regular file")
        with artifact.open("rb") as artifact_file:
            if artifact_file.read(5) != b"PGDMP":
                raise RuntimeError("Hindsight restore artifact has a malformed archive header")

        toc = await self._inspect_archive(artifact)
        self._validate_archive_toc(toc)
        await self.test(config)

        allowlist = self._write_restore_allowlist(toc)
        password_file = self._password_file(config)
        restore_sql: Path | None = None
        try:
            restore_sql = await self._render_restore_sql(artifact, allowlist)
            with tempfile.TemporaryFile(mode="w+b") as error_file:
                try:
                    process = await asyncio.create_subprocess_exec(
                        "psql",
                        "-X",
                        "-h",
                        str(config["host"]),
                        "-p",
                        str(config.get("port", 5432)),
                        "-U",
                        str(config["user"]),
                        "--dbname",
                        str(config["database"]),
                        "--single-transaction",
                        "--set",
                        "ON_ERROR_STOP=on",
                        "--file",
                        str(restore_sql),
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=error_file,
                        env=self._environment(
                            password_file,
                            statement_timeout_seconds=RESTORE_TIMEOUT_SECONDS,
                        ),
                    )
                except FileNotFoundError as exc:
                    raise FileNotFoundError("PostgreSQL 18 psql client is unavailable") from exc
                await run_process_with_timeout(
                    process,
                    process.wait(),
                    operation="Hindsight transactional restore",
                    timeout_seconds=RESTORE_TIMEOUT_SECONDS,
                )
                error_file.seek(0)
                stderr = error_file.read(MAX_TOC_BYTES + 1)
            if process.returncode != 0:
                raise RuntimeError("Hindsight transactional restore failed")
            if stderr:
                raise RuntimeError("Hindsight transactional restore emitted warning output")
        finally:
            password_file.unlink(missing_ok=True)
            allowlist.unlink(missing_ok=True)
            if restore_sql is not None:
                restore_sql.unlink(missing_ok=True)

        return {
            "status": "success",
            "artifact_path": str(artifact),
            "artifact_bytes": artifact.stat().st_size,
            "message": (
                "Hindsight database restore completed; exact-image boot and "
                "external OAuth/configuration proof remain required"
            ),
        }

    async def _render_restore_sql(self, artifact: Path, allowlist: Path) -> Path:
        descriptor, raw_path = tempfile.mkstemp(prefix="hindsight-restore-sql-")
        restore_sql = Path(raw_path)
        os.fchmod(descriptor, 0o600)
        os.close(descriptor)
        try:
            with tempfile.TemporaryFile(mode="w+b") as error_file:
                try:
                    process = await asyncio.create_subprocess_exec(
                        "pg_restore",
                        "--use-list",
                        str(allowlist),
                        "--exit-on-error",
                        "--no-owner",
                        "--no-privileges",
                        "--file",
                        str(restore_sql),
                        str(artifact),
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=error_file,
                    )
                except FileNotFoundError as exc:
                    raise FileNotFoundError(
                        "PostgreSQL 18 pg_restore client is unavailable"
                    ) from exc
                await run_process_with_timeout(
                    process,
                    process.wait(),
                    operation="Hindsight restore SQL rendering",
                    timeout_seconds=RESTORE_TIMEOUT_SECONDS,
                )
                error_file.seek(0)
                stderr = error_file.read(MAX_TOC_BYTES + 1)
            if process.returncode != 0:
                raise RuntimeError("Hindsight restore SQL rendering failed")
            if stderr:
                raise RuntimeError("Hindsight restore SQL rendering emitted diagnostics")
            with restore_sql.open("a", encoding="utf-8") as sql_file:
                sql_file.write("\n")
                sql_file.write(_POST_RESTORE_VALIDATION_SQL)
                sql_file.write("\n")
                sql_file.flush()
                os.fsync(sql_file.fileno())
            return restore_sql
        except BaseException:
            restore_sql.unlink(missing_ok=True)
            raise

    def _write_restore_allowlist(self, toc: bytes) -> Path:
        text = toc.decode("utf-8")
        lines = text.splitlines(keepends=True)
        kept: list[str] = []
        omitted = 0
        for line in lines:
            if " EXTENSION - vector" in line or " COMMENT - EXTENSION vector" in line:
                omitted += 1
                continue
            kept.append(line)
        if omitted != 2:
            raise RuntimeError("Hindsight archive vector TOC entries were ambiguous")
        descriptor, raw_path = tempfile.mkstemp(prefix="hindsight-restore-toc-")
        path = Path(raw_path)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as allowlist_file:
                allowlist_file.writelines(kept)
                allowlist_file.flush()
                os.fsync(allowlist_file.fileno())
        except BaseException:
            try:
                os.close(descriptor)
            except OSError:
                pass
            path.unlink(missing_ok=True)
            raise
        return path

    async def get_status(self, context: BackupContext) -> Dict[str, Any]:
        try:
            await self.test(context.config or {})
        except Exception as exc:
            return {"status": "error", "error": str(exc)}
        return {"status": "ok"}
