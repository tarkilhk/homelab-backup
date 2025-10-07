from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from typing import Any, Dict
import logging

from app.core.plugins.base import BackupContext, BackupPlugin, RestoreContext
from app.core.plugins.restore_utils import copy_artifact_for_restore

BACKUP_BASE_PATH = "/backups"


class MySQLPlugin(BackupPlugin):
    """MySQL backup plugin executed via a temporary Docker container.
    Research notes:
    - `mysqldump` is the standard utility to export a MySQL database.
    - To avoid relying on host binaries, this plugin runs `mysqldump` inside the
      official `mysql` container and uses `aiomysql` for connectivity tests.
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
        """Check database connectivity using aiomysql."""
        if not await self.validate_config(config):
            return False
        host = str(config.get("host"))
        port = int(config.get("port", 3306))
        user = str(config.get("user"))
        password = str(config.get("password"))
        database = str(config.get("database"))

        try:
            import aiomysql  # type: ignore
        except Exception as exc:  # pragma: no cover - environment dependent
            self._logger.warning("aiomysql_not_available | error=%s", exc)
            return False

        conn = None
        try:
            conn = await aiomysql.connect(
                host=host,
                port=port,
                user=user,
                password=password,
                db=database,
            )
            async with conn.cursor() as cur:
                await cur.execute("SELECT 1")
                row = await cur.fetchone()
            return bool(row) and row[0] == 1
        except Exception as exc:
            self._logger.warning("mysql_test_failed | host=%s error=%s", host, exc)
            return False
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    async def backup(self, context: BackupContext) -> Dict[str, Any]:
        cfg = getattr(context, "config", {}) or {}
        host = str(cfg.get("host"))
        port = int(cfg.get("port", 3306))
        user = str(cfg.get("user"))
        password = str(cfg.get("password"))
        database = str(cfg.get("database"))
        if not host or not user or not password or not database:
            raise ValueError("mysql config requires host, user, password, database")

        meta = context.metadata or {}
        target_slug = meta.get("target_slug") or str(context.target_id)
        today = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
        base_dir = os.path.join(BACKUP_BASE_PATH, target_slug, today)
        os.makedirs(base_dir, exist_ok=True)
        timestamp = datetime.now(timezone.utc).astimezone().strftime("%Y%m%dT%H%M%S")
        artifact_path = os.path.join(base_dir, f"mysql-dump-{timestamp}.sql")

        cmd = [
            "docker",
            "run",
            "--rm",
            "-e",
            f"MYSQL_PWD={password}",
            "mysql:8",
            "mysqldump",
            "-h",
            host,
            "-P",
            str(port),
            "-u",
            user,
            database,
        ]

        self._logger.info(
            "mysql_backup_start | job_id=%s target_id=%s target_slug=%s host=%s artifact=%s",
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
                "mysqldump_exec_error | job_id=%s target_id=%s error=%s",
                context.job_id,
                context.target_id,
                exc,
            )
            raise
        if proc.returncode != 0:
            err = stderr_data.decode(errors="ignore").strip()
            raise RuntimeError(f"mysqldump failed: {err}")

        with open(artifact_path, "wb") as fh:
            fh.write(stdout_data)
        return {"artifact_path": artifact_path}

    async def restore(self, context: RestoreContext) -> Dict[str, Any]:
        """Restore a MySQL database from a SQL dump file using mysql command.
        
        Executes the mysql command to import the SQL dump back into the database.
        """
        cfg = context.config or {}
        host = str(cfg.get("host"))
        port = int(cfg.get("port", 3306))
        user = str(cfg.get("user"))
        password = str(cfg.get("password"))
        database = str(cfg.get("database"))
        
        if not host or not user or not password or not database:
            raise ValueError("mysql config requires host, user, password, database")
        
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
        
        # Use Docker to run mysql command similar to how backup uses mysqldump
        cmd = [
            "docker",
            "run",
            "--rm",
            "-i",
            "-e",
            f"MYSQL_PWD={password}",
            "-v",
            f"{artifact_path}:/backup.sql:ro",
            "mysql:8",
            "mysql",
            "-h",
            host,
            "-P",
            str(port),
            "-u",
            user,
            database,
            "-e",
            "source /backup.sql",
        ]
        
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_data, stderr_data = await proc.communicate()
        except OSError as exc:
            self._logger.error(
                "mysql_restore_exec_error | job_id=%s source=%s dest=%s error=%s",
                context.job_id,
                context.source_target_id,
                context.destination_target_id,
                exc,
            )
            raise
        
        if proc.returncode != 0:
            err = stderr_data.decode(errors="ignore").strip()
            raise RuntimeError(f"mysql restore failed: {err}")
        
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

    async def get_status(self, context: BackupContext) -> Dict[str, Any]:  # pragma: no cover - not implemented
        return {"status": "unknown"}
