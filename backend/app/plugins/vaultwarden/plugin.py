from __future__ import annotations

import os
import tarfile
import tempfile
from datetime import datetime, timezone
from typing import Any, Dict, Tuple
import logging

import httpx

from app.core.plugins.base import BackupContext, BackupPlugin, RestoreContext

BACKUP_BASE_PATH = "/backups"
DEFAULT_DATA_PATH = "/data"
DOCKER_SOCKET_PATH = "/var/run/docker.sock"


class VaultWardenPlugin(BackupPlugin):
    """Vaultwarden backup plugin using Docker Engine API via unix socket.

    Research summary (Vaultwarden wiki: Backing up your vault):
    - Simplest backup: `docker exec <container> /vaultwarden backup` and copy
      resulting archive, or tar the critical data directly.
    - We back up exactly: `/data/db.sqlite3`, `/data/config.json`, and
      `/data/attachments` (if present).
    - Accesses container files via Docker socket (`/var/run/docker.sock` mounted
      into the backend container); no docker CLI required.
    - Restores are performed by PUTting an archive back into `/data` and
      restarting the service.
    """

    def __init__(self, name: str, version: str = "0.1.0") -> None:
        super().__init__(name=name, version=version)
        self._logger = logging.getLogger(__name__)

    async def validate_config(self, config: Dict[str, Any]) -> bool:
        if not isinstance(config, dict):
            return False
        container = config.get("container_name")
        if not container or not isinstance(container, str) or not container.strip():
            return False
        data_path = config.get("data_path", DEFAULT_DATA_PATH)
        if not isinstance(data_path, str) or not data_path.strip():
            return False
        return True

    async def test(self, config: Dict[str, Any]) -> bool:
        """Verify the container is reachable and core data files exist."""
        if not await self.validate_config(config):
            raise ValueError("Invalid configuration: container_name and data_path are required")
        container = str(config.get("container_name")).strip()
        data_path = str(config.get("data_path", DEFAULT_DATA_PATH)).rstrip("/") or "/"
        try:
            async with self._docker_client() as client:
                exists = await self._container_exists(client, container)
                if not exists:
                    self._logger.warning("vaultwarden_test_failed | container=%s missing", container)
                    return False
                db_ok, db_err = await self._path_exists(client, container, f"{data_path}/db.sqlite3")
                if not db_ok:
                    self._logger.warning(
                        "vaultwarden_test_failed | container=%s error=%s", container, db_err
                    )
                    return False
                cfg_ok, cfg_err = await self._path_exists(
                    client, container, f"{data_path}/config.json"
                )
                if not cfg_ok:
                    self._logger.warning(
                        "vaultwarden_test_failed | container=%s error=%s", container, cfg_err
                    )
                    return False
                return True
        except ValueError:
            raise
        except Exception as exc:  # pragma: no cover - defensive
            self._logger.warning("vaultwarden_test_error | container=%s error=%s", container, exc)
            raise ConnectionError(f"Failed to test VaultWarden container: {exc}") from exc

    async def backup(self, context: BackupContext) -> Dict[str, Any]:
        cfg = getattr(context, "config", {}) or {}
        if not await self.validate_config(cfg):
            raise ValueError("vaultwarden config invalid; container_name and data_path required")
        container = str(cfg.get("container_name")).strip()
        data_path = str(cfg.get("data_path", DEFAULT_DATA_PATH)).rstrip("/") or "/"

        meta = context.metadata or {}
        target_slug = meta.get("target_slug") or str(context.target_id)
        today = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
        base_dir = os.path.join(BACKUP_BASE_PATH, target_slug, today)
        os.makedirs(base_dir, exist_ok=True)
        timestamp = datetime.now(timezone.utc).astimezone().strftime("%Y%m%dT%H%M%S")
        artifact_path = os.path.join(base_dir, f"vaultwarden-backup-{timestamp}.tar.gz")

        self._logger.info(
            "vaultwarden_backup_start | job_id=%s target_id=%s container=%s data_path=%s artifact=%s",
            context.job_id,
            context.target_id,
            container,
            data_path,
            artifact_path,
        )

        async with self._docker_client() as client:
            exists = await self._container_exists(client, container)
            if not exists:
                raise FileNotFoundError(f"Container {container} not found")

            with tempfile.TemporaryDirectory() as staging_dir:
                await self._fetch_archive(
                    client,
                    container,
                    os.path.join(data_path, "db.sqlite3"),
                    staging_dir,
                    required=True,
                )
                await self._fetch_archive(
                    client,
                    container,
                    os.path.join(data_path, "config.json"),
                    staging_dir,
                    required=True,
                )
                await self._fetch_archive(
                    client,
                    container,
                    os.path.join(data_path, "attachments"),
                    staging_dir,
                    required=False,
                )

                db_local = os.path.join(staging_dir, "db.sqlite3")
                cfg_local = os.path.join(staging_dir, "config.json")
                if not os.path.isfile(db_local):
                    raise FileNotFoundError(f"db.sqlite3 missing in {data_path}")
                if not os.path.isfile(cfg_local):
                    raise FileNotFoundError(f"config.json missing in {data_path}")

                with tarfile.open(artifact_path, "w:gz") as tar:
                    tar.add(db_local, arcname="db.sqlite3")
                    tar.add(cfg_local, arcname="config.json")
                    attachments_local = os.path.join(staging_dir, "attachments")
                    if os.path.isdir(attachments_local):
                        tar.add(attachments_local, arcname="attachments")

            self._verify_artifact(artifact_path)
            return {"artifact_path": artifact_path}

    async def restore(self, context: RestoreContext) -> Dict[str, Any]:
        cfg = context.config or {}
        if not await self.validate_config(cfg):
            raise ValueError("vaultwarden config invalid; container_name and data_path required")
        container = str(cfg.get("container_name")).strip()
        data_path = str(cfg.get("data_path", DEFAULT_DATA_PATH)).rstrip("/") or "/"

        artifact_path = context.artifact_path
        if not artifact_path or not os.path.exists(artifact_path):
            raise FileNotFoundError(f"Artifact not found: {artifact_path}")

        self._logger.info(
            "vaultwarden_restore_start | job_id=%s source=%s dest=%s container=%s artifact=%s",
            context.job_id,
            context.source_target_id,
            context.destination_target_id,
            container,
            artifact_path,
        )

        async with self._docker_client() as client:
            exists = await self._container_exists(client, container)
            if not exists:
                raise FileNotFoundError(f"Container {container} not found")

            with tempfile.TemporaryDirectory() as staging_dir:
                extracted_dir = os.path.join(staging_dir, "extracted")
                os.makedirs(extracted_dir, exist_ok=True)
                self._safe_extract_tar(artifact_path, extracted_dir)

                db_local = os.path.join(extracted_dir, "db.sqlite3")
                cfg_local = os.path.join(extracted_dir, "config.json")
                if not os.path.isfile(db_local):
                    raise FileNotFoundError("db.sqlite3 missing in artifact")
                if not os.path.isfile(cfg_local):
                    raise FileNotFoundError("config.json missing in artifact")

                put_tar_path = os.path.join(staging_dir, "restore.tar")
                with tarfile.open(put_tar_path, "w") as tar:
                    tar.add(db_local, arcname="db.sqlite3")
                    tar.add(cfg_local, arcname="config.json")
                    attachments_local = os.path.join(extracted_dir, "attachments")
                    if os.path.isdir(attachments_local):
                        tar.add(attachments_local, arcname="attachments")

                resp = await client.put(
                    f"/containers/{container}/archive",
                    params={"path": data_path},
                    content=self._iter_file_chunks(put_tar_path),
                )
                if resp.status_code // 100 != 2:
                    raise RuntimeError(
                        f"vaultwarden restore failed: {resp.status_code} {resp.text}"
                    )

        return {
            "status": "success",
            "message": f"Restored vaultwarden data into {container}:{data_path}",
            "restored_path": f"{container}:{data_path}",
        }

    async def get_status(self, context: BackupContext) -> Dict[str, Any]:  # pragma: no cover - trivial
        return {"status": "ok"}

    def _docker_client(self) -> httpx.AsyncClient:
        transport = httpx.AsyncHTTPTransport(uds=DOCKER_SOCKET_PATH)
        return httpx.AsyncClient(
            transport=transport,
            base_url="http://docker",
            timeout=httpx.Timeout(30.0, connect=10.0),
        )

    async def _container_exists(self, client: httpx.AsyncClient, container: str) -> bool:
        resp = await client.get(f"/containers/{container}/json")
        if resp.status_code == 404:
            return False
        resp.raise_for_status()
        return True

    async def _path_exists(
        self, client: httpx.AsyncClient, container: str, path: str
    ) -> Tuple[bool, str]:
        try:
            resp = await client.get(f"/containers/{container}/archive", params={"path": path})
        except Exception as exc:  # pragma: no cover - defensive
            return False, str(exc)
        if resp.status_code == 404:
            return False, f"{path} not found"
        if resp.status_code // 100 != 2:
            return False, f"status {resp.status_code}"
        await resp.aclose()
        return True, ""

    async def _fetch_archive(
        self,
        client: httpx.AsyncClient,
        container: str,
        path: str,
        staging_dir: str,
        *,
        required: bool,
    ) -> bool:
        async with client.stream(
            "GET", f"/containers/{container}/archive", params={"path": path}
        ) as resp:
            if resp.status_code == 404:
                if required:
                    raise FileNotFoundError(f"{path} not found in container")
                return False
            resp.raise_for_status()
            tmp_tar_path = os.path.join(staging_dir, f"{os.path.basename(path)}.tar")
            with open(tmp_tar_path, "wb") as fh:
                async for chunk in resp.aiter_bytes():
                    fh.write(chunk)
        self._safe_extract_tar(tmp_tar_path, staging_dir)
        return True

    def _safe_extract_tar(self, tar_path: str, dest_dir: str) -> None:
        with tarfile.open(tar_path, "r:*") as tar:
            members = []
            for member in tar.getmembers():
                name = member.name
                if name.startswith("/") or ".." in name.split(os.path.sep):
                    continue
                members.append(member)
            tar.extractall(path=dest_dir, members=members, filter="data")

    def _verify_artifact(self, artifact_path: str) -> None:
        if not os.path.exists(artifact_path):
            raise RuntimeError("vaultwarden backup did not produce artifact")
        with tarfile.open(artifact_path, "r:gz") as tar:
            names = tar.getnames()
            if "db.sqlite3" not in names or "config.json" not in names:
                raise RuntimeError("vaultwarden artifact missing required files")

    async def _iter_file_chunks(self, path: str):
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(1024 * 1024)
                if not chunk:
                    break
                yield chunk
