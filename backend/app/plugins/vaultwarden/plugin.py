from __future__ import annotations

import logging
import os
import re
import sqlite3
import tarfile
import tempfile
from pathlib import Path
from typing import Any, Dict, Tuple

import httpx

from app.core.plugins.artifacts import create_backup_artifact
from app.core.plugins.base import BackupContext, BackupPlugin, RestoreContext

BACKUP_BASE_PATH = "/backups"
DEFAULT_DATA_PATH = "/data"
DOCKER_SOCKET_PATH = "/var/run/docker.sock"


class VaultWardenPlugin(BackupPlugin):
    restore_capability = "manual"
    """Vaultwarden backup plugin using Docker Engine API via unix socket.

    Research summary (Vaultwarden wiki: Backing up your vault):
    - Simplest backup: `docker exec <container> /vaultwarden backup` and copy
      resulting archive, or tar the critical data directly.
    - We back up exactly: `/data/db.sqlite3`, `/data/config.json` (if present),
      and `/data/attachments` (if present).
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
                    self._logger.warning(
                        "vaultwarden_test_failed | container=%s missing", container
                    )
                    raise FileNotFoundError(f"Container '{container}' not found")
                db_ok, db_err = await self._path_exists(
                    client, container, f"{data_path}/db.sqlite3"
                )
                if not db_ok:
                    self._logger.warning(
                        "vaultwarden_test_failed | container=%s error=%s", container, db_err
                    )
                    raise FileNotFoundError(f"db.sqlite3 not found in container: {db_err}")
                cfg_ok, _ = await self._path_exists(
                    client, container, f"{data_path}/config.json", optional=True
                )
                if not cfg_ok:
                    self._logger.info(
                        "vaultwarden_test_warn_config_missing | container=%s path=%s",
                        container,
                        f"{data_path}/config.json",
                    )
                return True
        except ValueError:
            raise
        except FileNotFoundError:
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

        self._logger.info(
            "vaultwarden_backup_start | job_id=%s target_id=%s container=%s data_path=%s artifact=%s",
            context.job_id,
            context.target_id,
            container,
            data_path,
            "<pending>",
        )

        async with self._docker_client() as client:
            exists = await self._container_exists(client, container)
            if not exists:
                raise FileNotFoundError(f"Container {container} not found")

            await self._exec_container_command(
                client,
                container,
                ["/vaultwarden", "backup"],
            )

            with tempfile.TemporaryDirectory() as staging_dir:
                await self._fetch_archive(
                    client,
                    container,
                    data_path,
                    staging_dir,
                    required=True,
                )
                generated_db = self._find_generated_database_backup(staging_dir)
                self._verify_sqlite_database(generated_db)
                data_dir = generated_db.parent

                try:
                    with create_backup_artifact(
                        self,
                        context,
                        prefix="vaultwarden-backup",
                        suffix=".tar.gz",
                        backup_root=BACKUP_BASE_PATH,
                    ) as artifact:
                        with tarfile.open(artifact.temporary_path, "w:gz") as tar:
                            tar.add(generated_db, arcname="db.sqlite3")
                            cfg_local = data_dir / "config.json"
                            if cfg_local.is_file():
                                tar.add(cfg_local, arcname="config.json")
                            attachments_local = data_dir / "attachments"
                            if attachments_local.is_dir():
                                tar.add(attachments_local, arcname="attachments")
                        self._verify_artifact(str(artifact.temporary_path))
                finally:
                    generated_container_path = os.path.join(
                        data_path,
                        generated_db.name,
                    )
                    try:
                        await self._exec_container_command(
                            client,
                            container,
                            ["rm", "-f", generated_container_path],
                        )
                    except Exception as exc:
                        self._logger.warning(
                            "vaultwarden_generated_backup_cleanup_failed | container=%s path=%s error=%s",
                            container,
                            generated_container_path,
                            exc,
                        )

            return {"artifact_path": str(artifact.final_path)}

    async def restore(self, context: RestoreContext) -> Dict[str, Any]:
        raise NotImplementedError(
            "Vaultwarden restore requires a stopped container and removal of stale SQLite WAL files"
        )

    async def get_status(
        self, context: BackupContext
    ) -> Dict[str, Any]:  # pragma: no cover - trivial
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

    async def _exec_container_command(
        self,
        client: httpx.AsyncClient,
        container: str,
        command: list[str],
    ) -> None:
        create_response = await client.post(
            f"/containers/{container}/exec",
            json={
                "AttachStdout": True,
                "AttachStderr": True,
                "Cmd": command,
            },
        )
        create_response.raise_for_status()
        exec_id = create_response.json().get("Id")
        if not isinstance(exec_id, str) or not exec_id:
            raise RuntimeError("Docker did not return an exec identifier")

        start_response = await client.post(
            f"/exec/{exec_id}/start",
            json={"Detach": False, "Tty": False},
        )
        start_response.raise_for_status()
        inspect_response = await client.get(f"/exec/{exec_id}/json")
        inspect_response.raise_for_status()
        exit_code = inspect_response.json().get("ExitCode")
        if exit_code != 0:
            raise RuntimeError(
                f"Container command {command[0]!r} failed with exit code {exit_code}"
            )

    def _find_generated_database_backup(self, staging_dir: str) -> Path:
        pattern = re.compile(r"^db_\d{8}_\d{6}\.sqlite3$")
        candidates = [
            path
            for path in Path(staging_dir).rglob("db_*.sqlite3")
            if path.is_file() and pattern.fullmatch(path.name)
        ]
        if not candidates:
            raise FileNotFoundError(
                "Vaultwarden built-in backup did not create a db_*.sqlite3 file"
            )
        return max(candidates, key=lambda path: path.name)

    def _verify_sqlite_database(self, database_path: Path) -> None:
        try:
            with sqlite3.connect(
                f"file:{database_path}?mode=ro",
                uri=True,
            ) as connection:
                result = connection.execute("PRAGMA quick_check").fetchone()
        except sqlite3.Error as exc:
            raise RuntimeError("Vaultwarden generated an unreadable SQLite backup") from exc
        if result is None or result[0] != "ok":
            raise RuntimeError("Vaultwarden generated an invalid SQLite backup")

    async def _path_exists(
        self, client: httpx.AsyncClient, container: str, path: str, optional: bool = False
    ) -> Tuple[bool, str]:
        try:
            resp = await client.get(f"/containers/{container}/archive", params={"path": path})
        except Exception as exc:  # pragma: no cover - defensive
            return False, str(exc)
        if resp.status_code == 404:
            return (True, "") if optional else (False, f"{path} not found")
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
            if "db.sqlite3" not in names:
                raise RuntimeError("vaultwarden artifact missing db.sqlite3")
