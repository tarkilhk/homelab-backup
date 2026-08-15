from __future__ import annotations

import asyncio
import io
import logging
import os
import zipfile
from pathlib import Path
from typing import Any, Dict

import httpx

from app.core.plugins.artifacts import validate_zip_bytes, write_backup_bytes
from app.core.plugins.base import BackupContext, BackupPlugin, RestoreContext

BACKUP_BASE_PATH = "/backups"


def _http_client(*, timeout: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout, follow_redirects=False)


class PiHolePlugin(BackupPlugin):
    """Pi-hole backup plugin using Teleporter export (session auth).

    Flow:
    - POST {base_url}/api/auth with JSON {"password": ...} to obtain a session ID.
    - GET {base_url}/api/teleporter with the documented X-FTL-SID header.
    - Save artifact under `/backups/<slug>/<YYYY-MM-DD>/pihole-teleporter-<ts>.zip`
    """

    def __init__(self, name: str, version: str = "0.1.0") -> None:
        super().__init__(name=name, version=version)
        self._logger = logging.getLogger(__name__)

    restore_capability = "automatic"

    async def validate_config(self, config: Dict[str, Any]) -> bool:  # pragma: no cover - trivial
        # Minimal validation: ensure required keys exist
        if not isinstance(config, dict):
            return False
        base_url = config.get("base_url")
        password = config.get("password")
        if not base_url or not isinstance(base_url, str):
            return False
        if not password or not isinstance(password, str):
            return False
        return True

    async def test(self, config: Dict[str, Any]) -> bool:
        """Prove authentication and the Teleporter export path end to end."""
        if not await self.validate_config(config):
            raise ValueError("Invalid configuration: base_url and password are required")
        await self._download_teleporter(config, timeout=10.0)
        return True

    def _validate_teleporter(self, content: bytes) -> None:
        validate_zip_bytes(content, artifact_label="Pi-hole Teleporter")
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            names = {name.strip("/") for name in archive.namelist()}
        if "etc/pihole/pihole.toml" not in names:
            raise RuntimeError("Pi-hole Teleporter archive is missing etc/pihole/pihole.toml")

    async def _download_teleporter(self, config: Dict[str, Any], *, timeout: float) -> bytes:
        base_url = str(config.get("base_url", "")).rstrip("/")
        password = config.get("password")
        auth_url = f"{base_url}/api/auth"
        teleporter_url = f"{base_url}/api/teleporter"

        async with _http_client(timeout=timeout) as client:
            sid: str | None = None
            try:
                auth_resp = await client.post(
                    auth_url,
                    json={"password": str(password)},
                    headers={"Accept": "application/json"},
                )
                if auth_resp.status_code // 100 != 2:
                    raise RuntimeError(
                        f"Pi-hole authentication failed with status {auth_resp.status_code}"
                    )
                data: Dict[str, Any] = auth_resp.json()
                session = data.get("session") if isinstance(data, dict) else None
                if not isinstance(session, dict):
                    raise ValueError("Pi-hole authentication failed: invalid session response")
                sid_value = session.get("sid")
                if session.get("valid") is not True or not sid_value:
                    raise ValueError("Pi-hole authentication failed: invalid session response")
                sid = str(sid_value)

                response = await client.get(
                    teleporter_url,
                    headers={
                        "X-FTL-SID": sid,
                        "X-Requested-With": "XMLHttpRequest",
                        "Accept": "application/zip, application/octet-stream",
                    },
                )
                if response.status_code // 100 != 2:
                    raise RuntimeError(
                        f"Pi-hole Teleporter export failed with status {response.status_code}"
                    )
                content = response.content or b""
                self._validate_teleporter(content)
                return content
            except (RuntimeError, ValueError):
                raise
            except httpx.HTTPError as exc:
                raise ConnectionError(f"Failed to connect to Pi-hole server: {exc}") from exc
            finally:
                if sid:
                    try:
                        await client.delete(auth_url, headers={"X-FTL-SID": sid})
                    except httpx.HTTPError:
                        self._logger.warning("pihole_session_logout_failed")

    async def backup(self, context: BackupContext) -> Dict[str, Any]:
        # Determine directories following convention: /backups/<targetSlug>/<YYYY-MM-DD>/
        # We derive slug from context.metadata["target_slug"] if available, else use target_id.
        meta = context.metadata or {}
        target_slug = meta.get("target_slug") or str(context.target_id)
        # Read config
        cfg = getattr(context, "config", {}) or {}
        base_url = str(cfg.get("base_url", "")).rstrip("/")
        password = cfg.get("password")
        if not base_url or not password:
            raise ValueError("Pi-hole config must include base_url and password")
        self._logger.info(
            "pihole_backup_start | job_id=%s target_id=%s target_slug=%s base_url=%s artifact=%s",
            context.job_id,
            context.target_id,
            target_slug,
            base_url,
            "<pending>",
        )

        content = await self._download_teleporter(cfg, timeout=30.0)

        artifact_path = write_backup_bytes(
            self,
            context,
            content,
            prefix="pihole-teleporter",
            suffix=".zip",
            backup_root=BACKUP_BASE_PATH,
        )
        self._logger.info(
            "pihole_backup_success | job_id=%s target_id=%s artifact=%s size_bytes=%s",
            context.job_id,
            context.target_id,
            artifact_path,
            len(content),
        )

        return {"artifact_path": artifact_path}

    async def restore(self, context: RestoreContext) -> Dict[str, Any]:
        cfg = context.config or {}
        if not await self.validate_config(cfg):
            raise ValueError("Pi-hole config must include base_url and password")
        artifact_path = context.artifact_path
        if not artifact_path or not os.path.isfile(artifact_path):
            raise FileNotFoundError(f"Artifact not found: {artifact_path}")
        content = Path(artifact_path).read_bytes()
        self._validate_teleporter(content)

        base_url = str(cfg.get("base_url", "")).rstrip("/")
        password = str(cfg.get("password", ""))
        auth_url = f"{base_url}/api/auth"
        teleporter_url = f"{base_url}/api/teleporter"
        async with _http_client(timeout=60.0) as client:
            sid: str | None = None
            try:
                auth_response = await client.post(
                    auth_url,
                    json={"password": password},
                    headers={"Accept": "application/json"},
                )
                auth_response.raise_for_status()
                session = auth_response.json().get("session") or {}
                sid_value = session.get("sid")
                if session.get("valid") is not True or not sid_value:
                    raise RuntimeError("Pi-hole auth did not return a valid session")
                sid = str(sid_value)
                with open(artifact_path, "rb") as artifact_file:
                    response = await client.post(
                        teleporter_url,
                        headers={"X-FTL-SID": sid},
                        files={
                            "file": (
                                os.path.basename(artifact_path),
                                artifact_file,
                                "application/zip",
                            )
                        },
                    )
                if response.status_code // 100 != 2:
                    raise RuntimeError(
                        f"Pi-hole Teleporter import failed with status {response.status_code}"
                    )
            finally:
                if sid:
                    try:
                        await client.delete(auth_url, headers={"X-FTL-SID": sid})
                    except httpx.HTTPError:
                        pass

        deadline = asyncio.get_running_loop().time() + 120.0
        while True:
            try:
                await self._download_teleporter(cfg, timeout=15.0)
                break
            except (ConnectionError, RuntimeError, ValueError, httpx.HTTPError):
                if asyncio.get_running_loop().time() >= deadline:
                    raise RuntimeError(
                        "Pi-hole did not become ready after Teleporter import"
                    ) from None
                await asyncio.sleep(2.0)

        return {
            "status": "success",
            "artifact_path": artifact_path,
            "artifact_bytes": os.path.getsize(artifact_path),
            "message": "Pi-hole Teleporter import completed and backup path revalidated",
        }

    async def get_status(
        self, context: BackupContext
    ) -> Dict[str, Any]:  # pragma: no cover - trivial
        return {"status": "ok"}
