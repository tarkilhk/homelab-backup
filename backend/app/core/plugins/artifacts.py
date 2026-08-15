"""Transactional creation of backup artifacts and their recovery metadata."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import io
import os
import re
import stat
import uuid
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .base import BackupContext, BackupPlugin
from .sidecar import read_backup_sidecar, write_backup_sidecar

_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
FILE_CACHE_EVICTION_BYTES = 8 * 1024 * 1024


@dataclass
class PendingBackupArtifact:
    """Paths exposed while one artifact is being created."""

    temporary_path: Path
    final_path: Path
    sidecar_metadata: dict[str, object] = field(default_factory=dict)
    publication_fd: int | None = None
    publication_sha256: str | None = None


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


def _file_identity(status: os.stat_result) -> tuple[int, int]:
    return status.st_dev, status.st_ino


def _require_bound_temporary_path(pending: PendingBackupArtifact) -> os.stat_result:
    if pending.publication_fd is None:
        raise RuntimeError("Backup artifact has no bound publication descriptor")
    opened = os.fstat(pending.publication_fd)
    try:
        named = pending.temporary_path.stat(follow_symlinks=False)
    except OSError as exc:
        raise RuntimeError("Backup artifact changed before publication") from exc
    if (
        not stat.S_ISREG(opened.st_mode)
        or not stat.S_ISREG(named.st_mode)
        or _file_identity(opened) != _file_identity(named)
    ):
        raise RuntimeError("Backup artifact changed before publication")
    return opened


def _link_open_file(publication_fd: int, final_path: Path) -> None:
    directory_fd = os.open(final_path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        library = ctypes.CDLL(None, use_errno=True)
        linkat = getattr(library, "linkat", None)
        if linkat is None:
            raise RuntimeError("Bound artifact publication requires Linux linkat")
        linkat.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
        ]
        linkat.restype = ctypes.c_int
        if (
            linkat(
                -100,
                os.fsencode(f"/proc/self/fd/{publication_fd}"),
                directory_fd,
                os.fsencode(final_path.name),
                0x400,
            )
            != 0
        ):
            error_number = ctypes.get_errno()
            if error_number == errno.EEXIST:
                raise FileExistsError("Backup artifact destination already exists")
            raise OSError(error_number, os.strerror(error_number))
    finally:
        os.close(directory_fd)


def _hash_open_file(file_descriptor: int) -> str:
    digest = hashlib.sha256()
    offset = 0
    while True:
        chunk = os.pread(file_descriptor, 1024 * 1024, offset)
        if not chunk:
            break
        digest.update(chunk)
        offset += len(chunk)
    return digest.hexdigest()


def _path_has_identity(path: Path, expected_identity: tuple[int, int]) -> bool:
    try:
        status = path.stat(follow_symlinks=False)
    except OSError:
        return False
    return stat.S_ISREG(status.st_mode) and _file_identity(status) == expected_identity


def _unlink_owned_file(path: Path, expected_identity: tuple[int, int] | None) -> None:
    if expected_identity is not None and _path_has_identity(path, expected_identity):
        path.unlink()


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
    artifact_identity: tuple[int, int] | None = None
    sidecar_identity: tuple[int, int] | None = None

    try:
        yield pending
        if pending.publication_fd is None:
            publication_flags = os.O_RDONLY
            if hasattr(os, "O_NOFOLLOW"):
                publication_flags |= os.O_NOFOLLOW
            try:
                pending.publication_fd = os.open(temporary_path, publication_flags)
            except OSError as exc:
                raise ValueError("Backup plugin did not create a regular artifact") from exc
        opened = _require_bound_temporary_path(pending)
        if opened.st_size <= 0:
            raise ValueError("Backup plugin created an empty artifact")
        artifact_identity = _file_identity(opened)
        if pending.publication_sha256 is None:
            artifact_sha256 = _hash_open_file(pending.publication_fd)
        elif re.fullmatch(r"[0-9a-f]{64}", pending.publication_sha256) is None:
            raise ValueError("Backup plugin supplied an invalid artifact hash")
        else:
            artifact_sha256 = pending.publication_sha256
        os.fsync(pending.publication_fd)
        _link_open_file(pending.publication_fd, final_path)
        if not _path_has_identity(final_path, artifact_identity):
            raise RuntimeError("Backup artifact publication identity changed")
        _require_bound_temporary_path(pending)
        temporary_path.unlink()
        _fsync_directory(artifact_dir)
        sidecar_identity = write_backup_sidecar(
            str(final_path),
            plugin,
            context,
            extra_metadata=pending.sidecar_metadata,
            artifact_bytes=opened.st_size,
            artifact_sha256=artifact_sha256,
        )
        if not _path_has_identity(final_path, artifact_identity):
            raise RuntimeError("Backup artifact changed while its sidecar was committed")
        sidecar_path = Path(f"{final_path}.meta.json")
        if not _path_has_identity(sidecar_path, sidecar_identity):
            raise RuntimeError("Backup sidecar changed while it was committed")
    except BaseException:
        if pending.publication_fd is None:
            temporary_path.unlink(missing_ok=True)
        else:
            try:
                _require_bound_temporary_path(pending)
            except RuntimeError:
                pass
            else:
                temporary_path.unlink(missing_ok=True)
        _unlink_owned_file(Path(f"{final_path}.meta.json"), sidecar_identity)
        _unlink_owned_file(final_path, artifact_identity)
        _fsync_directory(artifact_dir)
        raise
    finally:
        if pending.publication_fd is not None:
            os.close(pending.publication_fd)
            pending.publication_fd = None


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


def _sidecar_artifact_identity(sidecar: dict[str, object]) -> tuple[int, str]:
    size_value = sidecar.get("artifact_bytes")
    digest_value = sidecar.get("sha256")
    if isinstance(size_value, bool) or not isinstance(size_value, int) or size_value <= 0:
        raise ValueError("Backup sidecar artifact size is invalid")
    if not isinstance(digest_value, str) or re.fullmatch(r"[0-9a-f]{64}", digest_value) is None:
        raise ValueError("Backup sidecar artifact hash is invalid")
    return size_value, digest_value


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
    sidecar_size, sidecar_sha256 = _sidecar_artifact_identity(sidecar)
    if sidecar_size is not None and size_bytes != sidecar_size:
        raise ValueError("Backup artifact size does not match its sidecar")

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
    sha256 = digest.hexdigest()
    if sidecar_sha256 is not None and sha256 != sidecar_sha256:
        raise ValueError("Backup artifact hash does not match its sidecar")
    return ValidatedBackupArtifact(
        path=path,
        size_bytes=size_bytes,
        sha256=sha256,
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
    sidecar_size, sidecar_sha256 = _sidecar_artifact_identity(sidecar)

    size_bytes = path.stat().st_size
    if size_bytes <= 0:
        raise ValueError("Restore artifact is empty")
    if expected_size_bytes is not None and size_bytes != expected_size_bytes:
        raise ValueError("Restore artifact size does not match the backup record")
    if sidecar_size is not None and size_bytes != sidecar_size:
        raise ValueError("Restore artifact size does not match its sidecar")

    digest = hashlib.sha256()
    with path.open("rb") as artifact_file:
        for chunk in iter(lambda: artifact_file.read(1024 * 1024), b""):
            digest.update(chunk)
    sha256 = digest.hexdigest()
    if expected_sha256 is not None and sha256 != expected_sha256:
        raise ValueError("Restore artifact hash does not match the backup record")
    if sidecar_sha256 is not None and sha256 != sidecar_sha256:
        raise ValueError("Restore artifact hash does not match its sidecar")

    return ValidatedBackupArtifact(
        path=path,
        size_bytes=size_bytes,
        sha256=sha256,
    )
