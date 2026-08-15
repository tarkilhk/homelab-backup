from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import shutil
import sqlite3
import tarfile
import tempfile
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, AsyncIterator, Dict, Tuple
from urllib.parse import urlsplit

import httpx

from app.core.plugins.artifacts import create_backup_artifact
from app.core.plugins.base import BackupContext, BackupPlugin, RestoreContext

BACKUP_BASE_PATH = "/backups"
DEFAULT_DATA_PATH = "/data"
DOCKER_SOCKET_PATH = "/var/run/docker.sock"
CONTAINER_COMMAND_TIMEOUT_SECONDS = 120.0
CONTAINER_COMMAND_POLL_SECONDS = 0.25
READINESS_ATTEMPTS = 240
READINESS_POLL_SECONDS = 0.25
ARTIFACT_COMPONENTS = (
    "db.sqlite3",
    "config.json",
    "rsa_key.pem",
    "rsa_key.pub.pem",
    "attachments",
    "sends",
)


class VaultWardenPlugin(BackupPlugin):
    restore_capability = "automatic"
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
        try:
            self._data_path(config)
            self._health_url(config)
        except ValueError:
            return False
        return True

    def _data_path(self, config: Dict[str, Any]) -> str:
        value = config.get("data_path", DEFAULT_DATA_PATH)
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Vaultwarden data_path must be a non-empty absolute path")
        raw = value.strip().rstrip("/") or "/"
        path = PurePosixPath(raw)
        forbidden_roots = {
            PurePosixPath("/app"),
            PurePosixPath("/backups"),
            PurePosixPath("/etc"),
            PurePosixPath("/usr"),
            PurePosixPath("/var"),
        }
        if (
            not raw.startswith("/")
            or ".." in path.parts
            or str(path) != raw
            or path == PurePosixPath("/")
            or any(path == root or root in path.parents for root in forbidden_roots)
        ):
            raise ValueError("Vaultwarden data_path is unsafe")
        return str(path)

    def _health_url(self, config: Dict[str, Any]) -> str | None:
        value = config.get("health_url")
        if value is None or value == "":
            return None
        if not isinstance(value, str):
            raise ValueError("Vaultwarden health_url must be an HTTP or HTTPS URL")
        url = value.strip()
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("Vaultwarden health_url must be an HTTP or HTTPS URL")
        return url

    async def test(self, config: Dict[str, Any]) -> bool:
        """Verify the container is reachable and core data files exist."""
        if not await self.validate_config(config):
            raise ValueError("Invalid configuration: container_name and data_path are required")
        container = str(config.get("container_name")).strip()
        data_path = self._data_path(config)
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
        data_path = self._data_path(cfg)

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
                        components = ["db.sqlite3"]
                        with tarfile.open(artifact.temporary_path, "w:gz") as tar:
                            tar.add(generated_db, arcname="db.sqlite3")
                            cfg_local = data_dir / "config.json"
                            if cfg_local.is_file():
                                tar.add(cfg_local, arcname="config.json")
                                components.append("config.json")
                            attachments_local = data_dir / "attachments"
                            if attachments_local.is_dir():
                                tar.add(attachments_local, arcname="attachments")
                                components.append("attachments")
                            sends_local = data_dir / "sends"
                            if sends_local.is_dir():
                                tar.add(sends_local, arcname="sends")
                                components.append("sends")
                            for key_name in ("rsa_key.pem", "rsa_key.pub.pem"):
                                key_local = data_dir / key_name
                                if key_local.is_file():
                                    tar.add(key_local, arcname=key_name)
                                    components.append(key_name)
                            manifest = json.dumps(
                                {
                                    "format_version": 1,
                                    "components": sorted(components),
                                },
                                sort_keys=True,
                            ).encode()
                            manifest_info = tarfile.TarInfo("backup-manifest.json")
                            manifest_info.size = len(manifest)
                            tar.addfile(manifest_info, io.BytesIO(manifest))
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
        cfg = context.config or {}
        if not await self.validate_config(cfg):
            raise ValueError("vaultwarden config invalid; container_name and data_path required")
        container = str(cfg.get("container_name")).strip()
        data_path = self._data_path(cfg)
        health_url = self._health_url(cfg)
        artifact_path = Path(context.artifact_path)
        if not artifact_path.is_file() or artifact_path.is_symlink():
            raise FileNotFoundError(f"Artifact not found: {artifact_path}")

        self._verify_artifact(str(artifact_path))
        with tempfile.TemporaryDirectory(prefix="vaultwarden-restore-verify-") as verify_dir:
            self._safe_extract_tar(str(artifact_path), verify_dir)
            restored_database = Path(verify_dir) / "db.sqlite3"
            if not restored_database.is_file():
                raise RuntimeError("Vaultwarden artifact missing db.sqlite3")
            self._verify_sqlite_database(restored_database)

        async with self._docker_client() as client:
            inspect = await client.get(f"/containers/{container}/json")
            if inspect.status_code == 404:
                raise FileNotFoundError(f"Container '{container}' not found")
            inspect.raise_for_status()
            details = inspect.json()
            image = (details.get("Config") or {}).get("Image")
            was_running = (details.get("State") or {}).get("Running") is True
            if not isinstance(image, str) or not image:
                raise RuntimeError("Vaultwarden container image could not be determined")
            self._validate_restore_mount(details, data_path)

            if was_running:
                stop = await client.post(f"/containers/{container}/stop", params={"t": 30})
                if stop.status_code not in {204, 304}:
                    stop.raise_for_status()

            helper_id: str | None = None
            destination_restarted = False
            readiness_verified = not was_running
            safe_to_restart = True
            destination_running = False
            mutation_started = False
            rollback_applied = False
            try:
                with tempfile.TemporaryDirectory(
                    prefix="vaultwarden-restore-rollback-"
                ) as rollback_dir:
                    rollback_artifact = await self._capture_container_artifact(
                        client,
                        container,
                        data_path,
                        Path(rollback_dir),
                    )
                    helper_name = f"homelab-backup-vaultwarden-restore-{uuid.uuid4().hex[:12]}"
                    created = await client.post(
                        "/containers/create",
                        params={"name": helper_name},
                        json={
                            "Image": image,
                            "Entrypoint": ["/usr/bin/sleep"],
                            "Cmd": ["infinity"],
                            "HostConfig": {
                                "VolumesFrom": [f"{container}:rw"],
                                "NetworkMode": "none",
                            },
                        },
                    )
                    created.raise_for_status()
                    helper_value = created.json().get("Id")
                    if not isinstance(helper_value, str) or not helper_value:
                        raise RuntimeError("Docker did not return a restore helper identifier")
                    helper_id = helper_value
                    start_helper = await client.post(f"/containers/{helper_id}/start")
                    start_helper.raise_for_status()

                    try:
                        await self._upload_restore_archive(client, helper_id, artifact_path)
                        mutation_started = True
                        await self._apply_restore_archive(client, helper_id, data_path)
                        await self._verify_container_database(client, container, data_path)
                        if was_running:
                            await self._start_container(client, container)
                            destination_running = True
                            destination_restarted = True
                            try:
                                readiness_verified = await self._wait_for_container_readiness(
                                    client, container, health_url
                                )
                            except Exception as readiness_error:
                                await self._stop_container(client, container)
                                destination_running = False
                                await self._upload_restore_archive(
                                    client,
                                    helper_id,
                                    rollback_artifact,
                                )
                                await self._apply_restore_archive(client, helper_id, data_path)
                                await self._verify_container_database(
                                    client,
                                    container,
                                    data_path,
                                )
                                rollback_applied = True
                                try:
                                    await self._start_container(client, container)
                                    destination_running = True
                                    await self._wait_for_container_readiness(
                                        client, container, health_url
                                    )
                                except Exception as rollback_start_error:
                                    if destination_running:
                                        await self._stop_container(client, container)
                                        destination_running = False
                                    safe_to_restart = False
                                    raise RuntimeError(
                                        "Vaultwarden restore failed readiness; previous data was "
                                        "restored, but the destination could not be restarted and "
                                        "was left stopped"
                                    ) from rollback_start_error
                                raise RuntimeError(
                                    "Vaultwarden restore failed readiness; previous data was "
                                    "restored and the destination was restarted"
                                ) from readiness_error
                    except Exception as restore_error:
                        if mutation_started and not rollback_applied:
                            if destination_running:
                                await self._stop_container(client, container)
                                destination_running = False
                            try:
                                await self._upload_restore_archive(
                                    client,
                                    helper_id,
                                    rollback_artifact,
                                )
                                await self._apply_restore_archive(client, helper_id, data_path)
                                await self._verify_container_database(
                                    client,
                                    container,
                                    data_path,
                                )
                                rollback_applied = True
                            except Exception as rollback_error:
                                safe_to_restart = False
                                raise RuntimeError(
                                    "Vaultwarden restore failed and rollback also failed: "
                                    f"restore={restore_error}; rollback={rollback_error}. "
                                    "The destination was left stopped."
                                ) from restore_error
                        raise
            finally:
                if helper_id:
                    try:
                        await client.delete(
                            f"/containers/{helper_id}",
                            params={"force": "true", "v": "true"},
                        )
                    except httpx.HTTPError:
                        self._logger.warning(
                            "vaultwarden_restore_helper_cleanup_failed | helper=%s", helper_id
                        )
                if was_running and safe_to_restart and not destination_running:
                    await self._start_container(client, container)
                    destination_running = True
                    destination_restarted = True
                    readiness_verified = await self._wait_for_container_readiness(
                        client, container, health_url
                    )

        return {
            "status": "success" if readiness_verified else "partial",
            "artifact_path": str(artifact_path),
            "artifact_bytes": artifact_path.stat().st_size,
            "message": (
                "Vaultwarden restore completed and destination health was verified"
                if readiness_verified and destination_restarted
                else (
                    "Vaultwarden restore completed while the destination remained stopped"
                    if not destination_restarted
                    else (
                        "Vaultwarden restore completed and the destination restarted, but no "
                        "container healthcheck was available to verify application readiness"
                    )
                )
            ),
        }

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

    async def _wait_for_container_readiness(
        self,
        client: httpx.AsyncClient,
        container: str,
        configured_health_url: str | None = None,
    ) -> bool:
        for _ in range(READINESS_ATTEMPTS):
            response = await client.get(f"/containers/{container}/json")
            response.raise_for_status()
            details = response.json()
            state = details.get("State") or {}
            if state.get("Running") is not True:
                await asyncio.sleep(READINESS_POLL_SECONDS)
                continue
            health = state.get("Health")
            if isinstance(health, dict):
                status = health.get("Status")
                if status == "healthy":
                    return True
                if status == "unhealthy":
                    raise RuntimeError("Vaultwarden destination became unhealthy after restore")
            elif await self._probe_vaultwarden_http(
                self._vaultwarden_health_urls(details, configured_health_url)
            ):
                return True
            await asyncio.sleep(READINESS_POLL_SECONDS)
        raise RuntimeError("Vaultwarden application did not become ready after restore")

    def _vaultwarden_health_urls(
        self,
        details: Dict[str, Any],
        configured_health_url: str | None,
    ) -> list[str]:
        if configured_health_url:
            return [configured_health_url]
        port = 80
        environment = (details.get("Config") or {}).get("Env")
        if isinstance(environment, list):
            for item in environment:
                if isinstance(item, str) and item.startswith("ROCKET_PORT="):
                    try:
                        port = int(item.split("=", 1)[1])
                    except ValueError:
                        pass
        networks = (details.get("NetworkSettings") or {}).get("Networks")
        if not isinstance(networks, dict):
            return []
        urls: list[str] = []
        for network in networks.values():
            if not isinstance(network, dict):
                continue
            address = network.get("IPAddress")
            if isinstance(address, str) and address:
                urls.append(f"http://{address}:{port}/alive")
        return urls

    async def _probe_vaultwarden_http(self, urls: list[str]) -> bool:
        async with httpx.AsyncClient(timeout=2.0, follow_redirects=False) as probe_client:
            for url in urls:
                try:
                    response = await probe_client.get(url)
                except httpx.HTTPError:
                    continue
                if response.status_code // 100 == 2:
                    return True
        return False

    def _validate_restore_mount(self, details: Dict[str, Any], data_path: str) -> None:
        destination = PurePosixPath(data_path)
        mounts = details.get("Mounts")
        if not isinstance(mounts, list):
            mounts = []
        for mount in mounts:
            if not isinstance(mount, dict) or mount.get("RW") is not True:
                continue
            mount_destination = mount.get("Destination")
            if not isinstance(mount_destination, str) or not mount_destination.startswith("/"):
                continue
            mounted_root = PurePosixPath(mount_destination)
            if destination == mounted_root:
                return
        raise RuntimeError(
            "Vaultwarden data_path must exactly match a writable Docker mount before restore"
        )

    async def _start_container(self, client: httpx.AsyncClient, container: str) -> None:
        response = await client.post(f"/containers/{container}/start")
        if response.status_code not in {204, 304}:
            response.raise_for_status()

    async def _stop_container(self, client: httpx.AsyncClient, container: str) -> None:
        response = await client.post(f"/containers/{container}/stop", params={"t": 30})
        if response.status_code not in {204, 304}:
            response.raise_for_status()

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
                "Cmd": [
                    "/usr/bin/timeout",
                    "--signal=KILL",
                    str(int(CONTAINER_COMMAND_TIMEOUT_SECONDS)),
                    *command,
                ],
            },
        )
        create_response.raise_for_status()
        exec_id = create_response.json().get("Id")
        if not isinstance(exec_id, str) or not exec_id:
            raise RuntimeError("Docker did not return an exec identifier")

        start_response = await client.post(
            f"/exec/{exec_id}/start",
            json={"Detach": True, "Tty": False},
        )
        start_response.raise_for_status()
        deadline = asyncio.get_running_loop().time() + CONTAINER_COMMAND_TIMEOUT_SECONDS + 5.0
        while True:
            try:
                inspect_response = await client.get(f"/exec/{exec_id}/json")
                inspect_response.raise_for_status()
                state = inspect_response.json()
                if state.get("Running") is not True:
                    exit_code = state.get("ExitCode")
                    if exit_code != 0:
                        raise RuntimeError(
                            f"Container command {command[0]!r} failed with exit code "
                            f"{exit_code}"
                        )
                    return
            except httpx.HTTPError:
                pass
            if asyncio.get_running_loop().time() >= deadline:
                raise RuntimeError(
                    f"Container command {command[0]!r} did not stop before its deadline"
                )
            await asyncio.sleep(CONTAINER_COMMAND_POLL_SECONDS)

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
            resp = await client.head(f"/containers/{container}/archive", params={"path": path})
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

    async def _capture_container_artifact(
        self,
        client: httpx.AsyncClient,
        container: str,
        data_path: str,
        staging_dir: Path,
    ) -> Path:
        components: list[str] = []
        for component in ARTIFACT_COMPONENTS:
            found = await self._fetch_archive(
                client,
                container,
                f"{data_path}/{component}",
                str(staging_dir),
                required=component == "db.sqlite3",
            )
            if found:
                components.append(component)
        database = staging_dir / "db.sqlite3"
        self._verify_sqlite_database(database)
        artifact = staging_dir / "rollback.tar.gz"
        self._write_component_archive(staging_dir, artifact, components)
        return artifact

    def _write_component_archive(
        self,
        source_dir: Path,
        artifact: Path,
        components: list[str],
    ) -> None:
        with tarfile.open(artifact, "w:gz") as archive:
            for component in components:
                archive.add(source_dir / component, arcname=component)
            manifest = json.dumps(
                {"format_version": 1, "components": sorted(components)},
                sort_keys=True,
            ).encode()
            info = tarfile.TarInfo("backup-manifest.json")
            info.size = len(manifest)
            archive.addfile(info, io.BytesIO(manifest))

    async def _upload_restore_archive(
        self,
        client: httpx.AsyncClient,
        helper_id: str,
        artifact_path: Path,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="vaultwarden-restore-upload-") as upload_dir:
            docker_archive = Path(upload_dir) / "restore-upload.tar"
            with tarfile.open(docker_archive, "w") as archive:
                archive.add(artifact_path, arcname="restore.tar.gz")

            async def chunks() -> AsyncIterator[bytes]:
                with docker_archive.open("rb") as archive_file:
                    while chunk := archive_file.read(1024 * 1024):
                        yield chunk

            upload = await client.put(
                f"/containers/{helper_id}/archive",
                params={"path": "/tmp"},
                content=chunks(),
            )
            upload.raise_for_status()

    async def _apply_restore_archive(
        self,
        client: httpx.AsyncClient,
        helper_id: str,
        data_path: str,
    ) -> None:
        await self._exec_container_command(
            client,
            helper_id,
            [
                "rm",
                "-f",
                f"{data_path}/db.sqlite3",
                f"{data_path}/db.sqlite3-wal",
                f"{data_path}/db.sqlite3-shm",
                f"{data_path}/config.json",
                f"{data_path}/rsa_key.pem",
                f"{data_path}/rsa_key.pub.pem",
                f"{data_path}/backup-manifest.json",
            ],
        )
        await self._exec_container_command(
            client,
            helper_id,
            ["rm", "-rf", f"{data_path}/attachments", f"{data_path}/sends"],
        )
        await self._exec_container_command(
            client,
            helper_id,
            ["tar", "-xzf", "/tmp/restore.tar.gz", "-C", data_path],
        )
        await self._exec_container_command(
            client,
            helper_id,
            ["rm", "-f", f"{data_path}/backup-manifest.json"],
        )

    async def _verify_container_database(
        self,
        client: httpx.AsyncClient,
        container: str,
        data_path: str,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="vaultwarden-restore-check-") as directory:
            await self._fetch_archive(
                client,
                container,
                f"{data_path}/db.sqlite3",
                directory,
                required=True,
            )
            self._verify_sqlite_database(Path(directory) / "db.sqlite3")

    def _safe_extract_tar(self, tar_path: str, dest_dir: str) -> None:
        with tarfile.open(tar_path, "r:*") as tar:
            members = []
            for member in tar.getmembers():
                name = member.name
                if (
                    name.startswith("/")
                    or ".." in Path(name).parts
                    or member.issym()
                    or member.islnk()
                    or not (member.isdir() or member.isreg())
                ):
                    raise RuntimeError(f"Vaultwarden backup contains unsafe path: {name}")
                members.append(member)
            tar.extractall(path=dest_dir, members=members, filter="data")

    def _verify_artifact(self, artifact_path: str) -> None:
        if not os.path.exists(artifact_path):
            raise RuntimeError("vaultwarden backup did not produce artifact")
        with tarfile.open(artifact_path, "r:gz") as tar:
            names = {name.strip("/") for name in tar.getnames()}
            if "db.sqlite3" not in names:
                raise RuntimeError("vaultwarden artifact missing db.sqlite3")
            if "backup-manifest.json" not in names:
                raise RuntimeError("vaultwarden artifact missing backup-manifest.json")
            manifest_file = tar.extractfile("backup-manifest.json")
            if manifest_file is None:
                raise RuntimeError("vaultwarden artifact manifest is unreadable")
            try:
                manifest = json.load(manifest_file)
            except (UnicodeDecodeError, ValueError) as exc:
                raise RuntimeError("vaultwarden artifact manifest is invalid") from exc
            if not isinstance(manifest, dict):
                raise RuntimeError("vaultwarden artifact manifest is invalid")
            components = manifest.get("components")
            if (
                manifest.get("format_version") != 1
                or not isinstance(components, list)
                or "db.sqlite3" not in components
            ):
                raise RuntimeError("vaultwarden artifact manifest is invalid")
            top_level = {name.split("/", 1)[0] for name in names}
            if any(component not in ARTIFACT_COMPONENTS for component in components):
                raise RuntimeError("vaultwarden artifact manifest contains unknown components")
            if any(component not in top_level for component in components):
                raise RuntimeError("vaultwarden artifact is missing a declared component")
