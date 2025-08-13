from __future__ import annotations

import os
import logging
import asyncio
from datetime import datetime, timezone
from typing import Any, Dict

import httpx

from app.core.plugins.base import BackupContext, BackupPlugin


class InvoiceNinjaPlugin(BackupPlugin):
    """Invoice Ninja backup plugin using export API.

    Research summary:
    - `GET /api/v1/ping` returns company and user info, used for connectivity tests.
    - `POST /api/v1/export` queues a `CompanyExport` job and responds with a
      signed temporary URL for `GET /api/v1/protected_download/<hash>`.
    - The job writes a zip containing JSON data, documents and backups; the
      URL becomes valid once the job completes so polling is required.
    Authentication uses the `X-API-Token` header.
    """

    def __init__(self, name: str, version: str = "0.2.0") -> None:
        super().__init__(name=name, version=version)
        self._logger = logging.getLogger(__name__)

    # ---- helpers -----------------------------------------------------------------
    def _base_dir(self) -> str:
        return "/backups"

    # ---- interface implementation -------------------------------------------------
    async def validate_config(self, config: Dict[str, Any]) -> bool:  # pragma: no cover - trivial
        if not isinstance(config, dict):
            return False
        base_url = config.get("base_url")
        token = config.get("token")
        if not base_url or not isinstance(base_url, str):
            return False
        if not token or not isinstance(token, str):
            return False
        return True

    async def test(self, config: Dict[str, Any]) -> bool:
        """Ping the Invoice Ninja API to verify credentials."""
        if not await self.validate_config(config):
            return False
        base_url = str(config.get("base_url", "")).rstrip("/")
        token = config.get("token")
        url = f"{base_url}/api/v1/ping"
        headers = {"X-API-Token": str(token), "Accept": "application/json"}
        try:
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
                resp = await client.get(url, headers=headers)
        except httpx.HTTPError as exc:  # pragma: no cover - network failures
            self._logger.warning("invoiceninja_test_http_error | url=%s error=%s", url, exc)
            return False
        if resp.status_code // 100 != 2:
            self._logger.warning(
                "invoiceninja_test_non_2xx | url=%s status=%s", url, resp.status_code
            )
            return False
        return True

    async def backup(self, context: BackupContext) -> Dict[str, Any]:
        cfg = context.config or {}
        if not await self.validate_config(cfg):
            raise ValueError("Invoice Ninja config must include base_url and token")
        base_url = str(cfg.get("base_url", "")).rstrip("/")
        token = cfg.get("token")
        headers = {"X-API-Token": str(token)}
        export_url = f"{base_url}/api/v1/export"

        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            # 1) trigger export
            self._logger.info(
                "invoiceninja_backup_request | job_id=%s target_id=%s url=%s",
                context.job_id,
                context.target_id,
                export_url,
            )
            post_headers = {**headers, "X-Requested-With": "XMLHttpRequest"}
            resp = await client.post(export_url, headers=post_headers)
            resp.raise_for_status()
            data = resp.json()
            download_url = data.get("url")
            if not download_url:
                raise RuntimeError("export did not return download url")

            # 2) poll for archive readiness
            dl_resp = None
            for attempt in range(10):
                self._logger.info(
                    "invoiceninja_poll_download | attempt=%s url=%s", attempt + 1, download_url
                )
                dl_resp = await client.get(download_url, headers=headers)
                if dl_resp.status_code == 200 and dl_resp.headers.get("content-type", "") != "application/json":
                    break
                await asyncio.sleep(3)
            else:
                raise RuntimeError("export download not ready")
            dl_resp.raise_for_status()

        meta = context.metadata or {}
        target_slug = meta.get("target_slug") or str(context.target_id)
        today = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
        base_dir = os.path.join(self._base_dir(), target_slug, today)
        os.makedirs(base_dir, exist_ok=True)
        ts = datetime.now(timezone.utc).astimezone().strftime("%Y%m%dT%H%M%S")
        artifact_path = os.path.join(base_dir, f"invoiceninja-export-{ts}.zip")
        with open(artifact_path, "wb") as f:
            f.write(dl_resp.content)
        self._logger.info(
            "invoiceninja_backup_success | job_id=%s target_id=%s artifact=%s bytes=%s",
            context.job_id,
            context.target_id,
            artifact_path,
            len(dl_resp.content),
        )
        return {"artifact_path": artifact_path}

    async def restore(self, context: BackupContext) -> Dict[str, Any]:  # pragma: no cover - not implemented
        return {"ok": False, "error": "Restore not implemented"}

    async def get_status(self, context: BackupContext) -> Dict[str, Any]:  # pragma: no cover - minimal
        return {"ok": True}
