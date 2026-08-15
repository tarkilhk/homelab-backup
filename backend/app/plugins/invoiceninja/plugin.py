from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import zipfile
from pathlib import Path
from typing import Any, Dict
from urllib.parse import urljoin, urlsplit

import httpx

from app.core.plugins.artifacts import create_backup_artifact
from app.core.plugins.base import BackupContext, BackupPlugin, RestoreContext


class InvoiceNinjaPlugin(BackupPlugin):
    restore_capability = "partial"
    """Invoice Ninja backup plugin using export API.
    Research summary:
    - `GET /api/v1/ping` returns company and user info, used for connectivity tests.
    - `POST /api/v1/export` queues a `CompanyExport` job and responds with a
      signed temporary URL for `GET /api/v1/protected_download/<hash>`.
    - The job writes a zip containing JSON data, documents and backups; the
      URL becomes valid once the job completes so polling is required.
    Authentication uses the `X-API-Token` header.
    """

    def __init__(self, name: str, version: str = "0.2.1") -> None:
        super().__init__(name=name, version=version)
        self._logger = logging.getLogger(__name__)

    # ---- helpers -----------------------------------------------------------------
    def _base_dir(self) -> str:
        return "/backups"

    def _validate_export(self, path: Path) -> None:
        try:
            with zipfile.ZipFile(path) as archive:
                if not archive.infolist() or archive.testzip() is not None:
                    raise RuntimeError("Invoice Ninja export did not return a valid ZIP archive")
                names = {name.strip("/").lower() for name in archive.namelist()}
                backup_member = next(
                    (
                        name
                        for name in archive.namelist()
                        if name.strip("/").lower() == "backup.json"
                    ),
                    None,
                )
                backup_data = archive.read(backup_member) if backup_member else b""
        except (zipfile.BadZipFile, OSError) as exc:
            raise RuntimeError("Invoice Ninja export did not return a valid ZIP archive") from exc
        if "backup.json" not in names:
            raise RuntimeError("Invoice Ninja export archive is missing backup.json")
        try:
            parsed = json.loads(backup_data)
        except (UnicodeDecodeError, ValueError) as exc:
            raise RuntimeError("Invoice Ninja export contains invalid backup.json") from exc
        if not isinstance(parsed, dict):
            raise RuntimeError("Invoice Ninja export contains invalid backup.json")

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
            raise ValueError("Invalid configuration: base_url and token are required")
        base_url = str(config.get("base_url", "")).rstrip("/")
        token = config.get("token")
        url = f"{base_url}/api/v1/ping"
        headers = {
            "X-API-Token": str(token),
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url, headers=headers)
        except httpx.HTTPError as exc:  # pragma: no cover - network failures
            self._logger.warning("invoiceninja_test_http_error | url=%s error=%s", url, exc)
            raise ConnectionError(f"Failed to connect to Invoice Ninja server: {exc}") from exc
        if resp.status_code // 100 != 2:
            self._logger.warning(
                "invoiceninja_test_non_2xx | url=%s status=%s", url, resp.status_code
            )
            raise RuntimeError(f"Invoice Ninja API returned status {resp.status_code}")
        return True

    async def backup(self, context: BackupContext) -> Dict[str, Any]:
        cfg = context.config or {}
        if not await self.validate_config(cfg):
            raise ValueError("Invoice Ninja config must include base_url and token")
        base_url = str(cfg.get("base_url", "")).rstrip("/")
        token = cfg.get("token")
        headers = {"X-API-Token": str(token)}
        export_url = f"{base_url}/api/v1/export"

        async with httpx.AsyncClient(timeout=30.0) as client:
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
            download_value = data.get("url")
            if not isinstance(download_value, str) or not download_value:
                raise RuntimeError("export did not return download url")
            download_url = urljoin(f"{base_url}/", download_value)
            base_origin = urlsplit(base_url)
            download_origin = urlsplit(download_url)
            if (
                download_origin.scheme,
                download_origin.hostname,
                download_origin.port,
            ) != (base_origin.scheme, base_origin.hostname, base_origin.port):
                raise RuntimeError(
                    "Invoice Ninja export URL must remain on the configured same origin"
                )

            # 2) poll for archive readiness
            get_headers = {"Accept": "application/zip, application/octet-stream"}
            poll_interval = 5.0
            timeout_seconds = min(float(cfg.get("export_timeout_seconds", 55 * 60)), 55 * 60)
            attempts = max(1, math.ceil(timeout_seconds / poll_interval))
            with create_backup_artifact(
                self,
                context,
                prefix="invoiceninja-export",
                suffix=".zip",
                backup_root=self._base_dir(),
            ) as artifact:
                for attempt in range(attempts):
                    self._logger.info("invoiceninja_poll_download | attempt=%s", attempt + 1)
                    async with client.stream("GET", download_url, headers=get_headers) as dl_resp:
                        if dl_resp.status_code in {401, 403}:
                            raise RuntimeError(
                                "Invoice Ninja export download authorization expired"
                            )
                        if dl_resp.status_code == 200:
                            content_type = str(dl_resp.headers.get("content-type", "")).lower()
                            disposition = str(
                                dl_resp.headers.get("content-disposition", "")
                            ).lower()
                            looks_binary = (
                                "application/zip" in content_type
                                or "application/octet-stream" in content_type
                                or ".zip" in disposition
                            )
                            if looks_binary:
                                with artifact.temporary_path.open("wb") as artifact_file:
                                    async for chunk in dl_resp.aiter_bytes():
                                        artifact_file.write(chunk)
                                self._validate_export(artifact.temporary_path)
                                break
                    await asyncio.sleep(poll_interval)
                else:
                    raise RuntimeError("export download not ready")
            artifact_path = str(artifact.final_path)

        self._logger.info(
            "invoiceninja_backup_success | job_id=%s target_id=%s artifact=%s bytes=%s",
            context.job_id,
            context.target_id,
            artifact_path,
            os.path.getsize(artifact_path),
        )

        return {"artifact_path": artifact_path}

    async def restore(self, context: RestoreContext) -> Dict[str, Any]:
        cfg = context.config or {}
        if not await self.validate_config(cfg):
            raise ValueError("Invoice Ninja config must include base_url and token")
        artifact_path = context.artifact_path
        if not artifact_path or not os.path.isfile(artifact_path):
            raise FileNotFoundError(f"Artifact not found: {artifact_path}")
        self._validate_export(Path(artifact_path))

        base_url = str(cfg.get("base_url", "")).rstrip("/")
        headers = {
            "X-API-Token": str(cfg.get("token")),
            "X-Requested-With": "XMLHttpRequest",
        }
        async with httpx.AsyncClient(timeout=60.0) as client:
            with open(artifact_path, "rb") as artifact_file:
                response = await client.post(
                    f"{base_url}/api/v1/import_json",
                    headers=headers,
                    files={
                        "files": (
                            os.path.basename(artifact_path),
                            artifact_file,
                            "application/zip",
                        )
                    },
                    data={"import_settings": "true", "import_data": "true"},
                )
            response.raise_for_status()

        return {
            "status": "partial",
            "artifact_path": artifact_path,
            "artifact_bytes": os.path.getsize(artifact_path),
            "message": (
                "Invoice Ninja restore was accepted and queued; terminal import status "
                "is not exposed by the vendor API"
            ),
        }

    async def get_status(
        self, context: BackupContext
    ) -> Dict[str, Any]:  # pragma: no cover - minimal
        return {"ok": True}
