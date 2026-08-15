from __future__ import annotations

import asyncio
import configparser
import io
import logging
import re
import shutil
import tarfile
import tempfile
import threading
import time
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, AsyncIterator, Dict, NoReturn

import httpx

from app.core.plugins.artifacts import create_backup_artifact
from app.core.plugins.base import BackupContext, BackupPlugin, RestoreContext

_CONTAINER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_MIN_TIMEOUT_SECONDS = 30
_MAX_TIMEOUT_SECONDS = 3600
_EXPECTED_IMAGE = "gitea/gitea:1.27.1"
_DOCKER_SOCKET_PATH = "/var/run/docker.sock"
_HEALTH_COMMAND = [
    "/usr/bin/timeout",
    "-s",
    "KILL",
    "10",
    "curl",
    "-fsS",
    "http://127.0.0.1:3000/api/healthz",
]
BACKUP_BASE_PATH = "/backups"
_DUMP_PATH = "/tmp/gitea-dump.zip"
_RESTORE_DESTINATION_LABEL = "asia.hollinger.homelab-backup.restore-destination"
_REQUIRED_DUMP_MEMBERS = {"app.ini", "data/conf/app.ini", "gitea-db.sql"}
_MAX_DUMP_MEMBERS = 1_000_000
_MAX_DUMP_UNCOMPRESSED_BYTES = 4 * 1024**4
_MAX_COMPRESSION_RATIO = 1000
_SOURCE_LAYOUT = {
    "/data/git/repositories": "repos",
    "/data/gitea/attachments": "data/attachments",
    "/data/gitea/lfs": "data/lfs",
    "/data/gitea/packages": "data/packages",
}
_CLEAR_DATA_CONTENTS = """
rm -rf /data/git
mkdir -p /data/gitea
if [ -d /data/gitea/packages ]; then
  find /data/gitea/packages -mindepth 1 -maxdepth 1 -exec rm -rf -- '{}' +
fi
find /data/gitea -mindepth 1 -maxdepth 1 ! -name packages -exec rm -rf -- '{}' +
""".strip()
_LOGGER = logging.getLogger(__name__)
_CONTAINER_LOCKS: dict[str, threading.Lock] = {}
_CONTAINER_LOCKS_GUARD = threading.Lock()


class _HelperExecutionTerminated(RuntimeError):
    """The helper was force-removed after its command state became uncertain."""


class _HelperStateUnconfirmed(RuntimeError):
    """Docker could not confirm that an uncertain helper command stopped."""


class GiteaPlugin(BackupPlugin):
    """Consistent backup and isolated restore for Gitea 1.27.1 on Docker."""

    restore_capability = "automatic"

    def __init__(self, name: str, version: str = "0.1.0") -> None:
        """Initialize the Gitea plugin metadata."""
        super().__init__(name=name, version=version)

    async def validate_config(self, config: Dict[str, Any]) -> bool:
        """Return whether config matches the bounded Gitea target contract."""
        if not isinstance(config, dict):
            return False
        container_name = config.get("container_name")
        if not isinstance(container_name, str) or not _CONTAINER_NAME.fullmatch(container_name):
            return False
        if not isinstance(config.get("allow_service_stop"), bool):
            return False
        timeout_seconds = config.get("timeout_seconds", 600)
        return (
            isinstance(timeout_seconds, int)
            and not isinstance(timeout_seconds, bool)
            and _MIN_TIMEOUT_SECONDS <= timeout_seconds <= _MAX_TIMEOUT_SECONDS
        )

    async def test(self, config: Dict[str, Any]) -> bool:
        """Prove exact image, mount, SQLite configuration, and application health."""
        if not await self.validate_config(config):
            raise ValueError(
                "Invalid Gitea configuration: container_name, allow_service_stop, "
                "and timeout_seconds are required"
            )
        container_name = str(config["container_name"])
        try:
            async with self._docker_client() as client:
                details = await self._inspect_container(client, container_name)
                self._validate_container(details)
                await self._validate_sqlite_configuration(client, container_name)
                await self._run_health_check(client, container_name)
        except (ValueError, FileNotFoundError, RuntimeError):
            raise
        except httpx.RequestError as exc:
            raise ConnectionError("Failed to connect to the local Docker Engine") from exc
        return True

    async def backup(self, context: BackupContext) -> Dict[str, Any]:
        """Create a consistent native Gitea dump and restart the source safely."""
        container_lock = self._acquire_container_lock(context.config)
        started_at = time.monotonic()
        _LOGGER.info(
            "gitea_backup_start | job_id=%s target_id=%s",
            context.job_id,
            context.target_id,
            extra={"job_id": context.job_id, "target_id": context.target_id},
        )
        try:
            result = await self._backup_transaction(context)
        except asyncio.CancelledError:
            _LOGGER.warning(
                "gitea_backup_cancelled | job_id=%s duration_seconds=%.3f",
                context.job_id,
                time.monotonic() - started_at,
                extra={"job_id": context.job_id, "target_id": context.target_id},
            )
            raise
        except Exception:
            _LOGGER.exception(
                "gitea_backup_failed | job_id=%s duration_seconds=%.3f",
                context.job_id,
                time.monotonic() - started_at,
                extra={"job_id": context.job_id, "target_id": context.target_id},
            )
            raise
        finally:
            container_lock.release()
        _LOGGER.info(
            "gitea_backup_success | job_id=%s artifact_path=%s duration_seconds=%.3f",
            context.job_id,
            result["artifact_path"],
            time.monotonic() - started_at,
            extra={"job_id": context.job_id, "target_id": context.target_id},
        )
        return result

    async def _backup_transaction(self, context: BackupContext) -> Dict[str, Any]:
        config = context.config or {}
        if not await self.validate_config(config):
            raise ValueError("Invalid Gitea backup configuration")
        if config.get("allow_service_stop") is not True:
            raise ValueError("Gitea backup requires allow_service_stop=true for a consistent dump")
        container_name = str(config["container_name"])
        timeout_seconds = int(config.get("timeout_seconds", 600))
        helper_id: str | None = None
        stopped = False

        async with self._docker_client() as client:
            details = await self._inspect_container(client, container_name)
            self._validate_container(details)
            await self._validate_sqlite_configuration(client, container_name)
            required_layout = await self._source_content_layout(client, container_name)
            stopped = True
            try:
                await self._stop_container(client, container_name, timeout_seconds)
                await self._confirm_stopped(client, container_name)
                helper_id = await self._create_dump_helper(client, container_name)
                await self._start_container(client, helper_id)
                await self._wait_for_helper(client, helper_id, timeout_seconds)
                with create_backup_artifact(
                    self,
                    context,
                    prefix="gitea-dump",
                    suffix=".zip",
                    backup_root=BACKUP_BASE_PATH,
                ) as artifact:
                    await self._download_dump(
                        client,
                        helper_id,
                        artifact.temporary_path,
                        timeout_seconds,
                    )
                    self._validate_dump(
                        artifact.temporary_path,
                        required_layout=required_layout,
                    )
                    await self._remove_helper(client, helper_id, strict=True)
                    helper_id = None
                    await self._start_container(client, container_name)
                    await self._wait_for_readiness(client, container_name)
                    stopped = False
                artifact_path = str(artifact.final_path)
            finally:
                cleanup_error: Exception | None = None
                if helper_id is not None:
                    try:
                        await self._remove_helper(client, helper_id, strict=True)
                    except Exception as exc:
                        cleanup_error = exc
                if stopped:
                    try:
                        await self._start_container(client, container_name)
                        await self._wait_for_readiness(client, container_name)
                    except Exception as exc:
                        raise RuntimeError(
                            "Gitea backup could not safely restart the source container"
                        ) from exc
                if cleanup_error is not None:
                    raise RuntimeError(
                        "Gitea backup could not confirm helper cleanup"
                    ) from cleanup_error

        return {"artifact_path": artifact_path}

    async def restore(self, context: RestoreContext) -> Dict[str, Any]:
        """Restore a dump transactionally into an explicitly labeled destination."""
        container_lock = self._acquire_container_lock(context.config)
        started_at = time.monotonic()
        _LOGGER.info(
            "gitea_restore_start | job_id=%s destination_target_id=%s",
            context.job_id,
            context.destination_target_id,
            extra={
                "job_id": context.job_id,
                "destination_target_id": context.destination_target_id,
            },
        )
        try:
            result = await self._restore_transaction(context)
        except asyncio.CancelledError:
            _LOGGER.warning(
                "gitea_restore_cancelled | job_id=%s duration_seconds=%.3f",
                context.job_id,
                time.monotonic() - started_at,
                extra={"job_id": context.job_id},
            )
            raise
        except Exception:
            _LOGGER.exception(
                "gitea_restore_failed | job_id=%s duration_seconds=%.3f",
                context.job_id,
                time.monotonic() - started_at,
                extra={"job_id": context.job_id},
            )
            raise
        finally:
            container_lock.release()
        _LOGGER.info(
            "gitea_restore_success | job_id=%s artifact_path=%s duration_seconds=%.3f",
            context.job_id,
            result["artifact_path"],
            time.monotonic() - started_at,
            extra={"job_id": context.job_id},
        )
        return result

    async def _restore_transaction(self, context: RestoreContext) -> Dict[str, Any]:
        config = context.config or {}
        if not await self.validate_config(config):
            raise ValueError("Invalid Gitea restore configuration")
        if config.get("allow_service_stop") is not True:
            raise ValueError("Gitea restore requires allow_service_stop=true for an isolated drill")

        artifact_path = Path(context.artifact_path)
        if not artifact_path.is_file() or artifact_path.is_symlink():
            raise FileNotFoundError(f"Gitea artifact was not found: {artifact_path}")
        self._validate_dump(artifact_path)
        restored_layout = self._dump_content_layout(artifact_path)

        container_name = str(config["container_name"])
        timeout_seconds = int(config.get("timeout_seconds", 600))
        helper_id: str | None = None
        destination_running = True
        safe_to_restart = True
        mutation_started = False

        with tempfile.TemporaryDirectory(prefix="gitea-restore-") as staging_dir:
            rollback_path = Path(staging_dir) / "gitea-data-before.tar"
            async with self._docker_client() as client:
                details = await self._inspect_container(client, container_name)
                self._validate_container(details)
                self._validate_restore_destination(details)
                await self._validate_sqlite_configuration(client, container_name)
                try:
                    destination_running = False
                    await self._stop_container(client, container_name, timeout_seconds)
                    await self._confirm_stopped(client, container_name)
                    await self._capture_data_archive(
                        client,
                        container_name,
                        rollback_path,
                        timeout_seconds,
                    )
                    helper_id = await self._create_restore_helper(client, container_name)
                    await self._start_container(client, helper_id)
                    await self._upload_file(
                        client,
                        helper_id,
                        artifact_path,
                        "gitea-dump.zip",
                        timeout_seconds,
                    )
                    mutation_started = True
                    await self._apply_restore(
                        client,
                        helper_id,
                        timeout_seconds,
                        restored_layout,
                    )
                    destination_running = True
                    await self._start_container(client, container_name)
                    await self._wait_for_readiness(client, container_name)
                except _HelperStateUnconfirmed as restore_error:
                    safe_to_restart = False
                    raise RuntimeError(
                        "Gitea restore command termination could not be confirmed; the "
                        "destination was left stopped"
                    ) from restore_error
                except (Exception, asyncio.CancelledError) as restore_error:
                    if mutation_started:
                        try:
                            if destination_running:
                                await self._stop_container(
                                    client,
                                    container_name,
                                    timeout_seconds,
                                )
                                destination_running = False
                                await self._confirm_stopped(client, container_name)
                            if isinstance(restore_error, _HelperExecutionTerminated):
                                helper_id = await self._create_restore_helper(
                                    client,
                                    container_name,
                                )
                                await self._start_container(client, helper_id)
                            if helper_id is None:
                                raise RuntimeError("Gitea restore helper was unavailable")
                            await self._upload_file(
                                client,
                                helper_id,
                                rollback_path,
                                "gitea-data-before.tar",
                                timeout_seconds,
                            )
                            await self._apply_rollback(
                                client,
                                helper_id,
                                timeout_seconds,
                            )
                            destination_running = True
                            await self._start_container(client, container_name)
                            await self._wait_for_readiness(client, container_name)
                        except (Exception, asyncio.CancelledError) as rollback_error:
                            safe_to_restart = False
                            if destination_running:
                                try:
                                    await self._stop_container(
                                        client,
                                        container_name,
                                        timeout_seconds,
                                    )
                                except Exception:
                                    pass
                                destination_running = False
                            if isinstance(rollback_error, asyncio.CancelledError):
                                raise
                            raise RuntimeError(
                                "Gitea restore failed and rollback also failed; the "
                                "destination was left stopped"
                            ) from rollback_error
                        if isinstance(restore_error, asyncio.CancelledError):
                            raise
                        if isinstance(restore_error.__cause__, asyncio.CancelledError):
                            raise restore_error.__cause__
                        raise RuntimeError(
                            "Gitea restore failed; previous destination data was restored "
                            "and readiness was verified"
                        ) from restore_error
                    raise
                finally:
                    cleanup_error: Exception | None = None
                    if helper_id is not None:
                        try:
                            await self._remove_helper(client, helper_id, strict=True)
                        except Exception as exc:
                            cleanup_error = exc
                    if not destination_running and safe_to_restart:
                        destination_running = True
                        await self._start_container(client, container_name)
                        await self._wait_for_readiness(client, container_name)
                    if cleanup_error is not None:
                        raise RuntimeError(
                            "Gitea restore could not confirm helper cleanup"
                        ) from cleanup_error

        return {
            "status": "success",
            "artifact_path": str(artifact_path),
            "artifact_bytes": artifact_path.stat().st_size,
            "message": "Gitea restore completed and destination health was verified",
        }

    async def get_status(self, context: BackupContext) -> Dict[str, Any]:
        """Return healthy status after running the non-destructive target test."""
        await self.test(context.config)
        return {"status": "ok"}

    def _acquire_container_lock(self, config: Dict[str, Any]) -> threading.Lock:
        container_name = config.get("container_name") if isinstance(config, dict) else None
        if not isinstance(container_name, str) or not container_name:
            container_name = "<invalid>"
        with _CONTAINER_LOCKS_GUARD:
            operation_lock = _CONTAINER_LOCKS.setdefault(container_name, threading.Lock())
        if not operation_lock.acquire(blocking=False):
            raise RuntimeError(
                f"Gitea container '{container_name}' already has a backup or restore in progress"
            )
        return operation_lock

    def _docker_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=_DOCKER_SOCKET_PATH),
            base_url="http://docker",
            timeout=httpx.Timeout(30.0, connect=10.0),
        )

    async def _inspect_container(
        self, client: httpx.AsyncClient, container_name: str
    ) -> Dict[str, Any]:
        response = await client.get(f"/containers/{container_name}/json")
        if response.status_code == 404:
            raise FileNotFoundError(f"Gitea container '{container_name}' was not found")
        if response.status_code // 100 != 2:
            raise RuntimeError(
                f"Docker failed to inspect the Gitea container with status "
                f"{response.status_code}"
            )
        details = response.json()
        if not isinstance(details, dict):
            raise RuntimeError("Docker returned invalid Gitea container details")
        return details

    def _validate_container(self, details: Dict[str, Any]) -> None:
        container_config = details.get("Config")
        if not isinstance(container_config, dict):
            raise RuntimeError("Gitea container is missing Docker configuration")
        if container_config.get("Image") != _EXPECTED_IMAGE:
            raise RuntimeError(f"Gitea container must use {_EXPECTED_IMAGE}")

        mounts = details.get("Mounts")
        if not isinstance(mounts, list) or not any(
            isinstance(mount, dict)
            and mount.get("Destination") == "/data"
            and mount.get("RW") is True
            for mount in mounts
        ):
            raise RuntimeError("Gitea container must have an exact writable /data mount")

        state = details.get("State")
        if not isinstance(state, dict) or state.get("Running") is not True:
            raise RuntimeError("Gitea container is not running")
        health = state.get("Health")
        if not isinstance(health, dict) or health.get("Status") != "healthy":
            raise RuntimeError("Gitea container is not healthy")

    def _validate_restore_destination(self, details: Dict[str, Any]) -> None:
        container_config = details.get("Config")
        labels = container_config.get("Labels") if isinstance(container_config, dict) else None
        if not isinstance(labels, dict) or labels.get(_RESTORE_DESTINATION_LABEL) != "true":
            raise RuntimeError(
                "Gitea automatic restore is allowed only for a container explicitly "
                f"labeled {_RESTORE_DESTINATION_LABEL}=true"
            )

    async def _validate_sqlite_configuration(
        self, client: httpx.AsyncClient, container_name: str
    ) -> None:
        response = await client.get(
            f"/containers/{container_name}/archive",
            params={"path": "/data/gitea/conf/app.ini"},
        )
        if response.status_code == 404:
            raise FileNotFoundError("Gitea app.ini was not found under /data")
        if response.status_code // 100 != 2:
            raise RuntimeError(
                f"Docker failed to read the Gitea configuration with status "
                f"{response.status_code}"
            )
        if len(response.content) > 1024 * 1024:
            raise RuntimeError("Gitea configuration archive is unexpectedly large")
        try:
            with tarfile.open(fileobj=io.BytesIO(response.content), mode="r:") as archive:
                members = archive.getmembers()
                if len(members) != 1 or not members[0].isfile():
                    raise RuntimeError("Docker returned an invalid Gitea configuration archive")
                extracted = archive.extractfile(members[0])
                if extracted is None:
                    raise RuntimeError("Docker returned an unreadable Gitea configuration")
                contents = extracted.read().decode("utf-8")
        except (tarfile.TarError, UnicodeDecodeError) as exc:
            raise RuntimeError("Docker returned an invalid Gitea configuration archive") from exc

        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read_string(f"[DEFAULT]\n{contents}")
            database_type = parser.get("database", "DB_TYPE")
        except (configparser.Error, KeyError) as exc:
            raise RuntimeError("Gitea app.ini is missing its database type") from exc
        if database_type.strip().lower() != "sqlite3":
            raise RuntimeError("Gitea container must use the SQLite database backend")

    async def _run_health_check(self, client: httpx.AsyncClient, container_name: str) -> None:
        create_response = await client.post(
            f"/containers/{container_name}/exec",
            json={
                "AttachStdout": False,
                "AttachStderr": False,
                "Cmd": _HEALTH_COMMAND,
            },
        )
        if create_response.status_code != 201:
            raise RuntimeError(
                f"Docker failed to create the Gitea health check with status "
                f"{create_response.status_code}"
            )
        payload = create_response.json()
        exec_id = payload.get("Id") if isinstance(payload, dict) else None
        if not isinstance(exec_id, str) or not exec_id:
            raise RuntimeError("Docker did not return a Gitea health-check ID")

        start_task = asyncio.create_task(
            client.post(
                f"/exec/{exec_id}/start",
                json={"Detach": False, "Tty": False},
                timeout=15.0,
            )
        )
        try:
            start_response = await asyncio.shield(start_task)
        except asyncio.CancelledError:
            await asyncio.shield(start_task)
            await self._confirm_health_exec_stopped(client, exec_id)
            raise
        except httpx.RequestError:
            await self._confirm_health_exec_stopped(client, exec_id)
            raise
        if start_response.status_code // 100 != 2:
            raise RuntimeError(
                f"Docker failed to run the Gitea health check with status "
                f"{start_response.status_code}"
            )
        await self._confirm_health_exec_stopped(client, exec_id)

    async def _confirm_health_exec_stopped(
        self,
        client: httpx.AsyncClient,
        exec_id: str,
    ) -> None:
        deadline = asyncio.get_running_loop().time() + 15
        last_error: BaseException | None = None
        while asyncio.get_running_loop().time() < deadline:
            try:
                response = await client.get(f"/exec/{exec_id}/json")
                if response.status_code // 100 == 2:
                    payload = response.json()
                    if isinstance(payload, dict) and payload.get("Running") is False:
                        if payload.get("ExitCode") != 0:
                            raise RuntimeError("Gitea health endpoint check failed")
                        return
                else:
                    last_error = RuntimeError(
                        f"Docker failed to inspect the Gitea health check with status "
                        f"{response.status_code}"
                    )
            except httpx.RequestError as exc:
                last_error = exc
            await asyncio.sleep(0.25)
        raise RuntimeError(
            "Docker did not confirm that the Gitea health check stopped"
        ) from last_error

    async def _stop_container(
        self,
        client: httpx.AsyncClient,
        container_name: str,
        timeout_seconds: int,
    ) -> None:
        response = await client.post(
            f"/containers/{container_name}/stop",
            params={"t": min(timeout_seconds, 60)},
            timeout=float(min(timeout_seconds, 70)),
        )
        if response.status_code not in {204, 304}:
            raise RuntimeError(f"Docker failed to stop Gitea with status {response.status_code}")

    async def _confirm_stopped(self, client: httpx.AsyncClient, container_name: str) -> None:
        details = await self._inspect_container(client, container_name)
        state = details.get("State")
        if not isinstance(state, dict) or state.get("Running") is not False:
            raise RuntimeError("Docker did not confirm that Gitea stopped")

    async def _create_dump_helper(self, client: httpx.AsyncClient, container_name: str) -> str:
        helper_name = f"homelab-backup-gitea-{uuid.uuid4().hex}"
        response = await client.post(
            "/containers/create",
            params={"name": helper_name},
            json={
                "Image": _EXPECTED_IMAGE,
                "User": "1000:1000",
                "WorkingDir": "/tmp",
                "Entrypoint": ["/usr/local/bin/gitea"],
                "Cmd": [
                    "--config",
                    "/data/gitea/conf/app.ini",
                    "dump",
                    "--file",
                    _DUMP_PATH,
                    "--tempdir",
                    "/tmp",
                    "--skip-log",
                ],
                "NetworkDisabled": True,
                "Volumes": {"/tmp": {}},
                "HostConfig": {
                    "VolumesFrom": [f"{container_name}:ro"],
                    "NetworkMode": "none",
                    "ReadonlyRootfs": True,
                    "CapDrop": ["ALL"],
                    "SecurityOpt": ["no-new-privileges:true"],
                    "PidsLimit": 256,
                },
            },
        )
        if response.status_code != 201:
            raise RuntimeError(
                f"Docker failed to create the Gitea dump helper with status "
                f"{response.status_code}"
            )
        payload = response.json()
        helper_id = payload.get("Id") if isinstance(payload, dict) else None
        if not isinstance(helper_id, str) or not helper_id:
            raise RuntimeError("Docker did not return a Gitea dump helper ID")
        return helper_id

    async def _create_restore_helper(self, client: httpx.AsyncClient, container_name: str) -> str:
        helper_name = f"homelab-backup-gitea-restore-{uuid.uuid4().hex}"
        response = await client.post(
            "/containers/create",
            params={"name": helper_name},
            json={
                "Image": _EXPECTED_IMAGE,
                "User": "0:0",
                "Entrypoint": ["/bin/sleep"],
                "Cmd": ["infinity"],
                "NetworkDisabled": True,
                "Volumes": {"/tmp": {}},
                "HostConfig": {
                    "VolumesFrom": [f"{container_name}:rw"],
                    "NetworkMode": "none",
                    "ReadonlyRootfs": True,
                    "CapDrop": ["ALL"],
                    "CapAdd": [
                        "CHOWN",
                        "DAC_OVERRIDE",
                        "FOWNER",
                        "SETGID",
                        "SETUID",
                    ],
                    "SecurityOpt": ["no-new-privileges:true"],
                    "PidsLimit": 256,
                },
            },
        )
        if response.status_code != 201:
            raise RuntimeError(
                f"Docker failed to create the Gitea restore helper with status "
                f"{response.status_code}"
            )
        payload = response.json()
        helper_id = payload.get("Id") if isinstance(payload, dict) else None
        if not isinstance(helper_id, str) or not helper_id:
            raise RuntimeError("Docker did not return a Gitea restore helper ID")
        return helper_id

    async def _start_container(self, client: httpx.AsyncClient, container_name: str) -> None:
        response = await client.post(f"/containers/{container_name}/start")
        if response.status_code not in {204, 304}:
            raise RuntimeError(
                f"Docker failed to start the Gitea container with status " f"{response.status_code}"
            )

    async def _wait_for_helper(
        self,
        client: httpx.AsyncClient,
        helper_id: str,
        timeout_seconds: int,
    ) -> None:
        response = await client.post(
            f"/containers/{helper_id}/wait",
            params={"condition": "not-running"},
            timeout=float(timeout_seconds),
        )
        if response.status_code // 100 != 2:
            raise RuntimeError(
                f"Docker failed while waiting for the Gitea dump with status "
                f"{response.status_code}"
            )
        payload = response.json()
        exit_code = payload.get("StatusCode") if isinstance(payload, dict) else None
        if exit_code != 0:
            raise RuntimeError(f"Gitea dump helper exited with status {exit_code}")

    async def _download_dump(
        self,
        client: httpx.AsyncClient,
        helper_id: str,
        destination: Path,
        timeout_seconds: int,
    ) -> None:
        raw_archive = destination.with_name(f".{destination.name}.docker.tar")
        try:
            async with asyncio.timeout(timeout_seconds):
                async with client.stream(
                    "GET",
                    f"/containers/{helper_id}/archive",
                    params={"path": _DUMP_PATH},
                ) as response:
                    if response.status_code // 100 != 2:
                        raise RuntimeError(
                            f"Docker failed to download the Gitea dump with status "
                            f"{response.status_code}"
                        )
                    with raw_archive.open("wb") as output:
                        async for chunk in response.aiter_bytes(1024 * 1024):
                            output.write(chunk)

            with tarfile.open(raw_archive, mode="r:") as archive:
                members = archive.getmembers()
                if len(members) != 1 or not members[0].isfile():
                    raise RuntimeError("Docker returned an invalid Gitea dump archive")
                source = archive.extractfile(members[0])
                if source is None:
                    raise RuntimeError("Docker returned an unreadable Gitea dump")
                with destination.open("wb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
        except tarfile.TarError as exc:
            raise RuntimeError("Docker returned an invalid Gitea dump archive") from exc
        finally:
            raw_archive.unlink(missing_ok=True)

    def _validate_dump(
        self,
        artifact_path: Path,
        *,
        required_layout: set[str] | None = None,
    ) -> None:
        try:
            with zipfile.ZipFile(artifact_path) as archive:
                members = archive.infolist()
                if not members:
                    raise RuntimeError("Gitea dump is not a valid ZIP archive")
                if len(members) > _MAX_DUMP_MEMBERS:
                    raise RuntimeError("Gitea dump contains too many members")
                names: set[str] = set()
                uncompressed_bytes = 0
                for member in members:
                    path = PurePosixPath(member.filename)
                    normalized_name = member.filename.rstrip("/")
                    mode_type = (member.external_attr >> 16) & 0o170000
                    if (
                        member.filename.startswith("/")
                        or ".." in path.parts
                        or str(path) != normalized_name
                        or any(ord(character) < 32 for character in member.filename)
                        or mode_type not in {0, 0o040000, 0o100000}
                    ):
                        raise RuntimeError("Gitea dump contains an unsafe member")
                    if normalized_name in names:
                        raise RuntimeError("Gitea dump contains a duplicate member")
                    names.add(normalized_name)
                    uncompressed_bytes += member.file_size
                    if uncompressed_bytes > _MAX_DUMP_UNCOMPRESSED_BYTES:
                        raise RuntimeError("Gitea dump expands beyond its safety limit")
                    if (
                        member.file_size > 1024 * 1024
                        and member.compress_size > 0
                        and member.file_size > member.compress_size * _MAX_COMPRESSION_RATIO
                    ):
                        raise RuntimeError("Gitea dump contains an excessive compression ratio")
                if archive.testzip() is not None:
                    raise RuntimeError("Gitea dump is not a valid ZIP archive")
                if not _REQUIRED_DUMP_MEMBERS.issubset(names):
                    raise RuntimeError("Gitea dump is missing required recovery data")
                for prefix in required_layout or set():
                    if not any(name == prefix or name.startswith(f"{prefix}/") for name in names):
                        raise RuntimeError(f"Gitea dump is missing source content under {prefix}")
                with archive.open("gitea-db.sql") as sql_file:
                    sql_prefix = sql_file.read(1024 * 1024)
                if b"CREATE TABLE" not in sql_prefix.upper():
                    raise RuntimeError("Gitea dump contains an unusable SQL export")
        except (zipfile.BadZipFile, OSError) as exc:
            raise RuntimeError("Gitea dump is not a valid ZIP archive") from exc

    async def _source_content_layout(
        self,
        client: httpx.AsyncClient,
        container_name: str,
    ) -> set[str]:
        required: set[str] = set()
        for source_path, dump_prefix in _SOURCE_LAYOUT.items():
            response = await client.head(
                f"/containers/{container_name}/archive",
                params={"path": source_path},
            )
            if response.status_code == 404:
                continue
            if response.status_code // 100 != 2:
                raise RuntimeError(
                    f"Docker could not inspect Gitea source content with status "
                    f"{response.status_code}"
                )
            required.add(dump_prefix)
        return required

    def _dump_content_layout(self, artifact_path: Path) -> set[str]:
        with zipfile.ZipFile(artifact_path) as archive:
            names = {member.filename.rstrip("/") for member in archive.infolist()}
        return {
            prefix
            for prefix in _SOURCE_LAYOUT.values()
            if any(name == prefix or name.startswith(f"{prefix}/") for name in names)
        }

    async def _capture_data_archive(
        self,
        client: httpx.AsyncClient,
        container_name: str,
        destination: Path,
        timeout_seconds: int,
    ) -> None:
        async with asyncio.timeout(timeout_seconds):
            async with client.stream(
                "GET",
                f"/containers/{container_name}/archive",
                params={"path": "/data"},
            ) as response:
                if response.status_code // 100 != 2:
                    raise RuntimeError(
                        "Docker could not capture the existing Gitea data before restore"
                    )
                with destination.open("wb") as output:
                    async for chunk in response.aiter_bytes(1024 * 1024):
                        output.write(chunk)
        try:
            with tarfile.open(destination, mode="r:") as archive:
                members = archive.getmembers()
                if not members:
                    raise RuntimeError("Docker returned an empty Gitea data archive")
                for member in members:
                    path = PurePosixPath(member.name)
                    if (
                        member.name.startswith("/")
                        or ".." in path.parts
                        or not path.parts
                        or path.parts[0] != "data"
                    ):
                        raise RuntimeError("Docker returned an unsafe Gitea data archive")
        except tarfile.TarError as exc:
            raise RuntimeError("Docker returned an invalid Gitea data archive") from exc

    async def _upload_file(
        self,
        client: httpx.AsyncClient,
        helper_id: str,
        source_path: Path,
        archive_name: str,
        timeout_seconds: int,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="gitea-upload-") as upload_dir:
            docker_archive = Path(upload_dir) / "upload.tar"
            with tarfile.open(docker_archive, mode="w") as archive:
                archive.add(source_path, arcname=archive_name, recursive=False)

            async def chunks() -> AsyncIterator[bytes]:
                with docker_archive.open("rb") as upload:
                    while chunk := upload.read(1024 * 1024):
                        yield chunk

            async with asyncio.timeout(timeout_seconds):
                response = await client.put(
                    f"/containers/{helper_id}/archive",
                    params={"path": "/tmp"},
                    content=chunks(),
                )
            if response.status_code // 100 != 2:
                raise RuntimeError(
                    f"Docker failed to upload Gitea restore data with status "
                    f"{response.status_code}"
                )

    async def _apply_restore(
        self,
        client: httpx.AsyncClient,
        helper_id: str,
        timeout_seconds: int,
        restored_layout: set[str],
    ) -> None:
        verification_commands = [
            'test "$(sqlite3 -safe -batch -noinit /data/gitea/gitea.db '
            '\'PRAGMA quick_check;\')" = "ok"'
        ]
        if "repos" in restored_layout:
            verification_commands.extend(
                [
                    "test -n \"$(find /data/git/repositories -type d -name '*.git' "
                    '-print -quit)"',
                    "find /data/git/repositories -type d -name '*.git' "
                    "-exec git --git-dir='{}' fsck --no-dangling \\;",
                    self._tree_equality_command(
                        "/tmp/restore/repos",
                        "/data/git/repositories",
                    ),
                ]
            )
        for prefix in ("data/attachments", "data/lfs", "data/packages"):
            if prefix in restored_layout:
                destination = f"/data/gitea/{prefix.removeprefix('data/')}"
                verification_commands.append(
                    self._tree_equality_command(
                        f"/tmp/restore/{prefix}",
                        destination,
                    )
                )
        content_verification = "\n".join(verification_commands)
        script = f"""
rm -rf /tmp/restore /tmp/new-data
mkdir -p /tmp/restore /tmp/new-data/gitea/conf /tmp/new-data/git/repositories
unzip -q /tmp/gitea-dump.zip -d /tmp/restore
cp -a /tmp/restore/data/. /tmp/new-data/gitea/
if [ -d /tmp/restore/repos ]; then
  cp -a /tmp/restore/repos/. /tmp/new-data/git/repositories/
fi
cp /tmp/restore/app.ini /tmp/new-data/gitea/conf/app.ini
rm -f /tmp/new-data/gitea/gitea.db /tmp/new-data/gitea/gitea.db-wal /tmp/new-data/gitea/gitea.db-shm
sqlite3 -safe -bail -batch -noinit /tmp/new-data/gitea/gitea.db < /tmp/restore/gitea-db.sql
test "$(sqlite3 -safe -batch -noinit /tmp/new-data/gitea/gitea.db 'PRAGMA quick_check;')" = "ok"
{_CLEAR_DATA_CONTENTS}
cp -a /tmp/new-data/. /data/
chown -R 1000:1000 /data
su-exec git gitea --config /data/gitea/conf/app.ini admin regenerate hooks
{content_verification}
""".strip()
        await self._run_helper_script(client, helper_id, script, timeout_seconds)

    @staticmethod
    def _tree_equality_command(source: str, destination: str) -> str:
        return f"""
test "$(find {source} -type f -exec printf x \\; | wc -c)" = \
     "$(find {destination} -type f -exec printf x \\; | wc -c)"
find {source} -type f -exec sh -ceu '
source_root=$1
destination_root=$2
shift 2
for source_file do
  relative=${{source_file#"$source_root"/}}
  cmp "$source_file" "$destination_root/$relative"
done
' sh {source} {destination} {{}} +
""".strip()

    async def _apply_rollback(
        self,
        client: httpx.AsyncClient,
        helper_id: str,
        timeout_seconds: int,
    ) -> None:
        script = f"""
{_CLEAR_DATA_CONTENTS}
tar -xf /tmp/gitea-data-before.tar -C /
chown -R 1000:1000 /data
test -f /data/gitea/conf/app.ini
test "$(sqlite3 -safe -batch -noinit /data/gitea/gitea.db 'PRAGMA quick_check;')" = "ok"
""".strip()
        await self._run_helper_script(client, helper_id, script, timeout_seconds)

    async def _run_helper_script(
        self,
        client: httpx.AsyncClient,
        helper_id: str,
        script: str,
        timeout_seconds: int,
    ) -> None:
        response = await client.post(
            f"/containers/{helper_id}/exec",
            json={
                "AttachStdout": False,
                "AttachStderr": False,
                "Cmd": [
                    "/usr/bin/timeout",
                    "-s",
                    "KILL",
                    str(timeout_seconds),
                    "/bin/sh",
                    "-ceu",
                    script,
                ],
            },
        )
        if response.status_code != 201:
            raise RuntimeError(
                f"Docker failed to create the Gitea restore command with status "
                f"{response.status_code}"
            )
        payload = response.json()
        exec_id = payload.get("Id") if isinstance(payload, dict) else None
        if not isinstance(exec_id, str) or not exec_id:
            raise RuntimeError("Docker did not return a Gitea restore command ID")
        try:
            started = await client.post(
                f"/exec/{exec_id}/start",
                json={"Detach": True, "Tty": False},
            )
            if started.status_code // 100 != 2:
                await self._terminate_uncertain_helper(
                    client,
                    helper_id,
                    RuntimeError("Docker failed to start the Gitea restore command"),
                )
            deadline = asyncio.get_running_loop().time() + timeout_seconds + 5
            while True:
                inspection = await client.get(f"/exec/{exec_id}/json")
                if inspection.status_code // 100 != 2:
                    await self._terminate_uncertain_helper(
                        client,
                        helper_id,
                        RuntimeError("Docker failed to inspect the Gitea restore command"),
                    )
                state = inspection.json()
                if isinstance(state, dict) and state.get("Running") is False:
                    if state.get("ExitCode") != 0:
                        raise RuntimeError(
                            f"Gitea restore command exited with status " f"{state.get('ExitCode')}"
                        )
                    return
                if asyncio.get_running_loop().time() >= deadline:
                    await self._terminate_uncertain_helper(
                        client,
                        helper_id,
                        RuntimeError("Gitea restore command exceeded its deadline"),
                    )
                await asyncio.sleep(0.25)
        except (httpx.RequestError, asyncio.CancelledError) as exc:
            await self._terminate_uncertain_helper(client, helper_id, exc)

    async def _terminate_uncertain_helper(
        self,
        client: httpx.AsyncClient,
        helper_id: str,
        cause: BaseException,
    ) -> NoReturn:
        try:
            await self._remove_helper(client, helper_id, strict=True)
        except Exception as termination_error:
            raise _HelperStateUnconfirmed(
                "Docker could not confirm Gitea restore helper termination"
            ) from termination_error
        raise _HelperExecutionTerminated(
            "Gitea restore helper was terminated after its command became uncertain"
        ) from cause

    async def _remove_helper(
        self,
        client: httpx.AsyncClient,
        helper_id: str,
        *,
        strict: bool = False,
    ) -> None:
        try:
            response = await client.delete(
                f"/containers/{helper_id}",
                params={"force": "true", "v": "true"},
            )
        except httpx.RequestError as exc:
            _LOGGER.warning("gitea_dump_helper_cleanup_failed")
            if strict:
                raise RuntimeError("Docker could not remove the Gitea helper") from exc
            return
        if response.status_code not in {204, 404}:
            _LOGGER.warning("gitea_dump_helper_cleanup_failed")
            if strict:
                raise RuntimeError(
                    f"Docker failed to remove the Gitea helper with status "
                    f"{response.status_code}"
                )

    async def _wait_for_readiness(self, client: httpx.AsyncClient, container_name: str) -> None:
        for _ in range(240):
            details = await self._inspect_container(client, container_name)
            state = details.get("State")
            if isinstance(state, dict) and state.get("Running") is True:
                health = state.get("Health")
                if isinstance(health, dict) and health.get("Status") == "healthy":
                    await self._run_health_check(client, container_name)
                    return
                if isinstance(health, dict) and health.get("Status") == "unhealthy":
                    raise RuntimeError("Gitea became unhealthy after restart")
            await asyncio.sleep(0.25)
        raise RuntimeError("Gitea did not become ready after restart")
