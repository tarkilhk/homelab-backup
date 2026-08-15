"""Exact Bazarr 1.5.6 native backup contract."""

from __future__ import annotations

import asyncio
import ctypes
import errno
import hashlib
import json
import multiprocessing
import os
import re
import stat
import tempfile
import time
import uuid
import zipfile
from dataclasses import dataclass
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from pathlib import Path
from typing import Any, Dict
from urllib.parse import urlsplit

import httpx
import pysqlite3 as sqlite3  # type: ignore[import-untyped]
import yaml  # type: ignore[import-untyped]

from app.core.plugins.artifacts import create_backup_artifact
from app.core.plugins.base import BackupContext, BackupPlugin, RestoreContext

_SOURCE_KEYS = frozenset({"mode", "base_url", "api_key", "backup_directory"})
_RESTORE_KEYS = frozenset({"mode", "restore_directory"})
_FORBIDDEN_PATHS = (Path("/"), Path("/app"), Path("/backups"), Path("/config"))
_BAZARR_VERSION = "1.5.6"
_PACKAGE_VERSION = "v1.5.6-ls349 by linuxserver.io"
_DATABASE_MIGRATION = "df76a4410347"
_DATABASE_ENGINE_PATTERN = re.compile(r"Sqlite [^\s]+")
_BACKUP_NAME_PATTERN = re.compile(
    r"bazarr_backup_v1\.5\.6_\d{4}\.\d{2}\.\d{2}_\d{2}\.\d{2}\.\d{2}\.zip"
)
_LISTED_BACKUP_NAME_PATTERN = re.compile(
    r"bazarr_backup_v[^/\\\s]+_\d{4}\.\d{2}\.\d{2}_\d{2}\.\d{2}\.\d{2}\.zip"
)
_REQUEST_TIMEOUT_SECONDS = 15.0
_BACKUP_DEADLINE_SECONDS = 300.0
_BACKUP_WORKER_TIMEOUT_SECONDS = 300.0
_RESTORE_WORKER_TIMEOUT_SECONDS = 300.0
_WORKER_STOP_TIMEOUT_SECONDS = 5.0
_POLL_INTERVAL_SECONDS = 1.0
_STABILITY_OBSERVATIONS = 2
_MAX_ZIP_MEMBERS = 2
_MAX_COMPRESSED_BYTES = 512 * 1024 * 1024
_MAX_UNCOMPRESSED_BYTES = 1024 * 1024 * 1024
_MAX_EXPANSION_RATIO = 200
_MAX_CONFIG_BYTES = 4 * 1024 * 1024
_MAX_TABLE_ROWS = 10_000_000

_EXPECTED_MEMBERS = frozenset({"bazarr.db", "config.yaml"})
_EXPECTED_TABLES = frozenset(
    {
        "alembic_version",
        "system",
        "table_announcements",
        "table_blacklist",
        "table_blacklist_movie",
        "table_episodes",
        "table_history",
        "table_history_movie",
        "table_languages_profiles",
        "table_movies",
        "table_movies_rootfolder",
        "table_settings_languages",
        "table_settings_notifier",
        "table_shows",
        "table_shows_rootfolder",
    }
)
_EXPECTED_CONFIG_SECTIONS = frozenset(
    {
        "addic7ed",
        "analytics",
        "anidb",
        "animetosho",
        "anticaptcha",
        "assrt",
        "auth",
        "avistaz",
        "backup",
        "betaseries",
        "cinemaz",
        "cors",
        "deathbycaptcha",
        "embeddedsubtitles",
        "general",
        "hdbits",
        "jimaku",
        "karagarga",
        "ktuvit",
        "legendasdivx",
        "legendasnet",
        "log",
        "movie_scores",
        "napiprojekt",
        "napisy24",
        "opensubtitlescom",
        "plex",
        "podnapisi",
        "postgresql",
        "proxy",
        "radarr",
        "series_scores",
        "sonarr",
        "subdl",
        "subf2m",
        "subsource",
        "subsync",
        "subx",
        "titlovi",
        "titulky",
        "translator",
        "turkcealtyaziorg",
        "whisperai",
        "xsubs",
    }
)

BACKUP_BASE_PATH = "/backups"
ISOLATED_RESTORE_ENV = "HOMELAB_BACKUP_ALLOW_ISOLATED_RESTORE"

RESTORE_SENTINEL_NAME = ".bazarr-restore-destination"
RESTORE_SENTINEL_CONTENT = "bazarr-v1.5.6-isolated-restore-v1\n"


@dataclass(frozen=True)
class _FileEvidence:
    device: int
    inode: int
    size: int
    modified_ns: int
    changed_ns: int


@dataclass(frozen=True)
class _ArchiveEvidence:
    device: int
    inode: int
    artifact_bytes: int
    sha256: str
    table_counts: dict[str, int]


def _file_evidence(status: os.stat_result) -> _FileEvidence:
    return _FileEvidence(
        device=status.st_dev,
        inode=status.st_ino,
        size=status.st_size,
        modified_ns=status.st_mtime_ns,
        changed_ns=status.st_ctime_ns,
    )


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_absolute_path(value: object) -> bool:
    if not isinstance(value, str) or not value or any(ord(character) < 32 for character in value):
        return False
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        return False
    return all(path != forbidden for forbidden in _FORBIDDEN_PATHS)


def _safe_origin(value: object) -> bool:
    if not isinstance(value, str) or not value or any(ord(character) < 32 for character in value):
        return False
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    return bool(
        parsed.scheme in {"http", "https"}
        and parsed.hostname
        and parsed.username is None
        and parsed.password is None
        and parsed.query == ""
        and parsed.fragment == ""
        and parsed.path in {"", "/"}
        and (port is None or 1 <= port <= 65535)
    )


def _require_read_only_backup_mount(value: str) -> Path:
    path = Path(value)
    if not path.exists() or not path.is_dir() or path.is_symlink():
        raise FileNotFoundError("Bazarr native backup directory was not found")
    try:
        if path.resolve(strict=True) != path:
            raise ValueError("Bazarr native backup directory must not use symlinks")
    except OSError as exc:
        raise FileNotFoundError("Bazarr native backup directory was not found") from exc
    if not os.path.ismount(path):
        raise RuntimeError("Bazarr native backup directory must be a dedicated mount")
    try:
        read_only = bool(os.statvfs(path).f_flag & os.ST_RDONLY)
    except OSError as exc:
        raise RuntimeError("Bazarr native backup mount could not be inspected") from exc
    if not read_only:
        raise RuntimeError("Bazarr native backup mount must be read-only")
    return path


def _require_restore_destination(value: str) -> Path:
    destination = Path(value)
    if os.path.lexists(destination):
        raise FileExistsError("Bazarr restore destination already exists")
    parent = destination.parent
    if parent.is_symlink():
        raise ValueError("Bazarr restore destination parent must not be a symlink")
    if not parent.exists() or not parent.is_dir():
        raise FileNotFoundError("Bazarr restore destination parent was not found")
    try:
        if parent.resolve(strict=True) != parent:
            raise ValueError("Bazarr restore destination parent must not use symlinks")
        parent_status = parent.stat()
    except OSError as exc:
        raise FileNotFoundError("Bazarr restore destination parent was not found") from exc
    if stat.S_IMODE(parent_status.st_mode) & 0o077:
        raise RuntimeError("Bazarr restore destination parent must be private")

    sentinel = parent / RESTORE_SENTINEL_NAME
    if not sentinel.exists() or not sentinel.is_file() or sentinel.is_symlink():
        raise FileNotFoundError("Bazarr restore destination sentinel was not found")
    try:
        marker = sentinel.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError("Bazarr restore destination sentinel is invalid") from exc
    if marker != RESTORE_SENTINEL_CONTENT:
        raise ValueError("Bazarr restore destination sentinel is invalid")
    if {entry.name for entry in parent.iterdir()} != {RESTORE_SENTINEL_NAME}:
        raise ValueError("Bazarr restore destination parent must contain only its sentinel")
    return destination


def _require_isolated_restore_destination(value: str) -> Path:
    destination = Path(value)
    try:
        resolved_parent = destination.parent.resolve(strict=True)
    except OSError as exc:
        raise FileNotFoundError("Bazarr restore destination parent was not found") from exc
    if not (
        resolved_parent.is_relative_to(Path("/tmp"))
        or resolved_parent.is_relative_to(Path("/restore"))
    ):
        raise ValueError("Bazarr restore destination is not an isolated local path")
    return _require_restore_destination(value)


def _network_interfaces() -> set[str]:
    try:
        return {entry.name for entry in Path("/sys/class/net").iterdir()}
    except OSError as exc:
        raise RuntimeError("Bazarr restore network isolation could not be verified") from exc


def _require_isolated_restore_authorization() -> None:
    if os.getenv(ISOLATED_RESTORE_ENV) != "1":
        raise RuntimeError("Bazarr restore is disabled outside an authorized isolated drill")
    if _network_interfaces() != {"lo"}:
        raise RuntimeError("Bazarr restore requires a loopback-only network namespace")


def _response_payload(response: httpx.Response) -> object:
    if response.status_code in {401, 403}:
        raise RuntimeError("Bazarr authentication failed")
    if response.status_code != 200:
        raise RuntimeError(f"Bazarr API returned status {response.status_code}")
    try:
        return response.json()
    except json.JSONDecodeError as exc:
        raise RuntimeError("Bazarr API returned a malformed response") from exc


def _validate_status_response(payload: object) -> None:
    if not isinstance(payload, dict) or set(payload) != {"data"}:
        raise RuntimeError("Bazarr status response is malformed")
    data = payload.get("data")
    required = {
        "bazarr_version",
        "package_version",
        "database_engine",
        "database_migration",
    }
    if not isinstance(data, dict) or not required.issubset(data):
        raise RuntimeError("Bazarr status response is malformed")
    if data["bazarr_version"] != _BAZARR_VERSION:
        raise RuntimeError("Bazarr version is not the supported 1.5.6 release")
    if data["package_version"] != _PACKAGE_VERSION:
        raise RuntimeError("Bazarr package is not the supported LinuxServer image")
    engine = data["database_engine"]
    if not isinstance(engine, str) or _DATABASE_ENGINE_PATTERN.fullmatch(engine) is None:
        raise RuntimeError("Bazarr must use the supported SQLite database engine")
    if data["database_migration"] != _DATABASE_MIGRATION:
        raise RuntimeError("Bazarr database migration is not the supported revision")


def _backup_names(payload: object) -> set[str]:
    if not isinstance(payload, dict) or set(payload) != {"data"}:
        raise RuntimeError("Bazarr backup-list response is malformed")
    backups = payload.get("data")
    if not isinstance(backups, list):
        raise RuntimeError("Bazarr backup-list response is malformed")
    required = {"date", "filename", "size", "type"}
    names: set[str] = set()
    for backup in backups:
        if (
            not isinstance(backup, dict)
            or set(backup) != required
            or not all(isinstance(backup[key], str) and backup[key] for key in required)
            or backup["type"] != "backup"
        ):
            raise RuntimeError("Bazarr backup-list response is malformed")
        name = backup["filename"]
        if _LISTED_BACKUP_NAME_PATTERN.fullmatch(name) is None:
            raise RuntimeError("Bazarr backup filename is malformed")
        if name in names:
            raise RuntimeError("Bazarr backup-list response contains duplicate filenames")
        names.add(name)
    return names


def _validate_backups_response(payload: object) -> None:
    _backup_names(payload)


def _scan_native_backup_directory(directory: Path) -> dict[str, _FileEvidence]:
    files: dict[str, _FileEvidence] = {}
    try:
        entries = tuple(directory.iterdir())
    except OSError as exc:
        raise RuntimeError("Bazarr native backup directory could not be read") from exc
    for entry in entries:
        if _LISTED_BACKUP_NAME_PATTERN.fullmatch(entry.name) is None:
            continue
        try:
            status = entry.lstat()
        except OSError as exc:
            raise RuntimeError("Bazarr native backup candidate disappeared") from exc
        if stat.S_ISLNK(status.st_mode):
            raise RuntimeError("Bazarr native backup candidate must not be a link")
        if not stat.S_ISREG(status.st_mode):
            raise RuntimeError("Bazarr native backup candidate must be a regular file")
        try:
            entry.resolve(strict=True).relative_to(directory)
        except (OSError, ValueError) as exc:
            raise RuntimeError("Bazarr native backup candidate escaped its mount") from exc
        files[entry.name] = _file_evidence(status)
    return files


async def _request_backup_names(client: httpx.AsyncClient) -> set[str]:
    return _backup_names(_response_payload(await client.get("/api/system/backups")))


def _require_backup_accepted(response: httpx.Response) -> None:
    if response.status_code in {401, 403}:
        raise RuntimeError("Bazarr authentication failed")
    if response.status_code != 204:
        raise RuntimeError(f"Bazarr API returned status {response.status_code}")


async def _wait_for_unique_backup(
    client: httpx.AsyncClient,
    directory: Path,
    baseline_api: set[str],
    baseline_local: set[str],
    trigger_monotonic: float,
    trigger_wall_ns: int,
) -> tuple[str, _FileEvidence]:
    loop = asyncio.get_running_loop()
    if loop.time() < trigger_monotonic:
        raise RuntimeError("Bazarr backup trigger boundary is invalid")
    deadline = trigger_monotonic + _BACKUP_DEADLINE_SECONDS
    stable_name: str | None = None
    stable_evidence: _FileEvidence | None = None
    stable_observations = 0
    saw_mismatch = False

    while loop.time() < deadline:
        api_candidates = await _request_backup_names(client) - baseline_api
        local_files = _scan_native_backup_directory(directory)
        local_candidates = set(local_files) - baseline_local
        if len(api_candidates) > 1 or len(local_candidates) > 1:
            raise RuntimeError("Bazarr produced multiple new backup candidates")
        if len(api_candidates) == len(local_candidates) == 1:
            api_name = next(iter(api_candidates))
            local_name = next(iter(local_candidates))
            if api_name != local_name:
                saw_mismatch = True
            else:
                if _BACKUP_NAME_PATTERN.fullmatch(api_name) is None:
                    raise RuntimeError("Bazarr backup filename or version is unsupported")
                evidence = local_files[local_name]
                if evidence.changed_ns < trigger_wall_ns:
                    raise RuntimeError(
                        "Bazarr backup candidate predates the trigger boundary; overlap or "
                        "collision is possible"
                    )
                if stable_name == local_name and stable_evidence == evidence:
                    stable_observations += 1
                else:
                    stable_name = local_name
                    stable_evidence = evidence
                    stable_observations = 1
                if stable_observations >= _STABILITY_OBSERVATIONS:
                    return local_name, evidence
        elif api_candidates or local_candidates:
            saw_mismatch = True
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)

    if saw_mismatch:
        raise RuntimeError("Bazarr API and local mount did not expose one matching backup")
    raise RuntimeError("Bazarr backup timed out before a candidate appeared")


def _copy_stable_source(
    directory: Path,
    name: str,
    expected: _FileEvidence,
    destination: Path,
) -> None:
    directory_flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        directory_flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        directory_flags |= os.O_NOFOLLOW
    directory_fd = os.open(directory, directory_flags)
    source_fd: int | None = None
    destination_fd: int | None = None
    try:
        source_flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            source_flags |= os.O_NOFOLLOW
        source_fd = os.open(name, source_flags, dir_fd=directory_fd)
        source_before = os.fstat(source_fd)
        if not stat.S_ISREG(source_before.st_mode) or _file_evidence(source_before) != expected:
            raise RuntimeError("Bazarr native backup changed before it could be copied")
        destination_fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        copied = 0
        while True:
            chunk = os.read(source_fd, 1024 * 1024)
            if not chunk:
                break
            view = memoryview(chunk)
            while view:
                written = os.write(destination_fd, view)
                if written <= 0:
                    raise OSError("short write while copying Bazarr backup")
                copied += written
                view = view[written:]
        os.fsync(destination_fd)
        source_after = os.fstat(source_fd)
        named_after = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if (
            copied != expected.size
            or _file_evidence(source_after) != expected
            or _file_evidence(named_after) != expected
        ):
            raise RuntimeError("Bazarr native backup changed while it was copied")
    finally:
        if destination_fd is not None:
            os.close(destination_fd)
        if source_fd is not None:
            os.close(source_fd)
        os.close(directory_fd)


def _require_unambiguous_zip(path: Path) -> None:
    size = path.stat().st_size
    if size > _MAX_COMPRESSED_BYTES:
        raise RuntimeError("Bazarr archive exceeds the compressed safety limit")
    tail_size = min(size, 65_557)
    with path.open("rb") as archive_file:
        if archive_file.read(4) != b"PK\x03\x04":
            raise RuntimeError("Bazarr archive is not a valid ZIP")
        archive_file.seek(size - tail_size)
        tail = archive_file.read(tail_size)
    eocd_offset = tail.rfind(b"PK\x05\x06")
    if eocd_offset < 0 or len(tail) - eocd_offset < 22:
        raise RuntimeError("Bazarr archive has an ambiguous ZIP trailer")
    comment_length = int.from_bytes(tail[eocd_offset + 20 : eocd_offset + 22], "little")
    if eocd_offset + 22 + comment_length != len(tail):
        raise RuntimeError("Bazarr archive has trailing or ambiguous data")


def _require_regular_zip_member(member: zipfile.ZipInfo) -> None:
    if member.flag_bits & 0x1:
        raise RuntimeError("Bazarr archive contains an encrypted member")
    if member.is_dir():
        raise RuntimeError("Bazarr archive members must be regular files")
    if member.create_system == 3:
        file_type = stat.S_IFMT(member.external_attr >> 16)
        if file_type not in {0, stat.S_IFREG}:
            if file_type == stat.S_IFLNK:
                raise RuntimeError("Bazarr archive contains a link member")
            raise RuntimeError("Bazarr archive members must be regular files")
    if member.compress_type != zipfile.ZIP_DEFLATED:
        raise RuntimeError("Bazarr archive uses an unsupported compression method")


def _read_config_member(archive: zipfile.ZipFile, member: zipfile.ZipInfo) -> bytes:
    if member.file_size > _MAX_CONFIG_BYTES:
        raise RuntimeError("Bazarr configuration exceeds its safety limit")
    with archive.open(member) as source:
        payload = source.read(_MAX_CONFIG_BYTES + 1)
    if len(payload) != member.file_size or len(payload) > _MAX_CONFIG_BYTES:
        raise RuntimeError("Bazarr configuration exceeds its safety limit")
    return payload


def _validate_config_yaml(payload: bytes) -> None:
    try:
        config = yaml.safe_load(payload)
    except (UnicodeError, yaml.YAMLError) as exc:
        raise RuntimeError("Bazarr YAML configuration is invalid") from exc
    if (
        not isinstance(config, dict)
        or set(config) != _EXPECTED_CONFIG_SECTIONS
        or not all(
            isinstance(section, str) and isinstance(value, dict)
            for section, value in config.items()
        )
    ):
        raise RuntimeError("Bazarr configuration structure is incompatible")
    postgresql = config.get("postgresql")
    if not isinstance(postgresql, dict) or postgresql.get("enabled") is not False:
        raise RuntimeError("Bazarr PostgreSQL archives are unsupported; SQLite is required")


def _extract_database_member(
    archive: zipfile.ZipFile,
    member: zipfile.ZipInfo,
    destination: Path,
) -> None:
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    copied = 0
    try:
        with archive.open(member) as source:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                copied += len(chunk)
                if copied > member.file_size or copied > _MAX_UNCOMPRESSED_BYTES:
                    raise RuntimeError("Bazarr database exceeds its safety limit")
                view = memoryview(chunk)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise OSError("short write while extracting Bazarr database")
                    view = view[written:]
        if copied != member.file_size:
            raise RuntimeError("Bazarr database member was truncated")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _validate_sqlite_database(path: Path) -> dict[str, int]:
    with path.open("rb") as database_file:
        if database_file.read(16) != b"SQLite format 3\x00":
            raise RuntimeError("Bazarr database is not a SQLite database")
    try:
        with sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True) as connection:
            connection.execute("PRAGMA query_only = ON")
            if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                raise RuntimeError("Bazarr SQLite integrity check failed")
            if connection.execute("PRAGMA foreign_key_check").fetchall():
                raise RuntimeError("Bazarr SQLite foreign key check failed")
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
            }
            if tables != _EXPECTED_TABLES:
                raise RuntimeError("Bazarr SQLite table set is incompatible")
            migration_rows = connection.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchall()
            if migration_rows != [(_DATABASE_MIGRATION,)]:
                raise RuntimeError("Bazarr SQLite migration is incompatible")
            table_counts: dict[str, int] = {}
            for table in sorted(_EXPECTED_TABLES):
                count = connection.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
                if not isinstance(count, int) or count < 0 or count > _MAX_TABLE_ROWS:
                    raise RuntimeError("Bazarr SQLite row count exceeds its safety limit")
                table_counts[table] = count
            return table_counts
    except RuntimeError:
        raise
    except sqlite3.DatabaseError as exc:
        raise RuntimeError("Bazarr SQLite integrity validation failed") from exc


def _validate_native_archive(
    path: Path,
    *,
    validation_root: Path | None = None,
) -> _ArchiveEvidence:
    _require_unambiguous_zip(path)
    database_path: Path | None = None
    try:
        try:
            with zipfile.ZipFile(path) as archive:
                members = archive.infolist()
                names = [member.filename for member in members]
                if len(members) > _MAX_ZIP_MEMBERS:
                    raise RuntimeError("Bazarr archive exceeds the members safety limit")
                if len(members) != 2 or len(set(names)) != 2 or set(names) != _EXPECTED_MEMBERS:
                    raise RuntimeError("Bazarr archive must contain exactly its two members")
                for member in members:
                    _require_regular_zip_member(member)
                compressed = sum(member.compress_size for member in members)
                uncompressed = sum(member.file_size for member in members)
                if compressed > _MAX_COMPRESSED_BYTES:
                    raise RuntimeError("Bazarr archive exceeds the compressed safety limit")
                if uncompressed > _MAX_UNCOMPRESSED_BYTES:
                    raise RuntimeError("Bazarr archive exceeds the uncompressed safety limit")
                if uncompressed / max(1, compressed) > _MAX_EXPANSION_RATIO:
                    raise RuntimeError("Bazarr archive expansion ratio exceeds its safety limit")
                corrupt_member = archive.testzip()
                if corrupt_member is not None:
                    raise RuntimeError("Bazarr archive failed its CRC check")
                by_name = {member.filename: member for member in members}
                _validate_config_yaml(_read_config_member(archive, by_name["config.yaml"]))
                descriptor, raw_path = tempfile.mkstemp(
                    prefix=".bazarr-validation-",
                    suffix=".db",
                    dir=validation_root,
                )
                os.close(descriptor)
                database_path = Path(raw_path)
                database_path.unlink()
                _extract_database_member(archive, by_name["bazarr.db"], database_path)
        except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
            if isinstance(exc, RuntimeError):
                raise
            raise RuntimeError("Bazarr archive is corrupt or unreadable") from exc
        if database_path is None:
            raise RuntimeError("Bazarr archive database member was not extracted")
        table_counts = _validate_sqlite_database(database_path)
        return _ArchiveEvidence(
            device=path.stat().st_dev,
            inode=path.stat().st_ino,
            artifact_bytes=path.stat().st_size,
            sha256=_hash_file(path),
            table_counts=table_counts,
        )
    finally:
        if database_path is not None:
            database_path.unlink(missing_ok=True)


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
    evidence: _FileEvidence,
    artifact_path: Path,
    validation_root: Path,
    validation_identity: tuple[int, int],
    connection: Connection,
) -> None:
    validation_fd: int | None = None
    artifact_fd: int | None = None
    try:
        validation_fd = _open_owned_directory(validation_root, validation_identity)
        _copy_stable_source(directory, name, evidence, artifact_path)
        artifact_flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            artifact_flags |= os.O_NOFOLLOW
        artifact_fd = os.open(artifact_path, artifact_flags)
        archive_evidence = _validate_native_archive(
            Path(f"/proc/self/fd/{artifact_fd}"),
            validation_root=Path(f"/proc/self/fd/{validation_fd}"),
        )
        _send_worker_result(connection, ("ok", "", archive_evidence))
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
    evidence: _FileEvidence,
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
        name="bazarr-backup",
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
            raise RuntimeError(f"Bazarr {operation} worker could not be reaped")
        return
    process.terminate()
    await _join_worker_process(process, _WORKER_STOP_TIMEOUT_SECONDS)
    if process.is_alive():
        process.kill()
        await _join_worker_process(process, _WORKER_STOP_TIMEOUT_SECONDS)
    if process.is_alive() or process.exitcode is None:
        raise RuntimeError(f"Bazarr {operation} worker could not be stopped")


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


def _raise_worker_result(
    result: tuple[str, str, object | None] | None,
    *,
    operation: str,
) -> object | None:
    if result is None:
        raise RuntimeError(f"Bazarr {operation} worker returned no result")
    kind, message, payload = result
    if kind == "ok":
        return payload
    safe_message = message or f"Bazarr {operation} failed"
    if kind == "file-not-found":
        raise FileNotFoundError(safe_message)
    if kind == "permission":
        raise RuntimeError(safe_message)
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
) -> object | None:
    try:
        await _join_worker_process(process, timeout_seconds)
        if process.is_alive():
            await _stop_worker_process_before_return(process, operation=operation)
            raise TimeoutError(f"Bazarr {operation} timed out")
        result: tuple[str, str, object | None] | None = None
        if connection.poll():
            received = connection.recv()
            if (
                isinstance(received, tuple)
                and len(received) == 3
                and isinstance(received[0], str)
                and isinstance(received[1], str)
            ):
                result = received
        payload = _raise_worker_result(result, operation=operation)
        if process.exitcode != 0:
            raise RuntimeError(f"Bazarr {operation} worker failed")
        return payload
    except asyncio.CancelledError:
        await _stop_worker_process_before_return(process, operation=operation)
        raise
    except BaseException:
        if process.is_alive():
            await _stop_worker_process_before_return(process, operation=operation)
        raise
    finally:
        connection.close()


def _directory_flags() -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return flags


def _directory_identity(directory_fd: int) -> tuple[int, int]:
    status = os.fstat(directory_fd)
    if not stat.S_ISDIR(status.st_mode):
        raise ValueError("Bazarr restore-owned path is not a directory")
    return status.st_dev, status.st_ino


def _open_owned_directory(path: Path, expected_identity: tuple[int, int]) -> int:
    descriptor = os.open(path, _directory_flags())
    if _directory_identity(descriptor) != expected_identity:
        os.close(descriptor)
        raise ValueError("Bazarr restore-owned directory changed")
    return descriptor


def _clear_directory_fd(directory_fd: int) -> None:
    for name in os.listdir(directory_fd):
        entry = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISDIR(entry.st_mode):
            child_fd = os.open(name, _directory_flags(), dir_fd=directory_fd)
            try:
                _clear_directory_fd(child_fd)
            finally:
                os.close(child_fd)
            os.rmdir(name, dir_fd=directory_fd)
        else:
            os.unlink(name, dir_fd=directory_fd)


def _remove_owned_directory(
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
        if (
            stat.S_ISDIR(candidate.st_mode)
            and (
                candidate.st_dev,
                candidate.st_ino,
            )
            == expected_identity
        ):
            os.rmdir(name, dir_fd=parent_fd)
            return


def _create_private_validation_directory(parent: Path) -> tuple[Path, int, tuple[int, int]]:
    validation_root = Path(tempfile.mkdtemp(prefix=".bazarr-validation-", dir=parent))
    os.chmod(validation_root, 0o700)
    validation_fd = os.open(validation_root, _directory_flags())
    identity = _directory_identity(validation_fd)
    if os.fstat(validation_fd).st_mode & 0o077:
        os.close(validation_fd)
        validation_root.rmdir()
        raise RuntimeError("Bazarr validation directory must be private")
    return validation_root, validation_fd, identity


def _write_restored_member(
    archive: zipfile.ZipFile,
    member: zipfile.ZipInfo,
    directory_fd: int,
    filename: str,
) -> None:
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(filename, flags, 0o600, dir_fd=directory_fd)
    source_digest = hashlib.sha256()
    copied = 0
    try:
        with archive.open(member) as source:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                copied += len(chunk)
                if copied > member.file_size:
                    raise RuntimeError("Bazarr restore member exceeded its declared size")
                source_digest.update(chunk)
                view = memoryview(chunk)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise OSError("short write while restoring Bazarr state")
                    view = view[written:]
        if copied != member.file_size:
            raise RuntimeError("Bazarr restore member was truncated")
        os.fsync(descriptor)
        os.lseek(descriptor, 0, os.SEEK_SET)
        restored_digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            restored_digest.update(chunk)
        if restored_digest.digest() != source_digest.digest():
            raise RuntimeError("Bazarr restored bytes do not match the archive")
    finally:
        os.close(descriptor)


def _rename_noreplace(
    parent_fd: int,
    source_name: str,
    destination_name: str,
) -> None:
    library = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(library, "renameat2", None)
    if renameat2 is None:
        raise RuntimeError("Bazarr create-only restore requires Linux renameat2")
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
        raise FileExistsError("Bazarr restore destination already exists")
    raise OSError(error_number, os.strerror(error_number))


def _rename_directory_noreplace(
    parent_fd: int,
    source_name: str,
    destination_name: str,
) -> None:
    _rename_noreplace(parent_fd, source_name, destination_name)


def _same_directory_identity(parent_fd: int, name: str, expected: tuple[int, int]) -> bool:
    try:
        status = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError:
        return False
    return stat.S_ISDIR(status.st_mode) and (status.st_dev, status.st_ino) == expected


def _require_parent_path_identity(parent: Path, parent_fd: int) -> tuple[int, int]:
    opened = os.fstat(parent_fd)
    try:
        named = os.stat(parent, follow_symlinks=False)
    except OSError as exc:
        raise RuntimeError("Bazarr restore destination parent changed") from exc
    opened_identity = (opened.st_dev, opened.st_ino)
    if not stat.S_ISDIR(named.st_mode) or (named.st_dev, named.st_ino) != opened_identity:
        raise RuntimeError("Bazarr restore destination parent changed")
    return opened_identity


def _consume_restore_sentinel(parent_fd: int) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(RESTORE_SENTINEL_NAME, flags, dir_fd=parent_fd)
    try:
        marker = os.read(
            descriptor,
            len(RESTORE_SENTINEL_CONTENT.encode("utf-8")) + 1,
        )
        if marker != RESTORE_SENTINEL_CONTENT.encode("utf-8"):
            raise ValueError("Bazarr restore destination sentinel changed")
        expected = os.fstat(descriptor)
        consumed_name = f".{RESTORE_SENTINEL_NAME}.{uuid.uuid4().hex}.consumed"
        os.rename(
            RESTORE_SENTINEL_NAME,
            consumed_name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        current = os.stat(consumed_name, dir_fd=parent_fd, follow_symlinks=False)
        if (expected.st_dev, expected.st_ino) != (current.st_dev, current.st_ino):
            try:
                _rename_noreplace(parent_fd, consumed_name, RESTORE_SENTINEL_NAME)
            except OSError:
                pass
            raise ValueError("Bazarr restore destination sentinel changed")
        os.unlink(consumed_name, dir_fd=parent_fd)
    finally:
        os.close(descriptor)


def _require_named_directory_identity(
    parent_fd: int,
    name: str,
    expected_identity: tuple[int, int],
) -> None:
    if not _same_directory_identity(parent_fd, name, expected_identity):
        raise ValueError("Bazarr restore-owned directory changed")


def _materialize_bound_archive(
    artifact_fd: int,
    validation_fd: int,
    config_fd: int,
    database_fd: int,
    expected_artifact_bytes: int,
    expected_sha256: str,
) -> None:
    bound_artifact = Path(f"/proc/self/fd/{artifact_fd}")
    evidence = _validate_native_archive(
        bound_artifact,
        validation_root=Path(f"/proc/self/fd/{validation_fd}"),
    )
    if evidence.artifact_bytes != expected_artifact_bytes or evidence.sha256 != expected_sha256:
        raise ValueError("Bazarr restore artifact does not match its verified staging identity")
    with zipfile.ZipFile(bound_artifact) as archive:
        by_name = {member.filename: member for member in archive.infolist()}
        _write_restored_member(
            archive,
            by_name["config.yaml"],
            config_fd,
            "config.yaml",
        )
        _write_restored_member(
            archive,
            by_name["bazarr.db"],
            database_fd,
            "bazarr.db",
        )


def _restore_process_worker(
    artifact_path: Path,
    parent: Path,
    parent_identity: tuple[int, int],
    staging_name: str,
    staging_identity: tuple[int, int],
    config_identity: tuple[int, int],
    database_identity: tuple[int, int],
    validation_name: str,
    validation_identity: tuple[int, int],
    expected_artifact_bytes: int,
    expected_sha256: str,
    connection: Connection,
) -> None:
    parent_fd: int | None = None
    staging_fd: int | None = None
    config_fd: int | None = None
    database_fd: int | None = None
    validation_fd: int | None = None
    artifact_fd: int | None = None
    try:
        parent_fd = _open_owned_directory(parent, parent_identity)
        if _require_parent_path_identity(parent, parent_fd) != parent_identity:
            raise RuntimeError("Bazarr restore destination parent changed")
        staging_fd = os.open(staging_name, _directory_flags(), dir_fd=parent_fd)
        if _directory_identity(staging_fd) != staging_identity:
            raise ValueError("Bazarr restore staging directory changed")
        config_fd = os.open("config", _directory_flags(), dir_fd=staging_fd)
        if _directory_identity(config_fd) != config_identity:
            raise ValueError("Bazarr restore config directory changed")
        database_fd = os.open("db", _directory_flags(), dir_fd=staging_fd)
        if _directory_identity(database_fd) != database_identity:
            raise ValueError("Bazarr restore database directory changed")
        validation_fd = os.open(validation_name, _directory_flags(), dir_fd=parent_fd)
        if _directory_identity(validation_fd) != validation_identity:
            raise ValueError("Bazarr restore validation directory changed")
        artifact_flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            artifact_flags |= os.O_NOFOLLOW
        artifact_fd = os.open(artifact_path, artifact_flags)
        artifact_status = os.fstat(artifact_fd)
        if not stat.S_ISREG(artifact_status.st_mode):
            raise ValueError("Bazarr restore artifact is not a regular file")
        _materialize_bound_archive(
            artifact_fd,
            validation_fd,
            config_fd,
            database_fd,
            expected_artifact_bytes,
            expected_sha256,
        )
        os.fsync(config_fd)
        os.fsync(database_fd)
        os.fsync(staging_fd)
        if _require_parent_path_identity(parent, parent_fd) != parent_identity:
            raise RuntimeError("Bazarr restore destination parent changed")
        _require_named_directory_identity(parent_fd, staging_name, staging_identity)
        _send_worker_result(connection, ("ok", "", None))
    except BaseException as exc:
        _send_worker_result(connection, (_worker_error_kind(exc), str(exc), None))
        raise SystemExit(1) from None
    finally:
        if artifact_fd is not None:
            os.close(artifact_fd)
        if validation_fd is not None:
            os.close(validation_fd)
        if database_fd is not None:
            os.close(database_fd)
        if config_fd is not None:
            os.close(config_fd)
        if staging_fd is not None:
            os.close(staging_fd)
        if parent_fd is not None:
            os.close(parent_fd)
        connection.close()


def _start_restore_process(
    artifact_path: Path,
    parent: Path,
    parent_identity: tuple[int, int],
    staging_name: str,
    staging_identity: tuple[int, int],
    config_identity: tuple[int, int],
    database_identity: tuple[int, int],
    validation_name: str,
    validation_identity: tuple[int, int],
    expected_artifact_bytes: int,
    expected_sha256: str,
) -> tuple[BaseProcess, Connection]:
    context = multiprocessing.get_context("spawn")
    receiving, sending = context.Pipe(duplex=False)
    process = context.Process(
        target=_restore_process_worker,
        args=(
            artifact_path,
            parent,
            parent_identity,
            staging_name,
            staging_identity,
            config_identity,
            database_identity,
            validation_name,
            validation_identity,
            expected_artifact_bytes,
            expected_sha256,
            sending,
        ),
        name="bazarr-restore",
        daemon=True,
    )
    process.start()
    sending.close()
    return process, receiving


async def _materialize_restore(
    artifact_path: Path,
    destination: Path,
    expected_artifact_bytes: int,
    expected_sha256: str,
) -> None:
    parent = destination.parent
    parent_fd = os.open(parent, _directory_flags())
    staging_name = f".{destination.name}.{uuid.uuid4().hex}.restore.tmp"
    validation_name = f".{destination.name}.{uuid.uuid4().hex}.validation.tmp"
    staging_fd: int | None = None
    config_fd: int | None = None
    database_fd: int | None = None
    validation_fd: int | None = None
    staging_identity: tuple[int, int] | None = None
    validation_identity: tuple[int, int] | None = None
    process: BaseProcess | None = None
    validation_removed = False
    succeeded = False
    try:
        parent_identity = _require_parent_path_identity(parent, parent_fd)
        _require_restore_destination(str(destination))
        if _require_parent_path_identity(parent, parent_fd) != parent_identity:
            raise RuntimeError("Bazarr restore destination parent changed")
        os.mkdir(staging_name, 0o700, dir_fd=parent_fd)
        staging_fd = os.open(staging_name, _directory_flags(), dir_fd=parent_fd)
        staging_identity = _directory_identity(staging_fd)
        os.mkdir("config", 0o700, dir_fd=staging_fd)
        os.mkdir("db", 0o700, dir_fd=staging_fd)
        config_fd = os.open("config", _directory_flags(), dir_fd=staging_fd)
        database_fd = os.open("db", _directory_flags(), dir_fd=staging_fd)
        config_identity = _directory_identity(config_fd)
        database_identity = _directory_identity(database_fd)
        os.mkdir(validation_name, 0o700, dir_fd=parent_fd)
        validation_fd = os.open(validation_name, _directory_flags(), dir_fd=parent_fd)
        validation_identity = _directory_identity(validation_fd)
        process, connection = _start_restore_process(
            artifact_path,
            parent,
            parent_identity,
            staging_name,
            staging_identity,
            config_identity,
            database_identity,
            validation_name,
            validation_identity,
            expected_artifact_bytes,
            expected_sha256,
        )
        payload = await _await_worker(
            process,
            connection,
            operation="restore",
            timeout_seconds=_RESTORE_WORKER_TIMEOUT_SECONDS,
        )
        if payload is not None:
            raise RuntimeError("Bazarr restore worker returned an invalid result")
        _remove_owned_directory(
            parent_fd,
            validation_fd,
            expected_identity=validation_identity,
            candidate_names=(validation_name,),
        )
        validation_removed = True
        if _require_parent_path_identity(parent, parent_fd) != parent_identity:
            raise RuntimeError("Bazarr restore destination parent changed")
        _require_named_directory_identity(parent_fd, staging_name, staging_identity)
        _rename_directory_noreplace(parent_fd, staging_name, destination.name)
        _require_named_directory_identity(parent_fd, destination.name, staging_identity)
        os.fsync(parent_fd)
        _consume_restore_sentinel(parent_fd)
        os.fsync(parent_fd)
        if _require_parent_path_identity(parent, parent_fd) != parent_identity:
            raise RuntimeError("Bazarr restore destination parent changed")
        succeeded = True
    finally:
        cleanup_error: BaseException | None = None
        if process is None or not process.is_alive():
            try:
                if (
                    not validation_removed
                    and validation_fd is not None
                    and validation_identity is not None
                ):
                    _remove_owned_directory(
                        parent_fd,
                        validation_fd,
                        expected_identity=validation_identity,
                        candidate_names=(validation_name,),
                    )
                if not succeeded and staging_fd is not None and staging_identity is not None:
                    _remove_owned_directory(
                        parent_fd,
                        staging_fd,
                        expected_identity=staging_identity,
                        candidate_names=(staging_name, destination.name),
                    )
            except BaseException as exc:
                cleanup_error = exc
        if validation_fd is not None:
            os.close(validation_fd)
        if database_fd is not None:
            os.close(database_fd)
        if config_fd is not None:
            os.close(config_fd)
        if staging_fd is not None:
            os.close(staging_fd)
        os.close(parent_fd)
        if cleanup_error is not None:
            raise cleanup_error


class BazarrPlugin(BackupPlugin):
    """Back up the exact Bazarr 1.5.6 native control-plane archive."""

    restore_capability = "partial"

    def __init__(self, name: str, version: str = "0.2.1") -> None:
        super().__init__(name=name, version=version)

    async def validate_config(self, config: Dict[str, Any]) -> bool:
        if not isinstance(config, dict):
            return False
        mode = config.get("mode")
        if mode == "source":
            return bool(
                set(config) == _SOURCE_KEYS
                and _safe_origin(config.get("base_url"))
                and isinstance(config.get("api_key"), str)
                and bool(config["api_key"])
                and not any(ord(character) < 32 for character in config["api_key"])
                and _safe_absolute_path(config.get("backup_directory"))
            )
        if mode == "restore_destination":
            return bool(
                set(config) == _RESTORE_KEYS
                and _safe_absolute_path(config.get("restore_directory"))
            )
        return False

    async def test(self, config: Dict[str, Any]) -> bool:
        if not await self.validate_config(config):
            raise ValueError("Invalid Bazarr configuration")
        if config["mode"] == "restore_destination":
            _require_isolated_restore_authorization()
            _require_isolated_restore_destination(config["restore_directory"])
            return True

        _require_read_only_backup_mount(config["backup_directory"])
        headers = {"X-API-KEY": config["api_key"]}
        try:
            async with httpx.AsyncClient(
                base_url=config["base_url"].rstrip("/"),
                headers=headers,
                timeout=_REQUEST_TIMEOUT_SECONDS,
                follow_redirects=False,
            ) as client:
                status_response = await client.get("/api/system/status")
                _validate_status_response(_response_payload(status_response))
                backups_response = await client.get("/api/system/backups")
                _validate_backups_response(_response_payload(backups_response))
        except httpx.HTTPError:
            raise ConnectionError("Failed to connect to Bazarr") from None
        return True

    async def backup(self, context: BackupContext) -> Dict[str, Any]:
        if not await self.validate_config(context.config) or context.config["mode"] != "source":
            raise ValueError("Invalid Bazarr backup configuration")
        directory = _require_read_only_backup_mount(context.config["backup_directory"])
        headers = {"X-API-KEY": context.config["api_key"]}
        try:
            async with httpx.AsyncClient(
                base_url=context.config["base_url"].rstrip("/"),
                headers=headers,
                timeout=_REQUEST_TIMEOUT_SECONDS,
                follow_redirects=False,
            ) as client:
                status_response = await client.get("/api/system/status")
                _validate_status_response(_response_payload(status_response))
                baseline_api = await _request_backup_names(client)
                baseline_local = set(_scan_native_backup_directory(directory))
                trigger_monotonic = asyncio.get_running_loop().time()
                trigger_wall_ns = time.time_ns()
                _require_backup_accepted(await client.post("/api/system/backups"))
                name, evidence = await _wait_for_unique_backup(
                    client,
                    directory,
                    baseline_api,
                    baseline_local,
                    trigger_monotonic,
                    trigger_wall_ns,
                )
        except httpx.HTTPError:
            raise ConnectionError("Failed to connect to Bazarr") from None

        with create_backup_artifact(
            self,
            context,
            prefix="bazarr-native",
            suffix=".zip",
            backup_root=BACKUP_BASE_PATH,
        ) as artifact:
            validation_root, validation_fd, validation_identity = (
                _create_private_validation_directory(artifact.temporary_path.parent)
            )
            process: BaseProcess | None = None
            try:
                process, connection = _start_backup_process(
                    directory,
                    name,
                    evidence,
                    artifact.temporary_path,
                    validation_root,
                    validation_identity,
                )
                payload = await _await_worker(
                    process,
                    connection,
                    operation="backup copy and validation",
                    timeout_seconds=_BACKUP_WORKER_TIMEOUT_SECONDS,
                )
                if not isinstance(payload, _ArchiveEvidence):
                    raise RuntimeError("Bazarr backup worker returned an invalid result")
                archive_evidence = payload
                publication_flags = os.O_RDONLY
                if hasattr(os, "O_NOFOLLOW"):
                    publication_flags |= os.O_NOFOLLOW
                publication_fd = os.open(artifact.temporary_path, publication_flags)
                publication_status = os.fstat(publication_fd)
                if (
                    not stat.S_ISREG(publication_status.st_mode)
                    or publication_status.st_dev != archive_evidence.device
                    or publication_status.st_ino != archive_evidence.inode
                    or publication_status.st_size != archive_evidence.artifact_bytes
                ):
                    os.close(publication_fd)
                    raise RuntimeError("Bazarr validated artifact changed before publication")
                artifact.publication_fd = publication_fd
                artifact.publication_sha256 = archive_evidence.sha256
            finally:
                try:
                    if process is None or not process.is_alive():
                        parent_fd = os.open(validation_root.parent, _directory_flags())
                        try:
                            _remove_owned_directory(
                                parent_fd,
                                validation_fd,
                                expected_identity=validation_identity,
                                candidate_names=(validation_root.name,),
                            )
                        finally:
                            os.close(parent_fd)
                finally:
                    os.close(validation_fd)
            artifact.sidecar_metadata.update(
                {
                    "application_version": _BAZARR_VERSION,
                    "package_version": _PACKAGE_VERSION,
                    "database_backend": "sqlite",
                    "validation": "passed",
                    "table_counts": archive_evidence.table_counts,
                }
            )
        return {"artifact_path": str(artifact.final_path)}

    async def restore(self, context: RestoreContext) -> Dict[str, Any]:
        if (
            not await self.validate_config(context.config)
            or context.config["mode"] != "restore_destination"
        ):
            raise ValueError("Invalid Bazarr restore configuration or mode")
        _require_isolated_restore_authorization()
        artifact_path = Path(context.artifact_path)
        if not artifact_path.exists() or not artifact_path.is_file() or artifact_path.is_symlink():
            raise ValueError("Bazarr restore artifact is not a regular file")
        destination = _require_isolated_restore_destination(context.config["restore_directory"])
        try:
            artifact_path.resolve(strict=True).relative_to(destination.parent)
        except ValueError:
            pass
        else:
            raise ValueError("Bazarr restore destination overlaps its artifact")
        metadata = context.metadata or {}
        expected_artifact_bytes = metadata.get("artifact_bytes")
        expected_sha256 = metadata.get("artifact_sha256")
        if (
            isinstance(expected_artifact_bytes, bool)
            or not isinstance(expected_artifact_bytes, int)
            or expected_artifact_bytes <= 0
            or not isinstance(expected_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None
        ):
            raise ValueError("Bazarr restore requires verified staging identity metadata")
        await _materialize_restore(
            artifact_path,
            destination,
            expected_artifact_bytes,
            expected_sha256,
        )
        return {
            "status": "partial",
            "message": (
                "Bazarr control-plane state restored; Sonarr, Radarr, media, and "
                "subtitle payloads remain external recovery prerequisites"
            ),
            "restored_path": str(destination),
        }

    async def get_status(self, context: BackupContext) -> Dict[str, Any]:
        try:
            await self.test(context.config)
        except Exception as exc:
            return {"status": "error", "error": str(exc)}
        return {"status": "ok"}
