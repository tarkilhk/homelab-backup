from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from datetime import datetime, timezone
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


class PostgreSQLPlugin(BackupPlugin):
    restore_capability = "automatic"
    """PostgreSQL backup plugin executed via a temporary Docker container.
    Research notes:
    - `pg_dump` is the standard utility to export a PostgreSQL database into a
      script file or archive format.
    - Because the host environment may not ship PostgreSQL client binaries,
      this plugin runs `pg_dump` inside the official `postgres` container and
      uses `pg_dump --schema-only` to verify connectivity.
    Each target represents one named database and produces a custom-format
    archive that can be validated and restored transactionally.
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
        # Port is optional; default 5432
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
        """Check connectivity through the same pinned PostgreSQL client toolchain."""
        if not await self.validate_config(config):
            raise ValueError(
                "Invalid configuration: host, user, password, and database are required"
            )
        host = str(config["host"])
        port = int(config.get("port", 5432))
        user = str(config["user"])
        password = str(config["password"])
        database = str(config["database"])

        env = os.environ.copy()
        env["PGPASSWORD"] = password
        try:
            process = await asyncio.create_subprocess_exec(
                "psql",
                "-X",
                "-h",
                host,
                "-p",
                str(port),
                "-U",
                user,
                "--dbname",
                database,
                "--set",
                "ON_ERROR_STOP=on",
                "-tA",
                "-c",
                "SELECT 1",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            stdout, stderr = await run_process_with_timeout(
                process,
                process.communicate(),
                operation="postgresql connection test",
                timeout_seconds=CONNECT_TIMEOUT_SECONDS,
            )
        except FileNotFoundError as exc:
            raise FileNotFoundError("psql client command not found") from exc
        except OSError as exc:
            self._logger.warning("postgresql_test_failed | host=%s error=%s", host, exc)
            raise ConnectionError(f"Failed to connect to PostgreSQL database: {exc}") from exc
        if process.returncode != 0:
            detail = stderr[:MAX_ERROR_BYTES].decode(errors="ignore").strip()
            raise ConnectionError(f"Failed to connect to PostgreSQL database: {detail}")
        if stdout.decode(errors="ignore").strip() != "1":
            raise ConnectionError("Failed to validate PostgreSQL connection")
        return True

    async def backup(self, context: BackupContext) -> Dict[str, Any]:
        cfg = getattr(context, "config", {}) or {}
        if not await self.validate_config(cfg):
            raise ValueError("postgresql config requires host, user, password, database")
        host = str(cfg["host"])
        port = int(cfg.get("port", 5432))
        user = str(cfg["user"])
        password = str(cfg["password"])
        database = str(cfg["database"]).strip()

        meta = context.metadata or {}
        target_slug = meta.get("target_slug") or str(context.target_id)
        env = os.environ.copy()
        env["PGPASSWORD"] = password
        cmd = [
            "pg_dump",
            "-h",
            host,
            "-p",
            str(port),
            "-U",
            user,
            "--format=custom",
            "--compress=6",
            "--no-owner",
            "--no-privileges",
            database,
        ]
        self._logger.info(
            "postgresql_backup_start | job_id=%s target_id=%s target_slug=%s "
            "host=%s database=%s artifact=%s",
            context.job_id,
            context.target_id,
            target_slug,
            host,
            database,
            "<pending>",
        )

        with create_backup_artifact(
            self,
            context,
            prefix="postgresql-dump",
            suffix=".dump",
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
                        raise RuntimeError("pg_dump stdout pipe was not created")
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
                            artifact_file.fileno(),
                            eviction_offset,
                            pending_eviction_bytes,
                        )
                        return await proc.wait()

                    returncode = await run_process_with_timeout(
                        proc,
                        stream_dump(),
                        operation="pg_dump backup",
                        timeout_seconds=BACKUP_TIMEOUT_SECONDS,
                    )
                    error_file.seek(0)
                    stderr_data = error_file.read(MAX_ERROR_BYTES + 1)
            except OSError as exc:
                self._logger.error(
                    "pg_dump_exec_error | job_id=%s target_id=%s error=%s",
                    context.job_id,
                    context.target_id,
                    exc,
                )
                raise
            if returncode != 0:
                error_was_truncated = len(stderr_data) > MAX_ERROR_BYTES
                err = stderr_data[:MAX_ERROR_BYTES].decode(errors="ignore").strip()
                if error_was_truncated:
                    err = f"{err} [truncated]"
                raise RuntimeError(f"pg_dump failed: {err}")

            validator = await asyncio.create_subprocess_exec(
                "pg_restore",
                "--list",
                str(artifact.temporary_path),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, validation_error = await run_process_with_timeout(
                validator,
                validator.communicate(),
                operation="pg_restore archive validation",
                timeout_seconds=CONNECT_TIMEOUT_SECONDS,
            )
            if validator.returncode != 0:
                raise RuntimeError(
                    "pg_dump produced an invalid archive: "
                    f"{validation_error[:MAX_ERROR_BYTES].decode(errors='ignore').strip()}"
                )

        return {"artifact_path": str(artifact.final_path)}

    async def restore(self, context: RestoreContext) -> Dict[str, Any]:
        """Restore one PostgreSQL custom archive transactionally."""
        cfg = context.config or {}
        if not await self.validate_config(cfg):
            raise ValueError("postgresql config requires host, user, password, database")
        host = str(cfg["host"])
        port = int(cfg.get("port", 5432))
        user = str(cfg["user"])
        password = str(cfg["password"])
        database = str(cfg["database"]).strip()

        artifact_path = context.artifact_path
        if not artifact_path or not os.path.exists(artifact_path):
            raise FileNotFoundError(f"Artifact not found: {artifact_path}")

        env = os.environ.copy()
        env["PGPASSWORD"] = password
        cmd = [
            "pg_restore",
            "-h",
            host,
            "-p",
            str(port),
            "-U",
            user,
            "--dbname",
            database,
            "--exit-on-error",
            "--single-transaction",
            "--clean",
            "--if-exists",
            "--no-owner",
            "--no-privileges",
            artifact_path,
        ]
        self._logger.info(
            "postgresql_restore_start | job_id=%s source=%s dest=%s host=%s "
            "database=%s artifact=%s",
            context.job_id,
            context.source_target_id,
            context.destination_target_id,
            host,
            database,
            artifact_path,
        )

        try:
            with tempfile.TemporaryFile(mode="w+b") as error_file:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=error_file,
                    env=env,
                )
                returncode = await run_process_with_timeout(
                    proc,
                    proc.wait(),
                    operation="pg_restore restore",
                    timeout_seconds=RESTORE_TIMEOUT_SECONDS,
                )
                error_file.seek(0)
                stderr_data = error_file.read(MAX_ERROR_BYTES + 1)
        except OSError as exc:
            self._logger.error(
                "psql_exec_error | job_id=%s source=%s dest=%s error=%s",
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
            raise RuntimeError(f"pg_restore failed: {err}")

        artifact_bytes = os.path.getsize(artifact_path)

        self._logger.info(
            "postgresql_restore_success | job_id=%s source=%s dest=%s artifact=%s bytes=%s",
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
