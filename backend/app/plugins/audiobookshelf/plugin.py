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
import warnings
import zipfile
from dataclasses import dataclass
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable
from urllib.parse import quote

import pysqlite3 as sqlite3  # type: ignore[import-untyped]
from PIL import Image, UnidentifiedImageError

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
_MAX_DETAILS_BYTES = 32 * 1024 * 1024
_MAX_METADATA_FILE_BYTES = 256 * 1024 * 1024
_MAX_METADATA_JSON_BYTES = 8 * 1024 * 1024
_MAX_IMAGE_BYTES = 64 * 1024 * 1024
_MAX_IMAGE_PIXELS = 25_000_000

_FORBIDDEN_RESTORE_ROOTS = (
    Path("/backups"),
    Path("/sources/audiobookshelf"),
    Path("/config"),
    Path("/metadata"),
)

_REQUIRED_SCHEMA: dict[str, frozenset[str]] = {
    "apiKeys": frozenset(
        {
            "id",
            "userId",
            "name",
            "description",
            "permissions",
            "isActive",
            "expiresAt",
            "lastUsedAt",
            "createdByUserId",
            "createdAt",
            "updatedAt",
        }
    ),
    "authors": frozenset(
        {
            "id",
            "libraryId",
            "name",
            "lastFirst",
            "asin",
            "description",
            "imagePath",
            "createdAt",
            "updatedAt",
        }
    ),
    "bookAuthors": frozenset({"id", "bookId", "authorId", "createdAt"}),
    "bookSeries": frozenset({"id", "bookId", "seriesId", "sequence", "createdAt"}),
    "books": frozenset(
        {
            "id",
            "title",
            "titleIgnorePrefix",
            "subtitle",
            "publishedYear",
            "publishedDate",
            "publisher",
            "description",
            "isbn",
            "asin",
            "language",
            "explicit",
            "abridged",
            "coverPath",
            "duration",
            "audioFiles",
            "chapters",
            "ebookFile",
            "genres",
            "tags",
            "narrators",
            "createdAt",
            "updatedAt",
        }
    ),
    "collectionBooks": frozenset({"id", "collectionId", "bookId", "order", "createdAt"}),
    "collections": frozenset({"id", "libraryId", "name", "description", "createdAt", "updatedAt"}),
    "customMetadataProviders": frozenset(
        {
            "id",
            "name",
            "mediaType",
            "url",
            "authHeaderValue",
            "extraData",
            "createdAt",
            "updatedAt",
        }
    ),
    "devices": frozenset(
        {
            "id",
            "userId",
            "deviceId",
            "clientName",
            "clientVersion",
            "deviceName",
            "deviceVersion",
            "ipAddress",
            "extraData",
            "createdAt",
            "updatedAt",
        }
    ),
    "feedEpisodes": frozenset(
        {
            "id",
            "feedId",
            "title",
            "description",
            "author",
            "episode",
            "season",
            "episodeType",
            "pubDate",
            "duration",
            "explicit",
            "enclosureURL",
            "enclosureType",
            "enclosureSize",
            "siteURL",
            "filePath",
            "createdAt",
            "updatedAt",
        }
    ),
    "feeds": frozenset(
        {
            "id",
            "userId",
            "entityId",
            "entityType",
            "slug",
            "title",
            "author",
            "description",
            "coverPath",
            "imageURL",
            "feedURL",
            "serverAddress",
            "language",
            "explicit",
            "preventIndexing",
            "ownerName",
            "ownerEmail",
            "siteURL",
            "podcastType",
            "entityUpdatedAt",
            "createdAt",
            "updatedAt",
        }
    ),
    "libraries": frozenset(
        {
            "id",
            "name",
            "mediaType",
            "provider",
            "icon",
            "settings",
            "lastScan",
            "lastScanVersion",
            "displayOrder",
            "extraData",
            "createdAt",
            "updatedAt",
        }
    ),
    "libraryFolders": frozenset({"id", "libraryId", "path", "createdAt", "updatedAt"}),
    "libraryItems": frozenset(
        {
            "id",
            "libraryId",
            "libraryFolderId",
            "mediaId",
            "mediaType",
            "path",
            "relPath",
            "libraryFiles",
            "title",
            "authorNamesFirstLast",
            "authorNamesLastFirst",
            "titleIgnorePrefix",
            "isFile",
            "mtime",
            "ctime",
            "birthtime",
            "ino",
            "size",
            "isMissing",
            "isInvalid",
            "lastScan",
            "lastScanVersion",
            "extraData",
            "createdAt",
            "updatedAt",
        }
    ),
    "mediaItemShares": frozenset(
        {
            "id",
            "userId",
            "slug",
            "pash",
            "mediaItemId",
            "mediaItemType",
            "isDownloadable",
            "expiresAt",
            "extraData",
            "createdAt",
            "updatedAt",
        }
    ),
    "mediaProgresses": frozenset(
        {
            "id",
            "userId",
            "mediaItemId",
            "mediaItemType",
            "podcastId",
            "duration",
            "currentTime",
            "ebookLocation",
            "ebookProgress",
            "isFinished",
            "finishedAt",
            "hideFromContinueListening",
            "extraData",
            "createdAt",
            "updatedAt",
        }
    ),
    "migrationsMeta": frozenset({"key", "value"}),
    "playbackSessions": frozenset(
        {
            "id",
            "userId",
            "libraryId",
            "mediaItemId",
            "mediaItemType",
            "mediaMetadata",
            "displayTitle",
            "displayAuthor",
            "coverPath",
            "duration",
            "playMethod",
            "mediaPlayer",
            "deviceId",
            "serverVersion",
            "date",
            "dayOfWeek",
            "timeListening",
            "startTime",
            "currentTime",
            "extraData",
            "createdAt",
            "updatedAt",
        }
    ),
    "playlistMediaItems": frozenset(
        {"id", "playlistId", "mediaItemId", "mediaItemType", "order", "createdAt"}
    ),
    "playlists": frozenset(
        {"id", "userId", "libraryId", "name", "description", "createdAt", "updatedAt"}
    ),
    "podcastEpisodes": frozenset(
        {
            "id",
            "podcastId",
            "index",
            "title",
            "subtitle",
            "description",
            "episode",
            "season",
            "episodeType",
            "publishedAt",
            "pubDate",
            "enclosureURL",
            "enclosureType",
            "enclosureSize",
            "audioFile",
            "chapters",
            "extraData",
            "createdAt",
            "updatedAt",
        }
    ),
    "podcasts": frozenset(
        {
            "id",
            "title",
            "titleIgnorePrefix",
            "author",
            "description",
            "releaseDate",
            "genres",
            "tags",
            "coverPath",
            "feedURL",
            "imageURL",
            "itunesPageURL",
            "itunesId",
            "itunesArtistId",
            "explicit",
            "language",
            "podcastType",
            "numEpisodes",
            "lastEpisodeCheck",
            "autoDownloadEpisodes",
            "autoDownloadSchedule",
            "maxEpisodesToKeep",
            "maxNewEpisodesToDownload",
            "createdAt",
            "updatedAt",
        }
    ),
    "series": frozenset(
        {
            "id",
            "libraryId",
            "name",
            "nameIgnorePrefix",
            "description",
            "createdAt",
            "updatedAt",
        }
    ),
    "sessions": frozenset(
        {
            "id",
            "userId",
            "refreshToken",
            "lastRefreshToken",
            "lastRefreshTokenExpiresAt",
            "ipAddress",
            "userAgent",
            "expiresAt",
            "createdAt",
            "updatedAt",
        }
    ),
    "settings": frozenset({"key", "value", "createdAt", "updatedAt"}),
    "users": frozenset(
        {
            "id",
            "username",
            "email",
            "pash",
            "type",
            "token",
            "isActive",
            "isLocked",
            "lastSeen",
            "permissions",
            "bookmarks",
            "extraData",
            "createdAt",
            "updatedAt",
        }
    ),
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
        observed_tables = frozenset(
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        )
        if observed_tables != frozenset(_REQUIRED_SCHEMA):
            raise ValueError("Audiobookshelf database schema is not exact 2.36.0")
        for table, required_columns in _REQUIRED_SCHEMA.items():
            columns = _table_columns(connection, table)
            if columns != required_columns:
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
        if len(evidence) > _MAX_ARCHIVE_MEMBERS:
            raise ValueError("Audiobookshelf metadata source contains too many files")
        if status.st_size > _MAX_METADATA_FILE_BYTES:
            raise ValueError("Audiobookshelf metadata source contains an oversized file")
    if sum(item.size for item in evidence.values()) > _MAX_ARCHIVE_BYTES:
        raise ValueError("Audiobookshelf metadata source is too large")
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
            input_descriptor = os.open(
                path,
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            )
            try:
                opened_status = os.fstat(input_descriptor)
                if (
                    _status_identity(opened_status) != _status_identity(status)
                    or opened_status.st_size != status.st_size
                    or opened_status.st_mtime_ns != status.st_mtime_ns
                ):
                    raise RuntimeError("Audiobookshelf source changed during backup")
                with (
                    os.fdopen(
                        input_descriptor,
                        "rb",
                        closefd=False,
                    ) as input_file,
                    output.open("xb") as output_file,
                ):
                    shutil.copyfileobj(input_file, output_file, 1024 * 1024)
            finally:
                os.close(input_descriptor)
            os.chmod(output, 0o600)
        else:
            raise ValueError("Audiobookshelf metadata source contains a special file")


def _validate_image(path: Path) -> None:
    size = path.stat().st_size
    if size <= 0 or size > _MAX_IMAGE_BYTES:
        raise ValueError("Audiobookshelf referenced metadata file is not a valid image")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            Image.MAX_IMAGE_PIXELS = _MAX_IMAGE_PIXELS
            with Image.open(path) as image:
                if image.format not in {"GIF", "JPEG", "PNG", "WEBP"}:
                    raise ValueError("Audiobookshelf referenced metadata file is not a valid image")
                image.verify()
            with Image.open(path) as image:
                image.load()
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        OSError,
        UnidentifiedImageError,
    ) as exc:
        raise ValueError("Audiobookshelf referenced metadata file is not a valid image") from exc


def _validate_staged_metadata(root: Path, references: Iterable[str]) -> None:
    for path in root.rglob("*"):
        if path.is_dir():
            continue
        if path.name == "metadata.json":
            if path.stat().st_size > _MAX_METADATA_JSON_BYTES:
                raise ValueError("Audiobookshelf metadata.json is too large")
            try:
                with path.open("r", encoding="utf-8") as metadata_file:
                    json.load(metadata_file)
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("Audiobookshelf metadata.json is malformed") from exc
    for member_name in references:
        referenced = root / member_name
        if not referenced.exists() or not referenced.is_file() or referenced.is_symlink():
            raise ValueError("Audiobookshelf referenced metadata file is missing")
        _validate_image(referenced)


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
                for root_name in ("metadata-items", "metadata-authors"):
                    directory = zipfile.ZipInfo(f"{root_name}/")
                    directory.external_attr = (stat.S_IFDIR | 0o700) << 16
                    archive.writestr(directory, b"")
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
        if member.filename == "details" and member.file_size > _MAX_DETAILS_BYTES:
            archive.close()
            raise ValueError("Audiobookshelf archive details are too large")
        if member.filename != "absdatabase.sqlite" and member.filename != "details":
            if member.file_size > _MAX_METADATA_FILE_BYTES:
                archive.close()
                raise ValueError("Audiobookshelf archive contains an oversized metadata file")
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
    payload_names = {member.filename for member in members if not member.is_dir()} - {"details"}
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


@dataclass(frozen=True)
class OpenRestoreDestination:
    path: Path
    file_descriptor: int
    identity: tuple[int, int]
    sentinel_name: str
    sentinel_identity: tuple[int, int]


@dataclass(frozen=True)
class RestoreStaging:
    database_name: str
    database_fd: int
    database_identity: tuple[int, int]
    items_name: str
    items_fd: int
    items_identity: tuple[int, int]
    authors_name: str
    authors_fd: int
    authors_identity: tuple[int, int]


def _status_identity(status: os.stat_result) -> tuple[int, int]:
    return status.st_dev, status.st_ino


def _stable_fd_path(file_descriptor: int, child_name: str = "") -> Path:
    base = Path(f"/proc/{os.getpid()}/fd/{file_descriptor}")
    return base / child_name if child_name else base


def _validate_restore_root_path(path: Path, artifact: Path) -> None:
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


def _open_restore_destination(
    path: Path,
    *,
    sentinel_name: str,
    artifact: Path,
) -> OpenRestoreDestination:
    _validate_restore_root_path(path, artifact)
    flags = os.O_RDONLY | os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    file_descriptor = os.open(path, flags)
    try:
        identity = _status_identity(os.fstat(file_descriptor))
        if os.listdir(file_descriptor) != [sentinel_name]:
            raise ValueError("Audiobookshelf restore destination must be empty")
        sentinel_status = os.stat(sentinel_name, dir_fd=file_descriptor, follow_symlinks=False)
        if not stat.S_ISREG(sentinel_status.st_mode):
            raise ValueError("Audiobookshelf restore destination sentinel is missing")
        sentinel_flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            sentinel_flags |= os.O_NOFOLLOW
        sentinel_fd = os.open(sentinel_name, sentinel_flags, dir_fd=file_descriptor)
        try:
            marker = os.read(sentinel_fd, len(RESTORE_SENTINEL_CONTENT.encode("utf-8")) + 1)
        finally:
            os.close(sentinel_fd)
        if marker != RESTORE_SENTINEL_CONTENT.encode("utf-8"):
            raise ValueError("Audiobookshelf restore destination sentinel is invalid")
        stable_root = _stable_fd_path(file_descriptor).resolve(strict=True)
        _validate_restore_root_path(stable_root, artifact)
        return OpenRestoreDestination(
            path=path,
            file_descriptor=file_descriptor,
            identity=identity,
            sentinel_name=sentinel_name,
            sentinel_identity=_status_identity(sentinel_status),
        )
    except BaseException:
        os.close(file_descriptor)
        raise


def _require_distinct_restore_destinations(
    config: OpenRestoreDestination,
    metadata: OpenRestoreDestination,
) -> None:
    if config.identity == metadata.identity:
        raise ValueError("Audiobookshelf restore destinations overlap")
    config_path = _stable_fd_path(config.file_descriptor).resolve(strict=True)
    metadata_path = _stable_fd_path(metadata.file_descriptor).resolve(strict=True)
    if _is_within(config_path, metadata_path) or _is_within(metadata_path, config_path):
        raise ValueError("Audiobookshelf restore destinations overlap")


def _require_destination_still_named(destination: OpenRestoreDestination) -> None:
    status = os.stat(destination.path, follow_symlinks=False)
    if _status_identity(status) != destination.identity:
        raise ValueError("Audiobookshelf restore destination changed during restore")


def _create_restore_staging(
    config: OpenRestoreDestination,
    metadata: OpenRestoreDestination,
) -> RestoreStaging:
    unique = uuid.uuid4().hex
    database_name = f".absdatabase.sqlite.{unique}.tmp"
    items_name = f".items.{unique}.tmp"
    authors_name = f".authors.{unique}.tmp"
    database_fd = os.open(
        database_name,
        os.O_RDWR | os.O_CREAT | os.O_EXCL,
        0o600,
        dir_fd=config.file_descriptor,
    )
    items_fd: int | None = None
    authors_fd: int | None = None
    try:
        os.mkdir(items_name, mode=0o700, dir_fd=metadata.file_descriptor)
        items_fd = os.open(
            items_name,
            os.O_RDONLY | os.O_DIRECTORY,
            dir_fd=metadata.file_descriptor,
        )
        os.mkdir(authors_name, mode=0o700, dir_fd=metadata.file_descriptor)
        authors_fd = os.open(
            authors_name,
            os.O_RDONLY | os.O_DIRECTORY,
            dir_fd=metadata.file_descriptor,
        )
        return RestoreStaging(
            database_name=database_name,
            database_fd=database_fd,
            database_identity=_status_identity(os.fstat(database_fd)),
            items_name=items_name,
            items_fd=items_fd,
            items_identity=_status_identity(os.fstat(items_fd)),
            authors_name=authors_name,
            authors_fd=authors_fd,
            authors_identity=_status_identity(os.fstat(authors_fd)),
        )
    except BaseException:
        if authors_fd is not None:
            os.close(authors_fd)
        if items_fd is not None:
            os.close(items_fd)
        os.close(database_fd)
        try:
            os.rmdir(authors_name, dir_fd=metadata.file_descriptor)
        except FileNotFoundError:
            pass
        try:
            os.rmdir(items_name, dir_fd=metadata.file_descriptor)
        except FileNotFoundError:
            pass
        try:
            os.unlink(database_name, dir_fd=config.file_descriptor)
        except FileNotFoundError:
            pass
        raise


def _rename_directory_no_replace(
    parent_fd: int,
    source_name: str,
    destination_name: str,
) -> None:
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
        os.fsencode(source_name),
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


def _clear_directory_fd(directory_fd: int) -> None:
    for name in os.listdir(directory_fd):
        entry_status = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISDIR(entry_status.st_mode):
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


def _remove_owned_name(
    parent_fd: int,
    name: str,
    identity: tuple[int, int],
    *,
    directory: bool,
) -> None:
    try:
        status = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if _status_identity(status) != identity:
        return
    if directory:
        os.rmdir(name, dir_fd=parent_fd)
    else:
        os.unlink(name, dir_fd=parent_fd)


def _cleanup_restore_staging(
    config: OpenRestoreDestination,
    metadata: OpenRestoreDestination,
    staging: RestoreStaging,
    *,
    succeeded: bool,
) -> None:
    try:
        if not succeeded:
            os.ftruncate(staging.database_fd, 0)
            os.fsync(staging.database_fd)
            _clear_directory_fd(staging.items_fd)
            _clear_directory_fd(staging.authors_fd)
            for name in (staging.database_name, "absdatabase.sqlite"):
                _remove_owned_name(
                    config.file_descriptor,
                    name,
                    staging.database_identity,
                    directory=False,
                )
            for name in (staging.items_name, "items"):
                _remove_owned_name(
                    metadata.file_descriptor,
                    name,
                    staging.items_identity,
                    directory=True,
                )
            for name in (staging.authors_name, "authors"):
                _remove_owned_name(
                    metadata.file_descriptor,
                    name,
                    staging.authors_identity,
                    directory=True,
                )
    finally:
        os.close(staging.authors_fd)
        os.close(staging.items_fd)
        os.close(staging.database_fd)


def _consume_restore_sentinels(
    config: OpenRestoreDestination,
    metadata: OpenRestoreDestination,
) -> None:
    destinations = (config, metadata)
    preserved: list[tuple[OpenRestoreDestination, str]] = []
    try:
        for destination in destinations:
            current = os.stat(
                destination.sentinel_name,
                dir_fd=destination.file_descriptor,
                follow_symlinks=False,
            )
            if _status_identity(current) != destination.sentinel_identity:
                raise ValueError("Audiobookshelf restore sentinel changed during restore")
            preserved_name = f".{destination.sentinel_name}.{uuid.uuid4().hex}.consumed"
            os.link(
                destination.sentinel_name,
                preserved_name,
                src_dir_fd=destination.file_descriptor,
                dst_dir_fd=destination.file_descriptor,
                follow_symlinks=False,
            )
            preserved.append((destination, preserved_name))
            os.unlink(destination.sentinel_name, dir_fd=destination.file_descriptor)
    except BaseException:
        for destination, preserved_name in reversed(preserved):
            try:
                os.link(
                    preserved_name,
                    destination.sentinel_name,
                    src_dir_fd=destination.file_descriptor,
                    dst_dir_fd=destination.file_descriptor,
                    follow_symlinks=False,
                )
            except FileExistsError:
                pass
        raise
    finally:
        for destination, preserved_name in preserved:
            os.unlink(preserved_name, dir_fd=destination.file_descriptor)


def _stage_restore_archive(
    artifact: Path,
    staged_database: Path,
    staged_items: Path,
    staged_authors: Path,
    workspace: Path,
) -> None:
    extracted = workspace / "extracted"
    extracted.mkdir(mode=0o700)
    _extract_archive(artifact, extracted)
    references = _validate_database(extracted / "absdatabase.sqlite")
    _validate_staged_metadata(extracted, references)
    shutil.copyfile(extracted / "absdatabase.sqlite", staged_database)
    with staged_database.open("rb") as database_file:
        os.fsync(database_file.fileno())
    shutil.copytree(extracted / "metadata-items", staged_items, dirs_exist_ok=True)
    shutil.copytree(extracted / "metadata-authors", staged_authors, dirs_exist_ok=True)
    _fsync_tree(staged_items)
    _fsync_tree(staged_authors)
    staged_view = workspace / "staged-view"
    staged_view.mkdir(mode=0o700)
    os.symlink(staged_items, staged_view / "metadata-items")
    os.symlink(staged_authors, staged_view / "metadata-authors")
    staged_references = _validate_database(staged_database)
    _validate_staged_metadata(staged_view, staged_references)


def _fsync_tree(root: Path) -> None:
    directories: list[Path] = []
    for current_root, directory_names, file_names in os.walk(root):
        current = Path(current_root)
        directories.append(current)
        for name in directory_names:
            candidate = current / name
            if candidate.is_symlink():
                raise ValueError("Audiobookshelf restore staging contains a symlink")
        for name in file_names:
            descriptor = os.open(
                current / name,
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            )
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    for directory in reversed(directories):
        descriptor = os.open(
            directory,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _publish_restore(
    config: OpenRestoreDestination,
    metadata: OpenRestoreDestination,
    staging: RestoreStaging,
    validation_root: Path,
) -> None:
    _require_destination_still_named(config)
    _require_destination_still_named(metadata)
    os.link(
        staging.database_name,
        "absdatabase.sqlite",
        src_dir_fd=config.file_descriptor,
        dst_dir_fd=config.file_descriptor,
        follow_symlinks=False,
    )
    _rename_directory_no_replace(metadata.file_descriptor, staging.items_name, "items")
    _rename_directory_no_replace(metadata.file_descriptor, staging.authors_name, "authors")
    database_status = os.stat(
        "absdatabase.sqlite", dir_fd=config.file_descriptor, follow_symlinks=False
    )
    items_status = os.stat("items", dir_fd=metadata.file_descriptor, follow_symlinks=False)
    authors_status = os.stat("authors", dir_fd=metadata.file_descriptor, follow_symlinks=False)
    if (
        _status_identity(database_status) != staging.database_identity
        or _status_identity(items_status) != staging.items_identity
        or _status_identity(authors_status) != staging.authors_identity
    ):
        raise ValueError("Audiobookshelf restore publication changed unexpectedly")
    final_references = _validate_database(
        _stable_fd_path(config.file_descriptor, "absdatabase.sqlite")
    )
    final_view = validation_root / "final-view"
    final_view.mkdir(mode=0o700)
    os.symlink(
        _stable_fd_path(metadata.file_descriptor, "items"),
        final_view / "metadata-items",
    )
    os.symlink(
        _stable_fd_path(metadata.file_descriptor, "authors"),
        final_view / "metadata-authors",
    )
    _validate_staged_metadata(final_view, final_references)
    _require_destination_still_named(config)
    _require_destination_still_named(metadata)
    _remove_owned_name(
        config.file_descriptor,
        staging.database_name,
        staging.database_identity,
        directory=False,
    )


def _remove_private_workspace(workspace: Path) -> None:
    if workspace.exists():
        shutil.rmtree(workspace)
    if workspace.exists():
        raise RuntimeError("Audiobookshelf private workspace cleanup failed")


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
        elif operation == "validate-restore":
            _validate_archive(paths[0], paths[1])
        elif operation == "backup":
            _build_archive(paths[0], paths[1], paths[2], paths[3])
        elif operation == "restore":
            _stage_restore_archive(paths[0], paths[1], paths[2], paths[3], paths[4])
        else:
            raise RuntimeError("Unsupported Audiobookshelf worker operation")
        connection.send(("ok", "", ""))
    except BaseException as exc:
        if isinstance(exc, PermissionError):
            message = "Audiobookshelf operation lacks required filesystem permissions"
        elif isinstance(exc, sqlite3.Error):
            message = "Audiobookshelf SQLite operation failed"
        elif isinstance(exc, OSError):
            message = "Audiobookshelf filesystem operation failed"
        else:
            message = str(exc)
        connection.send(("error", type(exc).__name__, message))
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
    operation_label = "validation" if operation in {"validate", "validate-restore"} else operation
    try:
        while process.is_alive():
            if time.monotonic() - started >= timeout_seconds:
                _stop_process(process)
                raise TimeoutError(f"Audiobookshelf {operation_label} timed out")
            await asyncio.sleep(0.02)
        process.join(PROCESS_STOP_TIMEOUT_SECONDS)
        if not connection.poll():
            raise RuntimeError(f"Audiobookshelf {operation_label} worker returned no result")
        status, exception_name, message = connection.recv()
        if status != "ok":
            exception_types: dict[str, type[Exception]] = {
                "FileNotFoundError": FileNotFoundError,
                "PermissionError": PermissionError,
                "RuntimeError": RuntimeError,
                "TimeoutError": TimeoutError,
                "ValueError": ValueError,
            }
            exception_type = exception_types.get(exception_name, RuntimeError)
            raise exception_type(message)
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
                _remove_private_workspace(workspace)
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
        config_destination: OpenRestoreDestination | None = None
        metadata_destination: OpenRestoreDestination | None = None
        staging: RestoreStaging | None = None
        workspace: Path | None = None
        validation_root: Path | None = None
        succeeded = False
        try:
            workspace = Path(tempfile.mkdtemp(prefix="audiobookshelf-restore-preflight-"))
            os.chmod(workspace, 0o700)
            await _run_worker(
                "validate-restore",
                artifact,
                workspace / "validated",
                timeout_seconds=RESTORE_TIMEOUT_SECONDS,
            )
            _remove_private_workspace(workspace)
            workspace = None
            config_destination = _open_restore_destination(
                config_path,
                sentinel_name=CONFIG_RESTORE_SENTINEL,
                artifact=artifact,
            )
            metadata_destination = _open_restore_destination(
                metadata_path,
                sentinel_name=METADATA_RESTORE_SENTINEL,
                artifact=artifact,
            )
            _require_distinct_restore_destinations(config_destination, metadata_destination)
            staging = _create_restore_staging(config_destination, metadata_destination)
            workspace = Path(tempfile.mkdtemp(prefix="audiobookshelf-restore-"))
            os.chmod(workspace, 0o700)
            await _run_worker(
                "restore",
                artifact,
                _stable_fd_path(
                    config_destination.file_descriptor,
                    staging.database_name,
                ),
                _stable_fd_path(
                    metadata_destination.file_descriptor,
                    staging.items_name,
                ),
                _stable_fd_path(
                    metadata_destination.file_descriptor,
                    staging.authors_name,
                ),
                workspace,
                timeout_seconds=RESTORE_TIMEOUT_SECONDS,
            )
            _remove_private_workspace(workspace)
            workspace = None
            validation_root = Path(tempfile.mkdtemp(prefix="audiobookshelf-final-validation-"))
            os.chmod(validation_root, 0o700)
            _publish_restore(
                config_destination,
                metadata_destination,
                staging,
                validation_root,
            )
            _remove_private_workspace(validation_root)
            validation_root = None
            os.fsync(config_destination.file_descriptor)
            os.fsync(metadata_destination.file_descriptor)
            _consume_restore_sentinels(config_destination, metadata_destination)
            succeeded = True
        finally:
            cleanup_error: BaseException | None = None
            try:
                if workspace is not None:
                    try:
                        _remove_private_workspace(workspace)
                    except BaseException as exc:
                        cleanup_error = exc
                if validation_root is not None:
                    try:
                        _remove_private_workspace(validation_root)
                    except BaseException as exc:
                        if cleanup_error is None:
                            cleanup_error = exc
                if (
                    staging is not None
                    and config_destination is not None
                    and metadata_destination is not None
                ):
                    try:
                        _cleanup_restore_staging(
                            config_destination,
                            metadata_destination,
                            staging,
                            succeeded=succeeded,
                        )
                    except BaseException as exc:
                        if cleanup_error is None:
                            cleanup_error = exc
            finally:
                if metadata_destination is not None:
                    os.close(metadata_destination.file_descriptor)
                if config_destination is not None:
                    os.close(config_destination.file_descriptor)
            if cleanup_error is not None:
                raise cleanup_error
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
