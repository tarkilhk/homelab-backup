from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from app.core.plugins.base import BackupContext, BackupPlugin, RestoreContext
from app.core.plugins.restore_utils import copy_artifact_for_restore
from app.core.plugins.sidecar import write_backup_sidecar


class CalcomPlugin(BackupPlugin):
    """Backup Cal.com by dumping its PostgreSQL database.

    Research notes:
    - Cal.com requires PostgreSQL for storage.
    - `pg_dump` is the standard utility for backing up PostgreSQL databases.
    """

    def __init__(self, name: str, version: str = "0.1.0", base_dir: str = "/backups") -> None:
        super().__init__(name=name, version=version)
        self.base_dir = base_dir
        self._logger = logging.getLogger(__name__)

    async def validate_config(self, config: Dict[str, Any]) -> bool:  # pragma: no cover - trivial
        if not isinstance(config, dict):
            return False
        url = config.get("database_url")
        return isinstance(url, str) and bool(url)

    async def test(self, config: Dict[str, Any]) -> bool:
        if not await self.validate_config(config):
            raise ValueError("Invalid configuration: database_url is required")
        db_url = str(config["database_url"])
        try:
            proc = await asyncio.create_subprocess_exec(
                "pg_isready",
                "-d",
                db_url,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()
            if proc.returncode != 0:
                raise ConnectionError(f"PostgreSQL database is not ready (return code {proc.returncode})")
            return True
        except FileNotFoundError:
            self._logger.warning("pg_isready_not_found")
            raise FileNotFoundError("pg_isready command not found. Please ensure PostgreSQL client tools are installed.")
        except ConnectionError:
            raise

    async def backup(self, context: BackupContext) -> Dict[str, Any]:
        cfg = getattr(context, "config", {}) or {}
        db_url = str(cfg.get("database_url", ""))
        if not db_url:
            raise ValueError("database_url is required")

        meta = context.metadata or {}
        slug = meta.get("target_slug") or str(context.target_id)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        base_dir = Path(self.base_dir) / slug / today
        base_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        artifact_path = base_dir / f"calcom-db-{timestamp}.sql"

        proc = await asyncio.create_subprocess_exec(
            "pg_dump",
            db_url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            self._logger.error(
                "calcom_pg_dump_failed | code=%s stderr=%s", proc.returncode, stderr.decode()
            )
            raise RuntimeError("pg_dump failed")

        with open(artifact_path, "wb") as f:
            f.write(stdout)

        write_backup_sidecar(str(artifact_path), self, context, logger=self._logger)

        return {"artifact_path": str(artifact_path)}

    async def restore(self, context: RestoreContext) -> Dict[str, Any]:
        """Restore a Cal.com PostgreSQL database from a SQL dump file using psql."""
        cfg = context.config or {}
        db_url = str(cfg.get("database_url", ""))
        
        if not db_url:
            raise ValueError("database_url is required for restore")
        
        artifact_path = context.artifact_path
        if not artifact_path or not os.path.exists(artifact_path):
            raise FileNotFoundError(f"Artifact not found: {artifact_path}")
        
        self._logger.info(
            "calcom_restore_start | job_id=%s source=%s dest=%s artifact=%s",
            context.job_id,
            context.source_target_id,
            context.destination_target_id,
            artifact_path,
        )
        
        try:
            proc = await asyncio.create_subprocess_exec(
                "psql",
                db_url,
                "-f",
                artifact_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_data, stderr_data = await proc.communicate()
        except OSError as exc:
            self._logger.error(
                "calcom_restore_exec_error | job_id=%s source=%s dest=%s error=%s",
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
            "calcom_restore_success | job_id=%s source=%s dest=%s artifact=%s bytes=%s",
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

    async def get_status(self, context: BackupContext) -> Dict[str, Any]:  # pragma: no cover - trivial
        return {"status": "not implemented"}
