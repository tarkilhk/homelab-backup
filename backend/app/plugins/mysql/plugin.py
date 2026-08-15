from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from typing import Any, Dict

from app.core.plugins.artifacts import create_backup_artifact, evict_file_cache
from app.core.plugins.base import BackupContext, BackupPlugin, RestoreContext
from app.core.subprocesses import run_process_with_timeout

BACKUP_BASE_PATH = "/backups"
MAX_ERROR_BYTES = 64 * 1024
STREAM_CHUNK_BYTES = 1024 * 1024
FILE_CACHE_FLUSH_BYTES = 8 * 1024 * 1024
CONNECT_TIMEOUT_SECONDS = 30.0
BACKUP_TIMEOUT_SECONDS = 3600.0
RESTORE_TIMEOUT_SECONDS = 3600.0


class MySQLPlugin(BackupPlugin):
    restore_capability = "partial"
    """MySQL backup plugin using the pinned MySQL client binaries.

    Research notes:
    - mysqldump is the standard utility to export a MySQL database.
    - Connectivity tests use the same pinned client shipped for backup/restore.
    SQL dumps are stored under
    `/backups/<slug>/<date>/mysql-dump-<timestamp>.sql`.
    """

    def __init__(self, name: str, version: str = "0.1.0") -> None:
        super().__init__(name=name, version=version)
        self._logger = logging.getLogger(__name__)

    async def validate_config(self, config: Dict[str, Any]) -> bool:  # pragma: no cover - trivial
        if not isinstance(config, dict):
            return False
        host = config.get("host")
        user = config.get("user")
        password = config.get("password")
        database = config.get("database")
        if not host or not isinstance(host, str):
            return False
        if not user or not isinstance(user, str):
            return False
        if not password or not isinstance(password, str):
            return False
        if not database or not isinstance(database, str):
            return False
        return True

    async def test(self, config: Dict[str, Any]) -> bool:
        """Check database connectivity using the shipped MySQL client."""
        if not await self.validate_config(config):
            raise ValueError(
                "Invalid configuration: host, user, password, and database are required"
            )
        host = str(config["host"])
        port = int(config.get("port", 3306))
        user = str(config["user"])
        password = str(config["password"])
        database = str(config["database"])
        env = os.environ.copy()
        env["MYSQL_PWD"] = password
        try:
            process = await asyncio.create_subprocess_exec(
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
                "SELECT 1",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            stdout, stderr = await run_process_with_timeout(
                process,
                process.communicate(),
                operation="mysql connection test",
                timeout_seconds=CONNECT_TIMEOUT_SECONDS,
            )
        except FileNotFoundError as exc:
            raise FileNotFoundError("mysql client command not found") from exc
        except OSError as exc:
            self._logger.warning("mysql_test_failed | host=%s error=%s", host, exc)
            raise ConnectionError(f"Failed to connect to MySQL database: {exc}") from exc
        if process.returncode != 0:
            detail = stderr[:MAX_ERROR_BYTES].decode(errors="ignore").strip()
            raise ConnectionError(f"Failed to connect to MySQL database: {detail}")
        if stdout.decode(errors="ignore").strip() != "1":
            raise ConnectionError("Failed to validate MySQL connection")
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

    async def get_status(
        self, context: BackupContext
    ) -> Dict[str, Any]:  # pragma: no cover - not implemented
        return {"status": "unknown"}
