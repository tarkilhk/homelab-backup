from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from typing import Any, Dict
import logging

from app.core.plugins.base import BackupContext, BackupPlugin, RestoreContext
from app.core.plugins.restore_utils import copy_artifact_for_restore

BACKUP_BASE_PATH = "/backups"


class PostgreSQLPlugin(BackupPlugin):
    """PostgreSQL backup plugin executed via a temporary Docker container.
    Research notes:
    - `pg_dump` is the standard utility to export a PostgreSQL database into a
      script file or archive format.
    - Because the host environment may not ship PostgreSQL client binaries,
      this plugin runs `pg_dump` inside the official `postgres` container and
      uses `pg_dump --schema-only` to verify connectivity.
    SQL dumps are stored under
    `/backups/<slug>/<date>/postgresql-dump-<timestamp>.sql`.
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
        # Database is optional; if provided, must be a string
        if database is not None and not isinstance(database, str):
            return False
        return True

    async def test(self, config: Dict[str, Any]) -> bool:
        """Check database connectivity using an async PostgreSQL driver (no Docker/binaries)."""
        if not await self.validate_config(config):
            return False
        host = str(config.get("host"))
        port = int(config.get("port", 5432))
        user = str(config.get("user"))
        password = str(config.get("password"))
        database = config.get("database") or "postgres"  # Default to 'postgres' DB for testing

        # Import locally so the module remains importable even if the optional
        # dependency is not installed. We fail the test() gracefully in that case.
        try:
            import asyncpg  # type: ignore
        except Exception as exc:  # pragma: no cover - environment dependent
            self._logger.warning("asyncpg_not_available | error=%s", exc)
            return False

        conn = None
        try:
            conn = await asyncpg.connect(
                host=host,
                port=port,
                user=user,
                password=password,
                database=database,
            )
            value = await conn.fetchval("SELECT 1")
            return value == 1
        except Exception as exc:
            self._logger.warning("postgresql_test_failed | host=%s error=%s", host, exc)
            return False
        finally:
            if conn is not None:
                try:
                    await conn.close()
                except Exception:
                    pass

    async def backup(self, context: BackupContext) -> Dict[str, Any]:
        cfg = getattr(context, "config", {}) or {}
        host = str(cfg.get("host"))
        port = int(cfg.get("port", 5432))
        user = str(cfg.get("user"))
        password = str(cfg.get("password"))
        database = cfg.get("database", "").strip()
        if not host or not user or not password:
            raise ValueError("postgresql config requires host, user, password")

        meta = context.metadata or {}
        target_slug = meta.get("target_slug") or str(context.target_id)
        today = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
        base_dir = os.path.join(BACKUP_BASE_PATH, target_slug, today)
        os.makedirs(base_dir, exist_ok=True)
        timestamp = datetime.now(timezone.utc).astimezone().strftime("%Y%m%dT%H%M%S")
        
        # Use pg_dumpall for all databases, pg_dump for single database
        # Run pg_dump/pg_dumpall directly (installed in container) instead of via Docker
        env = os.environ.copy()
        env["PGPASSWORD"] = password
        
        if database:
            artifact_path = os.path.join(base_dir, f"postgresql-dump-{timestamp}.sql")
            cmd = [
                "pg_dump",
                "-h",
                host,
                "-p",
                str(port),
                "-U",
                user,
                database,
            ]
            log_msg = f"postgresql_backup_start | job_id={context.job_id} target_id={context.target_id} target_slug={target_slug} host={host} database={database} artifact={artifact_path}"
        else:
            artifact_path = os.path.join(base_dir, f"postgresql-dumpall-{timestamp}.sql")
            cmd = [
                "pg_dumpall",
                "-h",
                host,
                "-p",
                str(port),
                "-U",
                user,
            ]
            log_msg = f"postgresql_backup_start | job_id={context.job_id} target_id={context.target_id} target_slug={target_slug} host={host} database=all artifact={artifact_path}"

        self._logger.info(log_msg)

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            stdout_data, stderr_data = await proc.communicate()
        except OSError as exc:
            self._logger.error(
                "pg_dump_exec_error | job_id=%s target_id=%s error=%s",
                context.job_id,
                context.target_id,
                exc,
            )
            raise
        if proc.returncode != 0:
            err = stderr_data.decode(errors="ignore").strip()
            cmd_name = "pg_dumpall" if not database else "pg_dump"
            raise RuntimeError(f"{cmd_name} failed: {err}")

        with open(artifact_path, "wb") as fh:
            fh.write(stdout_data)
        return {"artifact_path": artifact_path}

    async def restore(self, context: RestoreContext) -> Dict[str, Any]:
        """Restore a PostgreSQL database from a SQL dump file using psql.
        
        For single database dumps (pg_dump): restores to the specified database
        For all database dumps (pg_dumpall): restores to all databases
        """
        cfg = context.config or {}
        host = str(cfg.get("host"))
        port = int(cfg.get("port", 5432))
        user = str(cfg.get("user"))
        password = str(cfg.get("password"))
        database = cfg.get("database", "").strip()
        
        if not host or not user or not password:
            raise ValueError("postgresql config requires host, user, password")
        
        artifact_path = context.artifact_path
        if not artifact_path or not os.path.exists(artifact_path):
            raise FileNotFoundError(f"Artifact not found: {artifact_path}")
        
        # Determine if this is a dumpall or single database dump based on filename
        is_dumpall = "dumpall" in os.path.basename(artifact_path)
        
        env = os.environ.copy()
        env["PGPASSWORD"] = password
        
        if is_dumpall:
            # For pg_dumpall dumps, restore to postgres database (system database)
            # psql will execute all CREATE DATABASE and connect statements in the dump
            cmd = [
                "psql",
                "-h",
                host,
                "-p",
                str(port),
                "-U",
                user,
                "-d",
                "postgres",  # Connect to postgres database for dumpall
                "-f",
                artifact_path,
            ]
            log_msg = f"postgresql_restore_start | job_id={context.job_id} source={context.source_target_id} dest={context.destination_target_id} host={host} database=all artifact={artifact_path}"
        else:
            # For pg_dump dumps, restore to the specified database
            target_db = database or "postgres"
            cmd = [
                "psql",
                "-h",
                host,
                "-p",
                str(port),
                "-U",
                user,
                "-d",
                target_db,
                "-f",
                artifact_path,
            ]
            log_msg = f"postgresql_restore_start | job_id={context.job_id} source={context.source_target_id} dest={context.destination_target_id} host={host} database={target_db} artifact={artifact_path}"
        
        self._logger.info(log_msg)
        
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            stdout_data, stderr_data = await proc.communicate()
        except OSError as exc:
            self._logger.error(
                "psql_exec_error | job_id=%s source=%s dest=%s error=%s",
                context.job_id,
                context.source_target_id,
                context.destination_target_id,
                exc,
            )
            raise
        
        if proc.returncode != 0:
            err = stderr_data.decode(errors="ignore").strip()
            raise RuntimeError(f"psql restore failed: {err}")
        
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

    async def get_status(self, context: BackupContext) -> Dict[str, Any]:  # pragma: no cover - not implemented
        return {"status": "unknown"}
