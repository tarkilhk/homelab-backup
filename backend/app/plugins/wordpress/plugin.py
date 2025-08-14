from __future__ import annotations

import asyncio
import os
import tarfile
import tempfile
from datetime import datetime, timezone
from typing import Any, Dict

from app.core.plugins.base import BackupContext, BackupPlugin
import logging


class WordPressPlugin(BackupPlugin):
    """WordPress backup via WP-CLI.

    WordPress documentation notes that a full backup requires both the
    database and the site files.
    WP-CLI provides a ``db export`` command to dump the database to a
    file for backups.
    This plugin uses WP-CLI to export the database and then archives the
    site directory along with the dump into ``tar.gz``.
    """

    def __init__(self, name: str, version: str = "0.1.0") -> None:
        super().__init__(name=name, version=version)
        self._logger = logging.getLogger(__name__)

    async def validate_config(self, config: Dict[str, Any]) -> bool:  # pragma: no cover - trivial
        if not isinstance(config, dict):
            return False
        site_path = config.get("site_path")
        if not site_path or not isinstance(site_path, str):
            return False
        return True

    async def test(self, config: Dict[str, Any]) -> bool:
        if not await self.validate_config(config):
            return False
        site_path = str(config.get("site_path"))
        wp_path = str(config.get("wp_path", "wp"))
        try:
            proc = await asyncio.create_subprocess_exec(
                wp_path,
                "--path",
                site_path,
                "core",
                "is-installed",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()
            return proc.returncode == 0
        except OSError as exc:
            self._logger.warning("wordpress_test_error | error=%s", exc)
            return False

    async def backup(self, context: BackupContext) -> Dict[str, Any]:
        meta = context.metadata or {}
        target_slug = meta.get("target_slug") or str(context.target_id)
        today = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")

        # Determine backup base directory with overrides and safe fallback
        cfg = getattr(context, "config", {}) or {}
        backup_base = str(
            cfg.get("backup_base_path")
            or os.environ.get("BACKUP_BASE_PATH")
            or "/backups"
        )

        base_dir = os.path.join(backup_base, target_slug, today)
        try:
            os.makedirs(base_dir, exist_ok=True)
        except PermissionError:
            # Fall back to a temp-writable location to avoid permission issues
            fallback_root = os.path.join(tempfile.gettempdir(), "backups")
            base_dir = os.path.join(fallback_root, target_slug, today)
            os.makedirs(base_dir, exist_ok=True)

        timestamp = datetime.now(timezone.utc).astimezone().strftime("%Y%m%dT%H%M%S")
        artifact_path = os.path.join(base_dir, f"wordpress-backup-{timestamp}.tar.gz")

        site_path = str(cfg.get("site_path", ""))
        wp_path = str(cfg.get("wp_path", "wp"))
        if not site_path:
            raise ValueError("WordPress config must include site_path")

        self._logger.info(
            "wordpress_backup_start | job_id=%s target_id=%s site_path=%s artifact=%s",
            context.job_id,
            context.target_id,
            site_path,
            artifact_path,
        )

        tmpdir = tempfile.mkdtemp()
        db_path = os.path.join(tmpdir, "db.sql")

        proc = await asyncio.create_subprocess_exec(
            wp_path,
            "--path",
            site_path,
            "db",
            "export",
            db_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            self._logger.error(
                "wordpress_db_export_failed | code=%s stdout=%s stderr=%s",
                proc.returncode,
                stdout.decode(errors="ignore"),
                stderr.decode(errors="ignore"),
            )
            raise RuntimeError("wp db export failed")

        with tarfile.open(artifact_path, "w:gz") as tar:
            tar.add(site_path, arcname="site")
            tar.add(db_path, arcname="db.sql")

        self._logger.info(
            "wordpress_backup_success | job_id=%s target_id=%s artifact=%s",
            context.job_id,
            context.target_id,
            artifact_path,
        )
        return {"artifact_path": artifact_path}

    async def restore(self, context: BackupContext) -> Dict[str, Any]:  # pragma: no cover - not implemented
        return {"status": "not_implemented"}

    async def get_status(self, context: BackupContext) -> Dict[str, Any]:  # pragma: no cover - trivial
        return {"status": "ok"}
