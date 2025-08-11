from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict, List

import httpx
from app.core.plugins.base import BackupContext, BackupPlugin
import logging


class LidarrPlugin(BackupPlugin):
    """Lidarr backup plugin using Servarr API.

    Research notes:
    - The Lidarr UI exposes a Backup section allowing manual and scheduled backups.
    - Documentation describes manual backups via System ➜ Backup and restoring from zip archives.
    - The underlying API uses the Servarr command endpoint to trigger a backup and exposes
      existing backups through `/api/v1/system/backup`.
    - Backups include the app's configuration directory, which lives under paths such as
      `/config` when running in Docker.
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
        """Check connectivity to Lidarr using provided API key."""
        if not await self.validate_config(config):
            return False
        base_url = str(config.get("base_url", "")).rstrip("/")
        api_key = config.get("api_key")
        status_url = f"{base_url}/api/v1/system/status"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(status_url, headers={"X-Api-Key": str(api_key)})
                if resp.status_code // 100 != 2:
                    self._logger.warning(
                        "lidarr_test_non_2xx | url=%s status=%s", status_url, resp.status_code
                    )
                    return False
        except httpx.HTTPError as exc:
            self._logger.warning("lidarr_test_error | url=%s error=%s", status_url, exc)
            return False
        return True

    async def backup(self, context: BackupContext) -> Dict[str, Any]:
        meta = context.metadata or {}
        target_slug = meta.get("target_slug") or str(context.target_id)
        today = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
        base_dir = os.path.join("/backups", target_slug, today)
        os.makedirs(base_dir, exist_ok=True)
        timestamp = datetime.now(timezone.utc).astimezone().strftime("%Y%m%dT%H%M%S")
        artifact_path = os.path.join(base_dir, f"lidarr-backup-{timestamp}.zip")

        cfg = context.config or {}
        base_url = str(cfg.get("base_url", "")).rstrip("/")
        api_key = cfg.get("api_key")
        if not base_url or not api_key:
            raise ValueError("Lidarr config must include base_url and api_key")

        command_url = f"{base_url}/api/v1/command"
        backup_list_url = f"{base_url}/api/v1/system/backup"
        headers = {"X-Api-Key": str(api_key)}

        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            # Trigger a backup via the command endpoint
            try:
                await client.post(command_url, json={"name": "Backup"}, headers=headers)
            except httpx.HTTPError as exc:
                self._logger.error(
                    "lidarr_backup_command_error | job_id=%s target_id=%s error=%s",
                    context.job_id,
                    context.target_id,
                    exc,
                )
                raise

            # Retrieve list of backups and pick the most recent one
            resp = await client.get(backup_list_url, headers=headers)
            resp.raise_for_status()
            backups: List[Dict[str, Any]] = resp.json() if resp.content else []
            if not backups:
                raise RuntimeError("Lidarr backup list empty")
            latest = sorted(backups, key=lambda b: b.get("time", ""), reverse=True)[0]
            backup_id = latest.get("id")
            if backup_id is None:
                raise RuntimeError("Latest backup lacks id")

            download_url = f"{backup_list_url}/{backup_id}"
            dl_resp = await client.get(download_url, headers=headers)
            dl_resp.raise_for_status()
            with open(artifact_path, "wb") as fh:
                fh.write(dl_resp.content)

        return {"artifact_path": artifact_path}

    async def restore(self, context: BackupContext) -> Dict[str, Any]:  # pragma: no cover - not implemented
        return {"status": "not_implemented"}

    async def get_status(self, context: BackupContext) -> Dict[str, Any]:  # pragma: no cover - trivial
        return {"status": "ok"}
