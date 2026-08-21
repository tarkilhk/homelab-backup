"""Shared, version-aware backup implementation for Servarr applications."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import multiprocessing
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import threading
import time
import zipfile
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib.metadata import version as installed_package_version
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from pathlib import Path
from typing import Any, AsyncIterator, Dict
from urllib.parse import urlsplit
from xml.etree import ElementTree

import httpx

from app.core.plugins.artifacts import create_backup_artifact
from app.core.plugins.base import BackupContext, BackupPlugin, RestoreContext

logger = logging.getLogger(__name__)

_LOCKS_GUARD = threading.Lock()
_BACKUP_LOCKS: dict[str, threading.Lock] = {}
_TRIGGER_SECONDS_GUARD = threading.Lock()
_LAST_TRIGGER_SECONDS: dict[str, int] = {}

_MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
_MAX_ZIP_MEMBERS = 3
_MAX_COMPRESSED_BYTES = 512 * 1024 * 1024
_MAX_UNCOMPRESSED_BYTES = 1024 * 1024 * 1024
_MAX_EXPANSION_RATIO = 200
_MAX_CONFIG_BYTES = 4 * 1024 * 1024
_MAX_INFO_BYTES = 64 * 1024
_MAX_DATABASE_BYTES = 1024 * 1024 * 1024
_MAX_SCHEMA_OBJECTS = 2048
_MAX_SQLITE_VM_STEPS = 5_000_000
_SQLITE_PROGRESS_INTERVAL = 1000
_MAX_RESTORE_CONTENT_ROWS = 1_000_000
_MAX_SEMANTIC_MARKERS = 100_000
_MAX_SEMANTIC_LABEL_BYTES = 4096
_MAX_SEMANTIC_LABEL_TOTAL_BYTES = 4 * 1024 * 1024
_MAX_RESTORE_API_BYTES = 16 * 1024 * 1024
_BACKUP_WORKER_TIMEOUT_SECONDS = 300.0
_RESTORE_WORKER_TIMEOUT_SECONDS = 300.0
_WORKER_STOP_TIMEOUT_SECONDS = 5.0
_ISOLATED_RESTORE_ENV = "HOMELAB_BACKUP_ALLOW_ISOLATED_RESTORE"
_ISOLATED_RESTORE_ORIGINS_ENV = "HOMELAB_BACKUP_ISOLATED_RESTORE_ALLOWED_ORIGINS"


@dataclass(frozen=True)
class _FileEvidence:
    device: int
    inode: int
    size: int
    modified_ns: int
    changed_ns: int


@dataclass(frozen=True)
class _ArchiveLimits:
    archive_bytes: int
    zip_members: int
    compressed_bytes: int
    uncompressed_bytes: int
    expansion_ratio: int
    config_bytes: int
    info_bytes: int
    database_bytes: int


@dataclass(frozen=True)
class _BackupWorkerEvidence:
    plugin: Any
    source: _FileEvidence
    limits: _ArchiveLimits


@dataclass(frozen=True)
class _ArchiveEvidence:
    device: int
    inode: int
    artifact_bytes: int
    sha256: str
    structure: "_RestoreContentEvidence"


@dataclass(frozen=True)
class _RestoreContentEvidence:
    archive_members: tuple[str, ...]
    database_tables: tuple[str, ...]
    resource_counts: tuple[tuple[str, int], ...]
    tag_labels_sha256: str


@dataclass(frozen=True)
class _RestoreWorkerEvidence:
    source: _FileEvidence
    sha256: str
    content: _RestoreContentEvidence
    restored_api_key: str


def _file_evidence(status: os.stat_result) -> _FileEvidence:
    return _FileEvidence(
        device=status.st_dev,
        inode=status.st_ino,
        size=status.st_size,
        modified_ns=status.st_mtime_ns,
        changed_ns=status.st_ctime_ns,
    )


def _same_open_file_content(status: os.stat_result, expected: _FileEvidence) -> bool:
    """Bind an open descriptor while allowing an unlink-only ctime change."""

    return (
        status.st_dev == expected.device
        and status.st_ino == expected.inode
        and status.st_size == expected.size
        and status.st_mtime_ns == expected.modified_ns
    )


def _current_archive_limits() -> _ArchiveLimits:
    return _ArchiveLimits(
        archive_bytes=_MAX_ARCHIVE_BYTES,
        zip_members=_MAX_ZIP_MEMBERS,
        compressed_bytes=_MAX_COMPRESSED_BYTES,
        uncompressed_bytes=_MAX_UNCOMPRESSED_BYTES,
        expansion_ratio=_MAX_EXPANSION_RATIO,
        config_bytes=_MAX_CONFIG_BYTES,
        info_bytes=_MAX_INFO_BYTES,
        database_bytes=_MAX_DATABASE_BYTES,
    )


def _canonical_origin(value: str) -> str:
    parsed = urlsplit(value)
    hostname = parsed.hostname
    if hostname is None:
        raise ValueError("Servarr origin has no hostname")
    port = parsed.port
    if port is None:
        port = 443 if parsed.scheme.lower() == "https" else 80
    normalized_host = hostname.lower()
    if ":" in normalized_host:
        normalized_host = f"[{normalized_host}]"
    return f"{parsed.scheme.lower()}://{normalized_host}:{port}"


def _backup_lock(key: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _BACKUP_LOCKS.setdefault(key, threading.Lock())


async def _reserve_trigger_second(key: str) -> datetime:
    """Reserve and cross a distinct whole-second vendor filename boundary."""

    now = datetime.now(timezone.utc)
    current_second = int(now.timestamp())
    with _TRIGGER_SECONDS_GUARD:
        target_second = max(
            current_second + 1,
            _LAST_TRIGGER_SECONDS.get(key, current_second) + 1,
        )
        _LAST_TRIGGER_SECONDS[key] = target_second
    await asyncio.sleep(max(0.0, target_second - now.timestamp()))
    return datetime.fromtimestamp(target_second, tz=timezone.utc)


@asynccontextmanager
async def _hold_lock(lock: threading.Lock) -> AsyncIterator[None]:
    while not lock.acquire(blocking=False):
        await asyncio.sleep(0.05)
    try:
        yield
    finally:
        lock.release()


def _directory_flags() -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return flags


def _directory_identity(descriptor: int) -> tuple[int, int]:
    status = os.fstat(descriptor)
    if not stat.S_ISDIR(status.st_mode):
        raise RuntimeError("Servarr validation path is not a directory")
    return status.st_dev, status.st_ino


def _open_owned_directory(path: Path, expected: tuple[int, int]) -> int:
    descriptor = os.open(path, _directory_flags())
    if _directory_identity(descriptor) != expected:
        os.close(descriptor)
        raise RuntimeError("Servarr validation directory changed")
    return descriptor


def _clear_directory_fd(descriptor: int) -> None:
    for name in os.listdir(descriptor):
        status = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        if stat.S_ISDIR(status.st_mode):
            child = os.open(name, _directory_flags(), dir_fd=descriptor)
            try:
                _clear_directory_fd(child)
            finally:
                os.close(child)
            os.rmdir(name, dir_fd=descriptor)
        else:
            os.unlink(name, dir_fd=descriptor)


def _remove_owned_directory(
    parent_fd: int,
    owned_fd: int,
    *,
    expected: tuple[int, int],
    name: str,
) -> None:
    _clear_directory_fd(owned_fd)
    try:
        status = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if stat.S_ISDIR(status.st_mode) and (status.st_dev, status.st_ino) == expected:
        os.rmdir(name, dir_fd=parent_fd)


def _create_private_validation_directory(parent: Path) -> tuple[Path, int, tuple[int, int]]:
    path = Path(tempfile.mkdtemp(prefix=".servarr-validation-", dir=parent))
    os.chmod(path, 0o700)
    descriptor = os.open(path, _directory_flags())
    identity = _directory_identity(descriptor)
    if os.fstat(descriptor).st_mode & 0o077:
        os.close(descriptor)
        path.rmdir()
        raise RuntimeError("Servarr validation directory must be private")
    return path, descriptor, identity


def _hash_descriptor(descriptor: int) -> str:
    digest = hashlib.sha256()
    offset = 0
    while True:
        chunk = os.pread(descriptor, 1024 * 1024, offset)
        if not chunk:
            return digest.hexdigest()
        digest.update(chunk)
        offset += len(chunk)


def _semantic_labels_sha256(labels: list[str] | tuple[str, ...]) -> str:
    digest = hashlib.sha256()
    total_bytes = 0
    for label in sorted(labels):
        encoded = label.encode("utf-8")
        total_bytes += len(encoded)
        if (
            len(encoded) > _MAX_SEMANTIC_LABEL_BYTES
            or total_bytes > _MAX_SEMANTIC_LABEL_TOTAL_BYTES
        ):
            raise RuntimeError("Servarr semantic labels exceed their byte limit")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _worker_error_kind(exc: BaseException) -> str:
    if isinstance(exc, FileNotFoundError):
        return "file-not-found"
    if isinstance(exc, ValueError):
        return "value"
    if isinstance(exc, TimeoutError):
        return "timeout"
    return "runtime"


def _send_worker_result(
    connection: Connection,
    result: tuple[str, str, object | None],
) -> None:
    try:
        connection.send(result)
    except (BrokenPipeError, EOFError, OSError):
        pass


def _backup_process_worker(
    directory: Path,
    name: str,
    evidence: _BackupWorkerEvidence,
    artifact_path: Path,
    validation_root: Path,
    validation_identity: tuple[int, int],
    connection: Connection,
) -> None:
    validation_fd: int | None = None
    artifact_fd: int | None = None
    try:
        validation_fd = _open_owned_directory(validation_root, validation_identity)
        source = directory / name
        evidence.plugin._copy_stable_local_backup(
            source,
            artifact_path,
            evidence.source,
        )
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        artifact_fd = os.open(artifact_path, flags)
        structure = evidence.plugin._validate_exact_native_archive(
            Path(f"/proc/self/fd/{artifact_fd}"),
            validation_root=Path(f"/proc/self/fd/{validation_fd}"),
            limits=evidence.limits,
        )
        status = os.fstat(artifact_fd)
        result = _ArchiveEvidence(
            device=status.st_dev,
            inode=status.st_ino,
            artifact_bytes=status.st_size,
            sha256=_hash_descriptor(artifact_fd),
            structure=structure,
        )
        _send_worker_result(connection, ("ok", "", result))
    except BaseException as exc:
        _send_worker_result(connection, (_worker_error_kind(exc), str(exc), None))
        raise SystemExit(1) from None
    finally:
        if artifact_fd is not None:
            os.close(artifact_fd)
        if validation_fd is not None:
            os.close(validation_fd)
        connection.close()


def _start_backup_process(
    directory: Path,
    name: str,
    evidence: _BackupWorkerEvidence,
    artifact_path: Path,
    validation_root: Path,
    validation_identity: tuple[int, int],
) -> tuple[BaseProcess, Connection]:
    context = multiprocessing.get_context("spawn")
    receiving, sending = context.Pipe(duplex=False)
    process = context.Process(
        target=_backup_process_worker,
        args=(
            directory,
            name,
            evidence,
            artifact_path,
            validation_root,
            validation_identity,
            sending,
        ),
        name="servarr-backup",
        daemon=True,
    )
    process.start()
    sending.close()
    return process, receiving


def _restore_process_worker(
    plugin_name: str,
    artifact_path: Path,
    expected_size: int,
    expected_sha256: str,
    validation_root: Path,
    validation_identity: tuple[int, int],
    connection: Connection,
) -> None:
    validation_fd: int | None = None
    artifact_fd: int | None = None
    try:
        from app.core.plugins.loader import get_plugin

        plugin = get_plugin(plugin_name)
        if not isinstance(plugin, ServarrPlugin):
            raise RuntimeError("Servarr restore worker received an incompatible plugin")
        validation_fd = _open_owned_directory(validation_root, validation_identity)
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        artifact_fd = os.open(artifact_path, flags)
        before = os.fstat(artifact_fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size != expected_size:
            raise ValueError(f"{plugin.app_name} restore artifact size is not verified")
        try:
            named = artifact_path.stat(follow_symlinks=False)
        except OSError as exc:
            raise ValueError(
                f"{plugin.app_name} restore artifact changed during verification"
            ) from exc
        if _file_evidence(before) != _file_evidence(named):
            raise ValueError(f"{plugin.app_name} restore artifact changed during verification")
        digest = _hash_descriptor(artifact_fd)
        if digest != expected_sha256:
            raise ValueError(f"{plugin.app_name} restore artifact hash is not verified")
        descriptor_path = Path(f"/proc/self/fd/{artifact_fd}")
        content = plugin._validate_exact_native_archive(
            descriptor_path,
            validation_root=Path(f"/proc/self/fd/{validation_fd}"),
        )
        restored_api_key = plugin._restored_api_key(descriptor_path)
        after = os.fstat(artifact_fd)
        try:
            named_after = artifact_path.stat(follow_symlinks=False)
        except OSError as exc:
            raise ValueError(
                f"{plugin.app_name} restore artifact changed during verification"
            ) from exc
        source = _file_evidence(before)
        if source != _file_evidence(after) or source != _file_evidence(named_after):
            raise ValueError(f"{plugin.app_name} restore artifact changed during verification")
        _send_worker_result(
            connection,
            (
                "ok",
                "",
                _RestoreWorkerEvidence(
                    source=source,
                    sha256=digest,
                    content=content,
                    restored_api_key=restored_api_key,
                ),
            ),
        )
    except BaseException as exc:
        _send_worker_result(connection, (_worker_error_kind(exc), str(exc), None))
        raise SystemExit(1) from None
    finally:
        if artifact_fd is not None:
            os.close(artifact_fd)
        if validation_fd is not None:
            os.close(validation_fd)
        connection.close()


def _start_restore_process(
    plugin_name: str,
    artifact_path: Path,
    expected_size: int,
    expected_sha256: str,
    validation_root: Path,
    validation_identity: tuple[int, int],
) -> tuple[BaseProcess, Connection]:
    context = multiprocessing.get_context("spawn")
    receiving, sending = context.Pipe(duplex=False)
    process = context.Process(
        target=_restore_process_worker,
        args=(
            plugin_name,
            artifact_path,
            expected_size,
            expected_sha256,
            validation_root,
            validation_identity,
            sending,
        ),
        name="servarr-restore",
        daemon=True,
    )
    process.start()
    sending.close()
    return process, receiving


async def _join_worker_process(process: BaseProcess, timeout_seconds: float) -> None:
    await asyncio.to_thread(process.join, timeout_seconds)


async def _stop_worker_process(process: BaseProcess) -> None:
    if not process.is_alive():
        await _join_worker_process(process, _WORKER_STOP_TIMEOUT_SECONDS)
        if process.exitcode is None:
            raise RuntimeError("Servarr backup worker could not be reaped")
        return
    process.terminate()
    await _join_worker_process(process, _WORKER_STOP_TIMEOUT_SECONDS)
    if process.is_alive():
        process.kill()
        await _join_worker_process(process, _WORKER_STOP_TIMEOUT_SECONDS)
    if process.is_alive() or process.exitcode is None:
        raise RuntimeError("Servarr backup worker could not be stopped")


async def _stop_worker_process_before_return(process: BaseProcess) -> None:
    stop_task = asyncio.create_task(_stop_worker_process(process))
    cancellation_seen = False
    while not stop_task.done():
        try:
            await asyncio.shield(stop_task)
        except asyncio.CancelledError:
            cancellation_seen = True
    stop_task.result()
    if cancellation_seen:
        raise asyncio.CancelledError


def _raise_worker_result(
    result: tuple[str, str, object | None] | None,
) -> object | None:
    if result is None:
        raise RuntimeError("Servarr backup worker returned no result")
    kind, message, payload = result
    if kind == "ok":
        return payload
    safe_message = message or "Servarr backup worker failed"
    if kind == "file-not-found":
        raise FileNotFoundError(safe_message)
    if kind == "value":
        raise ValueError(safe_message)
    if kind == "timeout":
        raise TimeoutError(safe_message)
    raise RuntimeError(safe_message)


async def _await_worker(
    process: BaseProcess,
    connection: Connection,
    *,
    timeout_seconds: float,
) -> object | None:
    receive_task = asyncio.create_task(asyncio.to_thread(connection.recv))
    try:
        await _join_worker_process(process, timeout_seconds)
        if process.is_alive():
            await _stop_worker_process_before_return(process)
            raise TimeoutError("Servarr backup worker timed out")
        result: tuple[str, str, object | None] | None = None
        try:
            received = await asyncio.wait_for(
                asyncio.shield(receive_task),
                timeout=_WORKER_STOP_TIMEOUT_SECONDS,
            )
        except (EOFError, OSError, TimeoutError):
            received = None
        if (
            isinstance(received, tuple)
            and len(received) == 3
            and isinstance(received[0], str)
            and isinstance(received[1], str)
        ):
            result = received
        payload = _raise_worker_result(result)
        if process.exitcode != 0:
            raise RuntimeError("Servarr backup worker failed")
        return payload
    except asyncio.CancelledError:
        await _stop_worker_process_before_return(process)
        raise
    except BaseException:
        if process.is_alive():
            await _stop_worker_process_before_return(process)
        raise
    finally:
        connection.close()
        if not receive_task.done():
            receive_task.cancel()
        try:
            await receive_task
        except BaseException:
            pass


class ServarrPlugin(BackupPlugin):
    """Deep module for the common Lidarr/Radarr/Sonarr backup protocol."""

    app_name = "Servarr"
    api_prefix = "/api/v3"
    database_members: tuple[str, ...] = ()
    expected_version: str | None = None
    expected_package_version: str | None = None
    expected_migration: int | None = None
    expected_database_type = "sqlite"
    require_start_time = False
    require_distinct_trigger_second = False
    require_persistence_restart = False
    native_backup_mount: Path | None = None
    required_native_tables: frozenset[str] = frozenset()
    fresh_restore_resource_paths: tuple[str, ...] = ()
    restore_content_tables: tuple[tuple[str, str], ...] = ()
    command_result_required = True
    backup_deadline_seconds = 120.0
    poll_interval_seconds = 1.0
    restore_deadline_seconds = 120.0
    restore_poll_interval_seconds = 2.0
    backup_root = "/backups"
    restore_capability = "automatic"

    def __init__(self, name: str, version: str | None = None) -> None:
        super().__init__(
            name=name,
            version=version or installed_package_version("homelab-backup"),
        )
        self._logger = logger

    async def validate_config(self, config: Dict[str, Any]) -> bool:
        required_keys = {"base_url", "api_key"}
        if self.native_backup_mount is not None:
            required_keys.add("backup_directory")
        if not isinstance(config, dict) or set(config) != required_keys:
            return False
        base_url = config.get("base_url")
        api_key = config.get("api_key")
        if (
            not isinstance(base_url, str)
            or not base_url
            or base_url != base_url.strip()
            or not isinstance(api_key, str)
            or not api_key
            or api_key != api_key.strip()
            or any(
                character.isspace() or ord(character) < 32 or ord(character) == 127
                for character in api_key
            )
        ):
            return False
        try:
            parsed = urlsplit(base_url)
            parsed.port
        except ValueError:
            return False
        valid_origin = (
            parsed.scheme in {"http", "https"}
            and parsed.hostname is not None
            and parsed.username is None
            and parsed.password is None
            and parsed.path == ""
            and not parsed.query
            and not parsed.fragment
        )
        if not valid_origin:
            return False
        if self.native_backup_mount is None:
            return True
        backup_directory = config.get("backup_directory")
        if not isinstance(backup_directory, str) or not backup_directory:
            return False
        path = Path(backup_directory)
        forbidden_roots = (Path("/app"), Path("/backups"), Path("/config"))
        return (
            path.is_absolute()
            and ".." not in path.parts
            and path == self.native_backup_mount
            and path != Path("/")
            and not any(root == path or root in path.parents for root in forbidden_roots)
            and not any(ord(character) < 32 for character in backup_directory)
        )

    def _require_read_only_backup_mount(self, config: Dict[str, Any]) -> Path:
        if self.native_backup_mount is None:
            raise RuntimeError(f"{self.app_name} has no native backup mount contract")
        value = config.get("backup_directory")
        if not isinstance(value, str):
            raise ValueError(f"{self.app_name} config is missing backup_directory")
        directory = Path(value)
        try:
            status = directory.lstat()
            resolved = directory.resolve(strict=True)
        except OSError as exc:
            raise FileNotFoundError(
                f"{self.app_name} native backup directory was not found"
            ) from exc
        if (
            not stat.S_ISDIR(status.st_mode)
            or stat.S_ISLNK(status.st_mode)
            or resolved != directory
        ):
            raise RuntimeError(f"{self.app_name} native backup directory must be a real directory")
        if not os.path.ismount(directory):
            raise RuntimeError(f"{self.app_name} native backup directory must be a dedicated mount")
        try:
            read_only = bool(os.statvfs(directory).f_flag & os.ST_RDONLY)
        except OSError as exc:
            raise RuntimeError(
                f"{self.app_name} native backup mount could not be inspected"
            ) from exc
        if not read_only:
            raise RuntimeError(f"{self.app_name} native backup mount must be read-only")
        return directory

    def _local_backup_path(self, directory: Path, item: dict[str, Any]) -> Path:
        api_path = self._download_path(item)
        candidate = directory / Path(api_path).name
        try:
            status = candidate.lstat()
        except OSError as exc:
            raise FileNotFoundError(f"{self.app_name} native backup file is missing") from exc
        if stat.S_ISLNK(status.st_mode) or not stat.S_ISREG(status.st_mode):
            raise RuntimeError(
                f"{self.app_name} native backup file must be a regular non-link file"
            )
        expected_size = item.get("size")
        if (
            isinstance(expected_size, bool)
            or not isinstance(expected_size, int)
            or status.st_size != expected_size
        ):
            raise RuntimeError(
                f"{self.app_name} native backup file size does not match its API entry"
            )
        return candidate

    def _validate_manual_backup_entries(
        self,
        directory: Path,
        backups: list[dict[str, Any]],
    ) -> None:
        observed_paths: set[str] = set()
        for item in backups:
            if item.get("type") != "manual":
                continue
            api_path = self._download_path(item)
            if api_path in observed_paths:
                raise RuntimeError(f"{self.app_name} manual backup entry collision")
            observed_paths.add(api_path)
            self._local_backup_path(directory, item)

    def _copy_stable_local_backup(
        self,
        source: Path,
        destination: Path,
        expected: _FileEvidence | None = None,
    ) -> None:
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            source_fd = os.open(source, flags)
        except OSError as exc:
            raise RuntimeError(
                f"{self.app_name} native backup file could not be opened safely"
            ) from exc
        try:
            before = os.fstat(source_fd)
            if not stat.S_ISREG(before.st_mode):
                raise RuntimeError(f"{self.app_name} native backup file must be a regular file")
            if expected is not None and _file_evidence(before) != expected:
                raise RuntimeError(f"{self.app_name} native backup file changed before copying")
            archive_limit = (
                _MAX_ARCHIVE_BYTES if expected is None else _current_archive_limits().archive_bytes
            )
            if before.st_size <= 0 or before.st_size > archive_limit:
                raise RuntimeError(f"{self.app_name} native backup file exceeds its size limit")
            destination_fd = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(os.dup(source_fd), "rb") as source_file:
                with os.fdopen(destination_fd, "wb") as destination_file:
                    shutil.copyfileobj(source_file, destination_file, length=1024 * 1024)
                    destination_file.flush()
                    os.fsync(destination_file.fileno())
            after = os.fstat(source_fd)
            try:
                path_status = source.lstat()
            except OSError as exc:
                raise RuntimeError(
                    f"{self.app_name} native backup file disappeared while copying"
                ) from exc
            identity_before = _file_evidence(before)
            identity_after = _file_evidence(after)
            path_identity = _file_evidence(path_status)
            if (
                identity_before != identity_after
                or identity_after != path_identity
                or (expected is not None and identity_after != expected)
            ):
                raise RuntimeError(f"{self.app_name} native backup file changed while copying")
        finally:
            os.close(source_fd)

    def _request_config(self, config: Dict[str, Any]) -> tuple[str, dict[str, str]]:
        base_url = config.get("base_url")
        api_key = config.get("api_key")
        if not isinstance(base_url, str) or not isinstance(api_key, str):
            raise ValueError(f"{self.app_name} config must include base_url and api_key")
        return base_url, {"X-Api-Key": api_key}

    def _validate_status(self, data: object) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise RuntimeError(f"{self.app_name} status response is invalid")
        version = data.get("version")
        if not isinstance(version, str) or not version:
            raise RuntimeError(f"{self.app_name} status response is missing version")
        if self.expected_version is not None:
            if data.get("appName") != self.app_name:
                raise RuntimeError(f"{self.app_name} application identity is incompatible")
            if version != self.expected_version:
                raise RuntimeError(f"{self.app_name} version is incompatible")
            if (
                self.expected_package_version is not None
                and data.get("packageVersion") != self.expected_package_version
            ):
                raise RuntimeError(f"{self.app_name} package version is incompatible")
            if str(data.get("databaseType", "")).lower() != self.expected_database_type:
                raise RuntimeError(f"{self.app_name} database must be SQLite")
            migration = data.get("migrationVersion")
            if (
                isinstance(migration, bool)
                or not isinstance(migration, int)
                or migration != self.expected_migration
            ):
                raise RuntimeError(f"{self.app_name} database migration is incompatible")
        if self.require_start_time:
            start_time = data.get("startTime")
            if not isinstance(start_time, str) or not start_time:
                raise RuntimeError(f"{self.app_name} process start time is invalid")
            try:
                parsed_start_time = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
            except ValueError as exc:
                raise RuntimeError(f"{self.app_name} process start time is invalid") from exc
            if parsed_start_time.tzinfo is None:
                raise RuntimeError(f"{self.app_name} process start time is invalid")
        return data

    async def test(self, config: Dict[str, Any]) -> bool:
        if not await self.validate_config(config):
            raise ValueError(
                "Invalid configuration: base_url, api_key, and backup_directory are required"
                if self.native_backup_mount is not None
                else "Invalid configuration: base_url and api_key are required"
            )
        backup_directory = (
            self._require_read_only_backup_mount(config)
            if self.native_backup_mount is not None
            else None
        )
        base_url, headers = self._request_config(config)
        status_url = f"{base_url}{self.api_prefix}/system/status"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(status_url, headers=headers)
                if response.status_code != 200:
                    raise RuntimeError(
                        f"{self.app_name} API returned status {response.status_code}"
                    )
                try:
                    data = response.json()
                except ValueError as exc:
                    raise RuntimeError(f"{self.app_name} status response is invalid") from exc
                self._validate_status(data)
                backups = await self._list_backups(
                    client,
                    f"{base_url}{self.api_prefix}/system/backup",
                    headers,
                )
                if backup_directory is not None:
                    self._validate_manual_backup_entries(backup_directory, backups)
        except RuntimeError:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            raise ConnectionError(f"Failed to connect to {self.app_name}: {exc}") from exc
        return True

    async def _list_backups(
        self,
        client: httpx.AsyncClient,
        url: str,
        headers: dict[str, str],
    ) -> list[dict[str, Any]]:
        response = await client.get(url, headers=headers)
        if response.status_code != 200:
            raise RuntimeError(
                f"{self.app_name} backup list returned status {response.status_code}"
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise RuntimeError(f"{self.app_name} backup list response is invalid") from exc
        if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
            raise RuntimeError(f"{self.app_name} backup list response is invalid")
        expected_fields = {"id", "name", "path", "type", "size", "time"}
        for item in data:
            backup_id = item.get("id")
            size = item.get("size")
            if (
                set(item) != expected_fields
                or not isinstance(backup_id, int)
                or isinstance(backup_id, bool)
                or not isinstance(size, int)
                or isinstance(size, bool)
                or size < 0
                or any(
                    not isinstance(item.get(field), str) or not item[field].strip()
                    for field in ("name", "path", "type", "time")
                )
                or self._backup_time(item) is None
            ):
                raise RuntimeError(f"{self.app_name} backup list response is invalid")
        return data

    async def _require_exact_status(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        headers: dict[str, str],
    ) -> dict[str, Any]:
        response = await client.get(
            f"{base_url}{self.api_prefix}/system/status",
            headers=headers,
        )
        if response.status_code != 200:
            raise RuntimeError(f"{self.app_name} API returned status {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError(f"{self.app_name} status response is invalid") from exc
        return self._validate_status(payload)

    @staticmethod
    def _backup_time(item: dict[str, Any]) -> datetime | None:
        value = item.get("time")
        if not isinstance(value, str):
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def _new_manual_backups(
        self,
        backups: list[dict[str, Any]],
        known: set[tuple[object, ...]],
        triggered_at: datetime,
    ) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        for item in backups:
            identity = self._backup_identity(item)
            observed_at = self._backup_time(item)
            if (
                item.get("type") == "manual"
                and identity not in known
                and isinstance(item.get("id"), int)
                and not isinstance(item.get("id"), bool)
                and observed_at is not None
                and observed_at >= triggered_at
            ):
                candidates.append(item)
        return candidates

    @staticmethod
    def _backup_identity(item: dict[str, Any]) -> tuple[object, ...]:
        return tuple(item.get(field) for field in ("id", "name", "path", "type", "size", "time"))

    def _download_path(self, item: dict[str, Any]) -> str:
        backup_path = item.get("path")
        backup_name = item.get("name")
        if not isinstance(backup_path, str) or not isinstance(backup_name, str):
            raise RuntimeError(f"{self.app_name} backup entry contained an unsafe path")
        parsed = urlsplit(backup_path)
        if (
            not backup_name
            or backup_name in {".", ".."}
            or "/" in backup_name
            or "\\" in backup_name
            or any(ord(character) < 32 or ord(character) == 127 for character in backup_name)
            or parsed.scheme
            or parsed.netloc
            or parsed.query
            or parsed.fragment
            or parsed.path != f"/backup/manual/{backup_name}"
        ):
            raise RuntimeError(f"{self.app_name} backup entry contained an unsafe path")
        return parsed.path

    def _validate_archive(self, archive_path: Path) -> _RestoreContentEvidence | None:
        if self.native_backup_mount is not None:
            return self._validate_exact_native_archive(archive_path)
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
        return None

    @staticmethod
    def _require_no_trailing_zip_data(archive_path: Path, archive_size: int) -> None:
        tail_size = min(archive_size, 65_557)
        with archive_path.open("rb") as source:
            source.seek(archive_size - tail_size)
            tail = source.read(tail_size)
        marker = tail.rfind(b"PK\x05\x06")
        if marker < 0 or marker + 22 > len(tail):
            raise RuntimeError("Servarr backup ZIP has no valid end record")
        comment_bytes = int.from_bytes(tail[marker + 20 : marker + 22], "little")
        if marker + 22 + comment_bytes != len(tail):
            raise RuntimeError("Servarr backup ZIP contains trailing data")

    def _validate_exact_native_archive(
        self,
        archive_path: Path,
        *,
        validation_root: Path | None = None,
        limits: _ArchiveLimits | None = None,
    ) -> _RestoreContentEvidence:
        active_limits = limits or _current_archive_limits()
        try:
            archive_status = archive_path.stat()
        except OSError as exc:
            raise RuntimeError(f"{self.app_name} backup archive is missing") from exc
        if (
            not stat.S_ISREG(archive_status.st_mode)
            or archive_status.st_size <= 0
            or archive_status.st_size > active_limits.archive_bytes
        ):
            raise RuntimeError(f"{self.app_name} backup archive exceeds its size limit")
        self._require_no_trailing_zip_data(archive_path, archive_status.st_size)

        expected_database = self.database_members[0]
        expected_members = {"config.xml", "INFO", expected_database}
        temporary_directory: tempfile.TemporaryDirectory[str] | None = None
        if validation_root is None:
            temporary_directory = tempfile.TemporaryDirectory(prefix="servarr-verify-")
            validation_root = Path(temporary_directory.name)
        database_path = validation_root / "database.sqlite"
        try:
            try:
                with zipfile.ZipFile(archive_path) as archive:
                    members = archive.infolist()
                    if len(members) > active_limits.zip_members:
                        raise RuntimeError(f"{self.app_name} backup has too many ZIP members")
                    names = [member.filename for member in members]
                    if len(names) != len(set(names)) or set(names) != expected_members:
                        raise RuntimeError(f"{self.app_name} backup has an unexpected member set")
                    total_compressed = 0
                    total_uncompressed = 0
                    for member in members:
                        if (
                            member.filename != Path(member.filename).name
                            or Path(member.filename).is_absolute()
                            or ".." in Path(member.filename).parts
                        ):
                            raise RuntimeError(
                                f"{self.app_name} backup contains an unsafe ZIP member"
                            )
                        file_type = stat.S_IFMT(member.external_attr >> 16)
                        if file_type not in {0, stat.S_IFREG}:
                            raise RuntimeError(
                                f"{self.app_name} backup contains a non-regular ZIP member"
                            )
                        if member.flag_bits & 0x1:
                            raise RuntimeError(
                                f"{self.app_name} backup contains an encrypted ZIP member"
                            )
                        if member.compress_type not in {
                            zipfile.ZIP_STORED,
                            zipfile.ZIP_DEFLATED,
                        }:
                            raise RuntimeError(
                                f"{self.app_name} backup uses unsupported ZIP compression"
                            )
                        total_compressed += member.compress_size
                        total_uncompressed += member.file_size
                        if (
                            member.file_size > 0
                            and member.file_size
                            > max(1, member.compress_size) * active_limits.expansion_ratio
                        ):
                            raise RuntimeError(
                                f"{self.app_name} backup exceeds its ZIP expansion limit"
                            )
                    if total_compressed > active_limits.compressed_bytes:
                        raise RuntimeError(
                            f"{self.app_name} backup exceeds its compressed size limit"
                        )
                    if total_uncompressed > active_limits.uncompressed_bytes:
                        raise RuntimeError(
                            f"{self.app_name} backup exceeds its uncompressed size limit"
                        )
                    config_member = archive.getinfo("config.xml")
                    info_member = archive.getinfo("INFO")
                    database_member = archive.getinfo(expected_database)
                    if config_member.file_size > active_limits.config_bytes:
                        raise RuntimeError(f"{self.app_name} config.xml exceeds its size limit")
                    if info_member.file_size > active_limits.info_bytes:
                        raise RuntimeError(f"{self.app_name} INFO exceeds its size limit")
                    if database_member.file_size > active_limits.database_bytes:
                        raise RuntimeError(f"{self.app_name} database exceeds its size limit")
                    if archive.testzip() is not None:
                        raise RuntimeError(f"{self.app_name} backup failed its ZIP CRC check")
                    config_bytes = archive.read(config_member)
                    info_bytes = archive.read(info_member)
                    database_fd = os.open(
                        database_path,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                        0o600,
                    )
                    with (
                        archive.open(database_member) as database_source,
                        os.fdopen(database_fd, "wb") as database_destination,
                    ):
                        shutil.copyfileobj(
                            database_source,
                            database_destination,
                            length=1024 * 1024,
                        )
            except (zipfile.BadZipFile, NotImplementedError, OSError) as exc:
                raise RuntimeError(
                    f"{self.app_name} backup did not return a valid ZIP archive"
                ) from exc

            try:
                config_root = ElementTree.fromstring(config_bytes)
            except ElementTree.ParseError as exc:
                raise RuntimeError(f"{self.app_name} backup contains invalid config.xml") from exc
            api_keys = list(config_root.iter("ApiKey"))
            if (
                config_root.tag != "Config"
                or len(api_keys) != 1
                or api_keys[0].text is None
                or not api_keys[0].text.strip()
            ):
                raise RuntimeError(f"{self.app_name} backup contains an incompatible config.xml")

            try:
                info_text = info_bytes.decode("utf-8")
                if "\r" in info_text or not info_text.endswith("\n"):
                    raise ValueError
                info_lines = info_text.split("\n")
                if (
                    len(info_lines) != 3
                    or info_lines[2] != ""
                    or info_lines[0] != f"v{self.expected_version}"
                ):
                    raise ValueError
                datetime.strptime(info_lines[1], "%Y-%m-%d %H:%M:%S")
            except (UnicodeDecodeError, ValueError) as exc:
                raise RuntimeError(
                    f"{self.app_name} backup contains incompatible INFO metadata"
                ) from exc

            try:
                with sqlite3.connect(
                    f"file:{database_path}?mode=ro&immutable=1",
                    uri=True,
                ) as connection:
                    observed_steps = 0

                    def enforce_step_limit() -> int:
                        nonlocal observed_steps
                        observed_steps += _SQLITE_PROGRESS_INTERVAL
                        return int(observed_steps > _MAX_SQLITE_VM_STEPS)

                    connection.set_progress_handler(
                        enforce_step_limit,
                        _SQLITE_PROGRESS_INTERVAL,
                    )
                    quick_check = connection.execute("PRAGMA quick_check(1)").fetchall()
                    foreign_key_violation = connection.execute(
                        "PRAGMA foreign_key_check"
                    ).fetchone()
                    schema_count_row = connection.execute(
                        "SELECT COUNT(*) FROM sqlite_schema"
                    ).fetchone()
                    if (
                        schema_count_row is None
                        or len(schema_count_row) != 1
                        or not isinstance(schema_count_row[0], int)
                        or schema_count_row[0] > _MAX_SCHEMA_OBJECTS
                    ):
                        raise RuntimeError(
                            f"{self.app_name} backup exceeds its schema object limit"
                        )
                    tables = {
                        row[0]
                        for row in connection.execute(
                            "SELECT name FROM sqlite_master WHERE type = 'table'"
                        ).fetchmany(_MAX_SCHEMA_OBJECTS + 1)
                    }
                    migration_row = connection.execute(
                        'SELECT MAX("Version") FROM "VersionInfo"'
                    ).fetchone()
                    if not self.required_native_tables.issubset(tables):
                        raise RuntimeError(
                            f"{self.app_name} backup is missing required database tables"
                        )
                    resource_counts: list[tuple[str, int]] = []
                    for resource_path, table_name in self.restore_content_tables:
                        count_row = connection.execute(
                            f'SELECT COUNT(*) FROM "{table_name}"'
                        ).fetchone()
                        if (
                            count_row is None
                            or len(count_row) != 1
                            or isinstance(count_row[0], bool)
                            or not isinstance(count_row[0], int)
                            or count_row[0] < 0
                            or count_row[0] > _MAX_RESTORE_CONTENT_ROWS
                        ):
                            raise RuntimeError(
                                f"{self.app_name} backup exceeds its restored-content row limit"
                            )
                        resource_counts.append((resource_path, count_row[0]))
                    tag_count = dict(resource_counts).get("tag", 0)
                    if tag_count > _MAX_SEMANTIC_MARKERS:
                        raise RuntimeError(
                            f"{self.app_name} backup exceeds its semantic marker limit"
                        )
                    tag_labels_sha256 = _semantic_labels_sha256(())
                    if "tag" in dict(resource_counts):
                        tag_rows = connection.execute(
                            'SELECT "Label" FROM "Tags" ORDER BY "Label"'
                        ).fetchmany(_MAX_SEMANTIC_MARKERS + 1)
                        if len(tag_rows) != tag_count or any(
                            len(row) != 1 or not isinstance(row[0], str) for row in tag_rows
                        ):
                            raise RuntimeError(
                                f"{self.app_name} backup contains invalid semantic markers"
                            )
                        tag_labels_sha256 = _semantic_labels_sha256([row[0] for row in tag_rows])
            except sqlite3.Error as exc:
                raise RuntimeError(
                    f"{self.app_name} backup contains an unreadable SQLite database"
                ) from exc
            if quick_check != [("ok",)]:
                raise RuntimeError(f"{self.app_name} backup contains an invalid SQLite database")
            if foreign_key_violation is not None:
                raise RuntimeError(f"{self.app_name} backup contains foreign-key violations")
            if not self.required_native_tables.issubset(tables):
                raise RuntimeError(f"{self.app_name} backup is missing required database tables")
            if (
                migration_row is None
                or len(migration_row) != 1
                or migration_row[0] != self.expected_migration
            ):
                raise RuntimeError(f"{self.app_name} backup database migration is incompatible")
            return _RestoreContentEvidence(
                archive_members=tuple(sorted(expected_members)),
                database_tables=tuple(sorted(tables)),
                resource_counts=tuple(resource_counts),
                tag_labels_sha256=tag_labels_sha256,
            )
        finally:
            database_path.unlink(missing_ok=True)
            if temporary_directory is not None:
                temporary_directory.cleanup()

    def _restored_api_key(self, archive_path: Path) -> str:
        """Read the post-restore key in memory so readiness can authenticate."""

        with zipfile.ZipFile(archive_path) as archive:
            config_member = next(
                (name for name in archive.namelist() if Path(name).name.lower() == "config.xml"),
                None,
            )
            if config_member is None:
                raise RuntimeError(f"{self.app_name} restore archive has no config.xml")
            try:
                root = ElementTree.fromstring(archive.read(config_member))
            except ElementTree.ParseError as exc:
                raise RuntimeError(f"{self.app_name} restore config.xml is invalid") from exc
        api_keys = [
            element.text.strip()
            for element in root.iter()
            if element.tag.lower() == "apikey" and element.text and element.text.strip()
        ]
        if len(api_keys) != 1:
            raise RuntimeError(f"{self.app_name} restore config.xml has no unique API key")
        return api_keys[0]

    def _require_isolated_restore_authorization(self, base_url: str) -> None:
        if self.native_backup_mount is None:
            return
        if os.getenv(_ISOLATED_RESTORE_ENV) != "1":
            raise RuntimeError(
                f"{self.app_name} restore is disabled outside an isolated local drill"
            )
        allowed_value = os.getenv(_ISOLATED_RESTORE_ORIGINS_ENV, "")
        try:
            allowed_origins = {
                _canonical_origin(value.strip())
                for value in allowed_value.split(",")
                if value.strip()
            }
        except ValueError as exc:
            raise RuntimeError(f"{self.app_name} restore origin allowlist is invalid") from exc
        if _canonical_origin(base_url) not in allowed_origins:
            raise RuntimeError(
                f"{self.app_name} restore origin is not authorized for this isolated drill"
            )

    async def _open_verified_restore_artifact(
        self,
        artifact: Path,
        metadata: dict[str, Any],
    ) -> tuple[int, _RestoreWorkerEvidence]:
        expected_size = metadata.get("artifact_bytes")
        expected_sha256 = metadata.get("artifact_sha256")
        if (
            isinstance(expected_size, bool)
            or not isinstance(expected_size, int)
            or expected_size <= 0
            or not isinstance(expected_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None
        ):
            raise ValueError(
                f"{self.app_name} restore requires verified artifact size and hash metadata"
            )
        validation_root, validation_fd, validation_identity = _create_private_validation_directory(
            artifact.parent
        )
        process: BaseProcess | None = None
        try:
            process, connection = _start_restore_process(
                self.name,
                artifact,
                expected_size,
                expected_sha256,
                validation_root,
                validation_identity,
            )
            payload = await _await_worker(
                process,
                connection,
                timeout_seconds=_RESTORE_WORKER_TIMEOUT_SECONDS,
            )
            if not isinstance(payload, _RestoreWorkerEvidence):
                raise RuntimeError(f"{self.app_name} restore worker returned an invalid result")
        finally:
            try:
                if process is None or not process.is_alive():
                    parent_fd = os.open(validation_root.parent, _directory_flags())
                    try:
                        _remove_owned_directory(
                            parent_fd,
                            validation_fd,
                            expected=validation_identity,
                            name=validation_root.name,
                        )
                    finally:
                        os.close(parent_fd)
            finally:
                os.close(validation_fd)

        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(artifact, flags)
        except OSError as exc:
            raise ValueError(f"{self.app_name} restore artifact could not be opened") from exc
        try:
            named = artifact.stat(follow_symlinks=False)
            if (
                _file_evidence(os.fstat(descriptor)) != payload.source
                or _file_evidence(named) != payload.source
                or payload.sha256 != expected_sha256
            ):
                raise ValueError(f"{self.app_name} restore artifact changed after verification")
            return descriptor, payload
        except BaseException:
            os.close(descriptor)
            raise

    async def _bounded_resource_list(
        self,
        client: httpx.AsyncClient,
        url: str,
        headers: dict[str, str],
        purpose: str,
    ) -> list[Any]:
        body = bytearray()
        async with client.stream("GET", url, headers=headers) as response:
            if response.status_code != 200:
                raise RuntimeError(
                    f"{self.app_name} {purpose} returned status {response.status_code}"
                )
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > _MAX_RESTORE_API_BYTES:
                    raise RuntimeError(f"{self.app_name} {purpose} exceeds its response size limit")
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"{self.app_name} {purpose} response is invalid") from exc
        if not isinstance(payload, list) or len(payload) > _MAX_RESTORE_CONTENT_ROWS:
            raise RuntimeError(f"{self.app_name} {purpose} response is invalid")
        return payload

    async def _require_fresh_restore_destination(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        headers: dict[str, str],
    ) -> None:
        resource_paths = (
            tuple(path for path, _table in self.restore_content_tables)
            if self.restore_content_tables
            else self.fresh_restore_resource_paths
        )
        for resource_path in resource_paths:
            payload = await self._bounded_resource_list(
                client,
                f"{base_url}{self.api_prefix}/{resource_path}",
                headers,
                "fresh-destination check",
            )
            if payload:
                raise RuntimeError(f"{self.app_name} restore destination is not fresh and empty")

    async def _require_restored_content(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        headers: dict[str, str],
        evidence: _RestoreContentEvidence,
    ) -> None:
        for resource_path, expected_count in evidence.resource_counts:
            payload = await self._bounded_resource_list(
                client,
                f"{base_url}{self.api_prefix}/{resource_path}",
                headers,
                "restored-content check",
            )
            if len(payload) != expected_count:
                raise RuntimeError(f"{self.app_name} restored content does not match its archive")
            if resource_path == "tag":
                labels: list[str] = []
                for item in payload:
                    label = item.get("label") if isinstance(item, dict) else None
                    if isinstance(label, str):
                        labels.append(label)
                if (
                    len(labels) != len(payload)
                    or _semantic_labels_sha256(labels) != evidence.tag_labels_sha256
                ):
                    raise RuntimeError(
                        f"{self.app_name} restored content does not match its archive"
                    )

    async def _request_restore_restart(
        self,
        client: httpx.AsyncClient,
        restart_url: str,
        headers: dict[str, str],
    ) -> None:
        restart = await client.post(restart_url, headers=headers, content=b"")
        if restart.status_code != 200:
            raise RuntimeError(
                f"{self.app_name} restore restart returned status {restart.status_code}"
            )
        try:
            restart_data = restart.json()
        except ValueError as exc:
            raise RuntimeError(f"{self.app_name} restore restart response is invalid") from exc
        if not isinstance(restart_data, dict) or restart_data.get("restarting") is not True:
            raise RuntimeError(f"{self.app_name} did not acknowledge the restore restart")

    async def _wait_for_restored_readiness(
        self,
        client: httpx.AsyncClient,
        status_url: str,
        restored_headers: dict[str, str],
        previous_start: str,
    ) -> str:
        deadline = asyncio.get_running_loop().time() + self.restore_deadline_seconds
        while True:
            try:
                status_response = await client.get(status_url, headers=restored_headers)
                if status_response.status_code == 200:
                    try:
                        status_data = status_response.json()
                    except ValueError as exc:
                        raise RuntimeError(
                            f"{self.app_name} post-restore status is invalid"
                        ) from exc
                    self._validate_status(status_data)
                    current_start = status_data.get("startTime")
                    if (
                        isinstance(current_start, str)
                        and current_start
                        and current_start != previous_start
                    ):
                        return current_start
                # Restart is asynchronous. The old process can briefly reject
                # the restored key; only exact status with a new start succeeds.
            except httpx.HTTPError:
                pass
            if asyncio.get_running_loop().time() >= deadline:
                raise RuntimeError(f"{self.app_name} did not become ready after restore restart")
            await asyncio.sleep(self.restore_poll_interval_seconds)

    async def backup(self, context: BackupContext) -> Dict[str, Any]:
        """Create one native backup with secret-safe lifecycle evidence."""

        started = time.monotonic()
        log_context = {"job_id": context.job_id, "target_id": context.target_id}
        self._logger.info("%s backup started", self.app_name, extra=log_context)
        try:
            result = await self._backup_operation(context)
        except BaseException as exc:
            self._logger.error(
                "%s backup failed duration_seconds=%.3f error_type=%s",
                self.app_name,
                time.monotonic() - started,
                type(exc).__name__,
                extra=log_context,
            )
            raise
        self._logger.info(
            "%s backup succeeded artifact_path=%s duration_seconds=%.3f",
            self.app_name,
            result.get("artifact_path", "[missing]"),
            time.monotonic() - started,
            extra=log_context,
        )
        return result

    async def _backup_operation(self, context: BackupContext) -> Dict[str, Any]:
        if not await self.validate_config(context.config or {}):
            raise ValueError(
                "Invalid configuration: base_url, api_key, and backup_directory are required"
                if self.native_backup_mount is not None
                else "Invalid configuration: base_url and api_key are required"
            )
        base_url, headers = self._request_config(context.config or {})
        backup_directory = (
            self._require_read_only_backup_mount(context.config or {})
            if self.native_backup_mount is not None
            else None
        )
        list_url = f"{base_url}{self.api_prefix}/system/backup"
        command_url = f"{base_url}{self.api_prefix}/command"

        lock_key = f"{self.app_name.lower()}:{_canonical_origin(base_url)}"
        async with _hold_lock(_backup_lock(lock_key)):
            async with httpx.AsyncClient(timeout=30.0) as client:
                if self.expected_version is not None:
                    await self._require_exact_status(client, base_url, headers)
                baseline = await self._list_backups(client, list_url, headers)
                if backup_directory is not None:
                    self._validate_manual_backup_entries(backup_directory, baseline)
                known = {
                    self._backup_identity(item) for item in baseline if item.get("type") == "manual"
                }

                # Native names and list timestamps have whole-second precision.
                # Reserve and cross a new boundary for every serialized trigger
                # rather than relying on a drill-only sleep or filename overwrite.
                triggered_at = (
                    await _reserve_trigger_second(lock_key)
                    if self.require_distinct_trigger_second
                    else datetime.now(timezone.utc).replace(microsecond=0)
                )
                trigger = await client.post(
                    command_url,
                    headers={**headers, "Content-Type": "application/json"},
                    json={"name": "Backup"},
                )
                trigger.raise_for_status()
                trigger_data = trigger.json()
                command_id = trigger_data.get("id") if isinstance(trigger_data, dict) else None
                if isinstance(command_id, bool) or not isinstance(command_id, int):
                    raise RuntimeError(f"{self.app_name} did not return a backup command id")

                command_status_url = f"{command_url}/{command_id}"
                deadline = asyncio.get_running_loop().time() + self.backup_deadline_seconds
                while True:
                    response = await client.get(command_status_url, headers=headers)
                    response.raise_for_status()
                    command = response.json()
                    status = str(command.get("status", "")).lower()
                    result = str(command.get("result", "")).lower()
                    if status == "completed":
                        if self.command_result_required and result != "successful":
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
                    await asyncio.sleep(self.poll_interval_seconds)

                backup_item: dict[str, Any] | None = None
                while backup_item is None:
                    observed_backups = await self._list_backups(client, list_url, headers)
                    if backup_directory is not None:
                        self._validate_manual_backup_entries(
                            backup_directory,
                            observed_backups,
                        )
                    candidates = self._new_manual_backups(
                        observed_backups,
                        known,
                        triggered_at,
                    )
                    if len(candidates) > 1:
                        raise RuntimeError(
                            f"{self.app_name} backup archive attribution was ambiguous"
                        )
                    if candidates:
                        backup_item = candidates[0]
                    if backup_item is not None:
                        break
                    if asyncio.get_running_loop().time() >= deadline:
                        raise RuntimeError(f"{self.app_name} backup archive did not appear")
                    await asyncio.sleep(self.poll_interval_seconds)

                validated_structure: _RestoreContentEvidence | None = None
                with create_backup_artifact(
                    self,
                    context,
                    prefix=f"{self.name}-backup",
                    suffix=".zip",
                    backup_root=self.backup_root,
                ) as artifact:
                    if backup_directory is not None:
                        source_path = self._local_backup_path(
                            backup_directory,
                            backup_item,
                        )
                        worker_evidence = _BackupWorkerEvidence(
                            plugin=self,
                            source=_file_evidence(source_path.lstat()),
                            limits=_current_archive_limits(),
                        )
                        validation_root, validation_fd, validation_identity = (
                            _create_private_validation_directory(artifact.temporary_path.parent)
                        )
                        process: BaseProcess | None = None
                        try:
                            process, connection = _start_backup_process(
                                backup_directory,
                                source_path.name,
                                worker_evidence,
                                artifact.temporary_path,
                                validation_root,
                                validation_identity,
                            )
                            payload = await _await_worker(
                                process,
                                connection,
                                timeout_seconds=_BACKUP_WORKER_TIMEOUT_SECONDS,
                            )
                            if not isinstance(payload, _ArchiveEvidence):
                                raise RuntimeError(
                                    f"{self.app_name} backup worker returned an invalid result"
                                )
                            validated_structure = payload.structure
                            publication_flags = os.O_RDONLY
                            if hasattr(os, "O_NOFOLLOW"):
                                publication_flags |= os.O_NOFOLLOW
                            publication_fd = os.open(
                                artifact.temporary_path,
                                publication_flags,
                            )
                            publication_status = os.fstat(publication_fd)
                            if (
                                not stat.S_ISREG(publication_status.st_mode)
                                or publication_status.st_dev != payload.device
                                or publication_status.st_ino != payload.inode
                                or publication_status.st_size != payload.artifact_bytes
                                or _hash_descriptor(publication_fd) != payload.sha256
                            ):
                                os.close(publication_fd)
                                raise RuntimeError(
                                    f"{self.app_name} validated artifact changed before publication"
                                )
                            artifact.publication_fd = publication_fd
                            artifact.publication_sha256 = payload.sha256
                        finally:
                            try:
                                if process is None or not process.is_alive():
                                    parent_fd = os.open(
                                        validation_root.parent,
                                        _directory_flags(),
                                    )
                                    try:
                                        _remove_owned_directory(
                                            parent_fd,
                                            validation_fd,
                                            expected=validation_identity,
                                            name=validation_root.name,
                                        )
                                    finally:
                                        os.close(parent_fd)
                            finally:
                                os.close(validation_fd)
                    else:
                        backup_path = self._download_path(backup_item)
                        download_url = f"{base_url}{backup_path}"
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
                    if backup_directory is None:
                        self._validate_archive(artifact.temporary_path)
                    if backup_directory is not None:
                        if validated_structure is None:
                            raise RuntimeError(
                                f"{self.app_name} backup structural evidence is missing"
                            )
                        artifact.sidecar_metadata.update(
                            {
                                "application": self.app_name,
                                "application_version": self.expected_version,
                                "database_backend": self.expected_database_type,
                                "database_migration": self.expected_migration,
                                "command_id": command_id,
                                "source_backup_id": backup_item["id"],
                                "source_backup_type": backup_item["type"],
                                "source_backup_time": backup_item["time"],
                                "archive_members": list(validated_structure.archive_members),
                                "archive_member_count": len(validated_structure.archive_members),
                                "database_tables": list(validated_structure.database_tables),
                                "database_table_count": len(validated_structure.database_tables),
                                "validation": "strict-native-v1",
                            }
                        )
                        if self.expected_package_version is not None:
                            artifact.sidecar_metadata["package_version"] = (
                                self.expected_package_version
                            )
                artifact_path = str(artifact.final_path)
                if backup_directory is not None:
                    backup_id = backup_item.get("id")
                    if isinstance(backup_id, bool) or not isinstance(backup_id, int):
                        raise RuntimeError(f"{self.app_name} attributed backup id is invalid")
                    cleanup = await client.delete(
                        f"{list_url}/{backup_id}",
                        headers=headers,
                    )
                    if cleanup.status_code != 200:
                        raise RuntimeError(
                            f"{self.app_name} native backup cleanup returned status "
                            f"{cleanup.status_code}; both copies were preserved"
                        )

        return {"artifact_path": artifact_path}

    async def restore(self, context: RestoreContext) -> Dict[str, Any]:
        """Restore one native artifact with secret-safe lifecycle evidence."""

        started = time.monotonic()
        log_context = {
            "job_id": context.job_id,
            "source_target_id": context.source_target_id,
            "destination_target_id": context.destination_target_id,
        }
        self._logger.info("%s restore started", self.app_name, extra=log_context)
        try:
            result = await self._restore_operation(context)
        except BaseException as exc:
            self._logger.error(
                "%s restore failed duration_seconds=%.3f error_type=%s",
                self.app_name,
                time.monotonic() - started,
                type(exc).__name__,
                extra=log_context,
            )
            raise
        self._logger.info(
            "%s restore succeeded artifact_path=%s duration_seconds=%.3f",
            self.app_name,
            result.get("artifact_path", "[missing]"),
            time.monotonic() - started,
            extra=log_context,
        )
        return result

    async def _restore_operation(self, context: RestoreContext) -> Dict[str, Any]:
        if not await self.validate_config(context.config or {}):
            raise ValueError(f"Invalid {self.app_name} restore configuration")
        base_url, headers = self._request_config(context.config or {})
        self._require_isolated_restore_authorization(base_url)
        if (
            self.native_backup_mount is not None
            and context.source_target_id == context.destination_target_id
        ):
            raise ValueError(
                f"{self.app_name} restore source and destination must be different targets"
            )
        lock_key = f"{self.app_name.lower()}:{_canonical_origin(base_url)}"
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
        artifact_fd: int | None = None
        restore_worker_evidence: _RestoreWorkerEvidence | None = None
        if self.native_backup_mount is not None:
            artifact_fd, restore_worker_evidence = await self._open_verified_restore_artifact(
                artifact,
                context.metadata or {},
            )
        verified_artifact = (
            Path(f"/proc/self/fd/{artifact_fd}") if artifact_fd is not None else artifact
        )
        verified_size = (
            os.fstat(artifact_fd).st_size if artifact_fd is not None else artifact.stat().st_size
        )
        destination_mutated = False
        try:
            content_evidence = (
                restore_worker_evidence.content
                if restore_worker_evidence is not None
                else self._validate_archive(verified_artifact)
            )
            if self.restore_content_tables and content_evidence is None:
                raise RuntimeError(f"{self.app_name} restore content evidence is missing")
            restored_key = (
                restore_worker_evidence.restored_api_key
                if restore_worker_evidence is not None
                else self._restored_api_key(verified_artifact)
            )
            status_url = f"{base_url}{self.api_prefix}/system/status"
            upload_url = f"{base_url}{self.api_prefix}/system/backup/restore/upload"
            restart_url = f"{base_url}{self.api_prefix}/system/restart"

            async with httpx.AsyncClient(timeout=60.0) as client:
                before = await client.get(status_url, headers=headers)
                if before.status_code != 200:
                    raise RuntimeError(
                        f"{self.app_name} restore destination returned status "
                        f"{before.status_code}"
                    )
                try:
                    before_data = before.json()
                except ValueError as exc:
                    raise RuntimeError(
                        f"{self.app_name} restore destination status is invalid"
                    ) from exc
                self._validate_status(before_data)
                previous_start = before_data.get("startTime")
                if not isinstance(previous_start, str) or not previous_start:
                    raise RuntimeError(
                        f"{self.app_name} restore destination has no process start time"
                    )
                await self._require_fresh_restore_destination(
                    client,
                    base_url,
                    headers,
                )

                upload_descriptor = (
                    os.dup(artifact_fd)
                    if artifact_fd is not None
                    else os.open(artifact, os.O_RDONLY)
                )
                if (
                    artifact_fd is not None
                    and restore_worker_evidence is not None
                    and not _same_open_file_content(
                        os.fstat(artifact_fd),
                        restore_worker_evidence.source,
                    )
                ):
                    os.close(upload_descriptor)
                    raise ValueError(f"{self.app_name} restore artifact changed before upload")
                with os.fdopen(upload_descriptor, "rb") as artifact_file:
                    destination_mutated = True
                    upload = await client.post(
                        upload_url,
                        headers=headers,
                        files={
                            "file": (
                                artifact.name,
                                artifact_file,
                                "application/zip",
                            )
                        },
                    )
                if upload.status_code != 200:
                    raise RuntimeError(
                        f"{self.app_name} restore upload returned status {upload.status_code}"
                    )
                try:
                    upload_data = upload.json()
                except ValueError as exc:
                    raise RuntimeError(
                        f"{self.app_name} restore upload response is invalid"
                    ) from exc
                if (
                    not isinstance(upload_data, dict)
                    or upload_data.get("restartRequired") is not True
                ):
                    raise RuntimeError(f"{self.app_name} did not accept the restore archive")

                await self._request_restore_restart(client, restart_url, headers)
                restored_headers = {"X-Api-Key": restored_key}
                current_start = await self._wait_for_restored_readiness(
                    client,
                    status_url,
                    restored_headers,
                    previous_start,
                )
                if content_evidence is not None:
                    await self._require_restored_content(
                        client,
                        base_url,
                        restored_headers,
                        content_evidence,
                    )
                if self.require_persistence_restart:
                    await self._request_restore_restart(
                        client,
                        restart_url,
                        restored_headers,
                    )
                    await self._wait_for_restored_readiness(
                        client,
                        status_url,
                        restored_headers,
                        current_start,
                    )
                    if content_evidence is not None:
                        await self._require_restored_content(
                            client,
                            base_url,
                            restored_headers,
                            content_evidence,
                        )

            return {
                "status": "success",
                "artifact_path": artifact_path,
                "artifact_bytes": verified_size,
                "message": f"{self.app_name} restore completed and restarted successfully",
            }
        except Exception:
            if destination_mutated:
                raise RuntimeError(
                    f"{self.app_name} restore failed after destination mutation; "
                    "discard the disposable destination"
                ) from None
            raise
        finally:
            if artifact_fd is not None:
                os.close(artifact_fd)

    async def get_status(self, context: BackupContext) -> Dict[str, Any]:
        try:
            await self.test(context.config or {})
        except Exception as exc:
            message = str(exc)
            api_key = (context.config or {}).get("api_key")
            if isinstance(api_key, str) and api_key:
                message = message.replace(api_key, "[redacted]")
            return {"status": "error", "error": message}
        return {"status": "ok"}
