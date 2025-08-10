from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict

import httpx
import logging

from app.core.plugins.base import BackupContext, BackupPlugin


class SonarrPlugin(BackupPlugin):
    """Sonarr backup plugin using built-in backup API.

    Research notes: Sonarr's UI exposes backups via *System → Backup* which generates
    a zip archive for download. This plugin automates that by calling the
    `/api/v3/system/backup` endpoint with an API key for authentication.
    """

    backup_root = "/backups"

    def __init__(self, name: str, version: str = "0.1.0") -> None:
        super().__init__(name=name, version=version)
        self._logger = logging.getLogger(__name__)

    async def validate_config(self, config: Dict[str, Any]) -> bool:  # pragma: no cover - trivial
        if not isinstance(config, dict):
            return False
        base_url = config.get("base_url")
        api_key = config.get("api_key")
        return bool(base_url) and isinstance(base_url, str) and bool(api_key) and isinstance(api_key, str)

    async def test(self, config: Dict[str, Any]) -> bool:
        """Verify connectivity by querying the system status endpoint."""
        if not await self.validate_config(config):
            return False
        base_url = str(config.get("base_url", "")).rstrip("/")
        api_key = str(config.get("api_key", ""))
        status_url = f"{base_url}/api/v3/system/status"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(status_url, headers={"X-Api-Key": api_key})
                return resp.status_code == 200
        except httpx.HTTPError as exc:  # pragma: no cover - network errors
            self._logger.warning("sonarr_test_error | url=%s error=%s", status_url, exc)
            return False

    async def backup(self, context: BackupContext) -> Dict[str, Any]:
        meta = context.metadata or {}
        target_slug = meta.get("target_slug") or str(context.target_id)
        today = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
        base_dir = os.path.join(self.backup_root, target_slug, today)
        os.makedirs(base_dir, exist_ok=True)
        timestamp = datetime.now(timezone.utc).astimezone().strftime("%Y%m%dT%H%M%S")
        artifact_path = os.path.join(base_dir, f"sonarr-backup-{timestamp}.zip")

        cfg = context.config or {}
        base_url = str(cfg.get("base_url", "")).rstrip("/")
        api_key = str(cfg.get("api_key", ""))
        if not base_url or not api_key:
            raise ValueError("Sonarr config must include base_url and api_key")

        backup_url = f"{base_url}/api/v3/system/backup"
        self._logger.info(
            "sonarr_backup_request | job_id=%s target_id=%s url=%s artifact=%s",
            context.job_id,
            context.target_id,
            backup_url,
            artifact_path,
        )
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(backup_url, headers={"X-Api-Key": api_key})
            resp.raise_for_status()
            with open(artifact_path, "wb") as fp:
                fp.write(resp.content)
        return {"artifact_path": artifact_path}

    async def restore(self, context: BackupContext) -> Dict[str, Any]:
        raise NotImplementedError("Restore is not implemented for Sonarr")

    async def get_status(self, context: BackupContext) -> Dict[str, Any]:
        return {}
