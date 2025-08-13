from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from app.core.plugins.base import BackupContext, BackupPlugin


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
            return False
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
            return proc.returncode == 0
        except FileNotFoundError:
            self._logger.warning("pg_isready_not_found")
            return False

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

        return {"artifact_path": str(artifact_path)}

    async def restore(self, context: BackupContext) -> Dict[str, Any]:  # pragma: no cover - not implemented
        raise NotImplementedError("Restore is not supported for Cal.com")

    async def get_status(self, context: BackupContext) -> Dict[str, Any]:  # pragma: no cover - trivial
        return {"status": "not implemented"}
