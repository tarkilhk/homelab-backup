"""Audiobookshelf 2.36.0 control-plane backup and isolated restore."""

from __future__ import annotations

import asyncio
import ctypes
import errno
import hashlib
import json
import multiprocessing
import os
import shutil
import stat
import tempfile
import time
import uuid
import zipfile
from dataclasses import dataclass
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable
from urllib.parse import quote

import pysqlite3 as sqlite3  # type: ignore[import-untyped]

from app.core.plugins.artifacts import create_backup_artifact
from app.core.plugins.base import BackupContext, BackupPlugin, RestoreContext

BACKUP_BASE_PATH = "/backups"
CONFIG_RESTORE_SENTINEL = ".audiobookshelf-config-restore-destination"
METADATA_RESTORE_SENTINEL = ".audiobookshelf-metadata-restore-destination"
RESTORE_SENTINEL_CONTENT = "audiobookshelf-v2.36.0-isolated-restore-v1\n"
SERVER_VERSION = "2.36.0"

BACKUP_TIMEOUT_SECONDS = 180.0
VALIDATION_TIMEOUT_SECONDS = 60.0
RESTORE_TIMEOUT_SECONDS = 180.0
PROCESS_STOP_TIMEOUT_SECONDS = 5.0
_WORKER_TEST_DELAY_SECONDS = 0.0
_MAX_STABLE_ATTEMPTS = 3
_MAX_ARCHIVE_MEMBERS = 100_000
_MAX_ARCHIVE_BYTES = 4 * 1024 * 1024 * 1024
_MAX_COMPRESSION_RATIO = 200

_FORBIDDEN_RESTORE_ROOTS = (
    Path("/backups"),
    Path("/sources/audiobookshelf"),
    Path("/config"),
    Path("/metadata"),
)

_REQUIRED_SCHEMA: dict[str, frozenset[str]] = {
    "migrationsMeta": frozenset({"key", "value"}),
    "users": frozenset({"id", "username", "pash", "type", "token", "isActive"}),
    "settings": frozenset({"key", "value"}),
    "libraries": frozenset({"id", "name", "mediaType", "provider"}),
    "libraryFolders": frozenset({"id", "libraryId", "path"}),
    "libraryItems": frozenset(
        {"id", "libraryId", "libraryFolderId", "mediaId", "mediaType", "path", "relPath"}
    ),
    "books": frozenset({"id", "coverPath"}),
    "podcasts": frozenset({"id", "coverPath"}),
    "authors": frozenset({"id", "imagePath"}),
    "feeds": frozenset({"id", "coverPath"}),
    "playbackSessions": frozenset({"id", "coverPath"}),
    "collections": frozenset({"id", "name"}),
    "playlists": frozenset({"id", "name"}),
    "mediaProgresses": frozenset({"id"}),
}
_REFERENCE_COLUMNS = (
    ("books", "coverPath"),
    ("podcasts", "coverPath"),
    ("authors", "imagePath"),
    ("feeds", "coverPath"),
    ("playbackSessions", "coverPath"),
)


@dataclass(frozen=True)
class FileEvidence:
    size: int
    modified_ns: int
    device: int
    inode: int


def _path_has_symlink(path: Path) -> bool:
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        try:
            if stat.S_ISLNK(current.lstat().st_mode):
                return True
        except FileNotFoundError:
            return False
    return False


def _require_absolute_path(value: Any, *, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Invalid configuration: {label} is required")
    path = Path(value)
    if not path.is_absolute():
        raise ValueError(f"Invalid configuration: {label} must be absolute")
    if _path_has_symlink(path):
        raise ValueError(f"Invalid configuration: {label} must not contain a symlink")
    return path


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _require_distinct_roots(config_path: Path, metadata_path: Path) -> None:
    resolved_config = config_path.resolve(strict=False)
    resolved_metadata = metadata_path.resolve(strict=False)
    if (
        resolved_config == resolved_metadata
        or _is_within(resolved_config, resolved_metadata)
        or _is_within(resolved_metadata, resolved_config)
    ):
        raise ValueError("Invalid configuration: source paths must be separate")


def _open_database_read_only(path: Path) -> sqlite3.Connection:
    if not path.exists() or not path.is_file() or path.is_symlink():
        raise FileNotFoundError("Audiobookshelf absdatabase.sqlite was not found")
    uri = f"file:{quote(str(path), safe='/')}?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=10.0)


def _table_columns(connection: sqlite3.Connection, table: str) -> frozenset[str]:
    escaped = table.replace('"', '""')
    return frozenset(row[1] for row in connection.execute(f'PRAGMA table_info("{escaped}")'))


def _reference_manifest(connection: sqlite3.Connection) -> frozenset[str]:
    references: set[str] = set()
    for table, column in _REFERENCE_COLUMNS:
        escaped_table = table.replace('"', '""')
        escaped_column = column.replace('"', '""')
        for (raw_value,) in connection.execute(
            f'SELECT "{escaped_column}" FROM "{escaped_table}" '
            f'WHERE "{escaped_column}" IS NOT NULL AND "{escaped_column}" != ""'
        ):
            if not isinstance(raw_value, str):
                raise ValueError("Audiobookshelf metadata reference has an invalid type")
            if raw_value.startswith("/metadata/items/"):
                relative = PurePosixPath(raw_value.removeprefix("/metadata/items/"))
                prefix = "metadata-items"
            elif raw_value.startswith("/metadata/authors/"):
                relative = PurePosixPath(raw_value.removeprefix("/metadata/authors/"))
                prefix = "metadata-authors"
            elif raw_value == "/audiobooks" or raw_value.startswith("/audiobooks/"):
                continue
            else:
                raise ValueError("Audiobookshelf database contains an unexpected metadata root")
            if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
                raise ValueError("Audiobookshelf metadata reference is unsafe")
            references.add(str(PurePosixPath(prefix) / relative))
    return frozenset(references)


def _validate_database(path: Path) -> frozenset[str]:
    with _open_database_read_only(path) as connection:
        quick_check = connection.execute("PRAGMA quick_check").fetchall()
        if quick_check != [("ok",)]:
            raise ValueError("Audiobookshelf database integrity check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError("Audiobookshelf database foreign-key check failed")
        for table, required_columns in _REQUIRED_SCHEMA.items():
            columns = _table_columns(connection, table)
            if not required_columns.issubset(columns):
                raise ValueError("Audiobookshelf database schema is not exact 2.36.0")
        versions = dict(connection.execute("SELECT key, value FROM migrationsMeta"))
        if (
            versions.get("version") != SERVER_VERSION
            or versions.get("maxVersion") != SERVER_VERSION
        ):
            raise ValueError(f"Audiobookshelf database must be exact {SERVER_VERSION}")
        root_count = connection.execute("SELECT COUNT(*) FROM users WHERE type = 'root'").fetchone()
        if root_count is None or int(root_count[0]) < 1:
            raise ValueError("Audiobookshelf database does not contain a root user")
        return _reference_manifest(connection)


def _snapshot_database(source: Path, destination: Path) -> None:
    with _open_database_read_only(source) as source_connection:
        with sqlite3.connect(destination) as destination_connection:
            source_connection.backup(destination_connection, pages=256, sleep=0.01)
    os.chmod(destination, 0o600)


def _metadata_files(root: Path) -> dict[str, FileEvidence]:
    if not root.exists() or not root.is_dir() or root.is_symlink():
        raise FileNotFoundError("Audiobookshelf metadata source directory was not found")
    evidence: dict[str, FileEvidence] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        status = path.lstat()
        if stat.S_ISLNK(status.st_mode):
            raise ValueError("Audiobookshelf metadata source contains a symlink")
        if path.is_dir():
            continue
        if not stat.S_ISREG(status.st_mode):
            raise ValueError("Audiobookshelf metadata source contains a special file")
        evidence[relative.as_posix()] = FileEvidence(
            size=status.st_size,
            modified_ns=status.st_mtime_ns,
            device=status.st_dev,
            inode=status.st_ino,
        )
    return evidence


def _copy_metadata_tree(source: Path, destination: Path) -> None:
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        status = path.lstat()
        if stat.S_ISLNK(status.st_mode):
            raise ValueError("Audiobookshelf metadata source contains a symlink")
        output = destination / relative
        if path.is_dir():
            output.mkdir(mode=0o700, exist_ok=True)
        elif stat.S_ISREG(status.st_mode):
            output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with path.open("rb") as input_file, output.open("xb") as output_file:
                shutil.copyfileobj(input_file, output_file, 1024 * 1024)
            os.chmod(output, 0o600)
        else:
            raise ValueError("Audiobookshelf metadata source contains a special file")


def _looks_like_image(payload: bytes) -> bool:
    return (
        payload.startswith(b"\x89PNG\r\n\x1a\n")
        or payload.startswith(b"\xff\xd8\xff")
        or (len(payload) >= 12 and payload[:4] in {b"RIFF", b"FORM"} and payload[8:12] == b"WEBP")
        or payload.startswith((b"GIF87a", b"GIF89a"))
    )


def _validate_staged_metadata(root: Path, references: Iterable[str]) -> None:
    for path in root.rglob("*"):
        if path.is_dir():
            continue
        if path.name == "metadata.json":
            try:
                json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("Audiobookshelf metadata.json is malformed") from exc
    for member_name in references:
        referenced = root / member_name
        if not referenced.exists() or not referenced.is_file() or referenced.is_symlink():
            raise ValueError("Audiobookshelf referenced metadata file is missing")
        payload = referenced.read_bytes()
        if not payload or not _looks_like_image(payload):
            raise ValueError("Audiobookshelf referenced metadata file is not a valid image")


def _archive_files(root: Path) -> Iterable[tuple[Path, str]]:
    for prefix in ("metadata-items", "metadata-authors"):
        subtree = root / prefix
        for path in sorted(subtree.rglob("*")):
            if path.is_file():
                yield path, f"{prefix}/{path.relative_to(subtree).as_posix()}"


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _write_private_archive(
    destination: Path,
    database: Path,
    metadata_root: Path,
) -> None:
    archived_files = [(database, "absdatabase.sqlite"), *_archive_files(metadata_root)]
    manifest_files = {
        name: {
            "size_bytes": path.stat().st_size,
            "sha256": _file_sha256(path),
        }
        for path, name in archived_files
    }
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w+b", closefd=False) as raw_file:
            with zipfile.ZipFile(raw_file, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.write(database, "absdatabase.sqlite")
                details = json.dumps(
                    {
                        "serverVersion": SERVER_VERSION,
                        "formatVersion": 1,
                        "producer": "homelab-backup",
                        "roots": ["metadata-items", "metadata-authors"],
                        "files": manifest_files,
                    },
                    separators=(",", ":"),
                ).encode("utf-8")
                archive.writestr("details", details)
                for path, name in archived_files[1:]:
                    archive.write(path, name)
            raw_file.flush()
            os.fsync(raw_file.fileno())
    finally:
        os.close(descriptor)
    os.chmod(destination, 0o600)


def _build_archive(
    config_path: Path,
    metadata_path: Path,
    destination: Path,
    workspace: Path,
) -> None:
    source_database = config_path / "absdatabase.sqlite"
    last_error: Exception | None = None
    for attempt in range(_MAX_STABLE_ATTEMPTS):
        attempt_root = workspace / f"attempt-{attempt}"
        attempt_root.mkdir(mode=0o700)
        first_database = attempt_root / "first.sqlite"
        second_database = attempt_root / "second.sqlite"
        staged_metadata = attempt_root / "metadata"
        try:
            _snapshot_database(source_database, first_database)
            references = _validate_database(first_database)
            before = {
                "metadata-items": _metadata_files(metadata_path / "items"),
                "metadata-authors": _metadata_files(metadata_path / "authors"),
            }
            _copy_metadata_tree(metadata_path / "items", staged_metadata / "metadata-items")
            _copy_metadata_tree(metadata_path / "authors", staged_metadata / "metadata-authors")
            after = {
                "metadata-items": _metadata_files(metadata_path / "items"),
                "metadata-authors": _metadata_files(metadata_path / "authors"),
            }
            _snapshot_database(source_database, second_database)
            second_references = _validate_database(second_database)
            if before != after or references != second_references:
                raise RuntimeError("Audiobookshelf source changed during backup")
            _validate_staged_metadata(staged_metadata, references)
            _write_private_archive(destination, first_database, staged_metadata)
            _validate_archive(destination, attempt_root / "archive-validation")
            return
        except RuntimeError as exc:
            last_error = exc
            shutil.rmtree(attempt_root, ignore_errors=True)
            continue
    raise RuntimeError("Audiobookshelf source did not stabilize for backup") from last_error


def _safe_member_name(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("Audiobookshelf archive contains an unsafe member")
    if path.parts[0] not in {"absdatabase.sqlite", "details", "metadata-items", "metadata-authors"}:
        raise ValueError("Audiobookshelf archive contains an unexpected member")
    if path.parts[0] in {"absdatabase.sqlite", "details"} and len(path.parts) != 1:
        raise ValueError("Audiobookshelf archive contains an unexpected member")
    return path


def _inspect_archive(artifact: Path) -> tuple[zipfile.ZipFile, list[zipfile.ZipInfo]]:
    try:
        archive = zipfile.ZipFile(artifact)
        members = archive.infolist()
    except (OSError, zipfile.BadZipFile) as exc:
        raise ValueError("Audiobookshelf artifact is not a valid archive") from exc
    names = [member.filename for member in members]
    if len(names) != len(set(names)):
        archive.close()
        raise ValueError("Audiobookshelf archive contains a duplicate member")
    if len(members) > _MAX_ARCHIVE_MEMBERS:
        archive.close()
        raise ValueError("Audiobookshelf archive contains too many members")
    total_size = 0
    for member in members:
        _safe_member_name(member.filename)
        mode = member.external_attr >> 16
        file_type = stat.S_IFMT(mode)
        if stat.S_ISLNK(mode) or file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
            archive.close()
            raise ValueError("Audiobookshelf archive contains an unsafe member type")
        total_size += member.file_size
        if member.file_size and member.compress_size == 0:
            archive.close()
            raise ValueError("Audiobookshelf archive has an invalid compression ratio")
        if (
            member.compress_size
            and member.file_size / member.compress_size > _MAX_COMPRESSION_RATIO
        ):
            archive.close()
            raise ValueError("Audiobookshelf archive has an unsafe compression ratio")
    if total_size > _MAX_ARCHIVE_BYTES:
        archive.close()
        raise ValueError("Audiobookshelf archive is too large")
    if "absdatabase.sqlite" not in names or "details" not in names:
        archive.close()
        raise ValueError("Audiobookshelf archive is incomplete")
    if archive.testzip() is not None:
        archive.close()
        raise ValueError("Audiobookshelf archive failed its CRC check")
    try:
        details = json.loads(archive.read("details"))
    except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        archive.close()
        raise ValueError("Audiobookshelf archive details are invalid") from exc
    if details.get("serverVersion") != SERVER_VERSION or details.get("formatVersion") != 1:
        archive.close()
        raise ValueError("Audiobookshelf archive version is incompatible")
    if details.get("roots") != ["metadata-items", "metadata-authors"]:
        archive.close()
        raise ValueError("Audiobookshelf archive metadata roots are invalid")
    file_manifest = details.get("files")
    payload_names = set(names) - {"details"}
    if not isinstance(file_manifest, dict) or set(file_manifest) != payload_names:
        archive.close()
        raise ValueError("Audiobookshelf archive manifest is incomplete")
    for name, expected in file_manifest.items():
        if not isinstance(expected, dict):
            archive.close()
            raise ValueError("Audiobookshelf archive manifest is invalid")
        digest = hashlib.sha256()
        observed_size = 0
        with archive.open(name) as payload:
            while chunk := payload.read(1024 * 1024):
                observed_size += len(chunk)
                digest.update(chunk)
        if expected != {
            "size_bytes": observed_size,
            "sha256": digest.hexdigest(),
        }:
            archive.close()
            raise ValueError("Audiobookshelf archive manifest does not match its payload")
    return archive, members


def _extract_archive(artifact: Path, destination: Path) -> None:
    archive, members = _inspect_archive(artifact)
    try:
        for member in members:
            relative = _safe_member_name(member.filename)
            output = destination.joinpath(*relative.parts)
            if member.is_dir():
                output.mkdir(mode=0o700, parents=True, exist_ok=True)
                continue
            output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as output_file, archive.open(member) as input_file:
                shutil.copyfileobj(input_file, output_file, 1024 * 1024)
                output_file.flush()
                os.fsync(output_file.fileno())
    finally:
        archive.close()


def _validate_archive(artifact: Path, root: Path) -> None:
    root.mkdir(mode=0o700)
    _extract_archive(artifact, root)
    references = _validate_database(root / "absdatabase.sqlite")
    _validate_staged_metadata(root, references)


def _validate_source(config_path: Path, metadata_path: Path) -> None:
    _validate_database(config_path / "absdatabase.sqlite")
    for subtree in (metadata_path / "items", metadata_path / "authors"):
        _metadata_files(subtree)


def _require_restore_destination(
    path: Path,
    *,
    sentinel_name: str,
    artifact: Path,
    other_destination: Path,
) -> None:
    if _path_has_symlink(path) or not path.exists() or not path.is_dir() or path.is_symlink():
        raise ValueError("Audiobookshelf restore destination is unsafe")
    resolved = path.resolve()
    for forbidden in _FORBIDDEN_RESTORE_ROOTS:
        forbidden_resolved = forbidden.resolve(strict=False)
        if resolved == forbidden_resolved or _is_within(resolved, forbidden_resolved):
            raise ValueError("Audiobookshelf restore destination is forbidden")
    artifact_resolved = artifact.resolve(strict=False)
    if resolved == artifact_resolved or _is_within(artifact_resolved, resolved):
        raise ValueError("Audiobookshelf restore artifact and destination overlap")
    other_resolved = other_destination.resolve(strict=False)
    if (
        resolved == other_resolved
        or _is_within(resolved, other_resolved)
        or _is_within(other_resolved, resolved)
    ):
        raise ValueError("Audiobookshelf restore destinations overlap")
    sentinel = path / sentinel_name
    if sentinel.is_symlink() or not sentinel.is_file():
        raise ValueError("Audiobookshelf restore destination sentinel is missing")
    if sentinel.read_text(encoding="utf-8") != RESTORE_SENTINEL_CONTENT:
        raise ValueError("Audiobookshelf restore destination sentinel is invalid")
    if {entry.name for entry in path.iterdir()} != {sentinel_name}:
        raise ValueError("Audiobookshelf restore destination must be empty")


def _remove_if_owned(path: Path, identity: tuple[int, int]) -> None:
    try:
        status = path.lstat()
    except FileNotFoundError:
        return
    if (status.st_dev, status.st_ino) != identity:
        return
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def _rename_directory_no_replace(path: Path, destination_name: str) -> None:
    parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        library = ctypes.CDLL(None, use_errno=True)
        renameat2 = getattr(library, "renameat2", None)
        if renameat2 is None:
            raise RuntimeError("Audiobookshelf restore requires Linux renameat2")
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
            os.fsencode(path.name),
            parent_fd,
            os.fsencode(destination_name),
            1,
        )
        if result == 0:
            return
        error_number = ctypes.get_errno()
        if error_number == errno.EEXIST:
            raise ValueError("Audiobookshelf restore destination already contains state")
        raise OSError(error_number, os.strerror(error_number))
    finally:
        os.close(parent_fd)


def _consume_restore_sentinels(config_path: Path, metadata_path: Path) -> None:
    sentinels = (
        config_path / CONFIG_RESTORE_SENTINEL,
        metadata_path / METADATA_RESTORE_SENTINEL,
    )
    preserved: list[tuple[Path, Path]] = []
    try:
        for sentinel in sentinels:
            preserved_path = sentinel.parent / f".{sentinel.name}.{uuid.uuid4().hex}.consumed"
            os.link(sentinel, preserved_path)
            preserved.append((sentinel, preserved_path))
            if not os.path.samefile(sentinel, preserved_path):
                raise RuntimeError("Audiobookshelf restore sentinel ownership changed")
            sentinel.unlink()
    except BaseException:
        for sentinel, preserved_path in reversed(preserved):
            try:
                os.link(preserved_path, sentinel)
            except FileExistsError:
                pass
        raise
    finally:
        for _, preserved_path in preserved:
            preserved_path.unlink(missing_ok=True)


def _restore_archive(
    artifact: Path,
    config_path: Path,
    metadata_path: Path,
    workspace: Path,
) -> None:
    _require_restore_destination(
        config_path,
        sentinel_name=CONFIG_RESTORE_SENTINEL,
        artifact=artifact,
        other_destination=metadata_path,
    )
    _require_restore_destination(
        metadata_path,
        sentinel_name=METADATA_RESTORE_SENTINEL,
        artifact=artifact,
        other_destination=config_path,
    )
    extracted = workspace / "extracted"
    extracted.mkdir(mode=0o700)
    _extract_archive(artifact, extracted)
    references = _validate_database(extracted / "absdatabase.sqlite")
    _validate_staged_metadata(extracted, references)

    staged_database = config_path / f".absdatabase.sqlite.{uuid.uuid4().hex}.tmp"
    staged_items = metadata_path / f".items.{uuid.uuid4().hex}.tmp"
    staged_authors = metadata_path / f".authors.{uuid.uuid4().hex}.tmp"
    published: list[tuple[Path, tuple[int, int]]] = []
    try:
        shutil.copy2(extracted / "absdatabase.sqlite", staged_database)
        os.chmod(staged_database, 0o600)
        shutil.copytree(extracted / "metadata-items", staged_items)
        shutil.copytree(extracted / "metadata-authors", staged_authors)
        database_identity = (staged_database.stat().st_dev, staged_database.stat().st_ino)
        items_identity = (staged_items.stat().st_dev, staged_items.stat().st_ino)
        authors_identity = (staged_authors.stat().st_dev, staged_authors.stat().st_ino)
        os.link(staged_database, config_path / "absdatabase.sqlite")
        published.append((config_path / "absdatabase.sqlite", database_identity))
        _rename_directory_no_replace(staged_items, "items")
        published.append((metadata_path / "items", items_identity))
        _rename_directory_no_replace(staged_authors, "authors")
        published.append((metadata_path / "authors", authors_identity))
        final_references = _validate_database(config_path / "absdatabase.sqlite")
        final_root = workspace / "final-view"
        final_root.mkdir(mode=0o700)
        os.symlink(metadata_path / "items", final_root / "metadata-items")
        os.symlink(metadata_path / "authors", final_root / "metadata-authors")
        _validate_staged_metadata(final_root, final_references)
        _consume_restore_sentinels(config_path, metadata_path)
    except BaseException:
        for path, identity in reversed(published):
            _remove_if_owned(path, identity)
        raise
    finally:
        staged_database.unlink(missing_ok=True)
        if staged_items.exists():
            shutil.rmtree(staged_items, ignore_errors=True)
        if staged_authors.exists():
            shutil.rmtree(staged_authors, ignore_errors=True)


def _worker_entry(
    connection: Connection,
    operation: str,
    arguments: tuple[str, ...],
    delay_seconds: float,
) -> None:
    try:
        if delay_seconds:
            time.sleep(delay_seconds)
        paths = tuple(Path(value) for value in arguments)
        if operation == "validate":
            _validate_source(paths[0], paths[1])
        elif operation == "backup":
            _build_archive(paths[0], paths[1], paths[2], paths[3])
        elif operation == "restore":
            _restore_archive(paths[0], paths[1], paths[2], paths[3])
        else:
            raise RuntimeError("Unsupported Audiobookshelf worker operation")
        connection.send(("ok", ""))
    except BaseException as exc:
        connection.send(("error", f"{type(exc).__name__}: {exc}"))
    finally:
        connection.close()


def _start_worker(operation: str, *paths: Path) -> tuple[BaseProcess, Connection]:
    process_context = multiprocessing.get_context("spawn")
    parent_connection, child_connection = process_context.Pipe(duplex=False)
    process = process_context.Process(
        target=_worker_entry,
        args=(
            child_connection,
            operation,
            tuple(str(path) for path in paths),
            _WORKER_TEST_DELAY_SECONDS,
        ),
        daemon=True,
    )
    process.start()
    child_connection.close()
    return process, parent_connection


def _stop_process(process: BaseProcess) -> None:
    if not process.is_alive():
        process.join(PROCESS_STOP_TIMEOUT_SECONDS)
        return
    process.terminate()
    process.join(PROCESS_STOP_TIMEOUT_SECONDS)
    if process.is_alive():
        process.kill()
        process.join(PROCESS_STOP_TIMEOUT_SECONDS)
    if process.is_alive():
        raise RuntimeError("Audiobookshelf worker could not be stopped")


async def _await_worker(
    process: BaseProcess,
    connection: Connection,
    *,
    operation: str,
    timeout_seconds: float,
) -> None:
    started = time.monotonic()
    operation_label = "validation" if operation == "validate" else operation
    try:
        while process.is_alive():
            if time.monotonic() - started >= timeout_seconds:
                _stop_process(process)
                raise TimeoutError(f"Audiobookshelf {operation_label} timed out")
            await asyncio.sleep(0.02)
        process.join(PROCESS_STOP_TIMEOUT_SECONDS)
        if not connection.poll():
            raise RuntimeError(f"Audiobookshelf {operation_label} worker returned no result")
        status, message = connection.recv()
        if status != "ok":
            if message.startswith("FileNotFoundError:"):
                raise FileNotFoundError(message.split(":", 1)[1].strip())
            raise ValueError(message.split(":", 1)[-1].strip())
        if process.exitcode != 0:
            raise RuntimeError(f"Audiobookshelf {operation_label} worker failed")
    except asyncio.CancelledError:
        _stop_process(process)
        raise
    finally:
        connection.close()


async def _run_worker(
    operation: str,
    *paths: Path,
    timeout_seconds: float,
) -> None:
    process, connection = _start_worker(operation, *paths)
    await _await_worker(
        process,
        connection,
        operation=operation,
        timeout_seconds=timeout_seconds,
    )


class AudiobookshelfPlugin(BackupPlugin):
    """Back up Audiobookshelf 2.36.0 state without application credentials."""

    restore_capability = "partial"

    async def validate_config(self, config: Dict[str, Any]) -> bool:
        if not isinstance(config, dict):
            return False
        if set(config) != {"config_path", "metadata_path"}:
            return False
        try:
            config_path = _require_absolute_path(config.get("config_path"), label="config_path")
            metadata_path = _require_absolute_path(
                config.get("metadata_path"), label="metadata_path"
            )
            _require_distinct_roots(config_path, metadata_path)
        except ValueError:
            return False
        return True

    async def test(self, config: Dict[str, Any]) -> bool:
        if not isinstance(config, dict):
            raise ValueError("Invalid configuration: config_path and metadata_path are required")
        config_path = _require_absolute_path(config.get("config_path"), label="config_path")
        metadata_path = _require_absolute_path(config.get("metadata_path"), label="metadata_path")
        _require_distinct_roots(config_path, metadata_path)
        await _run_worker(
            "validate",
            config_path,
            metadata_path,
            timeout_seconds=VALIDATION_TIMEOUT_SECONDS,
        )
        return True

    async def backup(self, context: BackupContext) -> Dict[str, Any]:
        if not await self.validate_config(context.config):
            raise ValueError("Invalid Audiobookshelf backup configuration")
        config_path = _require_absolute_path(context.config["config_path"], label="config_path")
        metadata_path = _require_absolute_path(
            context.config["metadata_path"], label="metadata_path"
        )
        with create_backup_artifact(
            self,
            context,
            prefix="audiobookshelf-control-plane",
            suffix=".audiobookshelf",
            backup_root=BACKUP_BASE_PATH,
        ) as artifact:
            workspace = Path(
                tempfile.mkdtemp(
                    prefix=".audiobookshelf-backup-",
                    dir=artifact.temporary_path.parent,
                )
            )
            os.chmod(workspace, 0o700)
            try:
                await _run_worker(
                    "backup",
                    config_path,
                    metadata_path,
                    artifact.temporary_path,
                    workspace,
                    timeout_seconds=BACKUP_TIMEOUT_SECONDS,
                )
            finally:
                shutil.rmtree(workspace, ignore_errors=True)
            if stat.S_IMODE(artifact.temporary_path.stat().st_mode) != 0o600:
                raise PermissionError("Audiobookshelf backup artifact is not private")
        return {"artifact_path": str(artifact.final_path)}

    async def restore(self, context: RestoreContext) -> Dict[str, Any]:
        if not await self.validate_config(context.config):
            raise ValueError("Invalid Audiobookshelf restore configuration")
        artifact = Path(context.artifact_path)
        config_path = _require_absolute_path(context.config["config_path"], label="config_path")
        metadata_path = _require_absolute_path(
            context.config["metadata_path"], label="metadata_path"
        )
        _require_restore_destination(
            config_path,
            sentinel_name=CONFIG_RESTORE_SENTINEL,
            artifact=artifact,
            other_destination=metadata_path,
        )
        _require_restore_destination(
            metadata_path,
            sentinel_name=METADATA_RESTORE_SENTINEL,
            artifact=artifact,
            other_destination=config_path,
        )
        workspace = Path(tempfile.mkdtemp(prefix="audiobookshelf-restore-"))
        os.chmod(workspace, 0o700)
        try:
            await _run_worker(
                "restore",
                artifact,
                config_path,
                metadata_path,
                workspace,
                timeout_seconds=RESTORE_TIMEOUT_SECONDS,
            )
        finally:
            shutil.rmtree(workspace, ignore_errors=True)
        return {
            "status": "partial",
            "message": (
                "Audiobookshelf 2.36.0 control-plane state was restored into isolated "
                "directories; exact-image startup and external media remain manual drill steps"
            ),
            "config_path": str(config_path),
            "metadata_path": str(metadata_path),
        }

    async def get_status(self, context: BackupContext) -> Dict[str, Any]:
        try:
            await self.test(context.config)
        except (FileNotFoundError, PermissionError, RuntimeError, TimeoutError, ValueError):
            return {"status": "unknown"}
        return {"status": "ok"}
