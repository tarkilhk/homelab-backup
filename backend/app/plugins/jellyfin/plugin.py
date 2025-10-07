from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict

import httpx
import logging

from app.core.plugins.base import BackupContext, BackupPlugin, RestoreContext
from app.core.plugins.restore_utils import copy_artifact_for_restore

BACKUP_BASE = "/backups"


class JellyfinPlugin(BackupPlugin):
    """Jellyfin backup plugin using the Backup plugin API.

    Research notes:
    - Jellyfin exposes an optional Backup plugin that can generate a ZIP archive
      of server configuration and metadata.
    - API access uses an administrator API key supplied via the `X-Emby-Token` header.
    - `GET /System/Info` is a lightweight connectivity check.
    - `GET /Backup/Archive` streams a ZIP archive of the backup.
    """

    def __init__(self, name: str, version: str = "0.1.0") -> None:
        super().__init__(name=name, version=version)
        self._logger = logging.getLogger(__name__)

    async def validate_config(self, config: Dict[str, Any]) -> bool:  # pragma: no cover - trivial
        if not isinstance(config, dict):
            return False
        base_url = config.get("base_url")
        api_key = config.get("api_key")
        if not base_url or not isinstance(base_url, str):
            return False
        if not api_key or not isinstance(api_key, str):
            return False
        return True

    async def test(self, config: Dict[str, Any]) -> bool:
        """Connectivity test against Jellyfin."""
        if not await self.validate_config(config):
            return False

        base_url = str(config.get("base_url", "")).rstrip("/")
        api_key = str(config.get("api_key", ""))
        info_url = f"{base_url}/System/Info"

        try:
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
                resp = await client.get(info_url, headers={"X-Emby-Token": api_key})
                if resp.status_code // 100 != 2:
                    self._logger.warning(
                        "jellyfin_test_non_2xx | url=%s status=%s", info_url, resp.status_code
                    )
                    return False
                data: Dict[str, Any] = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            self._logger.warning(
                "jellyfin_test_error | url=%s error=%s", info_url, exc
            )
            return False

        return isinstance(data, dict) and bool(data.get("Version"))

    async def backup(self, context: BackupContext) -> Dict[str, Any]:
        meta = context.metadata or {}
        target_slug = meta.get("target_slug") or str(context.target_id)
        today = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")

        base_dir = os.path.join(BACKUP_BASE, target_slug, today)
        os.makedirs(base_dir, exist_ok=True)

        ts = datetime.now(timezone.utc).astimezone().strftime("%Y%m%dT%H%M%S")
        artifact_path = os.path.join(base_dir, f"jellyfin-backup-{ts}.zip")

        cfg = getattr(context, "config", {}) or {}
        base_url = str(cfg.get("base_url", "")).rstrip("/")
        api_key = cfg.get("api_key")
        if not base_url or not api_key:
            raise ValueError("Jellyfin config must include base_url and api_key")

        backup_url = f"{base_url}/Backup/Archive"
        headers = {
            "X-Emby-Token": str(api_key),
            "Accept": "application/zip, application/octet-stream",
        }
        self._logger.info(
            "jellyfin_backup_start | job_id=%s target_id=%s target_slug=%s url=%s artifact=%s",
            context.job_id,
            context.target_id,
            target_slug,
            backup_url,
            artifact_path,
        )
        try:
            async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
                resp = await client.get(backup_url, headers=headers)
                self._logger.info(
                    "jellyfin_backup_response | job_id=%s target_id=%s status=%s bytes=%s",
                    context.job_id,
                    context.target_id,
                    resp.status_code,
                    len(resp.content or b""),
                )
                resp.raise_for_status()
                content = resp.content
        except httpx.HTTPError as exc:
            self._logger.error(
                "jellyfin_backup_http_error | job_id=%s target_id=%s error=%s",
                context.job_id,
                context.target_id,
                str(exc),
            )
            raise

        if not content:
            raise RuntimeError("Jellyfin backup returned no content")

        with open(artifact_path, "wb") as f:
            f.write(content)
        self._logger.info(
            "jellyfin_backup_success | job_id=%s target_id=%s artifact=%s size_bytes=%s",
            context.job_id,
            context.target_id,
            artifact_path,
            len(content),
        )

        return {"artifact_path": artifact_path}

    async def restore(self, context: RestoreContext) -> Dict[str, Any]:
        """Restore a Jellyfin backup.
        
        Note: Jellyfin's Backup plugin manages restoration. This function copies the
        backup file to a restore directory. To complete the restore:
        1. Stop the Jellyfin server
        2. Extract the backup ZIP to the Jellyfin config directory
        3. Restart Jellyfin server
        
        The backup ZIP contains configuration files and metadata but typically not media files.
        """
        return copy_artifact_for_restore(
            context,
            logger=self._logger,
            restore_root=BACKUP_BASE,
            prefix="jellyfin",
        )

    async def get_status(self, context: BackupContext) -> Dict[str, Any]:  # pragma: no cover - trivial
        return {"status": "ok"}
