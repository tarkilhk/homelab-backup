from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict

import httpx

from app.core.plugins.artifacts import write_backup_bytes
from app.core.plugins.base import BackupContext, BackupPlugin, RestoreContext
from app.core.plugins.restore_utils import copy_artifact_for_restore

BACKUP_BASE_PATH = "/backups"


def _http_client(*, timeout: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout, follow_redirects=True)


class PiHolePlugin(BackupPlugin):
    """Pi-hole backup plugin using Teleporter export (session auth).

    Flow:
    - POST {base_url}/api/auth with JSON {"password": ...} to obtain session cookie (sid)
      and CSRF token
    - GET {base_url}/api/teleporter with `X-CSRF-TOKEN` header and session cookie to
      download a ZIP archive
    - Save artifact under `/backups/<slug>/<YYYY-MM-DD>/pihole-teleporter-<ts>.zip`
    """

    def __init__(self, name: str, version: str = "0.1.0") -> None:
        super().__init__(name=name, version=version)
        self._logger = logging.getLogger(__name__)

    async def validate_config(self, config: Dict[str, Any]) -> bool:  # pragma: no cover - trivial
        # Minimal validation: ensure required keys exist
        if not isinstance(config, dict):
            return False
        base_url = config.get("base_url")
        # Accept login for UI parity, but Pi-hole v6 auth only requires password
        login = config.get("login")
        password = config.get("password")
        if not base_url or not isinstance(base_url, str):
            return False
        if not password or not isinstance(password, str):
            return False
        return True

    async def test(self, config: Dict[str, Any]) -> bool:
        """Connectivity test using provided configuration.

        Attempt real authentication against Pi-hole v6 API:
        - POST {base_url}/api/auth with JSON {"password": ...}
        - Expect a JSON body containing a valid session and CSRF token
        Returns True on success, raises exception on failure.
        """
        # Basic shape check first
        if not await self.validate_config(config):
            raise ValueError("Invalid configuration: base_url and password are required")

        base_url = str(config.get("base_url", "")).rstrip("/")
        # Login retained for UX parity; Pi-hole v6 uses password-only auth
        password = config.get("password")

        auth_url = f"{base_url}/api/auth"

        try:
            async with _http_client(timeout=10.0) as client:
                resp = await client.post(
                    auth_url,
                    json={"password": str(password)},
                    headers={"Accept": "application/json"},
                )
                # Non-2xx means auth failed or endpoint not reachable
                if resp.status_code // 100 != 2:
                    self._logger.warning(
                        "pihole_test_auth_non_2xx | url=%s status=%s", auth_url, resp.status_code
                    )
                    raise RuntimeError(
                        f"Pi-hole authentication failed with status {resp.status_code}"
                    )
                data: Dict[str, Any] = resp.json()
        except RuntimeError:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            # HTTP/network/JSON errors -> treat as failed auth
            self._logger.warning("pihole_test_auth_error | url=%s error=%s", auth_url, exc)
            raise ConnectionError(f"Failed to connect to Pi-hole server: {exc}") from exc

        # Accept either the documented v6 shape or be lenient if fields change slightly
        session = data.get("session") if isinstance(data, dict) else None
        if isinstance(session, dict):
            valid = session.get("valid") is True
            csrf_present = bool(session.get("csrf"))
            sid_present = bool(session.get("sid"))
            if valid and csrf_present and sid_present:
                return True

        # Fallback: if API returns another explicit success flag
        if isinstance(data, dict) and data.get("success") is True:
            return True

        raise ValueError("Pi-hole authentication failed: invalid session response")

    async def backup(self, context: BackupContext) -> Dict[str, Any]:
        # Determine directories following convention: /backups/<targetSlug>/<YYYY-MM-DD>/
        # We derive slug from context.metadata["target_slug"] if available, else use target_id.
        meta = context.metadata or {}
        target_slug = meta.get("target_slug") or str(context.target_id)
        # Read config
        cfg = getattr(context, "config", {}) or {}
        base_url = str(cfg.get("base_url", "")).rstrip("/")
        login = cfg.get("login")  # not used by v6 API, retained for UX
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

        # Endpoints
        auth_url = f"{base_url}/api/auth"
        teleporter_url = f"{base_url}/api/teleporter"

        async with _http_client(timeout=30.0) as client:
            sid: str | None = None
            try:
                # 1) Authenticate (password only)
                self._logger.info(
                    "pihole_auth_request | job_id=%s target_id=%s url=%s",
                    context.job_id,
                    context.target_id,
                    auth_url,
                )
                auth_resp = await client.post(
                    auth_url,
                    json={"password": str(password)},
                    headers={"Accept": "application/json"},
                )
                self._logger.info(
                    "pihole_auth_response | job_id=%s target_id=%s status=%s",
                    context.job_id,
                    context.target_id,
                    auth_resp.status_code,
                )
                auth_resp.raise_for_status()
                auth_data = auth_resp.json()
                session = auth_data.get("session") or {}
                csrf_token = session.get("csrf")
                sid_value = session.get("sid")
                if not csrf_token or not sid_value or session.get("valid") is not True:
                    raise RuntimeError("Pi-hole auth did not return a valid session")
                sid = str(sid_value)

                # 2) Teleporter download with CSRF header and session cookie
                self._logger.info(
                    "pihole_backup_request | job_id=%s target_id=%s url=%s auth=session",
                    context.job_id,
                    context.target_id,
                    teleporter_url,
                )
                resp = await client.get(
                    teleporter_url,
                    headers={
                        "X-FTL-SID": sid,
                        "X-FTL-CSRF": str(csrf_token),
                        "X-Requested-With": "XMLHttpRequest",
                        "Accept": "application/zip, application/octet-stream",
                    },
                )
                self._logger.info(
                    "pihole_backup_response | job_id=%s target_id=%s status=%s bytes=%s",
                    context.job_id,
                    context.target_id,
                    resp.status_code,
                    len(resp.content or b""),
                )
                if resp.status_code // 100 != 2:
                    raise RuntimeError(
                        f"Pi-hole Teleporter export failed with status {resp.status_code}"
                    )
                resp.raise_for_status()
                content = resp.content
            except RuntimeError:
                raise
            except httpx.HTTPError as exc:
                self._logger.error(
                    "pihole_backup_http_error | job_id=%s target_id=%s error=%s",
                    context.job_id,
                    context.target_id,
                    str(exc),
                )
                raise
            finally:
                if sid:
                    try:
                        await client.delete(
                            auth_url,
                            headers={"X-FTL-SID": sid},
                        )
                    except httpx.HTTPError:
                        self._logger.warning(
                            "pihole_session_logout_failed | job_id=%s target_id=%s",
                            context.job_id,
                            context.target_id,
                        )

        if not content:
            raise RuntimeError("Pi-hole Teleporter returned no content")

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
        """Restore a Pi-hole backup.

        Note: Pi-hole v6 Teleporter restoration. This function copies the backup file
        to a restore directory. To complete the restore:
        1. Access Pi-hole web interface
        2. Navigate to Settings → Teleporter
        3. Use the "Import" feature to upload the backup ZIP file

        The Teleporter import will restore settings, blocklists, and configurations.
        """
        return copy_artifact_for_restore(
            context,
            logger=self._logger,
            restore_root="/backups",
            prefix="pihole",
        )

    async def get_status(
        self, context: BackupContext
    ) -> Dict[str, Any]:  # pragma: no cover - trivial
        return {"status": "ok"}
