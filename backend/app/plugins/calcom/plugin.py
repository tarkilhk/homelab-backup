from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Dict
from urllib.parse import parse_qs, unquote, urlsplit

from app.core.plugins.artifacts import create_backup_artifact, evict_file_cache
from app.core.plugins.base import BackupContext, BackupPlugin, RestoreContext
from app.core.subprocesses import run_process_with_timeout

MAX_ERROR_BYTES = 64 * 1024
STREAM_CHUNK_BYTES = 1024 * 1024
FILE_CACHE_FLUSH_BYTES = 8 * 1024 * 1024
INHERITED_ENV_KEYS = ("PATH", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR")
PG_QUERY_ENV = {
    "sslmode": "PGSSLMODE",
    "connect_timeout": "PGCONNECT_TIMEOUT",
    "sslrootcert": "PGSSLROOTCERT",
    "sslcert": "PGSSLCERT",
    "sslkey": "PGSSLKEY",
    "application_name": "PGAPPNAME",
}
CONNECT_TIMEOUT_SECONDS = 30.0
BACKUP_TIMEOUT_SECONDS = 3600.0
RESTORE_TIMEOUT_SECONDS = 3600.0


class CalcomPlugin(BackupPlugin):
    """Back up a Cal.com PostgreSQL database using PostgreSQL 16 custom format."""

    restore_capability = "automatic"

    def __init__(self, name: str, version: str = "0.2.0", base_dir: str = "/backups") -> None:
        super().__init__(name=name, version=version)
        self.base_dir = base_dir
        self._logger = logging.getLogger(__name__)

    async def validate_config(self, config: Dict[str, Any]) -> bool:
        if not isinstance(config, dict):
            return False
        value = config.get("database_url")
        return isinstance(value, str) and bool(value.strip())

    def _connection_url(self, config: Dict[str, Any]) -> str:
        direct = config.get("database_direct_url")
        if isinstance(direct, str) and direct.strip():
            return direct.strip()
        primary = config.get("database_url")
        return primary.strip() if isinstance(primary, str) else ""

    def _connection_env(self, config: Dict[str, Any]) -> dict[str, str]:
        parsed = urlsplit(self._connection_url(config))
        if parsed.scheme not in {"postgres", "postgresql"}:
            raise ValueError("Cal.com database URL must use postgres:// or postgresql://")
        database = parsed.path.lstrip("/")
        if not parsed.hostname or not parsed.username or not database:
            raise ValueError("Cal.com database URL must include host, user, and database")
        env = {key: os.environ[key] for key in INHERITED_ENV_KEYS if key in os.environ}
        env.update(
            {
                "PGHOST": parsed.hostname,
                "PGUSER": unquote(parsed.username),
                "PGDATABASE": unquote(database),
            }
        )
        if parsed.password is not None:
            env["PGPASSWORD"] = unquote(parsed.password)
        if parsed.port is not None:
            env["PGPORT"] = str(parsed.port)
        query = parse_qs(parsed.query)
        for query_name, env_name in PG_QUERY_ENV.items():
            values = query.get(query_name)
            if values:
                env[env_name] = values[-1]
        return env

    @staticmethod
    def _read_error(error_file: Any) -> str:
        error_file.seek(0)
        data = error_file.read(MAX_ERROR_BYTES + 1)
        truncated = len(data) > MAX_ERROR_BYTES
        message = data[:MAX_ERROR_BYTES].decode(errors="ignore").strip()
        return f"{message} [truncated]" if truncated else message

    async def test(self, config: Dict[str, Any]) -> bool:
        if not await self.validate_config(config):
            raise ValueError("Invalid configuration: database_url is required")
        env = self._connection_env(config)
        try:
            process = await asyncio.create_subprocess_exec(
                "psql",
                "-X",
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
                operation="Cal.com database connection test",
                timeout_seconds=CONNECT_TIMEOUT_SECONDS,
            )
        except FileNotFoundError as exc:
            raise FileNotFoundError("psql command not found") from exc
        if process.returncode != 0:
            raise ConnectionError(
                f"Failed to connect to PostgreSQL database: {stderr.decode(errors='ignore').strip()}"
            )
        if stdout.decode(errors="ignore").strip() != "1":
            raise ConnectionError("Failed to validate PostgreSQL connection")
        return True

    async def backup(self, context: BackupContext) -> Dict[str, Any]:
        if not await self.validate_config(context.config or {}):
            raise ValueError("database_url is required")
        env = self._connection_env(context.config or {})
        with create_backup_artifact(
            self,
            context,
            prefix="calcom-db",
            suffix=".dump",
            backup_root=self.base_dir,
        ) as artifact:
            with tempfile.TemporaryFile(mode="w+b") as error_file:
                process = await asyncio.create_subprocess_exec(
                    "pg_dump",
                    "--format=custom",
                    "--compress=6",
                    "--no-owner",
                    "--no-privileges",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=error_file,
                    env=env,
                )
                if process.stdout is None:
                    raise RuntimeError("pg_dump stdout pipe was not created")
                stdout = process.stdout

                async def stream_dump() -> int:
                    with artifact.temporary_path.open("wb") as artifact_file:
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
                    return await process.wait()

                returncode = await run_process_with_timeout(
                    process,
                    stream_dump(),
                    operation="Cal.com pg_dump backup",
                    timeout_seconds=BACKUP_TIMEOUT_SECONDS,
                )
                error = self._read_error(error_file)
            if returncode != 0:
                raise RuntimeError(f"pg_dump failed: {error}")

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
                operation="Cal.com archive validation",
                timeout_seconds=CONNECT_TIMEOUT_SECONDS,
            )
            if validator.returncode != 0:
                raise RuntimeError(
                    "pg_dump produced an invalid archive: "
                    f"{validation_error.decode(errors='ignore').strip()}"
                )
        return {"artifact_path": str(artifact.final_path)}

    async def restore(self, context: RestoreContext) -> Dict[str, Any]:
        if not await self.validate_config(context.config or {}):
            raise ValueError("database_url is required for restore")
        artifact_path = Path(context.artifact_path)
        if not artifact_path.is_file() or artifact_path.is_symlink():
            raise FileNotFoundError(f"Artifact not found: {artifact_path}")
        env = self._connection_env(context.config or {})
        with tempfile.TemporaryFile(mode="w+b") as error_file:
            process = await asyncio.create_subprocess_exec(
                "pg_restore",
                "--exit-on-error",
                "--single-transaction",
                "--clean",
                "--if-exists",
                "--no-owner",
                "--no-privileges",
                "--dbname",
                env["PGDATABASE"],
                str(artifact_path),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=error_file,
                env=env,
            )
            returncode = await run_process_with_timeout(
                process,
                process.wait(),
                operation="Cal.com pg_restore",
                timeout_seconds=RESTORE_TIMEOUT_SECONDS,
            )
            error = self._read_error(error_file)
        if returncode != 0:
            raise RuntimeError(f"pg_restore failed: {error}")
        return {
            "status": "success",
            "artifact_path": str(artifact_path),
            "artifact_bytes": artifact_path.stat().st_size,
        }

    async def get_status(self, context: BackupContext) -> Dict[str, Any]:
        return {"status": "unknown"}
