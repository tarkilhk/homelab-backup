from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import multiprocessing
import os
import re
import shutil
import sqlite3
import stat
import tarfile
import tempfile
import threading
import time
import uuid
import zlib
from dataclasses import dataclass
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from pathlib import Path, PurePosixPath
from typing import Any, AsyncIterator, Dict, Tuple

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.core.plugins.artifacts import create_backup_artifact
from app.core.plugins.base import BackupContext, BackupPlugin, RestoreContext

BACKUP_BASE_PATH = "/backups"
DEFAULT_DATA_PATH = "/data"
DOCKER_SOCKET_PATH = "/var/run/docker.sock"
EXPECTED_VERSION = "1.37.1"
EXPECTED_IMAGE_DIGEST = "sha256:e9efdf001bf0d68c21f2cbfb8e1d9b5961a7ca9c85e0a7e58bf51a13b997d744"
EXPECTED_IMAGE = f"vaultwarden/server@{EXPECTED_IMAGE_DIGEST}"
EXPECTED_SOURCE_REVISION = "2629bcbe1380c894e3a7f52cafcac3988edb8fbb"
STORAGE_OVERRIDE_KEYS = {
    "ATTACHMENTS_FOLDER",
    "DATABASE_URL",
    "DATA_FOLDER",
    "RSA_KEY_FILENAME",
    "SENDS_FOLDER",
}
CONTAINER_COMMAND_TIMEOUT_SECONDS = 120.0
CONTAINER_COMMAND_POLL_SECONDS = 0.25
BACKUP_TIMEOUT_SECONDS = 300.0
MAX_ARTIFACT_BYTES = 8 * 1024**3
MAX_MEMBER_BYTES = 4 * 1024**3
MAX_MEMBER_COUNT = 1_000_000
MAX_PATH_DEPTH = 4
MAX_EXPANSION_RATIO = 1000
EXPECTED_MIGRATION = "20260505120000"
ISOLATED_RESTORE_ENV = "HOMELAB_BACKUP_ALLOW_ISOLATED_RESTORE"
ISOLATED_RESTORE_CONTAINERS_ENV = "HOMELAB_BACKUP_ISOLATED_RESTORE_ALLOWED_CONTAINERS"
RESTORE_DESTINATION_LABEL = "asia.hollinger.homelab-backup.restore-destination"
REQUIRED_TABLES = {
    "__diesel_schema_migrations",
    "attachments",
    "ciphers",
    "devices",
    "organizations",
    "sends",
    "users",
}
SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]+$")
ARTIFACT_COMPONENTS = (
    "db.sqlite3",
    "config.json",
    "rsa_key.pem",
    "attachments",
    "sends",
)
_LOGGER = logging.getLogger(__name__)
_CONTAINER_LOCKS: dict[str, threading.Lock] = {}
_CONTAINER_LOCKS_GUARD = threading.Lock()


@dataclass(frozen=True)
class _ArtifactEvidence:
    migration: str
    attachment_count: int
    file_send_count: int
    rsa_public_key_sha256: str
    state_sha256: str


class VaultWardenPlugin(BackupPlugin):
    """Back up and restore the exact Vaultwarden 1.37.1 default-data boundary.

    Backups briefly stop the source, combine its native SQLite snapshot with
    attachments, Sends, RSA material, and optional configuration, then restart
    and prove readiness before publication. Restores are restricted to fresh,
    labeled, explicitly allowlisted local containers and retain rollback state
    until exact restored-state and readiness checks pass.
    """

    restore_capability = "automatic"

    def __init__(self, name: str, version: str = "0.1.0") -> None:
        """Initialize immutable Vaultwarden plugin metadata."""
        super().__init__(name=name, version=version)

    async def validate_config(self, config: Dict[str, Any]) -> bool:
        """Return whether config matches the clean exact-container contract."""
        if not isinstance(config, dict):
            return False
        if set(config) != {"container_name", "allow_service_stop"}:
            return False
        container = config.get("container_name")
        if (
            not isinstance(container, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", container) is None
        ):
            return False
        if config.get("allow_service_stop") is not True:
            return False
        return True

    async def test(self, config: Dict[str, Any]) -> bool:
        """Prove the exact image, default storage layout, and live database health."""
        if not await self.validate_config(config):
            raise ValueError(
                "Invalid Vaultwarden configuration: container_name and "
                "allow_service_stop=true are required"
            )
        container = str(config["container_name"])
        try:
            async with self._docker_client() as client:
                await self._exact_preflight(client, container)
        except (ValueError, FileNotFoundError, RuntimeError):
            raise
        except httpx.RequestError as exc:
            raise ConnectionError("Failed to connect to the local Docker Engine") from exc
        return True

    async def backup(self, context: BackupContext) -> Dict[str, Any]:
        """Create one coherent artifact while Vaultwarden is briefly stopped."""
        started_at = time.monotonic()
        _LOGGER.info(
            "vaultwarden_backup_start | job_id=%s target_id=%s",
            context.job_id,
            context.target_id,
            extra={"job_id": context.job_id, "target_id": context.target_id},
        )
        try:
            result = await self._backup_transaction(context)
        except asyncio.CancelledError:
            _LOGGER.warning(
                "vaultwarden_backup_cancelled | job_id=%s duration_seconds=%.3f",
                context.job_id,
                time.monotonic() - started_at,
                extra={"job_id": context.job_id, "target_id": context.target_id},
            )
            raise
        except Exception:
            _LOGGER.exception(
                "vaultwarden_backup_failed | job_id=%s duration_seconds=%.3f",
                context.job_id,
                time.monotonic() - started_at,
                extra={"job_id": context.job_id, "target_id": context.target_id},
            )
            raise
        _LOGGER.info(
            "vaultwarden_backup_success | job_id=%s duration_seconds=%.3f",
            context.job_id,
            time.monotonic() - started_at,
            extra={"job_id": context.job_id, "target_id": context.target_id},
        )
        return result

    async def _backup_transaction(self, context: BackupContext) -> Dict[str, Any]:
        config = context.config or {}
        if not await self.validate_config(config):
            raise ValueError("Invalid Vaultwarden backup configuration")
        container = str(config["container_name"])
        helper_id: str | None = None
        source_stopped = False
        container_lock: threading.Lock | None = None

        async with self._docker_client() as client:
            initial = await self._exact_preflight(client, container)
            container_id = self._validate_exact_container(initial)
            container_lock = self._acquire_container_lock(container_id)
            try:
                repeated = await self._exact_preflight(client, container)
                if self._validate_exact_container(repeated) != container_id:
                    raise RuntimeError("Vaultwarden container identity changed before backup")
                await self._stop_container(client, container)
                source_stopped = True
                await self._confirm_stopped(client, container, container_id)
                helper_id = await self._create_backup_helper(client, container)
                await self._start_container(client, helper_id)
                await self._wait_for_backup_helper(client, helper_id)

                with tempfile.TemporaryDirectory(prefix="vaultwarden-backup-") as directory:
                    staging = Path(directory)
                    await self._fetch_archive(
                        client,
                        helper_id,
                        "/tmp/db.sqlite3",
                        directory,
                        required=True,
                    )
                    await self._fetch_archive(
                        client,
                        helper_id,
                        "/tmp/generated-name",
                        directory,
                        required=True,
                    )
                    generated_name = (
                        (staging / "generated-name").read_text(encoding="utf-8").strip()
                    )
                    if re.fullmatch(r"db_\d{8}_\d{6}\.sqlite3", generated_name) is None:
                        raise RuntimeError("Vaultwarden native snapshot attribution is invalid")
                    for component in ("attachments", "sends", "config.json"):
                        await self._fetch_archive(
                            client,
                            helper_id,
                            f"{DEFAULT_DATA_PATH}/{component}",
                            directory,
                            required=False,
                        )
                    await self._fetch_archive(
                        client,
                        helper_id,
                        f"{DEFAULT_DATA_PATH}/rsa_key.pem",
                        directory,
                        required=True,
                    )

                    with create_backup_artifact(
                        self,
                        context,
                        prefix="vaultwarden-backup",
                        suffix=".tar.gz",
                        backup_root=BACKUP_BASE_PATH,
                    ) as artifact:
                        evidence = await self._create_and_validate_artifact(
                            staging, artifact.temporary_path
                        )
                        artifact.sidecar_metadata.update(
                            {
                                "application_version": EXPECTED_VERSION,
                                "image_digest": EXPECTED_IMAGE_DIGEST,
                                "source_revision": EXPECTED_SOURCE_REVISION,
                                "source_container_id": container_id,
                                "database_migration": evidence.migration,
                                "attachment_count": evidence.attachment_count,
                                "file_send_count": evidence.file_send_count,
                                "validation": "strict-v2",
                            }
                        )
                        await self._remove_helper(client, helper_id, strict=True)
                        helper_id = None
                        await self._start_container(client, container)
                        await self._wait_for_exact_preflight(client, container)
                        source_stopped = False
                    artifact_path = str(artifact.final_path)
            finally:
                try:
                    await self._complete_backup_cleanup(
                        client,
                        helper_id=helper_id,
                        container=container,
                        restart_source=source_stopped,
                    )
                finally:
                    if container_lock is not None:
                        container_lock.release()
        return {"artifact_path": artifact_path}

    async def restore(self, context: RestoreContext) -> Dict[str, Any]:
        """Restore one verified artifact to a fresh authorized local container."""
        started_at = time.monotonic()
        _LOGGER.info(
            "vaultwarden_restore_start | job_id=%s destination_target_id=%s",
            context.job_id,
            context.destination_target_id,
            extra={"job_id": context.job_id},
        )
        try:
            result = await self._restore_transaction(context)
        except asyncio.CancelledError:
            _LOGGER.warning(
                "vaultwarden_restore_cancelled | job_id=%s duration_seconds=%.3f",
                context.job_id,
                time.monotonic() - started_at,
                extra={"job_id": context.job_id},
            )
            raise
        except Exception:
            _LOGGER.exception(
                "vaultwarden_restore_failed | job_id=%s duration_seconds=%.3f",
                context.job_id,
                time.monotonic() - started_at,
                extra={"job_id": context.job_id},
            )
            raise
        _LOGGER.info(
            "vaultwarden_restore_success | job_id=%s duration_seconds=%.3f",
            context.job_id,
            time.monotonic() - started_at,
            extra={"job_id": context.job_id},
        )
        return result

    async def _restore_transaction(self, context: RestoreContext) -> Dict[str, Any]:
        cfg = context.config or {}
        if not await self.validate_config(cfg):
            raise ValueError("Invalid Vaultwarden restore configuration")
        container = str(cfg["container_name"])
        metadata = context.metadata or {}
        self._require_restore_authorization(container, metadata)
        source_artifact = Path(context.artifact_path)
        if not source_artifact.is_file() or source_artifact.is_symlink():
            raise FileNotFoundError(f"Artifact not found: {source_artifact}")

        with tempfile.TemporaryDirectory(prefix="vaultwarden-restore-") as directory:
            workspace = Path(directory)
            trusted_artifact = workspace / "verified-artifact.tar.gz"
            artifact_fd, expected_evidence = await self._stage_verified_restore_artifact(
                source_artifact,
                trusted_artifact,
                metadata,
            )
            try:
                async with self._docker_client() as client:
                    initial = await self._exact_preflight(client, container)
                    source_container_id = metadata["artifact_sidecar"]["source_container_id"]
                    self._validate_restore_destination(initial, source_container_id)
                    await self._assert_fresh_destination(client, container)
                    destination_id = self._validate_exact_container(initial)
                    operation_lock = self._acquire_container_lock(destination_id)
                    try:
                        repeated = await self._exact_preflight(client, container)
                        if self._validate_exact_container(repeated) != destination_id:
                            raise RuntimeError(
                                "Vaultwarden destination identity changed before restore"
                            )
                        before_started_at = (repeated.get("State") or {}).get("StartedAt")
                        final = await self._perform_restore_with_rollback(
                            client,
                            container,
                            artifact_fd,
                            expected_evidence,
                            workspace,
                            before_started_at,
                        )
                        after_started_at = (final.get("State") or {}).get("StartedAt")
                        if (
                            not isinstance(before_started_at, str)
                            or not before_started_at
                            or not isinstance(after_started_at, str)
                            or not after_started_at
                            or after_started_at == before_started_at
                        ):
                            raise RuntimeError(
                                "Vaultwarden destination did not start a new ready process"
                            )
                    finally:
                        operation_lock.release()
            finally:
                os.close(artifact_fd)

        return {
            "status": "success",
            "artifact_path": str(source_artifact),
            "artifact_bytes": int(metadata["artifact_bytes"]),
            "message": "Vaultwarden restore completed and destination readiness was verified",
        }

    async def get_status(self, context: BackupContext) -> Dict[str, Any]:
        """Return exact checked Vaultwarden identity and health."""
        await self.test(context.config)
        return {
            "status": "ok",
            "version": EXPECTED_VERSION,
            "image_digest": EXPECTED_IMAGE_DIGEST,
        }

    def _acquire_container_lock(self, container_id: str) -> threading.Lock:
        with _CONTAINER_LOCKS_GUARD:
            operation_lock = _CONTAINER_LOCKS.setdefault(container_id, threading.Lock())
        if not operation_lock.acquire(blocking=False):
            raise RuntimeError("Vaultwarden already has a backup or restore in progress")
        return operation_lock

    def _require_restore_authorization(self, container: str, metadata: Dict[str, Any]) -> None:
        if os.environ.get(ISOLATED_RESTORE_ENV) != "1":
            raise ValueError("Vaultwarden isolated local restore is not authorized")
        raw_allowlist = os.environ.get(ISOLATED_RESTORE_CONTAINERS_ENV, "")
        allowed = {item.strip() for item in raw_allowlist.split(",") if item.strip()}
        if container not in allowed:
            raise ValueError("Vaultwarden destination is not allowlisted for local restore")
        sidecar = metadata.get("artifact_sidecar")
        source_identity = metadata.get("source_database_identity")
        if not isinstance(sidecar, dict) or not isinstance(sidecar.get("source_container_id"), str):
            raise ValueError("Vaultwarden restore source identity is missing")
        if not isinstance(source_identity, dict) or not isinstance(
            source_identity.get("container_name"), str
        ):
            raise ValueError("Vaultwarden restore source target identity is missing")

    async def _stage_verified_restore_artifact(
        self,
        source: Path,
        destination: Path,
        metadata: Dict[str, Any],
    ) -> tuple[int, _ArtifactEvidence]:
        expected_size = metadata.get("artifact_bytes")
        expected_digest = metadata.get("artifact_sha256")
        if (
            isinstance(expected_size, bool)
            or not isinstance(expected_size, int)
            or expected_size <= 0
            or not isinstance(expected_digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", expected_digest) is None
        ):
            raise ValueError("Vaultwarden staged artifact identity is missing")
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe(duplex=False)
        process = context.Process(
            target=_restore_validation_worker_entry,
            args=(
                child,
                str(source),
                str(destination),
                expected_size,
                expected_digest,
            ),
            name="vaultwarden-restore-validation",
            daemon=True,
        )
        process.start()
        child.close()
        try:
            payload = await self._await_artifact_worker(process, parent)
        finally:
            parent.close()
        evidence = _ArtifactEvidence(**payload)
        trusted_fd = os.open(destination, os.O_RDONLY | os.O_NOFOLLOW)
        trusted_state = os.fstat(trusted_fd)
        if not stat.S_ISREG(trusted_state.st_mode) or trusted_state.st_size != expected_size:
            os.close(trusted_fd)
            raise RuntimeError("Vaultwarden verified staging identity changed")
        return trusted_fd, evidence

    async def _perform_restore_with_rollback(
        self,
        client: httpx.AsyncClient,
        container: str,
        artifact_fd: int,
        expected_evidence: _ArtifactEvidence,
        workspace: Path,
        before_started_at: Any,
    ) -> Dict[str, Any]:
        await self._stop_container(client, container)
        destination_running = False
        details = await self._inspect_container(client, container)
        destination_id = self._validate_stopped_exact_container(details)
        rollback_artifact = await self._capture_container_artifact(
            client,
            container,
            DEFAULT_DATA_PATH,
            workspace / "rollback",
        )
        helper_id: str | None = None
        mutation_started = False
        try:
            helper_id = await self._create_restore_helper(client, container)
            await self._start_container(client, helper_id)
            await self._upload_restore_archive(client, helper_id, artifact_fd)
            mutation_started = True
            await self._apply_restore_archive(client, helper_id, DEFAULT_DATA_PATH)
            restored_evidence = await self._verify_container_state(client, container)
            if restored_evidence != expected_evidence:
                raise RuntimeError("Vaultwarden restored state does not match the artifact")
            await self._remove_helper(client, helper_id, strict=True)
            helper_id = None
            await self._start_container(client, container)
            destination_running = True
            final = await self._wait_for_exact_preflight(client, container)
            if self._validate_exact_container(final) != destination_id:
                raise RuntimeError("Vaultwarden destination identity changed during restore")
            after_started_at = (final.get("State") or {}).get("StartedAt")
            if (
                not isinstance(before_started_at, str)
                or not before_started_at
                or not isinstance(after_started_at, str)
                or not after_started_at
                or after_started_at == before_started_at
            ):
                raise RuntimeError("Vaultwarden destination did not start a new ready process")
            return final
        except (Exception, asyncio.CancelledError) as restore_error:

            async def recover() -> None:
                nonlocal helper_id, destination_running
                if mutation_started:
                    try:
                        if destination_running:
                            await self._stop_container(client, container)
                            destination_running = False
                        if helper_id is None:
                            helper_id = await self._create_restore_helper(client, container)
                            await self._start_container(client, helper_id)
                        await self._upload_restore_archive(client, helper_id, rollback_artifact)
                        await self._apply_restore_archive(client, helper_id, DEFAULT_DATA_PATH)
                        await self._verify_container_database(client, container, DEFAULT_DATA_PATH)
                        await self._remove_helper(client, helper_id, strict=True)
                        helper_id = None
                        await self._start_container(client, container)
                        destination_running = True
                        await self._wait_for_exact_preflight(client, container)
                        await self._assert_fresh_destination(client, container)
                    except BaseException as rollback_error:
                        if destination_running:
                            try:
                                await self._stop_container(client, container)
                            except Exception:
                                pass
                        raise RuntimeError(
                            "Vaultwarden restore and rollback failed; destination was left stopped"
                        ) from rollback_error
                elif not destination_running:
                    await self._start_container(client, container)
                    destination_running = True
                    await self._wait_for_exact_preflight(client, container)

            recovery_task = asyncio.create_task(recover())
            cancellation_seen = isinstance(restore_error, asyncio.CancelledError)
            while not recovery_task.done():
                try:
                    await asyncio.shield(recovery_task)
                except asyncio.CancelledError:
                    cancellation_seen = True
            recovery_task.result()
            if cancellation_seen:
                raise asyncio.CancelledError
            raise RuntimeError(
                "Vaultwarden restore failed; the fresh preimage was restored"
            ) from restore_error
        finally:
            if helper_id is not None:
                cleanup_task = asyncio.create_task(
                    self._remove_helper(client, helper_id, strict=True)
                )
                cancellation_seen = False
                while not cleanup_task.done():
                    try:
                        await asyncio.shield(cleanup_task)
                    except asyncio.CancelledError:
                        cancellation_seen = True
                cleanup_task.result()
                if cancellation_seen:
                    raise asyncio.CancelledError

    def _validate_stopped_exact_container(self, details: Dict[str, Any]) -> str:
        state = details.get("State")
        if not isinstance(state, dict) or state.get("Running") is not False:
            raise RuntimeError("Vaultwarden destination did not stop")
        adjusted = dict(details)
        adjusted["State"] = {
            "Running": True,
            "Health": {"Status": "healthy"},
        }
        return self._validate_exact_container(adjusted)

    async def _create_restore_helper(self, client: httpx.AsyncClient, container: str) -> str:
        response = await client.post(
            "/containers/create",
            params={"name": f"homelab-backup-vaultwarden-restore-{uuid.uuid4().hex}"},
            json={
                "Image": EXPECTED_IMAGE,
                "User": "0:0",
                "Entrypoint": ["/usr/bin/sleep"],
                "Cmd": ["infinity"],
                "NetworkDisabled": True,
                "Volumes": {"/tmp": {}},
                "HostConfig": {
                    "VolumesFrom": [f"{container}:rw"],
                    "NetworkMode": "none",
                    "ReadonlyRootfs": True,
                    "CapDrop": ["ALL"],
                    "SecurityOpt": ["no-new-privileges:true"],
                    "PidsLimit": 128,
                },
            },
        )
        if response.status_code != 201:
            raise RuntimeError("Docker could not create the Vaultwarden restore helper")
        payload = response.json()
        helper_id = payload.get("Id") if isinstance(payload, dict) else None
        if not isinstance(helper_id, str) or not helper_id:
            raise RuntimeError("Docker did not return a Vaultwarden restore helper ID")
        return helper_id

    async def _verify_container_state(
        self, client: httpx.AsyncClient, container: str
    ) -> _ArtifactEvidence:
        with tempfile.TemporaryDirectory(prefix="vaultwarden-restored-state-") as directory:
            for component in ARTIFACT_COMPONENTS:
                await self._fetch_archive(
                    client,
                    container,
                    f"{DEFAULT_DATA_PATH}/{component}",
                    directory,
                    required=component in {"db.sqlite3", "rsa_key.pem"},
                )
            return self._validate_staging(Path(directory))

    def _validate_restore_destination(
        self, details: Dict[str, Any], source_container_id: str
    ) -> None:
        destination_id = self._validate_exact_container(details)
        if destination_id == source_container_id:
            raise ValueError("Vaultwarden source and destination must be distinct")
        labels = (details.get("Config") or {}).get("Labels")
        if not isinstance(labels, dict) or labels.get(RESTORE_DESTINATION_LABEL) != "true":
            raise ValueError("Vaultwarden destination is not labeled for isolated restore")

    async def _assert_fresh_destination(self, client: httpx.AsyncClient, container: str) -> None:
        with tempfile.TemporaryDirectory(prefix="vaultwarden-freshness-") as directory:
            await self._fetch_archive(
                client,
                container,
                f"{DEFAULT_DATA_PATH}/db.sqlite3",
                directory,
                required=True,
            )
            database = Path(directory) / "db.sqlite3"
            self._database_expectations(database)
            self._assert_fresh_database(database)

    def _assert_fresh_database(self, database: Path) -> None:
        try:
            with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
                for table in ("users", "ciphers", "organizations", "attachments", "sends"):
                    row = connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()
                    if row != (0,):
                        raise ValueError("Vaultwarden restore destination is not fresh")
        except sqlite3.Error as exc:
            raise RuntimeError("Vaultwarden destination database is unreadable") from exc

    def _docker_client(self) -> httpx.AsyncClient:
        transport = httpx.AsyncHTTPTransport(uds=DOCKER_SOCKET_PATH)
        return httpx.AsyncClient(
            transport=transport,
            base_url="http://docker",
            timeout=httpx.Timeout(30.0, connect=10.0),
        )

    async def _inspect_container(self, client: httpx.AsyncClient, container: str) -> Dict[str, Any]:
        response = await client.get(f"/containers/{container}/json")
        if response.status_code == 404:
            raise FileNotFoundError("Vaultwarden container was not found")
        if response.status_code // 100 != 2:
            raise RuntimeError(
                f"Docker failed to inspect Vaultwarden with status {response.status_code}"
            )
        details = response.json()
        if not isinstance(details, dict):
            raise RuntimeError("Docker returned invalid Vaultwarden container details")
        return details

    async def _confirm_stopped(
        self,
        client: httpx.AsyncClient,
        container: str,
        expected_id: str,
    ) -> None:
        details = await self._inspect_container(client, container)
        if details.get("Id") != expected_id:
            raise RuntimeError("Vaultwarden container identity changed while stopping")
        state = details.get("State")
        if not isinstance(state, dict) or state.get("Running") is not False:
            raise RuntimeError("Docker did not confirm that Vaultwarden stopped")

    async def _create_backup_helper(self, client: httpx.AsyncClient, container: str) -> str:
        attribution_script = """
set -eu
find /data -maxdepth 1 -type f -name 'db_????????_??????.sqlite3' -printf '%f\n' \
  | LC_ALL=C sort > /tmp/before
/vaultwarden backup
find /data -maxdepth 1 -type f -name 'db_????????_??????.sqlite3' -printf '%f\n' \
  | LC_ALL=C sort > /tmp/after
comm -13 /tmp/before /tmp/after > /tmp/new
test "$(wc -l < /tmp/new)" -eq 1
generated="$(cat /tmp/new)"
case "$generated" in db_????????_??????.sqlite3) ;; *) exit 64 ;; esac
cp -- "/data/$generated" /tmp/db.sqlite3
rm -- "/data/$generated"
test ! -e "/data/$generated"
printf '%s\n' "$generated" > /tmp/generated-name
""".strip()
        response = await client.post(
            "/containers/create",
            params={"name": f"homelab-backup-vaultwarden-{uuid.uuid4().hex}"},
            json={
                "Image": EXPECTED_IMAGE,
                "User": "0:0",
                "WorkingDir": "/",
                "Entrypoint": ["/bin/sh"],
                "Cmd": ["-c", attribution_script],
                "NetworkDisabled": True,
                "Volumes": {"/tmp": {}},
                "HostConfig": {
                    "VolumesFrom": [f"{container}:rw"],
                    "NetworkMode": "none",
                    "ReadonlyRootfs": True,
                    "CapDrop": ["ALL"],
                    "SecurityOpt": ["no-new-privileges:true"],
                    "PidsLimit": 128,
                },
            },
        )
        if response.status_code != 201:
            raise RuntimeError(
                f"Docker failed to create the Vaultwarden backup helper with status "
                f"{response.status_code}"
            )
        payload = response.json()
        helper_id = payload.get("Id") if isinstance(payload, dict) else None
        if not isinstance(helper_id, str) or not helper_id:
            raise RuntimeError("Docker did not return a Vaultwarden backup helper ID")
        return helper_id

    async def _wait_for_backup_helper(self, client: httpx.AsyncClient, helper_id: str) -> None:
        try:
            response = await client.post(
                f"/containers/{helper_id}/wait",
                params={"condition": "not-running"},
                timeout=BACKUP_TIMEOUT_SECONDS,
            )
        except httpx.TimeoutException as exc:
            raise TimeoutError("Vaultwarden backup helper timed out") from exc
        if response.status_code // 100 != 2:
            raise RuntimeError(
                f"Docker failed while waiting for the Vaultwarden backup helper with status "
                f"{response.status_code}"
            )
        payload = response.json()
        exit_code = payload.get("StatusCode") if isinstance(payload, dict) else None
        if exit_code != 0:
            raise RuntimeError(f"Vaultwarden backup helper exited with status {exit_code}")

    async def _remove_helper(
        self,
        client: httpx.AsyncClient,
        helper_id: str,
        *,
        strict: bool,
    ) -> None:
        try:
            response = await client.delete(
                f"/containers/{helper_id}",
                params={"force": "true", "v": "true"},
            )
        except httpx.RequestError:
            if strict:
                raise RuntimeError("Docker could not remove the Vaultwarden helper") from None
            return
        if strict and response.status_code not in {204, 404}:
            raise RuntimeError(
                f"Docker failed to remove the Vaultwarden helper with status "
                f"{response.status_code}"
            )

    async def _complete_backup_cleanup(
        self,
        client: httpx.AsyncClient,
        *,
        helper_id: str | None,
        container: str,
        restart_source: bool,
    ) -> None:
        async def cleanup() -> None:
            cleanup_error: BaseException | None = None
            if helper_id is not None:
                try:
                    await self._remove_helper(client, helper_id, strict=True)
                except BaseException as exc:
                    cleanup_error = exc
            if restart_source:
                try:
                    await self._start_container(client, container)
                    await self._wait_for_exact_preflight(client, container)
                except BaseException as exc:
                    raise RuntimeError(
                        "Vaultwarden backup could not safely restart the source"
                    ) from exc
            if cleanup_error is not None:
                raise RuntimeError(
                    "Vaultwarden backup could not confirm helper cleanup"
                ) from cleanup_error

        cleanup_task = asyncio.create_task(cleanup())
        cancellation_seen = False
        while not cleanup_task.done():
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:
                cancellation_seen = True
        cleanup_task.result()
        if cancellation_seen:
            raise asyncio.CancelledError

    async def _create_and_validate_artifact(
        self, staging: Path, artifact: Path
    ) -> _ArtifactEvidence:
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe(duplex=False)
        process = context.Process(
            target=_artifact_worker_entry,
            args=(child, str(staging), str(artifact)),
            name="vaultwarden-artifact",
            daemon=True,
        )
        process.start()
        child.close()
        try:
            payload = await self._await_artifact_worker(process, parent)
        finally:
            parent.close()
        return _ArtifactEvidence(**payload)

    async def _await_artifact_worker(
        self, process: BaseProcess, connection: Connection
    ) -> Dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + BACKUP_TIMEOUT_SECONDS
        try:
            while True:
                if connection.poll():
                    payload = connection.recv()
                    await asyncio.to_thread(process.join, 5.0)
                    if process.is_alive():
                        await self._stop_artifact_worker(process)
                        raise RuntimeError("Vaultwarden artifact worker did not exit")
                    if (
                        process.exitcode != 0
                        or not isinstance(payload, dict)
                        or payload.get("ok") is not True
                    ):
                        if isinstance(payload, dict) and payload.get("error") == "identity":
                            raise ValueError("Vaultwarden staged artifact identity does not match")
                        raise RuntimeError("Vaultwarden artifact worker failed")
                    evidence = payload.get("evidence")
                    if not isinstance(evidence, dict):
                        raise RuntimeError("Vaultwarden artifact worker returned invalid evidence")
                    return evidence
                if not process.is_alive():
                    await asyncio.to_thread(process.join, 5.0)
                    raise RuntimeError("Vaultwarden artifact worker exited without evidence")
                if asyncio.get_running_loop().time() >= deadline:
                    await self._stop_artifact_worker_before_return(process)
                    raise TimeoutError("Vaultwarden artifact validation timed out")
                await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            await self._stop_artifact_worker_before_return(process)
            raise

    async def _stop_artifact_worker_before_return(self, process: BaseProcess) -> None:
        """Reap a worker before propagating timeout or repeated cancellation."""
        stop_task = asyncio.create_task(self._stop_artifact_worker(process))
        cancellation_seen = False
        while not stop_task.done():
            try:
                await asyncio.shield(stop_task)
            except asyncio.CancelledError:
                cancellation_seen = True
        stop_task.result()
        if cancellation_seen:
            raise asyncio.CancelledError

    async def _stop_artifact_worker(self, process: BaseProcess) -> None:
        if process.is_alive():
            process.terminate()
            await asyncio.to_thread(process.join, 5.0)
        if process.is_alive():
            process.kill()
            await asyncio.to_thread(process.join, 5.0)
        if process.is_alive():
            raise RuntimeError("Vaultwarden artifact worker could not be stopped")

    def _validate_exact_container(self, details: Dict[str, Any]) -> str:
        container_id = details.get("Id")
        if not isinstance(container_id, str) or not container_id:
            raise RuntimeError("Vaultwarden container identity is missing")

        config = details.get("Config")
        if not isinstance(config, dict):
            raise RuntimeError("Vaultwarden container configuration is missing")
        if config.get("Image") != EXPECTED_IMAGE:
            raise RuntimeError("Vaultwarden container image is not the exact approved digest")
        labels = config.get("Labels")
        if not isinstance(labels, dict):
            raise RuntimeError("Vaultwarden image provenance labels are missing")
        if labels.get("org.opencontainers.image.version") != EXPECTED_VERSION:
            raise RuntimeError("Vaultwarden image version is not exact 1.37.1")
        if labels.get("org.opencontainers.image.revision") != EXPECTED_SOURCE_REVISION:
            raise RuntimeError("Vaultwarden image source revision is not approved")

        environment = config.get("Env")
        if not isinstance(environment, list) or not all(
            isinstance(item, str) for item in environment
        ):
            raise RuntimeError("Vaultwarden container environment is malformed")
        configured_keys = {item.split("=", 1)[0] for item in environment}
        if configured_keys & STORAGE_OVERRIDE_KEYS:
            raise RuntimeError("Vaultwarden must use the exact default local storage layout")

        healthcheck = config.get("Healthcheck")
        if not isinstance(healthcheck, dict) or healthcheck.get("Test") != [
            "CMD",
            "/healthcheck.sh",
        ]:
            raise RuntimeError("Vaultwarden image healthcheck is not exact")

        mounts = details.get("Mounts")
        if (
            not isinstance(mounts, list)
            or sum(
                1
                for mount in mounts
                if isinstance(mount, dict)
                and mount.get("Destination") == DEFAULT_DATA_PATH
                and mount.get("RW") is True
            )
            != 1
        ):
            raise RuntimeError("Vaultwarden must have one exact writable /data mount")

        state = details.get("State")
        if not isinstance(state, dict) or state.get("Running") is not True:
            raise RuntimeError("Vaultwarden container is not running")
        health = state.get("Health")
        if not isinstance(health, dict) or health.get("Status") != "healthy":
            raise RuntimeError("Vaultwarden container is not healthy")
        return container_id

    async def _exact_preflight(self, client: httpx.AsyncClient, container: str) -> Dict[str, Any]:
        details = await self._inspect_container(client, container)
        self._validate_exact_container(details)
        database_exists, _ = await self._path_exists(
            client, container, f"{DEFAULT_DATA_PATH}/db.sqlite3"
        )
        if not database_exists:
            raise FileNotFoundError("Vaultwarden db.sqlite3 was not found under /data")
        await self._exec_container_command(client, container, ["/healthcheck.sh"])
        await self._exec_container_command(
            client,
            container,
            [
                "/bin/sh",
                "-c",
                'test "$(curl -fsS '
                'http://127.0.0.1:${ROCKET_PORT:-80}/api/version)" = \'"1.37.1"\'',
            ],
        )
        return details

    async def _wait_for_exact_preflight(
        self, client: httpx.AsyncClient, container: str
    ) -> Dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + CONTAINER_COMMAND_TIMEOUT_SECONDS
        while True:
            try:
                return await self._exact_preflight(client, container)
            except RuntimeError as exc:
                if str(exc) != "Vaultwarden container is not healthy":
                    raise
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError("Vaultwarden container did not become healthy") from None
            await asyncio.sleep(CONTAINER_COMMAND_POLL_SECONDS)

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
        staging_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
        for component in ARTIFACT_COMPONENTS:
            await self._fetch_archive(
                client,
                container,
                f"{data_path}/{component}",
                str(staging_dir),
                required=component == "db.sqlite3",
            )
        database = staging_dir / "db.sqlite3"
        self._database_expectations(database)
        self._assert_fresh_database(database)
        artifact = staging_dir / "rollback.tar.gz"
        self._write_exact_artifact(staging_dir, artifact)
        self._verify_artifact(str(artifact))
        return artifact

    def _database_expectations(self, database_path: Path) -> tuple[str, dict[str, int], int, int]:
        """Validate the native snapshot and derive its authoritative file map."""
        try:
            with sqlite3.connect(f"file:{database_path}?mode=ro", uri=True) as connection:
                quick_check = connection.execute("PRAGMA quick_check").fetchone()
                if quick_check != ("ok",):
                    raise RuntimeError("Vaultwarden SQLite quick_check failed")
                if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise RuntimeError("Vaultwarden SQLite foreign keys are invalid")
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                    if isinstance(row[0], str)
                }
                if not REQUIRED_TABLES.issubset(tables):
                    raise RuntimeError("Vaultwarden SQLite schema is incomplete")
                migration_row = connection.execute(
                    "SELECT version FROM __diesel_schema_migrations "
                    "ORDER BY version DESC LIMIT 1"
                ).fetchone()
                if migration_row != (EXPECTED_MIGRATION,):
                    raise RuntimeError("Vaultwarden SQLite migration is not exact")

                expected_files: dict[str, int] = {}
                attachments = connection.execute(
                    "SELECT id, cipher_uuid, file_size FROM attachments"
                ).fetchall()
                for attachment_id, cipher_uuid, file_size in attachments:
                    if (
                        not isinstance(attachment_id, str)
                        or SAFE_IDENTIFIER.fullmatch(attachment_id) is None
                        or not isinstance(cipher_uuid, str)
                        or SAFE_IDENTIFIER.fullmatch(cipher_uuid) is None
                        or isinstance(file_size, bool)
                        or not isinstance(file_size, int)
                        or file_size < 0
                    ):
                        raise RuntimeError("Vaultwarden attachment metadata is unsafe")
                    path = f"attachments/{cipher_uuid}/{attachment_id}"
                    if path in expected_files:
                        raise RuntimeError("Vaultwarden attachment paths are ambiguous")
                    expected_files[path] = file_size

                file_sends = connection.execute(
                    "SELECT uuid, data FROM sends WHERE atype = 1"
                ).fetchall()
                for send_uuid, raw_data in file_sends:
                    if (
                        not isinstance(send_uuid, str)
                        or SAFE_IDENTIFIER.fullmatch(send_uuid) is None
                        or not isinstance(raw_data, str)
                    ):
                        raise RuntimeError("Vaultwarden Send metadata is unsafe")
                    try:
                        send_data = json.loads(raw_data)
                    except ValueError as exc:
                        raise RuntimeError("Vaultwarden Send metadata is invalid") from exc
                    if not isinstance(send_data, dict):
                        raise RuntimeError("Vaultwarden Send metadata is invalid")
                    file_id = send_data.get("id")
                    file_size = send_data.get("size")
                    if (
                        not isinstance(file_id, str)
                        or SAFE_IDENTIFIER.fullmatch(file_id) is None
                        or isinstance(file_size, bool)
                        or not isinstance(file_size, int)
                        or file_size < 0
                    ):
                        raise RuntimeError("Vaultwarden Send file metadata is unsafe")
                    path = f"sends/{send_uuid}/{file_id}"
                    if path in expected_files:
                        raise RuntimeError("Vaultwarden Send paths are ambiguous")
                    expected_files[path] = file_size
        except sqlite3.Error as exc:
            raise RuntimeError("Vaultwarden generated an unreadable SQLite backup") from exc
        return EXPECTED_MIGRATION, expected_files, len(attachments), len(file_sends)

    def _validate_staging(self, source_dir: Path) -> _ArtifactEvidence:
        database = source_dir / "db.sqlite3"
        rsa_key_path = source_dir / "rsa_key.pem"
        for required in (database, rsa_key_path):
            if not required.is_file() or required.is_symlink():
                raise RuntimeError(f"Vaultwarden artifact missing {required.name}")

        migration, expected_files, attachment_count, file_send_count = self._database_expectations(
            database
        )
        observed_files: dict[str, int] = {}
        for root_name in ("attachments", "sends"):
            root = source_dir / root_name
            if not root.exists():
                continue
            if not root.is_dir() or root.is_symlink():
                raise RuntimeError(f"Vaultwarden {root_name} tree is unsafe")
            for path in root.rglob("*"):
                relative = path.relative_to(source_dir).as_posix()
                if path.is_symlink() or (
                    path.exists() and not path.is_file() and not path.is_dir()
                ):
                    raise RuntimeError(f"Vaultwarden artifact path is unsafe: {relative}")
                if path.is_file():
                    observed_files[relative] = path.stat().st_size
        if observed_files != expected_files:
            raise RuntimeError("Vaultwarden file state does not match its database snapshot")

        config_path = source_dir / "config.json"
        if config_path.exists():
            if not config_path.is_file() or config_path.is_symlink():
                raise RuntimeError("Vaultwarden config.json is unsafe")
            try:
                config = json.loads(config_path.read_text(encoding="utf-8"))
            except (UnicodeDecodeError, ValueError) as exc:
                raise RuntimeError("Vaultwarden config.json is invalid") from exc
            if not isinstance(config, dict):
                raise RuntimeError("Vaultwarden config.json is invalid")

        try:
            private_key = serialization.load_pem_private_key(
                rsa_key_path.read_bytes(), password=None
            )
            if not isinstance(private_key, rsa.RSAPrivateKey):
                raise ValueError("not an RSA key")
            public_der = private_key.public_key().public_bytes(
                serialization.Encoding.DER,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Vaultwarden RSA private key is invalid") from exc
        return _ArtifactEvidence(
            migration=migration,
            attachment_count=attachment_count,
            file_send_count=file_send_count,
            rsa_public_key_sha256=hashlib.sha256(public_der).hexdigest(),
            state_sha256=self._identity_digest(self._file_identities(source_dir)),
        )

    def _file_identities(self, source_dir: Path) -> tuple[tuple[str, int, str], ...]:
        identities: list[tuple[str, int, str]] = []
        for relative in ("db.sqlite3", "config.json", "rsa_key.pem"):
            path = source_dir / relative
            if not path.is_file():
                continue
            identities.append((relative, path.stat().st_size, self._sha256_file(path)))
        for root_name in ("attachments", "sends"):
            root = source_dir / root_name
            if root.is_dir():
                for path in sorted(item for item in root.rglob("*") if item.is_file()):
                    identities.append(
                        (
                            path.relative_to(source_dir).as_posix(),
                            path.stat().st_size,
                            self._sha256_file(path),
                        )
                    )
        return tuple(sorted(identities))

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _identity_digest(identities: tuple[tuple[str, int, str], ...]) -> str:
        payload = json.dumps(identities, ensure_ascii=True, separators=(",", ":")).encode("ascii")
        return hashlib.sha256(payload).hexdigest()

    def _write_exact_artifact(self, source_dir: Path, artifact: Path) -> None:
        evidence = self._validate_staging(source_dir)
        files = [source_dir / "db.sqlite3", source_dir / "rsa_key.pem"]
        config_path = source_dir / "config.json"
        if config_path.is_file():
            files.append(config_path)
        for root_name in ("attachments", "sends"):
            root = source_dir / root_name
            if root.is_dir():
                files.extend(path for path in root.rglob("*") if path.is_file())
        files.sort(key=lambda path: path.relative_to(source_dir).as_posix())

        inventory: list[dict[str, str | int]] = []
        for path in files:
            digest = hashlib.sha256()
            size = 0
            with path.open("rb") as source:
                while chunk := source.read(1024 * 1024):
                    digest.update(chunk)
                    size += len(chunk)
            inventory.append(
                {
                    "path": path.relative_to(source_dir).as_posix(),
                    "bytes": size,
                    "sha256": digest.hexdigest(),
                }
            )
        manifest = {
            "format_version": 2,
            "application": "vaultwarden",
            "application_version": EXPECTED_VERSION,
            "image_digest": EXPECTED_IMAGE_DIGEST,
            "source_revision": EXPECTED_SOURCE_REVISION,
            "database_migration": evidence.migration,
            "components": sorted({path.relative_to(source_dir).parts[0] for path in files}),
            "files": inventory,
            "rsa_public_key_sha256": evidence.rsa_public_key_sha256,
        }
        manifest_bytes = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
        descriptor = os.open(
            artifact,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "wb") as output:
            with tarfile.open(fileobj=output, mode="w:gz") as archive:
                for path in files:
                    relative = path.relative_to(source_dir).as_posix()
                    info = archive.gettarinfo(str(path), arcname=relative)
                    info.mode = 0o600
                    info.uid = 0
                    info.gid = 0
                    info.uname = ""
                    info.gname = ""
                    with path.open("rb") as source:
                        archive.addfile(info, source)
                info = tarfile.TarInfo("backup-manifest.json")
                info.size = len(manifest_bytes)
                info.mode = 0o600
                archive.addfile(info, io.BytesIO(manifest_bytes))

    async def _upload_restore_archive(
        self,
        client: httpx.AsyncClient,
        helper_id: str,
        artifact_source: Path | int,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="vaultwarden-restore-upload-") as upload_dir:
            docker_archive = Path(upload_dir) / "restore-upload.tar"
            with tarfile.open(docker_archive, "w") as archive:
                if isinstance(artifact_source, int):
                    state = os.fstat(artifact_source)
                    if not stat.S_ISREG(state.st_mode):
                        raise RuntimeError("Vaultwarden verified artifact is no longer regular")
                    info = tarfile.TarInfo("restore.tar.gz")
                    info.size = state.st_size
                    info.mode = 0o600
                    os.lseek(artifact_source, 0, os.SEEK_SET)
                    with os.fdopen(os.dup(artifact_source), "rb") as source:
                        archive.addfile(info, source)
                else:
                    archive.add(artifact_source, arcname="restore.tar.gz", recursive=False)

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

    def _verify_artifact(self, artifact_path: str) -> _ArtifactEvidence:
        artifact = Path(artifact_path)
        if not artifact.is_file() or artifact.is_symlink():
            raise RuntimeError("vaultwarden backup did not produce artifact")
        if artifact.stat().st_size <= 0 or artifact.stat().st_size > MAX_ARTIFACT_BYTES:
            raise RuntimeError("Vaultwarden artifact size is invalid")
        self._validate_gzip_envelope(artifact)
        with tempfile.TemporaryDirectory(prefix="vaultwarden-artifact-verify-") as directory:
            destination = Path(directory)
            try:
                with tarfile.open(artifact, "r:gz") as archive:
                    members = archive.getmembers()
                    if len(members) > MAX_MEMBER_COUNT:
                        raise RuntimeError("Vaultwarden artifact has too many members")
                    seen: set[str] = set()
                    seen_casefolded: set[str] = set()
                    uncompressed_bytes = 0
                    for member in members:
                        name = member.name
                        path = PurePosixPath(name)
                        if (
                            not member.isreg()
                            or name.startswith("/")
                            or ".." in path.parts
                            or len(path.parts) > MAX_PATH_DEPTH
                            or name in seen
                            or name.casefold() in seen_casefolded
                            or any(ord(character) < 32 for character in name)
                            or member.mode != 0o600
                            or member.size < 0
                            or member.size > MAX_MEMBER_BYTES
                        ):
                            raise RuntimeError("Vaultwarden artifact contains an unsafe member")
                        seen.add(name)
                        seen_casefolded.add(name.casefold())
                        uncompressed_bytes += member.size
                        if uncompressed_bytes > MAX_ARTIFACT_BYTES:
                            raise RuntimeError("Vaultwarden artifact expands beyond its limit")
                        extracted = archive.extractfile(member)
                        if extracted is None:
                            raise RuntimeError("Vaultwarden artifact member is unreadable")
                        output = destination / name
                        output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                        descriptor = os.open(
                            output,
                            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                            0o600,
                        )
                        written = 0
                        try:
                            while chunk := extracted.read(1024 * 1024):
                                view = memoryview(chunk)
                                while view:
                                    count = os.write(descriptor, view)
                                    written += count
                                    view = view[count:]
                        finally:
                            os.close(descriptor)
                        if written != member.size:
                            raise RuntimeError("Vaultwarden artifact member is truncated")
            except (tarfile.TarError, OSError) as exc:
                raise RuntimeError("Vaultwarden artifact is unreadable") from exc

            manifest_path = destination / "backup-manifest.json"
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (FileNotFoundError, UnicodeDecodeError, ValueError) as exc:
                raise RuntimeError("Vaultwarden artifact manifest is invalid") from exc
            if not isinstance(manifest, dict):
                raise RuntimeError("Vaultwarden artifact manifest is invalid")
            expected_keys = {
                "format_version",
                "application",
                "application_version",
                "image_digest",
                "source_revision",
                "database_migration",
                "components",
                "files",
                "rsa_public_key_sha256",
            }
            if set(manifest) != expected_keys or (
                manifest.get("format_version") != 2
                or manifest.get("application") != "vaultwarden"
                or manifest.get("application_version") != EXPECTED_VERSION
                or manifest.get("image_digest") != EXPECTED_IMAGE_DIGEST
                or manifest.get("source_revision") != EXPECTED_SOURCE_REVISION
                or manifest.get("database_migration") != EXPECTED_MIGRATION
            ):
                raise RuntimeError("Vaultwarden artifact manifest is invalid")
            raw_inventory = manifest.get("files")
            if not isinstance(raw_inventory, list):
                raise RuntimeError("Vaultwarden artifact inventory is invalid")
            inventory: dict[str, tuple[int, str]] = {}
            for entry in raw_inventory:
                if not isinstance(entry, dict) or set(entry) != {"path", "bytes", "sha256"}:
                    raise RuntimeError("Vaultwarden artifact inventory is invalid")
                raw_name = entry.get("path")
                raw_size = entry.get("bytes")
                raw_digest = entry.get("sha256")
                if (
                    not isinstance(raw_name, str)
                    or raw_name in inventory
                    or isinstance(raw_size, bool)
                    or not isinstance(raw_size, int)
                    or raw_size < 0
                    or not isinstance(raw_digest, str)
                    or re.fullmatch(r"[0-9a-f]{64}", raw_digest) is None
                ):
                    raise RuntimeError("Vaultwarden artifact inventory is invalid")
                inventory[raw_name] = (raw_size, raw_digest)
            actual_names = seen - {"backup-manifest.json"}
            if set(inventory) != actual_names:
                raise RuntimeError("Vaultwarden artifact inventory does not match its members")
            for name, (expected_size, expected_digest) in inventory.items():
                content = destination / name
                digest = hashlib.sha256()
                size = 0
                with content.open("rb") as source:
                    while chunk := source.read(1024 * 1024):
                        digest.update(chunk)
                        size += len(chunk)
                if size != expected_size or digest.hexdigest() != expected_digest:
                    raise RuntimeError("Vaultwarden artifact file identity is invalid")
            evidence = self._validate_staging(destination)
            manifest_identities = tuple(
                sorted((name, size, digest) for name, (size, digest) in inventory.items())
            )
            if self._identity_digest(manifest_identities) != evidence.state_sha256:
                raise RuntimeError("Vaultwarden artifact inventory has unknown state")
            if manifest.get("rsa_public_key_sha256") != evidence.rsa_public_key_sha256:
                raise RuntimeError("Vaultwarden artifact RSA identity is invalid")
            components = manifest.get("components")
            expected_components = sorted({name.split("/", 1)[0] for name in actual_names})
            if components != expected_components:
                raise RuntimeError("Vaultwarden artifact components are invalid")
            return evidence

    def _validate_gzip_envelope(self, artifact: Path) -> None:
        decompressor = zlib.decompressobj(wbits=16 + zlib.MAX_WBITS)
        compressed = 0
        expanded = 0
        with artifact.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                compressed += len(chunk)
                try:
                    expanded += len(decompressor.decompress(chunk))
                except zlib.error as exc:
                    raise RuntimeError("Vaultwarden artifact gzip envelope is invalid") from exc
                if expanded > MAX_ARTIFACT_BYTES:
                    raise RuntimeError("Vaultwarden artifact expands beyond its limit")
                if decompressor.unused_data:
                    raise RuntimeError("Vaultwarden artifact has trailing gzip data")
        try:
            expanded += len(decompressor.flush())
        except zlib.error as exc:
            raise RuntimeError("Vaultwarden artifact gzip envelope is invalid") from exc
        if not decompressor.eof:
            raise RuntimeError("Vaultwarden artifact gzip envelope is truncated")
        if compressed > 1024 * 1024 and expanded > compressed * MAX_EXPANSION_RATIO:
            raise RuntimeError("Vaultwarden artifact compression ratio is unsafe")


def _artifact_worker_entry(connection: Connection, staging: str, artifact: str) -> None:
    """Create and validate one private artifact in a killable worker process."""
    try:
        plugin = VaultWardenPlugin("vaultwarden")
        plugin._write_exact_artifact(Path(staging), Path(artifact))
        evidence = plugin._verify_artifact(artifact)
        connection.send(
            {
                "ok": True,
                "evidence": {
                    "migration": evidence.migration,
                    "attachment_count": evidence.attachment_count,
                    "file_send_count": evidence.file_send_count,
                    "rsa_public_key_sha256": evidence.rsa_public_key_sha256,
                    "state_sha256": evidence.state_sha256,
                },
            }
        )
    except BaseException:
        try:
            connection.send({"ok": False})
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        connection.close()


def _restore_validation_worker_entry(
    connection: Connection,
    source: str,
    destination: str,
    expected_size: int,
    expected_digest: str,
) -> None:
    """Copy and validate one staged restore artifact in a killable worker."""
    try:
        source_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            source_state = os.fstat(source_fd)
            if not stat.S_ISREG(source_state.st_mode):
                raise ValueError("restore source is not regular")
            destination_fd = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            digest = hashlib.sha256()
            size = 0
            try:
                while chunk := os.read(source_fd, 1024 * 1024):
                    size += len(chunk)
                    digest.update(chunk)
                    view = memoryview(chunk)
                    while view:
                        written = os.write(destination_fd, view)
                        view = view[written:]
                os.fsync(destination_fd)
            finally:
                os.close(destination_fd)
        finally:
            os.close(source_fd)
        if size != expected_size or digest.hexdigest() != expected_digest:
            connection.send({"ok": False, "error": "identity"})
            return
        evidence = VaultWardenPlugin("vaultwarden")._verify_artifact(destination)
        connection.send(
            {
                "ok": True,
                "evidence": {
                    "migration": evidence.migration,
                    "attachment_count": evidence.attachment_count,
                    "file_send_count": evidence.file_send_count,
                    "rsa_public_key_sha256": evidence.rsa_public_key_sha256,
                    "state_sha256": evidence.state_sha256,
                },
            }
        )
    except BaseException:
        try:
            connection.send({"ok": False})
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        connection.close()
