from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import re
import tempfile
from typing import Any, Dict

from app.core.plugins.artifacts import create_backup_artifact, evict_file_cache
from app.core.plugins.base import BackupContext, BackupPlugin, RestoreContext
from app.core.plugins.mysql import MySQLTarget, probe_mysql
from app.core.subprocesses import run_process_with_timeout

BACKUP_BASE_PATH = "/backups"
MAX_ERROR_BYTES = 64 * 1024
STREAM_CHUNK_BYTES = 1024 * 1024
FILE_CACHE_FLUSH_BYTES = 8 * 1024 * 1024
CONNECT_TIMEOUT_SECONDS = 30.0
BACKUP_TIMEOUT_SECONDS = 3600.0
RESTORE_TIMEOUT_SECONDS = 3600.0
_CONFIG_KEYS = frozenset({"mode", "host", "port", "database", "user", "password", "ssl_mode"})
_SYSTEM_SCHEMAS = frozenset({"information_schema", "mysql", "performance_schema", "sys"})
_DATABASE_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z")
_USER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,31}\Z")
_HOST_LABEL_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")
logger = logging.getLogger(__name__)


def _contains_forbidden_text(value: str) -> bool:
    return any(
        character.isspace() or ord(character) < 32 or 127 <= ord(character) <= 159
        for character in value
    )


def _valid_host(value: object) -> bool:
    if not isinstance(value, str) or not value or len(value) > 253:
        return False
    if _contains_forbidden_text(value) or any(
        marker in value for marker in ("/", "\\", "?", "#", "@")
    ):
        return False
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return all(_HOST_LABEL_RE.fullmatch(label) for label in value.split("."))


def _valid_secret(value: object) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 1024 and not _contains_forbidden_text(value)


class MySQLPlugin(BackupPlugin):
    """Strict Oracle MySQL 8.4 single-schema backup plugin.

    The public adapter keeps source and create-only restore-destination modes
    explicit. MySQL Shell dump/load mechanics live in the shared MySQL core.
    """

    restore_capability = "partial"

    def __init__(self, name: str, version: str = "0.2.1") -> None:
        super().__init__(name=name, version=version)
        self._logger = logger

    async def validate_config(self, config: Dict[str, Any]) -> bool:
        """Return whether config is the exact clean-breaking MySQL target shape."""
        if not isinstance(config, dict) or set(config) != _CONFIG_KEYS:
            return False
        if config.get("mode") not in {"source", "restore_destination"}:
            return False
        if not _valid_host(config.get("host")):
            return False
        port = config.get("port")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            return False
        database = config.get("database")
        if (
            not isinstance(database, str)
            or _DATABASE_RE.fullmatch(database) is None
            or database.lower() in _SYSTEM_SCHEMAS
        ):
            return False
        user = config.get("user")
        if not isinstance(user, str) or _USER_RE.fullmatch(user) is None:
            return False
        return _valid_secret(config.get("password")) and config.get("ssl_mode") in {
            "REQUIRED",
            "DISABLED",
        }

    async def test(self, config: Dict[str, Any]) -> bool:
        """Prove the exact MySQL 8.4 source or fresh restore destination."""
        if not await self.validate_config(config):
            raise ValueError("Invalid MySQL source or restore-destination configuration")
        await probe_mysql(MySQLTarget.from_config(config))
        return True

    async def backup(self, context: BackupContext) -> Dict[str, Any]:
        cfg = getattr(context, "config", {}) or {}
        if not await self.validate_config(cfg):
            raise ValueError("mysql config requires host, user, password, database")
        host = str(cfg["host"])
        port = int(cfg.get("port", 3306))
        user = str(cfg["user"])
        password = str(cfg["password"])
        database = str(cfg["database"])

        meta = context.metadata or {}
        target_slug = meta.get("target_slug") or str(context.target_id)

        # Run mysqldump directly (installed in container) instead of via Docker
        # Use MYSQL_PWD environment variable for password (same pattern as PostgreSQL)
        env = os.environ.copy()
        env["MYSQL_PWD"] = password

        cmd = [
            "mysqldump",
            "-h",
            host,
            "-P",
            str(port),
            "-u",
            user,
            "--single-transaction",
            "--quick",
            "--skip-lock-tables",
            "--routines",
            "--events",
            "--triggers",
            "--hex-blob",
            "--no-tablespaces",
            "--set-gtid-purged=OFF",
            database,
        ]

        self._logger.info(
            "mysql_backup_start | job_id=%s target_id=%s target_slug=%s host=%s artifact=%s",
            context.job_id,
            context.target_id,
            target_slug,
            host,
            "<pending>",
        )

        with create_backup_artifact(
            self,
            context,
            prefix="mysql-dump",
            suffix=".sql",
            backup_root=BACKUP_BASE_PATH,
        ) as artifact:
            try:
                with (
                    artifact.temporary_path.open("wb") as artifact_file,
                    tempfile.TemporaryFile(mode="w+b") as error_file,
                ):
                    proc = await asyncio.create_subprocess_exec(
                        *cmd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=error_file,
                        env=env,
                    )
                    if proc.stdout is None:
                        raise RuntimeError("mysqldump stdout pipe was not created")
                    stdout = proc.stdout

                    async def stream_dump() -> int:
                        eviction_offset = 0
                        pending_eviction_bytes = 0
                        while chunk := await stdout.read(STREAM_CHUNK_BYTES):
                            artifact_file.write(chunk)
                            pending_eviction_bytes += len(chunk)
                            if pending_eviction_bytes >= FILE_CACHE_FLUSH_BYTES:
                                artifact_file.flush()
                                os.fsync(artifact_file.fileno())
                                evict_file_cache(
                                    artifact_file.fileno(),
                                    eviction_offset,
                                    pending_eviction_bytes,
                                )
                                eviction_offset += pending_eviction_bytes
                                pending_eviction_bytes = 0
                        artifact_file.flush()
                        os.fsync(artifact_file.fileno())
                        evict_file_cache(
                            artifact_file.fileno(), eviction_offset, pending_eviction_bytes
                        )
                        return await proc.wait()

                    returncode = await run_process_with_timeout(
                        proc,
                        stream_dump(),
                        operation="mysqldump backup",
                        timeout_seconds=BACKUP_TIMEOUT_SECONDS,
                    )
                    error_file.seek(0)
                    stderr_data = error_file.read(MAX_ERROR_BYTES + 1)
            except OSError as exc:
                self._logger.error(
                    "mysqldump_exec_error | job_id=%s target_id=%s error=%s",
                    context.job_id,
                    context.target_id,
                    exc,
                )
                raise
            if returncode != 0:
                truncated = len(stderr_data) > MAX_ERROR_BYTES
                err = stderr_data[:MAX_ERROR_BYTES].decode(errors="ignore").strip()
                if truncated:
                    err = f"{err} [truncated]"
                raise RuntimeError(f"mysqldump failed: {err}")

        return {"artifact_path": str(artifact.final_path)}

    async def restore(self, context: RestoreContext) -> Dict[str, Any]:
        """Restore a MySQL database from a SQL dump file using mysql command.

        Executes the mysql command to import the SQL dump back into the database.
        Uses the same pattern as PostgreSQL: direct command execution with env vars.
        """
        cfg = context.config or {}
        if not await self.validate_config(cfg):
            raise ValueError("mysql config requires host, user, password, database")
        host = str(cfg["host"])
        port = int(cfg.get("port", 3306))
        user = str(cfg["user"])
        password = str(cfg["password"])
        database = str(cfg["database"])

        artifact_path = context.artifact_path
        if not artifact_path or not os.path.exists(artifact_path):
            raise FileNotFoundError(f"Artifact not found: {artifact_path}")

        self._logger.info(
            "mysql_restore_start | job_id=%s source=%s dest=%s host=%s database=%s artifact=%s",
            context.job_id,
            context.source_target_id,
            context.destination_target_id,
            host,
            database,
            artifact_path,
        )

        # Run mysql directly (same pattern as PostgreSQL with psql)
        # Use MYSQL_PWD environment variable for password
        env = os.environ.copy()
        env["MYSQL_PWD"] = password

        preflight = await asyncio.create_subprocess_exec(
            "mysql",
            "-h",
            host,
            "-P",
            str(port),
            "-u",
            user,
            "--database",
            database,
            "--batch",
            "--skip-column-names",
            "-e",
            (
                "SELECT "
                "(SELECT COUNT(*) FROM information_schema.tables "
                "WHERE table_schema = DATABASE()) + "
                "(SELECT COUNT(*) FROM information_schema.routines "
                "WHERE routine_schema = DATABASE()) + "
                "(SELECT COUNT(*) FROM information_schema.triggers "
                "WHERE trigger_schema = DATABASE()) + "
                "(SELECT COUNT(*) FROM information_schema.events "
                "WHERE event_schema = DATABASE())"
            ),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        preflight_stdout, preflight_stderr = await run_process_with_timeout(
            preflight,
            preflight.communicate(),
            operation="mysql restore preflight",
            timeout_seconds=CONNECT_TIMEOUT_SECONDS,
        )
        if preflight.returncode != 0:
            detail = preflight_stderr[:MAX_ERROR_BYTES].decode(errors="ignore").strip()
            raise RuntimeError(f"mysql restore preflight failed: {detail}")
        try:
            existing_objects = int(preflight_stdout.decode().strip())
        except ValueError as exc:
            raise RuntimeError("mysql restore preflight returned an invalid table count") from exc
        if existing_objects:
            raise ValueError(
                "MySQL restore destination database must be empty to avoid a partial overwrite"
            )

        cmd = [
            "mysql",
            "-h",
            host,
            "-P",
            str(port),
            "-u",
            user,
            database,
        ]

        try:
            with (
                open(artifact_path, "rb") as sql_file,
                tempfile.TemporaryFile(mode="w+b") as error_file,
            ):
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdin=sql_file,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=error_file,
                    env=env,
                )
                returncode = await run_process_with_timeout(
                    proc,
                    proc.wait(),
                    operation="mysql restore",
                    timeout_seconds=RESTORE_TIMEOUT_SECONDS,
                )
                error_file.seek(0)
                stderr_data = error_file.read(MAX_ERROR_BYTES + 1)
        except OSError as exc:
            self._logger.error(
                "mysql_restore_exec_error | job_id=%s source=%s dest=%s error=%s",
                context.job_id,
                context.source_target_id,
                context.destination_target_id,
                exc,
            )
            raise

        if returncode != 0:
            truncated = len(stderr_data) > MAX_ERROR_BYTES
            err = stderr_data[:MAX_ERROR_BYTES].decode(errors="ignore").strip()
            if truncated:
                err = f"{err} [truncated]"
            raise RuntimeError(
                f"mysql restore failed: {err}. The destination may contain partial data and "
                "must be reset before retrying"
            )

        validator = await asyncio.create_subprocess_exec(
            "mysqlcheck",
            "-h",
            host,
            "-P",
            str(port),
            "-u",
            user,
            "--check",
            "--databases",
            database,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        _, validation_error = await run_process_with_timeout(
            validator,
            validator.communicate(),
            operation="mysql restore validation",
            timeout_seconds=CONNECT_TIMEOUT_SECONDS,
        )
        if validator.returncode != 0:
            detail = validation_error[:MAX_ERROR_BYTES].decode(errors="ignore").strip()
            raise RuntimeError(f"mysql restore validation failed: {detail}")

        artifact_bytes = os.path.getsize(artifact_path)

        self._logger.info(
            "mysql_restore_success | job_id=%s source=%s dest=%s artifact=%s bytes=%s",
            context.job_id,
            context.source_target_id,
            context.destination_target_id,
            artifact_path,
            artifact_bytes,
        )

        return {
            "status": "success",
            "artifact_path": artifact_path,
            "artifact_bytes": artifact_bytes,
        }

    async def get_status(self, context: BackupContext) -> Dict[str, Any]:
        """Return secret-free status from the exact identity probe."""
        config = context.config or {}
        if not await self.validate_config(config):
            return {"status": "error", "database_state": "unavailable"}
        try:
            target = MySQLTarget.from_config(config)
            identity = await probe_mysql(target)
        except Exception:
            return {"status": "error", "database_state": "unavailable"}
        return {
            "status": "ok",
            "server_version": identity.server_version,
            "database_state": ("source" if target.mode == "source" else "fresh_destination"),
            "catalog_sha256": identity.catalog_sha256,
        }
