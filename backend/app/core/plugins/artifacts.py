"""Transactional creation of backup artifacts and their recovery metadata."""

from __future__ import annotations

import hashlib
import io
import os
import re
import uuid
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .base import BackupContext, BackupPlugin
from .sidecar import read_backup_sidecar, write_backup_sidecar

_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
FILE_CACHE_EVICTION_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class PendingBackupArtifact:
    """Paths exposed while one artifact is being created."""

    temporary_path: Path
    final_path: Path


@dataclass(frozen=True)
class ValidatedBackupArtifact:
    """Trusted metadata calculated from a finalized backup artifact."""

    path: Path
    size_bytes: int
    sha256: str


def validate_zip_bytes(data: bytes, *, artifact_label: str) -> None:
    """Reject empty, malformed, or CRC-damaged ZIP payloads before publication."""

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = archive.infolist()
            if not members:
                raise RuntimeError(f"{artifact_label} did not return a usable ZIP archive")
            if archive.testzip() is not None:
                raise RuntimeError(f"{artifact_label} did not return a valid ZIP archive")
    except (zipfile.BadZipFile, OSError) as exc:
        raise RuntimeError(f"{artifact_label} did not return a valid ZIP archive") from exc


def _validate_component(value: str, *, field: str) -> str:
    if not _SAFE_COMPONENT.fullmatch(value) or value in {".", ".."}:
        raise ValueError(f"Invalid {field}: {value!r}")
    return value


def _fsync_directory(path: Path) -> None:
    directory_fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def evict_file_cache(file_descriptor: int, offset: int, length: int) -> None:
    """Best-effort eviction of a completed file range from the Linux page cache."""

    if length <= 0 or not hasattr(os, "posix_fadvise"):
        return
    try:
        os.posix_fadvise(file_descriptor, offset, length, os.POSIX_FADV_DONTNEED)
    except OSError:
        # Cache advice must never change backup correctness or availability.
        pass


@contextmanager
def create_backup_artifact(
    plugin: BackupPlugin,
    context: BackupContext,
    *,
    prefix: str,
    suffix: str,
    backup_root: str | Path = "/backups",
) -> Iterator[PendingBackupArtifact]:
    """Create one unique artifact and publish it only after validation.

    Callers write the complete export to ``temporary_path``. A normal context
    exit verifies a non-empty regular file, flushes it, atomically renames it to
    ``final_path``, and atomically writes the required sidecar. Exceptions and
    invalid output leave neither a final artifact nor a temporary file.
    """

    metadata = context.metadata or {}
    target_slug = _validate_component(
        str(metadata.get("target_slug") or context.target_id),
        field="target slug",
    )
    safe_prefix = _validate_component(prefix, field="artifact prefix")
    if not suffix.startswith(".") or "/" in suffix or "\\" in suffix:
        raise ValueError(f"Invalid artifact suffix: {suffix!r}")

    now = datetime.now(timezone.utc)
    artifact_dir = Path(backup_root) / target_slug / now.strftime("%Y-%m-%d")
    artifact_dir.mkdir(parents=True, exist_ok=True)
    unique_part = f"{now.strftime('%Y%m%dT%H%M%S%fZ')}-{uuid.uuid4().hex[:12]}"
    final_path = artifact_dir / f"{safe_prefix}-{unique_part}{suffix}"
    temporary_path = artifact_dir / f".{final_path.name}.{uuid.uuid4().hex}.tmp"
    pending = PendingBackupArtifact(
        temporary_path=temporary_path,
        final_path=final_path,
    )
    committed = False

    try:
        yield pending
        if not temporary_path.exists() or not temporary_path.is_file():
            raise ValueError("Backup plugin did not create a regular artifact")
        if temporary_path.stat().st_size <= 0:
            raise ValueError("Backup plugin created an empty artifact")

        with temporary_path.open("rb") as artifact_file:
            os.fsync(artifact_file.fileno())
        os.replace(temporary_path, final_path)
        committed = True
        _fsync_directory(artifact_dir)
        write_backup_sidecar(str(final_path), plugin, context)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        if committed:
            Path(f"{final_path}.meta.json").unlink(missing_ok=True)
            final_path.unlink(missing_ok=True)
        raise


def write_backup_bytes(
    plugin: BackupPlugin,
    context: BackupContext,
    data: bytes,
    *,
    prefix: str,
    suffix: str,
    backup_root: str | Path = "/backups",
) -> str:
    """Atomically publish an in-memory export and return its final path."""

    with create_backup_artifact(
        plugin,
        context,
        prefix=prefix,
        suffix=suffix,
        backup_root=backup_root,
    ) as artifact:
        artifact.temporary_path.write_bytes(data)
    return str(artifact.final_path)


def validate_backup_artifact(
    artifact_path: str,
    plugin: BackupPlugin,
    context: BackupContext,
) -> ValidatedBackupArtifact:
    """Verify the artifact and sidecar contract before recording success."""

    path = Path(artifact_path)
    if not path.exists() or not path.is_file() or path.is_symlink():
        raise ValueError("Backup artifact is not a regular file")
    size_bytes = path.stat().st_size
    if size_bytes <= 0:
        raise ValueError("Backup artifact is empty")

    sidecar = read_backup_sidecar(str(path))
    if sidecar is None:
        raise ValueError("Backup artifact is missing a valid sidecar")
    if sidecar.get("plugin_name") != plugin.name:
        raise ValueError("Backup sidecar plugin does not match the executing plugin")
    sidecar_path = sidecar.get("artifact_path")
    if not isinstance(sidecar_path, str) or Path(sidecar_path).resolve() != path.resolve():
        raise ValueError("Backup sidecar path does not match the artifact")
    expected_slug = str((context.metadata or {}).get("target_slug") or context.target_id)
    if sidecar.get("target_slug") != expected_slug:
        raise ValueError("Backup sidecar target does not match the executing target")

    digest = hashlib.sha256()
    with path.open("rb") as artifact_file:
        eviction_offset = 0
        pending_eviction_bytes = 0
        for chunk in iter(lambda: artifact_file.read(1024 * 1024), b""):
            digest.update(chunk)
            pending_eviction_bytes += len(chunk)
            if pending_eviction_bytes >= FILE_CACHE_EVICTION_BYTES:
                evict_file_cache(artifact_file.fileno(), eviction_offset, pending_eviction_bytes)
                eviction_offset += pending_eviction_bytes
                pending_eviction_bytes = 0
        evict_file_cache(artifact_file.fileno(), eviction_offset, pending_eviction_bytes)
    return ValidatedBackupArtifact(
        path=path,
        size_bytes=size_bytes,
        sha256=digest.hexdigest(),
    )


def validate_restore_artifact(
    artifact_path: str,
    *,
    expected_plugin_name: str,
    backup_root: str | Path = "/backups",
    expected_target_slug: str | None = None,
    expected_size_bytes: int | None = None,
    expected_sha256: str | None = None,
) -> ValidatedBackupArtifact:
    """Authenticate an artifact and its recovery metadata before restore.

    Restore inputs must be finalized regular files below the configured backup
    root. Their sidecar establishes the producing plugin and target, while
    database metadata (when available) pins the expected size and digest.
    """

    path = Path(artifact_path)
    if not path.exists() or not path.is_file() or path.is_symlink():
        raise ValueError("Restore artifact is not a regular file")

    resolved_path = path.resolve()
    resolved_root = Path(backup_root).resolve()
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError("Restore artifact is outside the configured backup root") from exc

    sidecar = read_backup_sidecar(str(path))
    if sidecar is None:
        raise ValueError("Restore artifact is missing a valid sidecar")
    if sidecar.get("plugin_name") != expected_plugin_name:
        raise ValueError("Restore sidecar plugin does not match the destination plugin")
    sidecar_path = sidecar.get("artifact_path")
    if not isinstance(sidecar_path, str) or Path(sidecar_path).resolve() != resolved_path:
        raise ValueError("Restore sidecar path does not match the artifact")
    if expected_target_slug is not None and sidecar.get("target_slug") != expected_target_slug:
        raise ValueError("Restore sidecar target does not match the source target")

    size_bytes = path.stat().st_size
    if size_bytes <= 0:
        raise ValueError("Restore artifact is empty")
    if expected_size_bytes is not None and size_bytes != expected_size_bytes:
        raise ValueError("Restore artifact size does not match the backup record")

    digest = hashlib.sha256()
    with path.open("rb") as artifact_file:
        for chunk in iter(lambda: artifact_file.read(1024 * 1024), b""):
            digest.update(chunk)
    sha256 = digest.hexdigest()
    if expected_sha256 is not None and sha256 != expected_sha256:
        raise ValueError("Restore artifact hash does not match the backup record")

    return ValidatedBackupArtifact(
        path=path,
        size_bytes=size_bytes,
        sha256=sha256,
    )
