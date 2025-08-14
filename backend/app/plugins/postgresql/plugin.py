from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from typing import Any, Dict
import logging

from app.core.plugins.base import BackupContext, BackupPlugin

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
        if not database or not isinstance(database, str):
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
        database = str(config.get("database"))

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
        database = str(cfg.get("database"))
        if not host or not user or not password or not database:
            raise ValueError("postgresql config requires host, user, password, database")

        meta = context.metadata or {}
        target_slug = meta.get("target_slug") or str(context.target_id)
        today = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
        base_dir = os.path.join(BACKUP_BASE_PATH, target_slug, today)
        os.makedirs(base_dir, exist_ok=True)
        timestamp = datetime.now(timezone.utc).astimezone().strftime("%Y%m%dT%H%M%S")
        artifact_path = os.path.join(base_dir, f"postgresql-dump-{timestamp}.sql")

        cmd = [
            "docker",
            "run",
            "--rm",
            "-e",
            f"PGPASSWORD={password}",
            "postgres:16-alpine",
            "pg_dump",
            "-h",
            host,
            "-p",
            str(port),
            "-U",
            user,
            database,
        ]

        self._logger.info(
            "postgresql_backup_start | job_id=%s target_id=%s target_slug=%s host=%s artifact=%s",
            context.job_id,
            context.target_id,
            target_slug,
            host,
            artifact_path,
        )

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
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
            raise RuntimeError(f"pg_dump failed: {err}")

        with open(artifact_path, "wb") as fh:
            fh.write(stdout_data)
        return {"artifact_path": artifact_path}

    async def restore(self, context: BackupContext) -> Dict[str, Any]:  # pragma: no cover - not implemented
        raise NotImplementedError("Restore operation is not implemented")

    async def get_status(self, context: BackupContext) -> Dict[str, Any]:  # pragma: no cover - not implemented
        return {"status": "unknown"}