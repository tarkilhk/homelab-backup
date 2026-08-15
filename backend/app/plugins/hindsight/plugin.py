"""Exact Hindsight 0.8.6 PostgreSQL backup contract."""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Dict

from app.core.plugins.base import BackupContext, BackupPlugin, RestoreContext
from app.core.subprocesses import run_process_with_timeout

POSTGRES_SERVER_MAJOR = 18
VECTOR_VERSION = "0.8.6"
ALEMBIC_HEAD = "c7d1e9a4b3f2"
CONNECT_TIMEOUT_SECONDS = 30.0
RESTORE_DATABASE_PREFIX = "hlb_hindsight_restore_"
RESTORE_SENTINEL = "homelab-backup:hindsight-restore:v1"
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


def _has_control_characters(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def _escape_pgpass(value: str) -> str:
    return value.replace("\\", "\\\\").replace(":", "\\:")


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
            os.close(descriptor)
            path.unlink(missing_ok=True)
            raise
        return path

    def _environment(self, password_file: Path) -> dict[str, str]:
        environment = os.environ.copy()
        environment.pop("PGPASSWORD", None)
        environment["PGPASSFILE"] = str(password_file)
        environment["PGCONNECT_TIMEOUT"] = str(int(CONNECT_TIMEOUT_SECONDS))
        return environment

    async def _fingerprint(self, config: Dict[str, Any]) -> dict[str, Any]:
        password_file = self._password_file(config)
        try:
            sql = (
                _SOURCE_FINGERPRINT_SQL
                if config["mode"] == "source"
                else _DESTINATION_FINGERPRINT_SQL
            )
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
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=self._environment(password_file),
                )
            except FileNotFoundError as exc:
                raise FileNotFoundError("PostgreSQL 18 psql client is unavailable") from exc
            except OSError as exc:
                raise ConnectionError("Unable to connect to the Hindsight database") from exc
            stdout, _ = await run_process_with_timeout(
                process,
                process.communicate(),
                operation="Hindsight compatibility check",
                timeout_seconds=CONNECT_TIMEOUT_SECONDS,
            )
            if process.returncode != 0:
                raise ConnectionError("Unable to connect to the Hindsight database")
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
            if value.get("alembic_heads") != [ALEMBIC_HEAD]:
                raise RuntimeError("Hindsight database migration revision did not match 0.8.6")
            if set(value.get("tables", [])) != REQUIRED_TABLES:
                raise RuntimeError("Hindsight database schema did not match 0.8.6")
            if value.get("rls_tables") != []:
                raise RuntimeError("Hindsight backup role cannot safely export RLS tables")
            return
        database = str(config["database"])
        if not database.startswith(RESTORE_DATABASE_PREFIX):
            raise ValueError("Hindsight restore destination has an unsafe database name")
        if value.get("database_comment") != RESTORE_SENTINEL:
            raise RuntimeError("Hindsight restore destination sentinel did not match")
        if any(value.get(key) != [] for key in ("tables", "views", "sequences")):
            raise RuntimeError("Hindsight restore destination must be empty")

    async def test(self, config: Dict[str, Any]) -> bool:
        if not await self.validate_config(config):
            raise ValueError("Invalid Hindsight configuration")
        fingerprint = await self._fingerprint(config)
        self._validate_fingerprint(config, fingerprint)
        return True

    async def backup(self, context: BackupContext) -> Dict[str, Any]:
        raise NotImplementedError("Hindsight backup is not implemented")

    async def restore(self, context: RestoreContext) -> Dict[str, Any]:
        raise NotImplementedError("Hindsight restore is not implemented")

    async def get_status(self, context: BackupContext) -> Dict[str, Any]:
        try:
            await self.test(context.config or {})
        except Exception as exc:
            return {"status": "error", "error": str(exc)}
        return {"status": "ok"}
