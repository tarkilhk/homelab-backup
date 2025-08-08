from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict

import httpx
from app.core.plugins.base import BackupContext, BackupPlugin
import logging


class PiHolePlugin(BackupPlugin):
    """Pi-hole backup plugin using Teleporter export.

    Performs an authenticated download of the Teleporter archive (ZIP)
    and stores it under the conventional backup directory.
    """

    def __init__(self, name: str, version: str = "0.1.0") -> None:
        super().__init__(name=name, version=version)
        self._logger = logging.getLogger(__name__)

    async def validate_config(self, config: Dict[str, Any]) -> bool:  # pragma: no cover - trivial
        # Minimal validation: ensure required keys exist
        if not isinstance(config, dict):
            return False
        base_url = config.get("base_url")
        token = config.get("token") or config.get("api_token")
        if not base_url or not isinstance(base_url, str):
            return False
        if not token or not isinstance(token, str):
            return False
        return True

    async def test(self, config: Dict[str, Any]) -> bool:
        """Connectivity test using provided configuration.

        Dummy implementation: reuse validate_config to check for required fields.
        """
        return await self.validate_config(config)

    async def backup(self, context: BackupContext) -> Dict[str, Any]:
        # Determine directories following convention: /backups/<targetSlug>/<YYYY-MM-DD>/
        # We derive slug from context.metadata["target_slug"] if available, else use target_id.
        meta = context.metadata or {}
        target_slug = meta.get("target_slug") or str(context.target_id)
        today = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")

        base_dir = os.path.join("/backups", target_slug, today)
        os.makedirs(base_dir, exist_ok=True)

        timestamp = datetime.now(timezone.utc).astimezone().strftime("%Y%m%dT%H%M%S")
        artifact_path = os.path.join(base_dir, f"pihole-teleporter-{timestamp}.zip")

        # Read config
        cfg = getattr(context, "config", {}) or {}
        base_url = str(cfg.get("base_url", "")).rstrip("/")
        token = cfg.get("token") or cfg.get("api_token")
        if not base_url or not token:
            raise ValueError("Pi-hole config must include base_url and token")
        self._logger.info(
            "pihole_backup_start | job_id=%s target_id=%s target_slug=%s base_url=%s artifact=%s",
            context.job_id,
            context.target_id,
            target_slug,
            base_url,
            artifact_path,
        )

        # Pi-hole v6 Teleporter endpoint: GET /api/teleporter
        teleporter_url = f"{base_url}/api/teleporter"
        # Primary auth (v6): X-API-Key header
        headers_primary = {
            "X-API-Key": str(token),
            "Accept": "application/zip, application/octet-stream",
        }
        # Secondary auth retry (some setups): Authorization: Bearer <token>
        headers_retry = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/zip, application/octet-stream",
        }

        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            try:
                self._logger.info(
                    "pihole_backup_request | job_id=%s target_id=%s url=%s auth=header:X-API-Key",
                    context.job_id,
                    context.target_id,
                    teleporter_url,
                )
                resp = await client.get(teleporter_url, headers=headers_primary)
                self._logger.info(
                    "pihole_backup_response | job_id=%s target_id=%s status=%s bytes=%s",
                    context.job_id,
                    context.target_id,
                    resp.status_code,
                    len(resp.content or b""),
                )
                if resp.status_code == 401:
                    # Retry with Bearer auth for instances expecting that format
                    self._logger.info(
                        "pihole_backup_retry | job_id=%s target_id=%s url=%s auth=Authorization:Bearer",
                        context.job_id,
                        context.target_id,
                        teleporter_url,
                    )
                    resp = await client.get(teleporter_url, headers=headers_retry)
                    self._logger.info(
                        "pihole_backup_response | job_id=%s target_id=%s status=%s bytes=%s",
                        context.job_id,
                        context.target_id,
                        resp.status_code,
                        len(resp.content or b""),
                    )
                resp.raise_for_status()
                content = resp.content
            except httpx.HTTPError as exc:
                self._logger.error(
                    "pihole_backup_http_error | job_id=%s target_id=%s error=%s",
                    context.job_id,
                    context.target_id,
                    str(exc),
                )
                raise

        if not content:
            raise RuntimeError("Pi-hole Teleporter returned no content")

        with open(artifact_path, "wb") as f:
            f.write(content)
        self._logger.info(
            "pihole_backup_success | job_id=%s target_id=%s artifact=%s size_bytes=%s",
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


