from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict

import httpx
from app.core.plugins.base import BackupContext, BackupPlugin
import logging


class RadarrPlugin(BackupPlugin):
    """Radarr backup plugin using system backup endpoint.

    Research summary:
    - Radarr exposes an HTTP API secured with an API key supplied via the
      ``X-Api-Key`` header.
    - ``GET /api/v3/system/status`` returns instance metadata and is a
      non-destructive way to verify connectivity.
    - ``GET /api/v3/system/backup`` returns a ZIP archive containing the
      database and configuration files.
    - Backups are written to ``/backups/<slug>/<YYYY-MM-DD>/radarr-backup-<ts>.zip``.
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
        if not await self.validate_config(config):
            return False

        base_url = str(config.get("base_url", "")).rstrip("/")
        api_key = config.get("api_key")
        url = f"{base_url}/api/v3/system/status"
        headers = {"X-Api-Key": str(api_key)}

        try:
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
                resp = await client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            self._logger.warning("radarr_test_http_error | url=%s error=%s", url, exc)
            return False

        if resp.status_code // 100 != 2:
            self._logger.warning("radarr_test_status | url=%s status=%s", url, resp.status_code)
            return False

        try:
            data: Dict[str, Any] = resp.json()
        except ValueError:
            return False

        return bool(data.get("version"))

    async def backup(self, context: BackupContext) -> Dict[str, Any]:
        meta = context.metadata or {}
        target_slug = meta.get("target_slug") or str(context.target_id)
        today = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
        base_dir = os.path.join("/backups", target_slug, today)
        os.makedirs(base_dir, exist_ok=True)

        timestamp = datetime.now(timezone.utc).astimezone().strftime("%Y%m%dT%H%M%S")
        artifact_path = os.path.join(base_dir, f"radarr-backup-{timestamp}.zip")

        cfg = getattr(context, "config", {}) or {}
        base_url = str(cfg.get("base_url", "")).rstrip("/")
        api_key = cfg.get("api_key")
        if not base_url or not api_key:
            raise ValueError("Radarr config must include base_url and api_key")

        backup_url = f"{base_url}/api/v3/system/backup"
        headers = {"X-Api-Key": str(api_key)}

        self._logger.info(
            "radarr_backup_request | job_id=%s target_id=%s url=%s artifact=%s",
            context.job_id,
            context.target_id,
            backup_url,
            artifact_path,
        )

        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            try:
                resp = await client.get(backup_url, headers=headers)
                self._logger.info(
                    "radarr_backup_response | job_id=%s target_id=%s status=%s bytes=%s",
                    context.job_id,
                    context.target_id,
                    resp.status_code,
                    len(resp.content or b""),
                )
                resp.raise_for_status()
                content = resp.content
            except httpx.HTTPError as exc:
                self._logger.error(
                    "radarr_backup_http_error | job_id=%s target_id=%s error=%s",
                    context.job_id,
                    context.target_id,
                    exc,
                )
                raise

        if not content:
            raise RuntimeError("Radarr backup returned no content")

        with open(artifact_path, "wb") as f:
            f.write(content)

        self._logger.info(
            "radarr_backup_success | job_id=%s target_id=%s artifact=%s size_bytes=%s",
            context.job_id,
            context.target_id,
            artifact_path,
            len(content),
        )

        return {"artifact_path": artifact_path}

    async def restore(self, context: BackupContext) -> Dict[str, Any]:  # pragma: no cover - not implemented
        return {"status": "not_implemented"}

    async def get_status(self, context: BackupContext) -> Dict[str, Any]:  # pragma: no cover - trivial
        return {"status": "ok"}
