from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Dict

import httpx

from app.core.plugins.artifacts import create_backup_artifact
from app.core.plugins.base import BackupContext, BackupPlugin, RestoreContext

BACKUP_BASE = "/backups"
RESTORE_MAX_POLLS = 480
RESTORE_POLL_INTERVAL_SECONDS = 0.25


class JellyfinPlugin(BackupPlugin):
    """Back up Jellyfin 10.11 through its server-local backup directory."""

    restore_capability = "automatic"

    def __init__(self, name: str, version: str = "0.2.0") -> None:
        super().__init__(name=name, version=version)
        self._logger = logging.getLogger(__name__)

    async def validate_config(self, config: Dict[str, Any]) -> bool:
        if not isinstance(config, dict):
            return False
        return all(
            isinstance(config.get(key), str) and bool(str(config.get(key)).strip())
            for key in ("base_url", "api_key", "backup_path")
        )

    def _config(self, config: Dict[str, Any]) -> tuple[str, dict[str, str], Path]:
        base_url = str(config.get("base_url", "")).rstrip("/")
        api_key = str(config.get("api_key", ""))
        backup_path_value = str(config.get("backup_path", "")).strip()
        if not base_url or not api_key or not backup_path_value:
            raise ValueError("Jellyfin config must include base_url, api_key, and backup_path")
        authorization = (
            'MediaBrowser Client="homelab-backup", Device="backup-agent", '
            f'DeviceId="homelab-backup", Version="1", Token="{api_key}"'
        )
        return base_url, {"Authorization": authorization}, Path(backup_path_value)

    def _validate_archive(self, path: Path) -> None:
        try:
            with zipfile.ZipFile(path) as archive:
                if not archive.infolist() or archive.testzip() is not None:
                    raise RuntimeError("Jellyfin backup did not return a valid ZIP archive")
                members = {name.strip("/").lower(): name for name in archive.namelist()}
                manifest_name = members.get("manifest.json")
                if manifest_name is None:
                    raise RuntimeError("Jellyfin backup archive is missing manifest.json")
                try:
                    manifest = json.loads(archive.read(manifest_name))
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    raise RuntimeError("Jellyfin backup manifest is invalid") from exc
                if not isinstance(manifest, dict) or not manifest.get("ServerVersion"):
                    raise RuntimeError("Jellyfin backup manifest is missing ServerVersion")
                if not any(name.lower().startswith("database/") for name in archive.namelist()):
                    raise RuntimeError("Jellyfin backup archive is missing database content")
        except (zipfile.BadZipFile, OSError) as exc:
            raise RuntimeError("Jellyfin backup did not return a valid ZIP archive") from exc

    async def test(self, config: Dict[str, Any]) -> bool:
        if not await self.validate_config(config):
            raise ValueError(
                "Invalid configuration: base_url, api_key, and backup_path are required"
            )
        base_url, headers, backup_path = self._config(config)
        if not backup_path.is_dir():
            raise FileNotFoundError(f"Jellyfin backup path not found: {backup_path}")
        try:
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
                response = await client.get(f"{base_url}/System/Info", headers=headers)
                if response.status_code // 100 != 2:
                    raise RuntimeError(f"Jellyfin API returned status {response.status_code}")
                data = response.json()
        except RuntimeError:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            raise ConnectionError(f"Failed to connect to Jellyfin server: {exc}") from exc
        if not isinstance(data, dict) or not data.get("Version"):
            raise ValueError("Jellyfin API response missing Version field")
        return True

    async def backup(self, context: BackupContext) -> Dict[str, Any]:
        base_url, headers, backup_path = self._config(context.config or {})
        if not backup_path.is_dir():
            raise FileNotFoundError(f"Jellyfin backup path not found: {backup_path}")
        async with httpx.AsyncClient(timeout=300.0, follow_redirects=True) as client:
            response = await client.post(
                f"{base_url}/Backup/Create",
                headers={**headers, "Content-Type": "application/json"},
                json={
                    "Database": True,
                    "Metadata": False,
                    "Subtitles": False,
                    "Trickplay": False,
                },
            )
            response.raise_for_status()
            manifest = response.json()
        server_path = manifest.get("Path") if isinstance(manifest, dict) else None
        if not isinstance(server_path, str) or not server_path:
            raise RuntimeError("Jellyfin backup creation did not return an archive path")
        source_path = backup_path / Path(server_path).name
        deadline = asyncio.get_running_loop().time() + 30.0
        while not source_path.is_file():
            if asyncio.get_running_loop().time() >= deadline:
                raise RuntimeError("Jellyfin backup archive did not appear in the shared path")
            await asyncio.sleep(0.5)
        if source_path.is_symlink():
            raise RuntimeError("Jellyfin backup archive must not be a symbolic link")
        self._validate_archive(source_path)

        with create_backup_artifact(
            self,
            context,
            prefix="jellyfin-backup",
            suffix=".zip",
            backup_root=BACKUP_BASE,
        ) as artifact:
            with source_path.open("rb") as source, artifact.temporary_path.open("wb") as dest:
                shutil.copyfileobj(source, dest, length=1024 * 1024)
        source_path.unlink()
        return {"artifact_path": str(artifact.final_path)}

    async def restore(self, context: RestoreContext) -> Dict[str, Any]:
        base_url, headers, backup_path = self._config(context.config or {})
        artifact_path = Path(context.artifact_path)
        if not artifact_path.is_file() or artifact_path.is_symlink():
            raise FileNotFoundError(f"Artifact not found: {artifact_path}")
        if not backup_path.is_dir():
            raise FileNotFoundError(f"Jellyfin backup path not found: {backup_path}")
        self._validate_archive(artifact_path)

        destination = backup_path / artifact_path.name
        staged_by_plugin = artifact_path.resolve() != destination.resolve()
        if staged_by_plugin:
            temp_file = tempfile.NamedTemporaryFile(
                prefix=f".{artifact_path.name}.",
                suffix=".tmp",
                dir=backup_path,
                delete=False,
            )
            temp_path = Path(temp_file.name)
            try:
                with temp_file, artifact_path.open("rb") as source:
                    shutil.copyfileobj(source, temp_file, length=1024 * 1024)
                    temp_file.flush()
                    os.fsync(temp_file.fileno())
                os.replace(temp_path, destination)
            except Exception:
                temp_path.unlink(missing_ok=True)
                raise

        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            response = await client.post(
                f"{base_url}/Backup/Restore",
                headers={**headers, "Content-Type": "application/json"},
                json={"ArchiveFileName": destination.name},
            )
            response.raise_for_status()

            observed_restart = False
            server_remained_ready = False
            restore_status = "success"
            restore_message = "Jellyfin restore completed after an observed restart transition"
            for _ in range(RESTORE_MAX_POLLS):
                try:
                    ready = await client.get(f"{base_url}/System/Info/Public")
                    if ready.status_code != 200:
                        observed_restart = True
                    elif observed_restart and ready.json().get("Version"):
                        break
                    elif ready.json().get("Version"):
                        server_remained_ready = True
                except (httpx.HTTPError, ValueError):
                    observed_restart = True
                await asyncio.sleep(RESTORE_POLL_INTERVAL_SECONDS)
            else:
                if observed_restart or not server_remained_ready:
                    raise RuntimeError("Jellyfin did not become ready after restore")
                restore_status = "partial"
                restore_message = (
                    "Jellyfin restore was accepted and remained ready for the full restore "
                    "deadline, but the restart transition completed too quickly to observe"
                )

        if staged_by_plugin:
            destination.unlink(missing_ok=True)

        return {
            "status": restore_status,
            "artifact_path": str(artifact_path),
            "artifact_bytes": artifact_path.stat().st_size,
            "message": restore_message,
        }

    async def get_status(self, context: BackupContext) -> Dict[str, Any]:
        return {"status": "unknown"}
