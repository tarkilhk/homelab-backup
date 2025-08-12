from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import asyncio
import httpx
import logging

from app.core.plugins.base import BackupContext, BackupPlugin


class LidarrPlugin(BackupPlugin):
    """Lidarr backup plugin using built-in backup API.

    Research notes: Lidarr's UI exposes backups via System → Backup which
    generates a zip archive for download. This plugin automates that by calling
    the backup endpoints with an API key for authentication.

    Correct API flow (Servarr family, including Lidarr v1):
    - POST `/api/v1/command` with `{ "name": "Backup" }` to trigger a new manual backup
    - Poll GET `/api/v1/system/backup` to find the newly created backup entry
    - Prefer downloading via the `path` returned by the list API (e.g., `/backup/manual/<file>.zip`)
      with `?apikey=` and redirects; fall back to `/api/v1/system/backup/{id}/download` if needed
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
        status_url = f"{base_url}/api/v1/system/status"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(status_url, headers={"X-Api-Key": api_key})
                return resp.status_code == 200
        except httpx.HTTPError as exc:  # pragma: no cover - network errors
            self._logger.warning("lidarr_test_error | url=%s error=%s", status_url, exc)
            return False

    async def backup(self, context: BackupContext) -> Dict[str, Any]:
        meta = context.metadata or {}
        target_slug = meta.get("target_slug") or str(context.target_id)
        today = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
        base_dir = os.path.join(self.backup_root, target_slug, today)
        os.makedirs(base_dir, exist_ok=True)
        timestamp = datetime.now(timezone.utc).astimezone().strftime("%Y%m%dT%H%M%S")
        artifact_path = os.path.join(base_dir, f"lidarr-backup-{timestamp}.zip")

        cfg = context.config or {}
        base_url = str(cfg.get("base_url", "")).rstrip("/")
        api_key = str(cfg.get("api_key", ""))
        if not base_url or not api_key:
            raise ValueError("Lidarr config must include base_url and api_key")

        list_url = f"{base_url}/api/v1/system/backup"
        command_url = f"{base_url}/api/v1/command"

        started_at_iso = datetime.now(timezone.utc).isoformat()

        self._logger.info(
            "lidarr_backup_trigger | job_id=%s target_id=%s url=%s artifact=%s",
            context.job_id,
            context.target_id,
            command_url,
            artifact_path,
        )

        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            # 1) Trigger a new manual backup
            try:
                trigger_resp = await client.post(
                    command_url,
                    headers={"X-Api-Key": api_key, "Content-Type": "application/json"},
                    json={"name": "Backup"},
                )
                if trigger_resp.status_code // 100 not in (2, 3):
                    trigger_resp.raise_for_status()
            except httpx.HTTPError as exc:
                self._logger.error(
                    "lidarr_backup_trigger_error | job_id=%s target_id=%s error=%s",
                    context.job_id,
                    context.target_id,
                    str(exc),
                )
                raise

            try:
                post_content = trigger_resp.content or b""
            except Exception:  # pragma: no cover - very defensive
                post_content = b""
            post_looks_json = post_content.strip().startswith((b"{", b"["))
            if post_content and not post_looks_json:
                with open(artifact_path, "wb") as fp:
                    fp.write(post_content)
                return {"artifact_path": artifact_path}

            # 2) Poll the backup list for the newly created entry
            backup_id: Optional[int] = None
            backup_path: Optional[str] = None
            poll_deadline = asyncio.get_event_loop().time() + 60.0
            last_list_error: Optional[str] = None

            while backup_id is None and asyncio.get_event_loop().time() < poll_deadline:
                try:
                    list_resp = await client.get(list_url, headers={"X-Api-Key": api_key})
                    list_resp.raise_for_status()

                    body = list_resp.content or b""
                    is_json_like = body.strip().startswith((b"{", b"["))
                    if body and not is_json_like:
                        with open(artifact_path, "wb") as fp:
                            fp.write(body)
                        return {"artifact_path": artifact_path}

                    items: List[Dict[str, Any]] = list_resp.json() or []
                    candidate: Optional[Dict[str, Any]] = None
                    for it in items:
                        if not isinstance(it, dict):
                            continue
                        if it.get("type") not in ("manual", "scheduled"):
                            continue
                        it_time = it.get("time")
                        try:
                            if isinstance(it_time, str) and it_time >= started_at_iso:
                                if it.get("type") == "manual":
                                    candidate = it
                                    break
                                if candidate is None:
                                    candidate = it
                        except Exception:
                            pass

                    if candidate is None and items:
                        candidate = items[0]

                    if candidate and isinstance(candidate.get("id"), int):
                        backup_id = int(candidate["id"])
                        bp = candidate.get("path")
                        if isinstance(bp, str) and bp:
                            backup_path = bp
                        break
                except (httpx.HTTPError, ValueError) as exc:
                    last_list_error = str(exc)

                await asyncio.sleep(1.0)

            if backup_id is None:
                msg = (
                    "Unable to locate newly created Lidarr backup entry after trigger"
                    + (f" | last_error={last_list_error}" if last_list_error else "")
                )
                self._logger.error(
                    "lidarr_backup_list_timeout | job_id=%s target_id=%s msg=%s",
                    context.job_id,
                    context.target_id,
                    msg,
                )
                raise RuntimeError(msg)

            # 3) Download the archive
            if backup_path:
                if backup_path.startswith("/"):
                    fallback_url = f"{base_url}{backup_path}"
                else:
                    fallback_url = f"{base_url}/{backup_path}"
                self._logger.info(
                    "lidarr_backup_download_path | job_id=%s target_id=%s url=%s",
                    context.job_id,
                    context.target_id,
                    fallback_url,
                )
                try:
                    dl_path_resp = await client.get(
                        fallback_url,
                        headers={
                            "X-Api-Key": api_key,
                            "Accept": "application/zip, application/octet-stream",
                        },
                        params={"apikey": api_key},
                    )
                    if dl_path_resp.status_code == 200 and (dl_path_resp.content or b""):
                        with open(artifact_path, "wb") as fp:
                            fp.write(dl_path_resp.content)
                        return {"artifact_path": artifact_path}
                except httpx.HTTPError as exc:
                    self._logger.warning(
                        "lidarr_backup_download_path_error | job_id=%s target_id=%s error=%s",
                        context.job_id,
                        context.target_id,
                        str(exc),
                    )

            download_url = f"{base_url}/api/v1/system/backup/{backup_id}/download"
            self._logger.info(
                "lidarr_backup_download | job_id=%s target_id=%s url=%s backup_id=%s",
                context.job_id,
                context.target_id,
                download_url,
                backup_id,
            )
            try:
                dl_resp = await client.get(
                    download_url,
                    headers={
                        "X-Api-Key": api_key,
                        "Accept": "application/zip, application/octet-stream",
                    },
                    params={"apikey": api_key},
                )
                status = dl_resp.status_code
                content = dl_resp.content or b""
                if status == 200 and content:
                    with open(artifact_path, "wb") as fp:
                        fp.write(content)
                    return {"artifact_path": artifact_path}
            except httpx.HTTPError as exc:
                self._logger.error(
                    "lidarr_backup_download_error | job_id=%s target_id=%s error=%s",
                    context.job_id,
                    context.target_id,
                    str(exc),
                )

            fallback_url: Optional[str] = None
            try:
                list_resp2 = await client.get(list_url, headers={"X-Api-Key": api_key})
                list_resp2.raise_for_status()
                items2: List[Dict[str, Any]] = list_resp2.json() or []
                cand2: Optional[Dict[str, Any]] = next(
                    (it for it in items2 if isinstance(it, dict) and it.get("id") == backup_id),
                    None,
                )
                path_value = cand2.get("path") if isinstance(cand2, dict) else None
                if isinstance(path_value, str) and path_value:
                    if path_value.startswith("/"):
                        fallback_url = f"{base_url}{path_value}"
                    else:
                        fallback_url = f"{base_url}/{path_value}"
            except Exception:
                fallback_url = None

            if not fallback_url:
                raise RuntimeError(
                    f"Lidarr backup download failed for id={backup_id} and no fallback path available"
                )

            self._logger.info(
                "lidarr_backup_download_fallback | job_id=%s target_id=%s url=%s",
                context.job_id,
                context.target_id,
                fallback_url,
            )
            try:
                dl2_resp = await client.get(
                    fallback_url,
                    headers={
                        "X-Api-Key": api_key,
                        "Accept": "application/zip, application/octet-stream",
                    },
                    params={"apikey": api_key},
                )
                dl2_resp.raise_for_status()
                content2 = dl2_resp.content or b""
                if not content2:
                    raise RuntimeError("Lidarr backup fallback download returned no content")
                with open(artifact_path, "wb") as fp:
                    fp.write(content2)
            except httpx.HTTPError as exc:
                self._logger.error(
                    "lidarr_backup_download_fallback_error | job_id=%s target_id=%s error=%s",
                    context.job_id,
                    context.target_id,
                    str(exc),
                )
                raise

        return {"artifact_path": artifact_path}

    async def restore(self, context: BackupContext) -> Dict[str, Any]:
        raise NotImplementedError("Restore is not implemented for Lidarr")

    async def get_status(self, context: BackupContext) -> Dict[str, Any]:
        return {}
