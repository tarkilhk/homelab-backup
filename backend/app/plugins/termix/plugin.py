"""Validated Termix 2.3.2 encrypted-state backup and isolated restore."""

from __future__ import annotations

import asyncio
import ctypes
import errno
import hashlib
import json
import multiprocessing
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import time
import uuid
import zipfile
from dataclasses import dataclass
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from pathlib import Path, PurePath
from typing import Any, Dict

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from app.core.plugins.artifacts import create_backup_artifact
from app.core.plugins.base import BackupContext, BackupPlugin, RestoreContext

TERMIX_VERSION = "2.3.2"
TERMIX_COMMIT = "c3282b5dca081d52513e94329bbc71084338217d"
BACKUP_BASE_PATH = "/backups"
RESTORE_SENTINEL_NAME = ".termix-restore-destination"
RESTORE_SENTINEL_CONTENT = "termix-v2.3.2-isolated-restore-v1\n"

_DATABASE_KEY_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")
_MAX_ENV_BYTES = 1024 * 1024
_MAX_ENVELOPE_METADATA_BYTES = 64 * 1024
_MAX_MANIFEST_BYTES = 64 * 1024
_MAX_OPK_CONFIG_BYTES = 1024 * 1024
_MAX_DATABASE_BYTES = 8 * 1024 * 1024 * 1024
_MAX_COMPRESSION_RATIO = 200
_STABLE_SETTLE_SECONDS = 2.25
_STABLE_READ_ATTEMPTS = 3
_BACKUP_TIMEOUT_SECONDS = 30.0
_VALIDATION_TIMEOUT_SECONDS = 30.0
_RESTORE_TIMEOUT_SECONDS = 30.0
_WORKER_STOP_TIMEOUT_SECONDS = 5.0
_FORBIDDEN_RESTORE_ROOTS = (
    Path("/app/data"),
    Path("/backups"),
    Path("/sources/termix"),
)
_REQUIRED_ARCHIVE_FILES = frozenset({".env", "db.sqlite.encrypted"})
_OPTIONAL_ARCHIVE_FILES = frozenset({".opk/config.yml"})
_MANIFEST_KEYS = frozenset({"files", "format_version", "plugin", "termix_commit", "termix_version"})
_ALLOWED_ROOT_ENTRIES = frozenset(
    {
        ".env",
        ".opk",
        ".temp",
        "db.sqlite.encrypted",
        "opkssh",
        "uploads",
    }
)
_REQUIRED_TABLES = frozenset(
    {
        "api_keys",
        "sessions",
        "settings",
        "snippets",
        "ssh_credentials",
        "ssh_data",
        "trusted_devices",
        "users",
    }
)


@dataclass(frozen=True)
class FileEvidence:
    """Stable identity and content evidence for one authoritative source file."""

    device: int
    inode: int
    size_bytes: int
    modified_ns: int
    sha256: str


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _path_has_symlink(path: Path) -> bool:
    current = path
    while True:
        if current.is_symlink():
            return True
        if current == current.parent:
            return False
        current = current.parent


def _validate_regular_file(path: Path, *, label: str) -> None:
    if path.is_symlink() or not path.exists() or not path.is_file():
        raise RuntimeError(f"Termix {label} must be a regular non-symlink file")
    if not os.access(path, os.R_OK):
        raise PermissionError(f"Termix {label} is not readable")


def _validate_source_layout(
    data_path: Path,
    *,
    trusted_directory_handle: bool = False,
) -> tuple[Path, Path, Path | None]:
    if not data_path.exists() or not data_path.is_dir():
        raise FileNotFoundError("Termix data directory was not found")
    if not trusted_directory_handle and _path_has_symlink(data_path):
        raise RuntimeError("Termix data path must not contain a symbolic link")

    names = {entry.name for entry in data_path.iterdir()}
    if "db.sqlite" in names or "db.sqlite.encrypted.meta" in names:
        raise RuntimeError("Termix legacy or unencrypted database layout is unsupported")
    unexpected = names - _ALLOWED_ROOT_ENTRIES
    if unexpected:
        raise RuntimeError("Termix data directory contains unsupported persistent entries")

    for directory_name in ("opkssh", "uploads", ".temp"):
        candidate = data_path / directory_name
        if candidate.exists() and (candidate.is_symlink() or not candidate.is_dir()):
            raise RuntimeError(f"Termix {directory_name} entry has an unsupported type")

    env_path = data_path / ".env"
    database_path = data_path / "db.sqlite.encrypted"
    if not env_path.exists() or not database_path.exists():
        raise FileNotFoundError("Termix authoritative encrypted state was not found")
    _validate_regular_file(env_path, label="environment file")
    _validate_regular_file(database_path, label="encrypted database")

    opk_config: Path | None = None
    opk_path = data_path / ".opk"
    if opk_path.exists():
        if opk_path.is_symlink() or not opk_path.is_dir():
            raise RuntimeError("Termix .opk entry must be a regular directory")
        opk_names = {entry.name for entry in opk_path.iterdir()}
        if opk_names - {"config.yml"}:
            raise RuntimeError("Termix .opk directory contains unsupported persistent entries")
        candidate = opk_path / "config.yml"
        if candidate.exists():
            _validate_regular_file(candidate, label="OPKSSH configuration")
            opk_config = candidate
    return env_path, database_path, opk_config


def _read_database_key(env_path: Path) -> bytes:
    try:
        if env_path.stat().st_size > _MAX_ENV_BYTES:
            raise RuntimeError("Termix environment file is too large")
        content = env_path.read_text(encoding="utf-8")
    except UnicodeError as exc:
        raise RuntimeError("Termix environment file is not valid UTF-8") from exc
    except OSError as exc:
        raise RuntimeError("Termix environment file could not be read") from exc

    values = [
        line.removeprefix("DATABASE_KEY=")
        for line in content.splitlines()
        if line.startswith("DATABASE_KEY=")
    ]
    if len(values) != 1 or not _DATABASE_KEY_PATTERN.fullmatch(values[0]):
        raise RuntimeError("Termix DATABASE_KEY is missing or invalid")
    return bytes.fromhex(values[0])


def _load_envelope_metadata(source: Any) -> tuple[dict[str, Any], int]:
    length_bytes = source.read(4)
    if len(length_bytes) != 4:
        raise RuntimeError("Termix encrypted database envelope is malformed")
    metadata_length = int.from_bytes(length_bytes, byteorder="big")
    if metadata_length <= 0 or metadata_length > _MAX_ENVELOPE_METADATA_BYTES:
        raise RuntimeError("Termix encrypted database metadata length is invalid")
    payload = source.read(metadata_length)
    if len(payload) != metadata_length:
        raise RuntimeError("Termix encrypted database metadata is incomplete")
    try:
        metadata = json.loads(payload)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Termix encrypted database metadata is invalid") from exc
    if not isinstance(metadata, dict) or set(metadata) != {
        "algorithm",
        "dataSize",
        "fingerprint",
        "iv",
        "keySource",
        "tag",
        "version",
    }:
        raise RuntimeError("Termix encrypted database metadata contract is invalid")
    if (
        metadata.get("version") != "v2"
        or metadata.get("algorithm") != "aes-256-gcm"
        or metadata.get("fingerprint") != "termix-v2-systemcrypto"
        or metadata.get("keySource") != "SystemCrypto"
    ):
        raise RuntimeError("Termix encrypted database version or algorithm is unsupported")
    data_size = metadata.get("dataSize")
    if not isinstance(data_size, int) or isinstance(data_size, bool) or data_size <= 0:
        raise RuntimeError("Termix encrypted database data size is invalid")
    for field, byte_length in (("iv", 16), ("tag", 16)):
        value = metadata.get(field)
        if not isinstance(value, str):
            raise RuntimeError("Termix encrypted database cryptographic metadata is invalid")
        try:
            decoded = bytes.fromhex(value)
        except ValueError as exc:
            raise RuntimeError(
                "Termix encrypted database cryptographic metadata is invalid"
            ) from exc
        if len(decoded) != byte_length:
            raise RuntimeError("Termix encrypted database cryptographic metadata is invalid")
    return metadata, metadata_length


def _decrypt_database(database_path: Path, key: bytes, output_path: Path) -> None:
    try:
        with database_path.open("rb") as source:
            metadata, metadata_length = _load_envelope_metadata(source)
            expected_size = 4 + metadata_length + metadata["dataSize"]
            if database_path.stat().st_size != expected_size:
                raise RuntimeError("Termix encrypted database size does not match its metadata")
            decryptor = Cipher(
                algorithms.AES(key),
                modes.GCM(bytes.fromhex(metadata["iv"]), bytes.fromhex(metadata["tag"])),
            ).decryptor()
            descriptor = os.open(output_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(descriptor, "wb") as destination:
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        destination.write(decryptor.update(chunk))
                    destination.write(decryptor.finalize())
                    destination.flush()
                    os.fsync(destination.fileno())
            except BaseException:
                output_path.unlink(missing_ok=True)
                raise
    except InvalidTag as exc:
        raise RuntimeError("Termix encrypted database could not authenticate or decrypt") from exc
    except RuntimeError:
        raise
    except OSError as exc:
        raise RuntimeError("Termix encrypted database could not be read") from exc


def _validate_sqlite_database(path: Path) -> None:
    try:
        connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
        try:
            quick_check = connection.execute("PRAGMA quick_check").fetchone()
            if quick_check != ("ok",):
                raise RuntimeError("Termix SQLite database failed integrity validation")
            foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
            if foreign_keys:
                raise RuntimeError("Termix SQLite database failed foreign-key validation")
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            missing = sorted(_REQUIRED_TABLES - tables)
            if missing:
                raise RuntimeError(
                    f"Termix SQLite database is missing required tables: {', '.join(missing)}"
                )
        finally:
            connection.close()
    except RuntimeError:
        raise
    except sqlite3.DatabaseError as exc:
        raise RuntimeError("Termix SQLite database could not be validated") from exc


def _validate_source_state(
    data_path: Path,
    validation_root: Path,
    *,
    trusted_directory_handle: bool = False,
) -> None:
    env_path, database_path, _opk_config = _validate_source_layout(
        data_path,
        trusted_directory_handle=trusted_directory_handle,
    )
    key = _read_database_key(env_path)
    validation_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if validation_root.stat().st_mode & 0o077:
        raise PermissionError("Termix validation directory permissions are not private")
    temporary_path = validation_root / f"decrypted-{uuid.uuid4().hex}.db"
    try:
        _decrypt_database(database_path, key, temporary_path)
        _validate_sqlite_database(temporary_path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _authoritative_paths(data_path: Path) -> dict[str, Path]:
    env_path, database_path, opk_config = _validate_source_layout(data_path)
    paths = {
        ".env": env_path,
        "db.sqlite.encrypted": database_path,
    }
    if opk_config is not None:
        paths[".opk/config.yml"] = opk_config
    return paths


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_evidence(path: Path) -> FileEvidence:
    before = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode) or path.is_symlink():
        raise RuntimeError("Termix authoritative state changed to an unsafe file type")
    digest = _sha256_file(path)
    after = path.stat(follow_symlinks=False)
    before_identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    after_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if before_identity != after_identity:
        raise RuntimeError("Termix authoritative state changed while it was being read")
    return FileEvidence(
        device=after.st_dev,
        inode=after.st_ino,
        size_bytes=after.st_size,
        modified_ns=after.st_mtime_ns,
        sha256=digest,
    )


def _state_evidence(data_path: Path) -> dict[str, FileEvidence]:
    return {name: _file_evidence(path) for name, path in _authoritative_paths(data_path).items()}


def _private_zip_member(name: str) -> zipfile.ZipInfo:
    member = zipfile.ZipInfo(name)
    member.create_system = 3
    member.external_attr = (stat.S_IFREG | 0o600) << 16
    member.compress_type = zipfile.ZIP_DEFLATED
    return member


def _write_archive(
    data_path: Path,
    archive_path: Path,
    evidence: dict[str, FileEvidence],
) -> None:
    paths = _authoritative_paths(data_path)
    if set(paths) != set(evidence):
        raise RuntimeError("Termix authoritative state changed while preparing the archive")
    manifest = {
        "format_version": 1,
        "plugin": "termix",
        "termix_version": TERMIX_VERSION,
        "termix_commit": TERMIX_COMMIT,
        "files": {
            name: {
                "size_bytes": evidence[name].size_bytes,
                "sha256": evidence[name].sha256,
                "mode": 0o600,
            }
            for name in sorted(paths)
        },
    }
    try:
        descriptor = os.open(archive_path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w+b") as archive_file:
            with zipfile.ZipFile(
                archive_file,
                mode="w",
                compression=zipfile.ZIP_DEFLATED,
                compresslevel=6,
                allowZip64=True,
            ) as archive:
                archive.writestr(
                    _private_zip_member("manifest.json"),
                    json.dumps(manifest, sort_keys=True, separators=(",", ":")),
                )
                for name, path in sorted(paths.items()):
                    with (
                        path.open("rb") as source,
                        archive.open(_private_zip_member(name), "w") as target,
                    ):
                        shutil.copyfileobj(source, target, length=1024 * 1024)
            archive_file.flush()
            os.fsync(archive_file.fileno())
    except BaseException:
        archive_path.unlink(missing_ok=True)
        raise


def _validate_digest(value: Any) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError("Termix archive contains an invalid digest")
    return value


def _load_archive_contract(
    artifact_path: Path,
) -> tuple[dict[str, Any], dict[str, zipfile.ZipInfo]]:
    if artifact_path.is_symlink() or not artifact_path.exists() or not artifact_path.is_file():
        raise ValueError("Termix restore artifact is not a regular file")
    if artifact_path.stat().st_mode & 0o077:
        raise PermissionError("Termix restore artifact permissions are not private")
    try:
        with zipfile.ZipFile(artifact_path) as archive:
            members = archive.infolist()
            names = [member.filename for member in members]
            if len(names) != len(set(names)):
                raise ValueError("Termix archive contains a duplicate member")
            by_name = {member.filename: member for member in members}
            payload_names = set(names) - {"manifest.json"}
            if (
                "manifest.json" not in by_name
                or not _REQUIRED_ARCHIVE_FILES.issubset(payload_names)
                or payload_names - _REQUIRED_ARCHIVE_FILES - _OPTIONAL_ARCHIVE_FILES
            ):
                raise ValueError("Termix archive member contract is invalid")
            for member in members:
                if (
                    member.filename.startswith("/")
                    or ".." in PurePath(member.filename).parts
                    or "\\" in member.filename
                ):
                    raise ValueError("Termix archive contains an unsafe path")
                mode = member.external_attr >> 16
                if not stat.S_ISREG(mode) or stat.S_IMODE(mode) != 0o600:
                    raise ValueError("Termix archive contains an unsafe member type or mode")
                if member.file_size < 0 or member.compress_size < 0:
                    raise ValueError("Termix archive member size is invalid")
                if (
                    member.file_size > 0
                    and member.compress_size == 0
                    or member.compress_size > 0
                    and member.file_size > member.compress_size * _MAX_COMPRESSION_RATIO
                ):
                    raise ValueError("Termix archive compression ratio is unsafe")
            if by_name["manifest.json"].file_size > _MAX_MANIFEST_BYTES:
                raise ValueError("Termix archive manifest is too large")
            if by_name[".env"].file_size <= 0 or by_name[".env"].file_size > _MAX_ENV_BYTES:
                raise ValueError("Termix archive environment file size is invalid")
            database_size = by_name["db.sqlite.encrypted"].file_size
            if database_size <= 0 or database_size > _MAX_DATABASE_BYTES:
                raise ValueError("Termix archive database size is invalid")
            if ".opk/config.yml" in by_name and (
                by_name[".opk/config.yml"].file_size > _MAX_OPK_CONFIG_BYTES
            ):
                raise ValueError("Termix archive OPKSSH configuration is too large")
            if archive.testzip() is not None:
                raise ValueError("Termix archive failed CRC validation")
            manifest_payload = archive.read("manifest.json")
    except ValueError:
        raise
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        raise ValueError("Termix restore artifact is not a valid ZIP archive") from exc

    try:
        manifest = json.loads(manifest_payload)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("Termix archive manifest is not valid JSON") from exc
    if not isinstance(manifest, dict) or set(manifest) != _MANIFEST_KEYS:
        raise ValueError("Termix archive manifest contract is invalid")
    if (
        manifest.get("format_version") != 1
        or manifest.get("plugin") != "termix"
        or manifest.get("termix_version") != TERMIX_VERSION
        or manifest.get("termix_commit") != TERMIX_COMMIT
    ):
        raise ValueError("Termix archive version contract does not match")
    files = manifest.get("files")
    payload_names = set(by_name) - {"manifest.json"}
    if not isinstance(files, dict) or set(files) != payload_names:
        raise ValueError("Termix archive file manifest does not match its members")
    for name, details in files.items():
        if not isinstance(details, dict) or set(details) != {"mode", "sha256", "size_bytes"}:
            raise ValueError("Termix archive file metadata is invalid")
        if details.get("mode") != 0o600:
            raise ValueError("Termix archive file mode is invalid")
        size_bytes = details.get("size_bytes")
        if (
            not isinstance(size_bytes, int)
            or isinstance(size_bytes, bool)
            or size_bytes != by_name[name].file_size
        ):
            raise ValueError("Termix archive file size does not match")
        _validate_digest(details.get("sha256"))
    return manifest, by_name


def _extract_and_validate_archive(
    artifact_path: Path,
    destination: Path,
    validation_root: Path,
    *,
    destination_precreated: bool = False,
    trusted_directory_handle: bool = False,
    cleanup_on_error: bool = True,
) -> None:
    manifest, _members = _load_archive_contract(artifact_path)
    if destination_precreated:
        if (
            (not trusted_directory_handle and destination.is_symlink())
            or not destination.is_dir()
            or any(destination.iterdir())
            or destination.stat().st_mode & 0o077
        ):
            raise ValueError("Termix restore staging directory is unsafe")
    else:
        destination.mkdir(mode=0o700)
    try:
        with zipfile.ZipFile(artifact_path) as archive:
            for name, details in sorted(manifest["files"].items()):
                output_path = destination / name
                if name == ".opk/config.yml":
                    (destination / ".opk").mkdir(mode=0o700)
                output_path.parent.mkdir(mode=0o700, exist_ok=True)
                descriptor = os.open(output_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                digest = hashlib.sha256()
                size_bytes = 0
                with archive.open(name) as source, os.fdopen(descriptor, "wb") as target:
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        target.write(chunk)
                        digest.update(chunk)
                        size_bytes += len(chunk)
                    target.flush()
                    os.fsync(target.fileno())
                if size_bytes != details["size_bytes"] or digest.hexdigest() != details["sha256"]:
                    raise ValueError("Termix archive payload does not match its manifest")
                if output_path.stat().st_mode & 0o077:
                    raise PermissionError("Termix restored file permissions are not private")
        _validate_source_state(
            destination,
            validation_root,
            trusted_directory_handle=destination_precreated,
        )
    except BaseException:
        if cleanup_on_error:
            shutil.rmtree(destination, ignore_errors=True)
        raise


def _create_stable_archive(
    data_path: Path,
    archive_path: Path,
    validation_root: Path,
) -> None:
    last_error: RuntimeError | None = None
    for _attempt in range(_STABLE_READ_ATTEMPTS):
        try:
            before = _state_evidence(data_path)
            time.sleep(_STABLE_SETTLE_SECONDS)
            after = _state_evidence(data_path)
            if before != after:
                raise RuntimeError("Termix authoritative state changed before snapshot")
            _write_archive(data_path, archive_path, after)
            final = _state_evidence(data_path)
            if after != final:
                archive_path.unlink(missing_ok=True)
                raise RuntimeError("Termix authoritative state changed during snapshot")
            extracted_state = validation_root / "archive-state"
            shutil.rmtree(extracted_state, ignore_errors=True)
            _extract_and_validate_archive(
                archive_path,
                extracted_state,
                validation_root,
            )
            shutil.rmtree(extracted_state)
            return
        except RuntimeError as exc:
            archive_path.unlink(missing_ok=True)
            last_error = exc
            if "changed" not in str(exc):
                raise
    raise RuntimeError("Termix authoritative state did not become stable") from last_error


def _worker_error_kind(exc: BaseException) -> str:
    if isinstance(exc, FileNotFoundError):
        return "file-not-found"
    if isinstance(exc, PermissionError):
        return "permission"
    if isinstance(exc, ValueError):
        return "value"
    if isinstance(exc, TimeoutError):
        return "timeout"
    return "runtime"


def _create_private_temp_directory(*, prefix: str, parent: Path | None = None) -> Path:
    path = Path(tempfile.mkdtemp(prefix=prefix, dir=parent))
    os.chmod(path, 0o700)
    if path.stat().st_mode & 0o077:
        raise PermissionError("Termix temporary directory permissions are not private")
    return path


def _remove_private_temp_directory(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=False)
    if path.exists():
        raise RuntimeError("Termix sensitive temporary directory could not be removed")


def _backup_process_worker(
    data_path: Path,
    archive_path: Path,
    validation_root: Path,
    connection: Connection,
) -> None:
    try:
        _create_stable_archive(data_path, archive_path, validation_root)
        connection.send(("ok", ""))
    except BaseException as exc:
        archive_path.unlink(missing_ok=True)
        try:
            connection.send((_worker_error_kind(exc), str(exc)))
        except (BrokenPipeError, EOFError, OSError):
            pass
        raise SystemExit(1) from None
    finally:
        connection.close()


def _validation_process_worker(
    data_path: Path,
    validation_root: Path,
    connection: Connection,
) -> None:
    try:
        _validate_source_state(data_path, validation_root)
        connection.send(("ok", ""))
    except BaseException as exc:
        try:
            connection.send((_worker_error_kind(exc), str(exc)))
        except (BrokenPipeError, EOFError, OSError):
            pass
        raise SystemExit(1) from None
    finally:
        connection.close()


def _start_validation_process(
    data_path: Path,
    validation_root: Path,
) -> tuple[BaseProcess, Connection]:
    context = multiprocessing.get_context("spawn")
    receiving, sending = context.Pipe(duplex=False)
    process = context.Process(
        target=_validation_process_worker,
        args=(data_path, validation_root, sending),
        name="termix-validation",
        daemon=True,
    )
    process.start()
    sending.close()
    return process, receiving


def _start_backup_process(
    data_path: Path,
    archive_path: Path,
    validation_root: Path,
) -> tuple[BaseProcess, Connection]:
    context = multiprocessing.get_context("spawn")
    receiving, sending = context.Pipe(duplex=False)
    process = context.Process(
        target=_backup_process_worker,
        args=(data_path, archive_path, validation_root, sending),
        name="termix-backup",
        daemon=True,
    )
    process.start()
    sending.close()
    return process, receiving


async def _join_worker_process(process: BaseProcess, timeout_seconds: float) -> None:
    await asyncio.to_thread(process.join, timeout_seconds)


async def _stop_worker_process(process: BaseProcess, *, operation: str) -> None:
    if not process.is_alive():
        await _join_worker_process(process, _WORKER_STOP_TIMEOUT_SECONDS)
        if process.exitcode is None:
            raise RuntimeError(f"Termix {operation} worker could not be reaped")
        return
    process.terminate()
    await _join_worker_process(process, _WORKER_STOP_TIMEOUT_SECONDS)
    if process.is_alive():
        process.kill()
        await _join_worker_process(process, _WORKER_STOP_TIMEOUT_SECONDS)
    if process.is_alive() or process.exitcode is None:
        raise RuntimeError(f"Termix {operation} worker could not be stopped")


async def _stop_worker_process_before_return(
    process: BaseProcess,
    *,
    operation: str,
) -> None:
    stop_task = asyncio.create_task(_stop_worker_process(process, operation=operation))
    cancellation_seen = False
    while not stop_task.done():
        try:
            await asyncio.shield(stop_task)
        except asyncio.CancelledError:
            cancellation_seen = True
    stop_task.result()
    if cancellation_seen:
        raise asyncio.CancelledError


def _raise_worker_result(result: tuple[str, str] | None, *, operation: str) -> None:
    if result is None:
        raise RuntimeError(f"Termix {operation} worker returned no result")
    kind, message = result
    if kind == "ok":
        return
    safe_message = message or f"Termix {operation} failed"
    if kind == "file-not-found":
        raise FileNotFoundError(safe_message)
    if kind == "permission":
        raise PermissionError(safe_message)
    if kind == "value":
        raise ValueError(safe_message)
    if kind == "timeout":
        raise TimeoutError(safe_message)
    raise RuntimeError(safe_message)


async def _await_worker(
    process: BaseProcess,
    connection: Connection,
    *,
    operation: str,
    timeout_seconds: float,
) -> None:
    try:
        await _join_worker_process(process, timeout_seconds)
        if process.is_alive():
            await _stop_worker_process_before_return(process, operation=operation)
            raise TimeoutError(f"Termix {operation} timed out")
        result: tuple[str, str] | None = None
        if connection.poll():
            received = connection.recv()
            if (
                isinstance(received, tuple)
                and len(received) == 2
                and all(isinstance(value, str) for value in received)
            ):
                result = received
        _raise_worker_result(result, operation=operation)
        if process.exitcode != 0:
            raise RuntimeError(f"Termix {operation} worker failed")
    except asyncio.CancelledError:
        await _stop_worker_process_before_return(process, operation=operation)
        raise
    except BaseException:
        if process.is_alive():
            await _stop_worker_process_before_return(process, operation=operation)
        raise
    finally:
        connection.close()


def _validate_restore_path_location(path: Path) -> None:
    resolved = path.resolve(strict=False)
    for root in (*_FORBIDDEN_RESTORE_ROOTS, Path(BACKUP_BASE_PATH)):
        if _is_relative_to(resolved, root.resolve(strict=False)):
            raise ValueError("Termix restore destination uses a forbidden live or backup path")


def _open_validated_restore_parent(data_path: Path, artifact_path: Path) -> int:
    _validate_restore_path_location(data_path)
    if _path_has_symlink(data_path):
        raise ValueError("Termix restore destination must not contain a symbolic link")
    if data_path.exists():
        raise ValueError("Termix restore destination already exists")
    parent = data_path.parent
    if not parent.exists() or not parent.is_dir():
        raise ValueError("Termix restore destination parent was not found")
    flags = os.O_RDONLY | os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        parent_fd = os.open(parent, flags)
    except OSError as exc:
        raise ValueError("Termix restore destination parent is unsafe") from exc
    try:
        if os.listdir(parent_fd) != [RESTORE_SENTINEL_NAME]:
            raise ValueError("Termix restore destination parent must contain only its sentinel")
        sentinel_flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            sentinel_flags |= os.O_NOFOLLOW
        try:
            sentinel_fd = os.open(RESTORE_SENTINEL_NAME, sentinel_flags, dir_fd=parent_fd)
        except OSError as exc:
            raise ValueError("Termix restore destination sentinel was not found") from exc
        try:
            sentinel_stat = os.fstat(sentinel_fd)
            if not stat.S_ISREG(sentinel_stat.st_mode):
                raise ValueError("Termix restore destination sentinel is invalid")
            with os.fdopen(os.dup(sentinel_fd), "r", encoding="utf-8") as sentinel_file:
                marker = sentinel_file.read()
        except (OSError, UnicodeError) as exc:
            raise ValueError("Termix restore destination sentinel is invalid") from exc
        finally:
            os.close(sentinel_fd)
        if marker != RESTORE_SENTINEL_CONTENT:
            raise ValueError("Termix restore destination sentinel is invalid")
        resolved_parent = parent.resolve(strict=True)
        resolved_artifact = artifact_path.resolve(strict=True)
        if _is_relative_to(resolved_parent, resolved_artifact.parent) or _is_relative_to(
            resolved_artifact,
            resolved_parent,
        ):
            raise ValueError("Termix restore destination overlaps its backup artifact")
        if not os.access(parent, os.W_OK):
            raise PermissionError("Termix restore destination parent is not writable")
        return parent_fd
    except BaseException:
        os.close(parent_fd)
        raise


def _rename_directory_no_replace(
    parent_fd: int,
    source_name: str,
    destination_name: str,
) -> None:
    library = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(library, "renameat2", None)
    if renameat2 is None:
        raise RuntimeError("Termix create-only restore requires Linux renameat2")
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        parent_fd,
        os.fsencode(source_name),
        parent_fd,
        os.fsencode(destination_name),
        1,
    )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number == errno.EEXIST:
        raise ValueError("Termix restore destination already exists")
    raise OSError(error_number, os.strerror(error_number), destination_name)


def _clear_directory_fd(directory_fd: int) -> None:
    for name in os.listdir(directory_fd):
        entry_stat = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISDIR(entry_stat.st_mode):
            flags = os.O_RDONLY | os.O_DIRECTORY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            child_fd = os.open(name, flags, dir_fd=directory_fd)
            try:
                _clear_directory_fd(child_fd)
            finally:
                os.close(child_fd)
            os.rmdir(name, dir_fd=directory_fd)
        else:
            os.unlink(name, dir_fd=directory_fd)


def _remove_owned_restore_directory(
    parent_fd: int,
    owned_fd: int,
    *,
    expected_identity: tuple[int, int],
    candidate_names: tuple[str, ...],
) -> None:
    _clear_directory_fd(owned_fd)
    for name in candidate_names:
        try:
            candidate = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        if (candidate.st_dev, candidate.st_ino) == expected_identity:
            os.rmdir(name, dir_fd=parent_fd)
            return


def _require_owned_directory_name(
    parent_fd: int,
    name: str,
    expected_identity: tuple[int, int],
) -> None:
    candidate = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if (
        not stat.S_ISDIR(candidate.st_mode)
        or (
            candidate.st_dev,
            candidate.st_ino,
        )
        != expected_identity
    ):
        raise ValueError("Termix restore-owned directory changed")


def _restore_process_worker(
    artifact_path: Path,
    parent_path: Path,
    expected_parent_identity: tuple[int, int],
    staging_name: str,
    expected_staging_identity: tuple[int, int],
    destination_name: str,
    validation_name: str,
    expected_validation_identity: tuple[int, int],
    connection: Connection,
) -> None:
    parent_fd: int | None = None
    staging_fd: int | None = None
    validation_fd: int | None = None
    try:
        flags = os.O_RDONLY | os.O_DIRECTORY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        parent_fd = os.open(parent_path, flags)
        parent_stat = os.fstat(parent_fd)
        if (parent_stat.st_dev, parent_stat.st_ino) != expected_parent_identity:
            raise ValueError("Termix restore destination parent changed")
        directory_flags = os.O_RDONLY | os.O_DIRECTORY
        if hasattr(os, "O_NOFOLLOW"):
            directory_flags |= os.O_NOFOLLOW
        staging_fd = os.open(staging_name, directory_flags, dir_fd=parent_fd)
        staging_stat = os.fstat(staging_fd)
        if (staging_stat.st_dev, staging_stat.st_ino) != expected_staging_identity:
            raise ValueError("Termix restore staging directory changed")
        validation_fd = os.open(validation_name, directory_flags, dir_fd=parent_fd)
        validation_stat = os.fstat(validation_fd)
        if (
            validation_stat.st_dev,
            validation_stat.st_ino,
        ) != expected_validation_identity:
            raise ValueError("Termix restore validation directory changed")
        staging_path = Path(f"/proc/self/fd/{staging_fd}")
        validation_root = Path(f"/proc/self/fd/{validation_fd}")
        _extract_and_validate_archive(
            artifact_path,
            staging_path,
            validation_root,
            destination_precreated=True,
            trusted_directory_handle=True,
            cleanup_on_error=False,
        )
        _require_owned_directory_name(parent_fd, staging_name, expected_staging_identity)
        _rename_directory_no_replace(parent_fd, staging_name, destination_name)
        _require_owned_directory_name(
            parent_fd,
            destination_name,
            expected_staging_identity,
        )
        _validate_source_state(
            staging_path,
            validation_root,
            trusted_directory_handle=True,
        )
        connection.send(("ok", ""))
    except BaseException as exc:
        try:
            connection.send((_worker_error_kind(exc), str(exc)))
        except (BrokenPipeError, EOFError, OSError):
            pass
        raise SystemExit(1) from None
    finally:
        if validation_fd is not None:
            os.close(validation_fd)
        if staging_fd is not None:
            os.close(staging_fd)
        if parent_fd is not None:
            os.close(parent_fd)
        connection.close()


def _start_restore_process(
    artifact_path: Path,
    parent_path: Path,
    expected_parent_identity: tuple[int, int],
    staging_name: str,
    expected_staging_identity: tuple[int, int],
    destination_name: str,
    validation_name: str,
    expected_validation_identity: tuple[int, int],
) -> tuple[BaseProcess, Connection]:
    context = multiprocessing.get_context("spawn")
    receiving, sending = context.Pipe(duplex=False)
    process = context.Process(
        target=_restore_process_worker,
        args=(
            artifact_path,
            parent_path,
            expected_parent_identity,
            staging_name,
            expected_staging_identity,
            destination_name,
            validation_name,
            expected_validation_identity,
            sending,
        ),
        name="termix-restore",
        daemon=True,
    )
    process.start()
    sending.close()
    return process, receiving


async def _restore_archive(artifact_path: Path, data_path: Path, parent_fd: int) -> None:
    unique_part = uuid.uuid4().hex
    staging_name = f".{data_path.name}.{unique_part}.restore.tmp"
    os.mkdir(staging_name, mode=0o700, dir_fd=parent_fd)
    staging_fd: int | None = None
    staging_identity: tuple[int, int] | None = None
    validation_root: Path | None = None
    validation_fd: int | None = None
    validation_identity: tuple[int, int] | None = None
    process: BaseProcess | None = None
    succeeded = False
    try:
        directory_flags = os.O_RDONLY | os.O_DIRECTORY
        if hasattr(os, "O_NOFOLLOW"):
            directory_flags |= os.O_NOFOLLOW
        staging_fd = os.open(staging_name, directory_flags, dir_fd=parent_fd)
        staging_stat = os.fstat(staging_fd)
        staging_identity = (staging_stat.st_dev, staging_stat.st_ino)
        parent_stat = os.fstat(parent_fd)
        parent_identity = (parent_stat.st_dev, parent_stat.st_ino)
        stable_parent = Path(f"/proc/self/fd/{parent_fd}")
        validation_root = _create_private_temp_directory(
            prefix=f".termix-restore-validation-{unique_part}-",
            parent=stable_parent,
        )
        validation_fd = os.open(
            validation_root.name,
            directory_flags,
            dir_fd=parent_fd,
        )
        validation_stat = os.fstat(validation_fd)
        validation_identity = (validation_stat.st_dev, validation_stat.st_ino)
        if os.listdir(validation_fd) or validation_stat.st_mode & 0o077:
            raise ValueError("Termix restore validation directory is unsafe")
        process, connection = _start_restore_process(
            artifact_path,
            data_path.parent,
            parent_identity,
            staging_name,
            staging_identity,
            data_path.name,
            validation_root.name,
            validation_identity,
        )
        await _await_worker(
            process,
            connection,
            operation="restore",
            timeout_seconds=_RESTORE_TIMEOUT_SECONDS,
        )
        _require_owned_directory_name(
            parent_fd,
            data_path.name,
            staging_identity,
        )
        succeeded = True
    finally:
        try:
            if process is None or not process.is_alive():
                if validation_fd is not None and validation_identity is not None:
                    _remove_owned_restore_directory(
                        parent_fd,
                        validation_fd,
                        expected_identity=validation_identity,
                        candidate_names=(
                            validation_root.name if validation_root is not None else "",
                        ),
                    )
                if succeeded and staging_fd is not None:
                    os.fsync(staging_fd)
                    os.fsync(parent_fd)
                elif staging_fd is not None and staging_identity is not None:
                    _remove_owned_restore_directory(
                        parent_fd,
                        staging_fd,
                        expected_identity=staging_identity,
                        candidate_names=(staging_name, data_path.name),
                    )
        finally:
            if validation_fd is not None:
                os.close(validation_fd)
            if staging_fd is not None:
                os.close(staging_fd)


async def _validate_source_state_with_deadline(data_path: Path) -> None:
    validation_root = _create_private_temp_directory(prefix="termix-validation-")
    process: BaseProcess | None = None
    try:
        process, connection = _start_validation_process(data_path, validation_root)
        await _await_worker(
            process,
            connection,
            operation="validation",
            timeout_seconds=_VALIDATION_TIMEOUT_SECONDS,
        )
    finally:
        if process is None or not process.is_alive():
            _remove_private_temp_directory(validation_root)


class TermixPlugin(BackupPlugin):
    """Snapshot and create-only restore of Termix 2.3.2 encrypted state."""

    restore_capability = "partial"

    async def validate_config(self, config: Dict[str, Any]) -> bool:
        if not isinstance(config, dict) or set(config) != {"data_path"}:
            return False
        raw_path = config.get("data_path")
        if not isinstance(raw_path, str) or not raw_path:
            return False
        path = Path(raw_path)
        if not path.is_absolute() or path == Path("/") or ".." in PurePath(raw_path).parts:
            return False
        return not _is_relative_to(path.resolve(strict=False), Path("/backups"))

    async def test(self, config: Dict[str, Any]) -> bool:
        if not await self.validate_config(config):
            raise ValueError("Invalid configuration: data_path is required")
        data_path = Path(config["data_path"])
        await _validate_source_state_with_deadline(data_path)
        return True

    async def backup(self, context: BackupContext) -> Dict[str, Any]:
        if not await self.validate_config(context.config):
            raise ValueError("Invalid Termix backup configuration")
        data_path = Path(context.config["data_path"])
        with create_backup_artifact(
            self,
            context,
            prefix="termix-state",
            suffix=".zip",
            backup_root=BACKUP_BASE_PATH,
        ) as artifact:
            validation_root = _create_private_temp_directory(
                prefix=".termix-backup-validation-",
                parent=artifact.temporary_path.parent,
            )
            process: BaseProcess | None = None
            try:
                process, connection = _start_backup_process(
                    data_path,
                    artifact.temporary_path,
                    validation_root,
                )
                await _await_worker(
                    process,
                    connection,
                    operation="backup",
                    timeout_seconds=_BACKUP_TIMEOUT_SECONDS,
                )
            finally:
                if process is None or not process.is_alive():
                    _remove_private_temp_directory(validation_root)
            if artifact.temporary_path.stat().st_mode & 0o077:
                raise PermissionError("Termix backup artifact permissions are not private")
        if artifact.final_path.stat().st_mode & 0o077:
            Path(f"{artifact.final_path}.meta.json").unlink(missing_ok=True)
            artifact.final_path.unlink(missing_ok=True)
            raise PermissionError("Termix backup artifact permissions are not private")
        return {"artifact_path": str(artifact.final_path)}

    async def restore(self, context: RestoreContext) -> Dict[str, Any]:
        if not await self.validate_config(context.config):
            raise ValueError("Invalid Termix restore configuration or forbidden backup path")
        artifact_path = Path(context.artifact_path)
        data_path = Path(context.config["data_path"])
        parent_fd = _open_validated_restore_parent(data_path, artifact_path)
        try:
            await _restore_archive(artifact_path, data_path, parent_fd)
        finally:
            os.close(parent_fd)
        return {
            "status": "partial",
            "message": (
                "Termix 2.3.2 state restored into an isolated data directory; "
                "application startup remains a manual local drill step"
            ),
            "restored_path": str(data_path),
        }

    async def get_status(self, context: BackupContext) -> Dict[str, Any]:
        try:
            await self.test(context.config)
        except (FileNotFoundError, PermissionError, RuntimeError, TimeoutError, ValueError):
            return {"status": "unknown"}
        return {"status": "ok"}
