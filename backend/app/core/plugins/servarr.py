"""Shared, version-aware backup implementation for Servarr applications."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sqlite3
import tempfile
import threading
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Dict
from urllib.parse import urlsplit
from xml.etree import ElementTree

import httpx

from app.core.plugins.artifacts import create_backup_artifact
from app.core.plugins.base import BackupContext, BackupPlugin, RestoreContext

_LOCKS_GUARD = threading.Lock()
_BACKUP_LOCKS: dict[str, threading.Lock] = {}


def _backup_lock(key: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _BACKUP_LOCKS.setdefault(key, threading.Lock())


@asynccontextmanager
async def _hold_lock(lock: threading.Lock) -> AsyncIterator[None]:
    while not lock.acquire(blocking=False):
        await asyncio.sleep(0.05)
    try:
        yield
    finally:
        lock.release()


class ServarrPlugin(BackupPlugin):
    """Deep module for the common Lidarr/Radarr/Sonarr backup protocol."""

    app_name = "Servarr"
    api_prefix = "/api/v3"
    database_members: tuple[str, ...] = ()
    backup_root = "/backups"
    restore_capability = "automatic"

    def __init__(self, name: str, version: str = "0.2.0") -> None:
        super().__init__(name=name, version=version)
        self._logger = logging.getLogger(self.__class__.__module__)

    async def validate_config(self, config: Dict[str, Any]) -> bool:
        if not isinstance(config, dict):
            return False
        base_url = config.get("base_url")
        api_key = config.get("api_key")
        return (
            isinstance(base_url, str)
            and bool(base_url.strip())
            and isinstance(api_key, str)
            and bool(api_key.strip())
        )

    def _request_config(self, config: Dict[str, Any]) -> tuple[str, dict[str, str]]:
        base_url = str(config.get("base_url", "")).rstrip("/")
        api_key = str(config.get("api_key", ""))
        if not base_url or not api_key:
            raise ValueError(f"{self.app_name} config must include base_url and api_key")
        return base_url, {"X-Api-Key": api_key}

    async def test(self, config: Dict[str, Any]) -> bool:
        if not await self.validate_config(config):
            raise ValueError("Invalid configuration: base_url and api_key are required")
        base_url, headers = self._request_config(config)
        status_url = f"{base_url}{self.api_prefix}/system/status"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(status_url, headers=headers)
                if response.status_code != 200:
                    raise RuntimeError(
                        f"{self.app_name} API returned status {response.status_code}"
                    )
                data = response.json()
        except RuntimeError:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            raise ConnectionError(f"Failed to connect to {self.app_name}: {exc}") from exc
        if not isinstance(data, dict) or not data.get("version"):
            raise ValueError(f"{self.app_name} status response missing version")
        return True

    async def _list_backups(
        self,
        client: httpx.AsyncClient,
        url: str,
        headers: dict[str, str],
    ) -> list[dict[str, Any]]:
        response = await client.get(url, headers=headers)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, list):
            raise RuntimeError(f"{self.app_name} backup list returned an invalid response")
        return [item for item in data if isinstance(item, dict)]

    def _validate_archive(self, archive_path: Path) -> None:
        with tempfile.TemporaryDirectory(prefix="servarr-verify-") as directory:
            try:
                with zipfile.ZipFile(archive_path) as archive:
                    if not archive.infolist() or archive.testzip() is not None:
                        raise RuntimeError(
                            f"{self.app_name} backup did not return a valid ZIP archive"
                        )
                    members = {Path(name).name.lower(): name for name in archive.namelist()}
                    for required in ("config.xml", "info"):
                        if required not in members:
                            raise RuntimeError(
                                f"{self.app_name} backup archive is missing {required}"
                            )
                    try:
                        ElementTree.fromstring(archive.read(members["config.xml"]))
                    except ElementTree.ParseError as exc:
                        raise RuntimeError(
                            f"{self.app_name} backup contains invalid Config.xml"
                        ) from exc
                    database_key = next(
                        (name.lower() for name in self.database_members if name.lower() in members),
                        None,
                    )
                    if database_key is None:
                        expected = " or ".join(self.database_members)
                        raise RuntimeError(f"{self.app_name} backup archive is missing {expected}")
                    database_path = Path(directory) / "database.sqlite"
                    with (
                        archive.open(members[database_key]) as source,
                        database_path.open("wb") as dest,
                    ):
                        shutil.copyfileobj(source, dest, length=1024 * 1024)
            except zipfile.BadZipFile as exc:
                raise RuntimeError(
                    f"{self.app_name} backup did not return a valid ZIP archive"
                ) from exc
            try:
                with sqlite3.connect(f"file:{database_path}?mode=ro", uri=True) as connection:
                    result = connection.execute("PRAGMA quick_check").fetchone()
            except sqlite3.Error as exc:
                raise RuntimeError(
                    f"{self.app_name} backup contains an unreadable SQLite database"
                ) from exc
            if result is None or result[0] != "ok":
                raise RuntimeError(f"{self.app_name} backup contains an invalid SQLite database")

    def _restored_api_key(self, archive_path: Path, fallback: str) -> str:
        """Read the post-restore key in memory so readiness can authenticate."""

        with zipfile.ZipFile(archive_path) as archive:
            config_member = next(
                (name for name in archive.namelist() if Path(name).name.lower() == "config.xml"),
                None,
            )
            if config_member is None:
                return fallback
            try:
                root = ElementTree.fromstring(archive.read(config_member))
            except ElementTree.ParseError:
                return fallback
        for element in root.iter():
            if element.tag.lower() == "apikey" and element.text and element.text.strip():
                return element.text.strip()
        return fallback

    async def backup(self, context: BackupContext) -> Dict[str, Any]:
        base_url, headers = self._request_config(context.config or {})
        list_url = f"{base_url}{self.api_prefix}/system/backup"
        command_url = f"{base_url}{self.api_prefix}/command"

        lock_key = f"{self.app_name.lower()}:{base_url}"
        async with _hold_lock(_backup_lock(lock_key)):
            async with httpx.AsyncClient(timeout=30.0) as client:
                baseline = await self._list_backups(client, list_url, headers)
                known = {
                    (item.get("id"), item.get("path"))
                    for item in baseline
                    if item.get("type") == "manual"
                }

                trigger = await client.post(
                    command_url,
                    headers={**headers, "Content-Type": "application/json"},
                    json={"name": "Backup"},
                )
                trigger.raise_for_status()
                trigger_data = trigger.json()
                command_id = trigger_data.get("id") if isinstance(trigger_data, dict) else None
                if not isinstance(command_id, int):
                    raise RuntimeError(f"{self.app_name} did not return a backup command id")

                command_status_url = f"{command_url}/{command_id}"
                deadline = asyncio.get_running_loop().time() + 120.0
                while True:
                    response = await client.get(command_status_url, headers=headers)
                    response.raise_for_status()
                    command = response.json()
                    status = str(command.get("status", "")).lower()
                    result = str(command.get("result", "")).lower()
                    if status == "completed":
                        if result != "successful":
                            raise RuntimeError(
                                f"{self.app_name} backup command completed unsuccessfully"
                            )
                        break
                    if status in {"failed", "aborted", "cancelled", "orphaned"}:
                        raise RuntimeError(
                            f"{self.app_name} backup command ended with status {status}"
                        )
                    if asyncio.get_running_loop().time() >= deadline:
                        raise RuntimeError(f"{self.app_name} backup command timed out")
                    await asyncio.sleep(1.0)

                backup_item: dict[str, Any] | None = None
                while backup_item is None:
                    for item in await self._list_backups(client, list_url, headers):
                        identity = (item.get("id"), item.get("path"))
                        if item.get("type") == "manual" and identity not in known:
                            backup_item = item
                            break
                    if backup_item is not None:
                        break
                    if asyncio.get_running_loop().time() >= deadline:
                        raise RuntimeError(f"{self.app_name} backup archive did not appear")
                    await asyncio.sleep(1.0)

                backup_path = backup_item.get("path")
                if not isinstance(backup_path, str) or not backup_path:
                    raise RuntimeError(f"{self.app_name} backup entry did not contain a path")
                parsed = urlsplit(backup_path)
                if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
                    raise RuntimeError(f"{self.app_name} backup returned an unsafe download path")
                download_url = f"{base_url}/{backup_path.lstrip('/')}"
                with create_backup_artifact(
                    self,
                    context,
                    prefix=f"{self.name}-backup",
                    suffix=".zip",
                    backup_root=self.backup_root,
                ) as artifact:
                    async with client.stream(
                        "GET",
                        download_url,
                        headers={
                            **headers,
                            "Accept": "application/zip, application/octet-stream",
                        },
                    ) as download:
                        download.raise_for_status()
                        with artifact.temporary_path.open("wb") as artifact_file:
                            async for chunk in download.aiter_bytes():
                                artifact_file.write(chunk)
                    self._validate_archive(artifact.temporary_path)
                artifact_path = str(artifact.final_path)

        return {"artifact_path": artifact_path}

    async def restore(self, context: RestoreContext) -> Dict[str, Any]:
        base_url, headers = self._request_config(context.config or {})
        lock_key = f"{self.app_name.lower()}:{base_url}"
        async with _hold_lock(_backup_lock(lock_key)):
            return await self._restore_without_lock(context, base_url, headers)

    async def _restore_without_lock(
        self,
        context: RestoreContext,
        base_url: str,
        headers: dict[str, str],
    ) -> Dict[str, Any]:
        artifact_path = context.artifact_path
        if not artifact_path or not os.path.isfile(artifact_path):
            raise FileNotFoundError(f"Artifact not found: {artifact_path}")
        artifact = Path(artifact_path)
        self._validate_archive(artifact)
        restored_key = self._restored_api_key(artifact, headers["X-Api-Key"])
        status_url = f"{base_url}{self.api_prefix}/system/status"
        upload_url = f"{base_url}{self.api_prefix}/system/backup/restore/upload"
        restart_url = f"{base_url}{self.api_prefix}/system/restart"

        async with httpx.AsyncClient(timeout=60.0) as client:
            before = await client.get(status_url, headers=headers)
            before.raise_for_status()
            before_data = before.json()
            previous_start = before_data.get("startTime") if isinstance(before_data, dict) else None

            with open(artifact_path, "rb") as artifact_file:
                upload = await client.post(
                    upload_url,
                    headers=headers,
                    files={
                        "file": (
                            os.path.basename(artifact_path),
                            artifact_file,
                            "application/zip",
                        )
                    },
                )
            upload.raise_for_status()
            upload_data = upload.json()
            if not isinstance(upload_data, dict) or upload_data.get("restartRequired") is not True:
                raise RuntimeError(f"{self.app_name} did not accept the restore archive")

            restart = await client.post(restart_url, headers=headers, content=b"")
            restart.raise_for_status()
            restart_data = restart.json()
            if not isinstance(restart_data, dict) or restart_data.get("restarting") is not True:
                raise RuntimeError(f"{self.app_name} did not acknowledge the restore restart")

            restored_headers = {"X-Api-Key": restored_key}
            deadline = asyncio.get_running_loop().time() + 120.0
            while True:
                try:
                    status_response = await client.get(status_url, headers=restored_headers)
                    if status_response.status_code == 200:
                        status_data = status_response.json()
                        current_start = (
                            status_data.get("startTime") if isinstance(status_data, dict) else None
                        )
                        if current_start and current_start != previous_start:
                            break
                except (httpx.HTTPError, ValueError):
                    pass
                if asyncio.get_running_loop().time() >= deadline:
                    raise RuntimeError(
                        f"{self.app_name} did not become ready after restore restart"
                    )
                await asyncio.sleep(2.0)

        return {
            "status": "success",
            "artifact_path": artifact_path,
            "artifact_bytes": os.path.getsize(artifact_path),
            "message": f"{self.app_name} restore completed and restarted successfully",
        }

    async def get_status(self, context: BackupContext) -> Dict[str, Any]:
        return {"status": "unknown"}
