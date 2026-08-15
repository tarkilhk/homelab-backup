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
import signal
import sqlite3
import stat
import subprocess
import tempfile
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from pathlib import Path, PurePath
from typing import Any, Dict
from urllib.parse import urlsplit

import yaml  # type: ignore[import-untyped]

from app.core.plugins.artifacts import create_backup_artifact
from app.core.plugins.base import BackupContext, BackupPlugin, RestoreContext

_SOURCE_KEYS = {"mode", "database_path", "repository_path"}
_RESTORE_KEYS = {"mode", "restore_directory"}
_SOURCE_ROOT = Path("/sources/profilarr")
_RESTORE_ROOTS = (Path("/tmp"), Path("/restore"))
ISOLATED_RESTORE_ENV = "HOMELAB_BACKUP_ALLOW_ISOLATED_RESTORE"
RESTORE_SENTINEL_NAME = ".profilarr-restore-destination"
RESTORE_SENTINEL_CONTENT = "profilarr-v1.1.5-isolated-restore-v1\n"
_GIT_TIMEOUT_SECONDS = 60.0
_MAX_GIT_OUTPUT_BYTES = 4 * 1024 * 1024
_CAPTURE_ATTEMPTS = 3
_MAX_COMPRESSED_BYTES = 1024 * 1024 * 1024
_MAX_UNCOMPRESSED_BYTES = 2 * 1024 * 1024 * 1024
_MAX_EXPANSION_RATIO = 100
_MAX_MANIFEST_BYTES = 16 * 1024 * 1024
_MAX_INVENTORY_FILES = 10_000
_MAX_YAML_BYTES = 16 * 1024 * 1024
_MAX_AUTHORITATIVE_BYTES = 512 * 1024 * 1024
_BACKUP_WORKER_TIMEOUT_SECONDS = 300.0
_PROBE_WORKER_TIMEOUT_SECONDS = 300.0
_RESTORE_WORKER_TIMEOUT_SECONDS = 300.0
_WORKER_STOP_TIMEOUT_SECONDS = 5.0
BACKUP_BASE_PATH = "/backups"
_AUTHORITATIVE_DIRECTORIES = (
    "regex_patterns",
    "custom_formats",
    "profiles",
    "media_management",
)
_EXPECTED_MIGRATIONS = (
    (1, "initial_schema"),
    (2, "format_renames"),
    (3, "language_import_score"),
    (4, "update_language_score_default"),
)
_EXPECTED_SCHEMA = {
    "arr_config": (
        "id",
        "name",
        "type",
        "tags",
        "arr_server",
        "api_key",
        "data_to_sync",
        "last_sync_time",
        "sync_percentage",
        "sync_method",
        "sync_interval",
        "import_as_unique",
        "import_task_id",
        "created_at",
        "updated_at",
    ),
    "auth": ("username", "password_hash", "api_key", "session_id", "created_at"),
    "backups": ("id", "filename", "created_at", "status"),
    "failed_attempts": ("id", "ip_address", "attempt_time"),
    "format_renames": ("format_name",),
    "language_import_config": ("id", "score", "updated_at"),
    "migrations": ("id", "version", "name", "applied_at"),
    "scheduled_tasks": (
        "id",
        "name",
        "type",
        "interval_minutes",
        "last_run",
        "status",
        "created_at",
    ),
    "settings": ("id", "key", "value", "updated_at"),
}
_EXPECTED_COLUMN_DEFINITIONS = {
    "arr_config": (
        ("id", "INTEGER", 0, None, 1),
        ("name", "TEXT", 1, None, 0),
        ("type", "TEXT", 1, None, 0),
        ("tags", "TEXT", 0, None, 0),
        ("arr_server", "TEXT", 1, None, 0),
        ("api_key", "TEXT", 1, None, 0),
        ("data_to_sync", "TEXT", 0, None, 0),
        ("last_sync_time", "TIMESTAMP", 0, None, 0),
        ("sync_percentage", "INTEGER", 0, "0", 0),
        ("sync_method", "TEXT", 0, "'manual'", 0),
        ("sync_interval", "INTEGER", 0, "0", 0),
        ("import_as_unique", "BOOLEAN", 0, "0", 0),
        ("import_task_id", "INTEGER", 0, "NULL", 0),
        ("created_at", "TIMESTAMP", 0, "CURRENT_TIMESTAMP", 0),
        ("updated_at", "TIMESTAMP", 0, "CURRENT_TIMESTAMP", 0),
    ),
    "auth": (
        ("username", "TEXT", 1, None, 0),
        ("password_hash", "TEXT", 1, None, 0),
        ("api_key", "TEXT", 0, None, 0),
        ("session_id", "TEXT", 0, None, 0),
        ("created_at", "TIMESTAMP", 0, "CURRENT_TIMESTAMP", 0),
    ),
    "backups": (
        ("id", "INTEGER", 0, None, 1),
        ("filename", "TEXT", 1, None, 0),
        ("created_at", "TIMESTAMP", 0, "CURRENT_TIMESTAMP", 0),
        ("status", "TEXT", 0, "'pending'", 0),
    ),
    "failed_attempts": (
        ("id", "INTEGER", 0, None, 1),
        ("ip_address", "TEXT", 1, None, 0),
        ("attempt_time", "TIMESTAMP", 0, "CURRENT_TIMESTAMP", 0),
    ),
    "format_renames": (("format_name", "TEXT", 1, None, 1),),
    "language_import_config": (
        ("id", "INTEGER", 0, None, 1),
        ("score", "INTEGER", 1, "-99999", 0),
        ("updated_at", "TIMESTAMP", 0, "CURRENT_TIMESTAMP", 0),
    ),
    "migrations": (
        ("id", "INTEGER", 0, None, 1),
        ("version", "INTEGER", 1, None, 0),
        ("name", "TEXT", 1, None, 0),
        ("applied_at", "TIMESTAMP", 0, "CURRENT_TIMESTAMP", 0),
    ),
    "scheduled_tasks": (
        ("id", "INTEGER", 0, None, 1),
        ("name", "TEXT", 1, None, 0),
        ("type", "TEXT", 1, None, 0),
        ("interval_minutes", "INTEGER", 1, None, 0),
        ("last_run", "TIMESTAMP", 0, None, 0),
        ("status", "TEXT", 0, "'pending'", 0),
        ("created_at", "TIMESTAMP", 0, "CURRENT_TIMESTAMP", 0),
    ),
    "settings": (
        ("id", "INTEGER", 0, None, 1),
        ("key", "TEXT", 1, None, 0),
        ("value", "TEXT", 0, None, 0),
        ("updated_at", "TIMESTAMP", 0, "CURRENT_TIMESTAMP", 0),
    ),
}
_EXPECTED_INDEXES = {
    "arr_config": (("sqlite_autoindex_arr_config_1", 1, "u", 0, ("name",)),),
    "auth": (),
    "backups": (),
    "failed_attempts": (),
    "format_renames": (("sqlite_autoindex_format_renames_1", 1, "pk", 0, ("format_name",)),),
    "language_import_config": (),
    "migrations": (),
    "scheduled_tasks": (),
    "settings": (("sqlite_autoindex_settings_1", 1, "u", 0, ("key",)),),
}
_FORBIDDEN_PATHS = {
    Path("/"),
    Path("/app"),
    Path("/backups"),
    Path("/config"),
    Path("/restore"),
    Path("/sources"),
    Path("/tmp"),
}


@dataclass(frozen=True)
class _InventoryEntry:
    path: str
    mode: int
    size: int
    sha256: str


@dataclass(frozen=True)
class _GitFence:
    branch: str
    head: str
    refs: tuple[tuple[str, str], ...]
    inventory: tuple[_InventoryEntry, ...]


def _safe_path(value: object) -> Path | None:
    if not isinstance(value, str) or not value or any(ord(char) < 32 for char in value):
        return None
    path = Path(value)
    if not path.is_absolute() or path in _FORBIDDEN_PATHS:
        return None
    if ".." in PurePath(value).parts:
        return None
    return path


def _is_beneath(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return path != root


def _require_source_mount(path: Path, *, kind: str) -> os.stat_result:
    try:
        status = path.lstat()
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"Profilarr {kind} was not found") from exc
    except OSError as exc:
        raise RuntimeError(f"Profilarr {kind} could not be inspected") from exc
    if stat.S_ISLNK(status.st_mode):
        raise ValueError(f"Profilarr {kind} must not be a symlink")
    expected = stat.S_ISREG if kind == "database" else stat.S_ISDIR
    if not expected(status.st_mode):
        expected_name = "regular file" if kind == "database" else "directory"
        raise ValueError(f"Profilarr {kind} must be a {expected_name}")
    try:
        if path.resolve(strict=True) != path:
            raise ValueError(f"Profilarr {kind} must use a canonical path without symlinks")
    except OSError as exc:
        raise RuntimeError(f"Profilarr {kind} could not be resolved") from exc
    if not os.path.ismount(path):
        raise RuntimeError(f"Profilarr {kind} must be an exact dedicated mount")
    try:
        if not os.statvfs(path).f_flag & os.ST_RDONLY:
            raise RuntimeError(f"Profilarr {kind} mount must be read-only")
    except OSError as exc:
        raise RuntimeError(f"Profilarr {kind} mount could not be inspected") from exc
    return status


def _safe_repository_url(value: object) -> bool:
    if value in {None, ""}:
        return True
    if not isinstance(value, str) or any(ord(char) < 32 for char in value):
        return False
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    return bool(
        parsed.scheme in {"http", "https", "ssh"}
        and parsed.hostname
        and parsed.username is None
        and parsed.password is None
        and parsed.query == ""
        and parsed.fragment == ""
        and (port is None or 1 <= port <= 65535)
    )


def _validate_database(path: Path) -> None:
    for suffix in ("-wal", "-shm", "-journal"):
        if path.with_name(f"{path.name}{suffix}").exists():
            raise RuntimeError("Profilarr database has unsupported journal residue")
    try:
        with path.open("rb") as source:
            if source.read(16) != b"SQLite format 3\x00":
                raise RuntimeError("Profilarr SQLite database header is invalid")
        connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=5.0)
        try:
            journal_mode = connection.execute("PRAGMA journal_mode").fetchone()
            if journal_mode != ("delete",):
                raise RuntimeError("Profilarr database must use rollback journal mode")
            if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise RuntimeError("Profilarr SQLite database failed integrity validation")
            if connection.execute("PRAGMA foreign_key_check").fetchall():
                raise RuntimeError("Profilarr SQLite database failed foreign key validation")
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
            }
            if tables != set(_EXPECTED_SCHEMA):
                raise RuntimeError("Profilarr SQLite schema tables are incompatible")
            for table, expected_columns in _EXPECTED_SCHEMA.items():
                table_info = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
                columns = tuple(row[1] for row in table_info)
                if columns != expected_columns:
                    raise RuntimeError("Profilarr SQLite schema columns are incompatible")
                if (
                    tuple(tuple(row[1:6]) for row in table_info)
                    != _EXPECTED_COLUMN_DEFINITIONS[table]
                ):
                    raise RuntimeError("Profilarr SQLite column definitions are incompatible")
                indexes = []
                for row in connection.execute(f'PRAGMA index_list("{table}")').fetchall():
                    index_name = row[1]
                    index_columns = tuple(
                        index_row[2]
                        for index_row in connection.execute(
                            f'PRAGMA index_info("{index_name}")'
                        ).fetchall()
                    )
                    indexes.append((index_name, row[2], row[3], row[4], index_columns))
                if tuple(indexes) != _EXPECTED_INDEXES[table]:
                    raise RuntimeError("Profilarr SQLite schema indexes are incompatible")
                if connection.execute(f'PRAGMA foreign_key_list("{table}")').fetchall():
                    raise RuntimeError("Profilarr SQLite schema foreign keys are incompatible")
            if connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('view', 'trigger')"
            ).fetchall():
                raise RuntimeError("Profilarr SQLite schema objects are incompatible")
            migrations = tuple(
                connection.execute(
                    "SELECT version, name FROM migrations ORDER BY version"
                ).fetchall()
            )
            if migrations != _EXPECTED_MIGRATIONS:
                raise RuntimeError("Profilarr database migration history is incompatible")
            auth_count = connection.execute("SELECT COUNT(*) FROM auth").fetchone()[0]
            if auth_count > 1:
                raise RuntimeError("Profilarr database auth state is incompatible")
            repository_rows = connection.execute(
                "SELECT value FROM settings WHERE key = 'gitRepo'"
            ).fetchall()
            if len(repository_rows) > 1 or (
                repository_rows and not _safe_repository_url(repository_rows[0][0])
            ):
                raise RuntimeError("Profilarr linked repository configuration is unsafe")
        finally:
            connection.close()
    except RuntimeError:
        raise
    except sqlite3.DatabaseError as exc:
        raise RuntimeError("Profilarr SQLite database is invalid") from exc
    except OSError as exc:
        raise RuntimeError("Profilarr SQLite database could not be read") from exc


def _git_environment() -> dict[str, str]:
    return {
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "HOME": "/nonexistent",
        "LC_ALL": "C",
        "PATH": os.environ.get("PATH", ""),
    }


def _run_git(
    repository: Path,
    *arguments: str,
    allowed_returncodes: frozenset[int] = frozenset({0}),
) -> str:
    pass_fds: set[int] = set()
    for value in (str(repository), *arguments):
        match = re.match(r"^/proc/self/fd/([0-9]+)(?:/|$)", value)
        if match is not None:
            pass_fds.add(int(match.group(1)))
    try:
        with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as diagnostics:
            completed = subprocess.run(
                (
                    "git",
                    "--no-optional-locks",
                    "-c",
                    "core.fsmonitor=false",
                    "-c",
                    "core.hooksPath=/dev/null",
                    "-c",
                    "credential.helper=",
                    "-C",
                    str(repository),
                    *arguments,
                ),
                check=False,
                stdout=output,
                stderr=diagnostics,
                timeout=_GIT_TIMEOUT_SECONDS,
                env=_git_environment(),
                pass_fds=tuple(sorted(pass_fds)),
            )
            if completed.returncode not in allowed_returncodes:
                operation = arguments[0] if arguments else "command"
                raise RuntimeError(f"Profilarr Git {operation} object integrity check failed")
            output.seek(0)
            payload = output.read(_MAX_GIT_OUTPUT_BYTES + 1)
    except RuntimeError:
        raise
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("Profilarr Git command could not complete") from exc
    if len(payload) > _MAX_GIT_OUTPUT_BYTES:
        raise RuntimeError("Profilarr Git command output exceeds safe limits")
    try:
        return payload.decode("utf-8").strip()
    except UnicodeError as exc:
        raise RuntimeError("Profilarr Git command output is invalid") from exc


def _require_no_git_operation(repository: Path) -> None:
    git_directory = repository / ".git"
    if not git_directory.is_dir() or git_directory.is_symlink():
        raise RuntimeError("Profilarr Git repository metadata is invalid")
    markers = {
        "MERGE_HEAD": "merge",
        "CHERRY_PICK_HEAD": "cherry-pick",
        "REVERT_HEAD": "revert",
        "BISECT_LOG": "bisect",
        "rebase-apply": "rebase",
        "rebase-merge": "rebase",
        "sequencer": "sequencer",
    }
    for name, operation in markers.items():
        if (git_directory / name).exists():
            raise RuntimeError(f"Profilarr Git {operation} operation is active")
    if any(path.is_file() for path in git_directory.rglob("*.lock")):
        raise RuntimeError("Profilarr Git lock operation is active")
    if (git_directory / "shallow").exists():
        raise RuntimeError("Profilarr Git repository is shallow")
    if (git_directory / "objects/info/alternates").exists():
        raise RuntimeError("Profilarr Git repository uses an alternate object store")


def _validate_repository(repository: Path) -> None:
    _require_no_git_operation(repository)
    try:
        branch = _run_git(repository, "symbolic-ref", "--quiet", "--short", "HEAD")
    except RuntimeError as exc:
        raise RuntimeError("Profilarr Git repository has detached or unborn HEAD") from exc
    if not branch:
        raise RuntimeError("Profilarr Git repository has detached symbolic branch")
    try:
        head = _run_git(repository, "rev-parse", "--verify", "HEAD")
    except RuntimeError as exc:
        raise RuntimeError("Profilarr Git repository has unborn HEAD") from exc
    if _run_git(repository, "rev-parse", "--show-object-format") != "sha1":
        raise RuntimeError("Profilarr Git object format is unsupported")
    if _run_git(repository, "rev-parse", f"refs/heads/{branch}") != head:
        raise RuntimeError("Profilarr Git symbolic branch does not match HEAD")

    partial = (
        _run_git(
            repository,
            "config",
            "--get-regexp",
            "^(extensions\\.partialclone|remote\\..*\\.promisor)$",
        )
        if _git_config_has_partial_clone(repository)
        else ""
    )
    if partial:
        raise RuntimeError("Profilarr Git repository uses a partial or promisor clone")
    if _run_git(repository, "for-each-ref", "--format=%(refname)", "refs/replace"):
        raise RuntimeError("Profilarr Git repository uses replace refs")
    if (repository / ".gitmodules").exists() or "160000" in _run_git(
        repository, "ls-tree", "-r", "HEAD"
    ):
        raise RuntimeError("Profilarr Git repository uses submodules")

    status_output = _run_git(repository, "status", "--porcelain=v2", "--untracked-files=all")
    if status_output:
        if any(line.startswith("? ") for line in status_output.splitlines()):
            raise RuntimeError("Profilarr Git repository has untracked files")
        raise RuntimeError("Profilarr Git repository is not clean")
    ignored = _run_git(
        repository,
        "ls-files",
        "--others",
        "--ignored",
        "--exclude-standard",
        "--",
        *_AUTHORITATIVE_DIRECTORIES,
    )
    if ignored:
        raise RuntimeError("Profilarr Git repository has ignored authoritative files")

    tracked = set(_run_git(repository, "ls-files", "--", *_AUTHORITATIVE_DIRECTORIES).splitlines())
    for directory_name in _AUTHORITATIVE_DIRECTORIES:
        directory = repository / directory_name
        if not directory.is_dir() or directory.is_symlink():
            raise RuntimeError("Profilarr Git authoritative directory is invalid")
        for path in directory.rglob("*"):
            status = path.lstat()
            if stat.S_ISDIR(status.st_mode):
                continue
            if not stat.S_ISREG(status.st_mode):
                raise RuntimeError("Profilarr Git authoritative files must be regular")
            if stat.S_IMODE(status.st_mode) & 0o111:
                raise RuntimeError("Profilarr Git authoritative files must not be executable")
            relative = path.relative_to(repository).as_posix()
            if relative not in tracked:
                raise RuntimeError("Profilarr Git repository has untracked authoritative files")
            try:
                with path.open("rb") as source:
                    prefix = source.read(200)
            except OSError as exc:
                raise RuntimeError("Profilarr Git authoritative file could not be read") from exc
            if prefix.startswith(b"version https://git-lfs.github.com/spec/v1"):
                raise RuntimeError("Profilarr Git repository uses LFS")
    _run_git(repository, "fsck", "--full", "--strict")


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_authoritative_yaml(relative: str, parsed: object) -> None:
    if not isinstance(parsed, dict) or not parsed:
        raise RuntimeError("Profilarr Git authoritative YAML contract is invalid")
    pure_path = PurePath(relative)
    if pure_path.suffix not in {".yml", ".yaml"}:
        raise RuntimeError("Profilarr Git authoritative file type is invalid")
    directory = pure_path.parts[0]
    keys = set(parsed)
    if not all(isinstance(key, str) for key in keys):
        raise RuntimeError("Profilarr Git authoritative YAML contract is invalid")
    if directory == "regex_patterns":
        required = {"name", "pattern"}
        allowed = required | {"description", "tags", "tests"}
    elif directory == "custom_formats":
        required = {"name", "conditions"}
        allowed = required | {"description", "tags", "tests"}
    elif directory == "profiles":
        required = {"name", "custom_formats", "qualities"}
        allowed = required | {
            "description",
            "tags",
            "upgradesAllowed",
            "minCustomFormatScore",
            "upgradeUntilScore",
            "minScoreIncrement",
            "custom_formats_radarr",
            "custom_formats_sonarr",
            "upgrade_until",
            "language",
        }
    elif directory == "media_management" and pure_path.name in {"misc.yml", "misc.yaml"}:
        required = allowed = {"radarr", "sonarr"}
    elif directory == "media_management" and pure_path.name in {"naming.yml", "naming.yaml"}:
        required = allowed = {"radarr", "sonarr"}
    elif directory == "media_management" and pure_path.name in {
        "quality_definitions.yml",
        "quality_definitions.yaml",
    }:
        required = allowed = {"qualityDefinitions"}
    else:
        raise RuntimeError("Profilarr Git authoritative YAML contract is invalid")
    if not required.issubset(keys) or not keys.issubset(allowed):
        raise RuntimeError("Profilarr Git authoritative YAML contract is invalid")
    if directory in {"regex_patterns", "custom_formats", "profiles"} and (
        not isinstance(parsed.get("name"), str) or not parsed["name"]
    ):
        raise RuntimeError("Profilarr Git authoritative YAML contract is invalid")
    if directory == "regex_patterns" and not isinstance(parsed.get("pattern"), str):
        raise RuntimeError("Profilarr Git authoritative YAML contract is invalid")
    if directory == "custom_formats" and not isinstance(parsed.get("conditions"), list):
        raise RuntimeError("Profilarr Git authoritative YAML contract is invalid")
    if directory == "profiles" and (
        not isinstance(parsed.get("custom_formats"), list)
        or not isinstance(parsed.get("qualities"), list)
    ):
        raise RuntimeError("Profilarr Git authoritative YAML contract is invalid")
    if directory == "media_management" and not all(
        isinstance(value, dict) for value in parsed.values()
    ):
        raise RuntimeError("Profilarr Git authoritative YAML contract is invalid")


def _repository_inventory(repository: Path) -> tuple[_InventoryEntry, ...]:
    tracked = set(_run_git(repository, "ls-files", "--", *_AUTHORITATIVE_DIRECTORIES).splitlines())
    if not tracked or len(tracked) > _MAX_INVENTORY_FILES:
        raise RuntimeError("Profilarr Git authoritative inventory exceeds safe limits")
    inventory: list[_InventoryEntry] = []
    total_bytes = 0
    for relative in sorted(tracked):
        path = repository / relative
        try:
            status = path.lstat()
        except OSError as exc:
            raise RuntimeError("Profilarr Git authoritative file disappeared") from exc
        if not stat.S_ISREG(status.st_mode):
            raise RuntimeError("Profilarr Git authoritative files must be regular")
        if stat.S_IMODE(status.st_mode) & 0o111:
            raise RuntimeError("Profilarr Git authoritative files must not be executable")
        if status.st_size <= 0 or status.st_size > _MAX_YAML_BYTES:
            raise RuntimeError("Profilarr Git authoritative YAML exceeds safe limits")
        total_bytes += status.st_size
        if total_bytes > _MAX_AUTHORITATIVE_BYTES:
            raise RuntimeError("Profilarr Git authoritative inventory exceeds safe limits")
        try:
            parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            raise RuntimeError("Profilarr Git authoritative YAML is invalid") from exc
        _validate_authoritative_yaml(relative, parsed)
        inventory.append(
            _InventoryEntry(
                path=relative,
                mode=0o644,
                size=status.st_size,
                sha256=_hash_file(path),
            )
        )
    return tuple(inventory)


def _repository_fence(repository: Path) -> _GitFence:
    _validate_repository(repository)
    branch = _run_git(repository, "symbolic-ref", "--quiet", "--short", "HEAD")
    head = _run_git(repository, "rev-parse", "--verify", "HEAD")
    ref_output = _run_git(
        repository,
        "for-each-ref",
        "--format=%(refname)%09%(objectname)",
    )
    refs: list[tuple[str, str]] = []
    for line in ref_output.splitlines():
        try:
            name, object_id = line.split("\t", 1)
        except ValueError as exc:
            raise RuntimeError("Profilarr Git ref inventory is malformed") from exc
        if not name.startswith("refs/") or re.fullmatch(r"[0-9a-f]{40}", object_id) is None:
            raise RuntimeError("Profilarr Git ref inventory is malformed")
        refs.append((name, object_id))
    if not refs or len(refs) > _MAX_INVENTORY_FILES:
        raise RuntimeError("Profilarr Git repository has no refs")
    return _GitFence(
        branch=branch,
        head=head,
        refs=tuple(sorted(refs)),
        inventory=_repository_inventory(repository),
    )


def _create_sqlite_snapshot(source_path: Path, destination_path: Path) -> None:
    descriptor = os.open(destination_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    try:
        source = sqlite3.connect(
            f"{source_path.as_uri()}?mode=ro",
            uri=True,
            timeout=5.0,
        )
        destination = sqlite3.connect(destination_path)
        try:
            page_size = source.execute("PRAGMA page_size").fetchone()[0]
            page_count = source.execute("PRAGMA page_count").fetchone()[0]
            if page_size * page_count > _MAX_UNCOMPRESSED_BYTES:
                raise RuntimeError("Profilarr database exceeds safe backup limits")

            def enforce_size(_status: int, _remaining: int, total: int) -> None:
                if page_size * total > _MAX_UNCOMPRESSED_BYTES:
                    raise RuntimeError("Profilarr database exceeds safe backup limits")

            source.backup(
                destination,
                pages=256,
                progress=enforce_size,
                sleep=0.01,
            )
            destination.commit()
        finally:
            destination.close()
            source.close()
        os.chmod(destination_path, 0o600)
        if destination_path.stat().st_size > _MAX_UNCOMPRESSED_BYTES:
            raise RuntimeError("Profilarr database exceeds safe backup limits")
        _validate_database(destination_path)
    except BaseException:
        destination_path.unlink(missing_ok=True)
        raise


def _create_bundle(repository: Path, bundle_path: Path) -> None:
    _run_git(repository, "bundle", "create", str(bundle_path), "--all")
    try:
        if bundle_path.stat().st_size > _MAX_UNCOMPRESSED_BYTES:
            raise RuntimeError("Profilarr Git bundle exceeds safe backup limits")
        os.chmod(bundle_path, 0o600)
    except OSError as exc:
        raise RuntimeError("Profilarr Git bundle was not created") from exc
    _run_git(repository, "bundle", "verify", str(bundle_path))


def _schema_contract_sha256() -> str:
    contract = {
        "columns": _EXPECTED_SCHEMA,
        "column_definitions": _EXPECTED_COLUMN_DEFINITIONS,
        "indexes": _EXPECTED_INDEXES,
        "foreign_keys": {table: () for table in _EXPECTED_SCHEMA},
        "views": (),
        "triggers": (),
    }
    return hashlib.sha256(
        json.dumps(contract, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _manifest_for_capture(
    database_path: Path,
    bundle_path: Path,
    fence: _GitFence,
    repository: Path,
) -> dict[str, object]:
    database_status = database_path.stat()
    bundle_status = bundle_path.stat()
    if database_status.st_size + bundle_status.st_size > _MAX_UNCOMPRESSED_BYTES:
        raise RuntimeError("Profilarr composite state exceeds safe backup limits")
    table_counts: dict[str, int] = {}
    with sqlite3.connect(f"{database_path.as_uri()}?mode=ro", uri=True) as connection:
        for table in sorted(_EXPECTED_SCHEMA):
            table_counts[table] = connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[
                0
            ]
    refs = [{"name": name, "object_id": object_id} for name, object_id in fence.refs]
    inventory = [
        {
            "path": entry.path,
            "mode": entry.mode,
            "size": entry.size,
            "sha256": entry.sha256,
        }
        for entry in fence.inventory
    ]
    schema_sha256 = _schema_contract_sha256()
    ref_sha256 = hashlib.sha256(
        json.dumps(refs, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "format_version": 1,
        "validation_version": 1,
        "profilarr_version": "1.1.5",
        "source_commit": "21c8eaeb93241588323672866854275ff7dbed67",
        "image_tag": "santiagosayshey/profilarr:v1.1.5",
        "image_digest": ("sha256:4d37d6b2039697c842211d0879d4d6df19c1dcbd22a962ed67ba3de8f81dfdad"),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "tools": {
            "sqlite": sqlite3.sqlite_version,
            "git": _run_git(repository, "--version"),
        },
        "limits": {
            "compressed_bytes": _MAX_COMPRESSED_BYTES,
            "uncompressed_bytes": _MAX_UNCOMPRESSED_BYTES,
            "expansion_ratio": _MAX_EXPANSION_RATIO,
            "manifest_bytes": _MAX_MANIFEST_BYTES,
            "inventory_files": _MAX_INVENTORY_FILES,
            "yaml_bytes": _MAX_YAML_BYTES,
            "authoritative_bytes": _MAX_AUTHORITATIVE_BYTES,
        },
        "database": {
            "size": database_status.st_size,
            "sha256": _hash_file(database_path),
            "schema_sha256": schema_sha256,
            "integrity": "ok",
            "migrations": [
                {"version": version, "name": name} for version, name in _EXPECTED_MIGRATIONS
            ],
            "table_counts": table_counts,
        },
        "bundle": {
            "size": bundle_status.st_size,
            "sha256": _hash_file(bundle_path),
            "object_format": "sha1",
        },
        "git": {
            "object_format": "sha1",
            "branch": fence.branch,
            "head": fence.head,
            "refs": refs,
            "ref_sha256": ref_sha256,
            "clean": True,
            "inventory": inventory,
        },
    }


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = (stat.S_IFREG | 0o600) << 16
    return info


def _write_zip_bytes(
    archive: zipfile.ZipFile,
    name: str,
    payload: bytes,
) -> None:
    archive.writestr(_zip_info(name), payload)


def _write_zip_file(archive: zipfile.ZipFile, name: str, source_path: Path) -> None:
    with source_path.open("rb") as source, archive.open(_zip_info(name), "w") as target:
        shutil.copyfileobj(source, target, length=1024 * 1024)


def _write_capture_archive(
    archive_path: Path,
    database_path: Path,
    bundle_path: Path,
    manifest: dict[str, object],
) -> None:
    descriptor = os.open(archive_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as raw:
            with zipfile.ZipFile(raw, mode="w", allowZip64=True) as archive:
                _write_zip_file(archive, "profilarr.db", database_path)
                _write_zip_file(archive, "repository.bundle", bundle_path)
                _write_zip_bytes(
                    archive,
                    "manifest.json",
                    json.dumps(
                        manifest,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8"),
                )
            raw.flush()
            os.fsync(raw.fileno())
    except BaseException:
        archive_path.unlink(missing_ok=True)
        raise


def _capture_composite(
    database_path: Path,
    repository: Path,
    archive_path: Path,
    workspace_root: Path,
    expected_source_identities: tuple[tuple[int, int], tuple[int, int]] | None = None,
) -> dict[str, object]:
    last_error: RuntimeError | None = None
    for attempt in range(_CAPTURE_ATTEMPTS):
        workspace = workspace_root / f"attempt-{attempt}"
        workspace.mkdir(mode=0o700)
        os.chmod(workspace, 0o700)
        try:
            database_snapshot = workspace / "profilarr.db"
            bundle = workspace / "repository.bundle"
            database_before = database_path.lstat()
            repository_before = repository.lstat()
            if not stat.S_ISREG(database_before.st_mode) or not stat.S_ISDIR(
                repository_before.st_mode
            ):
                raise RuntimeError("Profilarr source identity changed during backup")
            if (
                expected_source_identities is not None
                and (
                    (database_before.st_dev, database_before.st_ino),
                    (repository_before.st_dev, repository_before.st_ino),
                )
                != expected_source_identities
            ):
                raise RuntimeError("Profilarr source mount identity changed during backup")
            _validate_source_content(database_path, repository)
            before = _repository_fence(repository)
            _create_sqlite_snapshot(database_path, database_snapshot)
            _create_bundle(repository, bundle)
            after = _repository_fence(repository)
            database_after = database_path.lstat()
            repository_after = repository.lstat()
            if (
                before != after
                or (database_before.st_dev, database_before.st_ino)
                != (database_after.st_dev, database_after.st_ino)
                or (repository_before.st_dev, repository_before.st_ino)
                != (repository_after.st_dev, repository_after.st_ino)
            ):
                last_error = RuntimeError("Profilarr source state changed during backup")
                continue
            manifest = _manifest_for_capture(database_snapshot, bundle, before, repository)
            _write_capture_archive(archive_path, database_snapshot, bundle, manifest)
            return manifest
        finally:
            shutil.rmtree(workspace, ignore_errors=False)
    raise last_error or RuntimeError("Profilarr Git repository did not stabilize")


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


def _worker_error_message(exc: BaseException, *, operation: str) -> str:
    message = str(exc)
    if isinstance(exc, (ValueError, RuntimeError, TimeoutError)) and message.startswith(
        "Profilarr "
    ):
        return message
    if isinstance(exc, FileNotFoundError):
        return f"Profilarr {operation} resource was not found"
    if isinstance(exc, PermissionError):
        return f"Profilarr {operation} lacked required filesystem permission"
    if isinstance(exc, OSError):
        return f"Profilarr {operation} filesystem I/O failed"
    return f"Profilarr {operation} failed"


def _backup_process_worker(
    database_path: Path,
    repository: Path,
    archive_path: Path,
    workspace_root: Path,
    expected_source_identities: tuple[tuple[int, int], tuple[int, int]],
    connection: Connection,
) -> None:
    try:
        os.setsid()
        _capture_composite(
            database_path,
            repository,
            archive_path,
            workspace_root,
            expected_source_identities,
        )
        validation = workspace_root / "validation"
        validation.mkdir(mode=0o700)
        try:
            manifest, _database_snapshot, _bundle = _validate_artifact_to_workspace(
                archive_path,
                validation,
                expected_size=archive_path.stat().st_size,
                expected_sha256=_hash_file(archive_path),
            )
        finally:
            shutil.rmtree(validation, ignore_errors=False)
        git_manifest = manifest.get("git")
        database_manifest = manifest.get("database")
        refs = git_manifest.get("refs") if isinstance(git_manifest, dict) else None
        table_counts = (
            database_manifest.get("table_counts") if isinstance(database_manifest, dict) else None
        )
        connection.send(
            (
                "ok",
                "",
                len(refs) if isinstance(refs, list) else 0,
                len(table_counts) if isinstance(table_counts, dict) else 0,
            )
        )
    except BaseException as exc:
        archive_path.unlink(missing_ok=True)
        try:
            connection.send(
                (
                    _worker_error_kind(exc),
                    _worker_error_message(exc, operation="backup"),
                    0,
                    0,
                )
            )
        except (BrokenPipeError, EOFError, OSError):
            pass
        raise SystemExit(1) from None
    finally:
        connection.close()


def _probe_process_worker(
    database_path: Path,
    repository: Path,
    connection: Connection,
) -> None:
    try:
        os.setsid()
        _validate_source_content(database_path, repository)
        connection.send(("ok", "", 0, 0))
    except BaseException as exc:
        try:
            connection.send(
                (
                    _worker_error_kind(exc),
                    _worker_error_message(exc, operation="source probe"),
                    0,
                    0,
                )
            )
        except (BrokenPipeError, EOFError, OSError):
            pass
        raise SystemExit(1) from None
    finally:
        connection.close()


def _start_probe_process(
    database_path: Path,
    repository: Path,
) -> tuple[BaseProcess, Connection]:
    context = multiprocessing.get_context("spawn")
    receiving, sending = context.Pipe(duplex=False)
    process = context.Process(
        target=_probe_process_worker,
        args=(database_path, repository, sending),
        name="profilarr-source-probe",
        daemon=True,
    )
    process.start()
    sending.close()
    return process, receiving


def _start_backup_process(
    database_path: Path,
    repository: Path,
    archive_path: Path,
    workspace_root: Path,
    expected_source_identities: tuple[tuple[int, int], tuple[int, int]],
) -> tuple[BaseProcess, Connection]:
    context = multiprocessing.get_context("spawn")
    receiving, sending = context.Pipe(duplex=False)
    process = context.Process(
        target=_backup_process_worker,
        args=(
            database_path,
            repository,
            archive_path,
            workspace_root,
            expected_source_identities,
            sending,
        ),
        name="profilarr-backup",
        daemon=True,
    )
    process.start()
    sending.close()
    return process, receiving


async def _join_worker_process(process: BaseProcess, timeout_seconds: float) -> None:
    await asyncio.to_thread(process.join, timeout_seconds)


def _signal_worker_group(process: BaseProcess, signal_number: int) -> None:
    pid = process.pid
    if isinstance(pid, int) and pid > 0:
        try:
            os.killpg(pid, signal_number)
            return
        except ProcessLookupError:
            pass
        except OSError:
            pass
    if process.is_alive():
        if signal_number == signal.SIGTERM:
            process.terminate()
        else:
            process.kill()


async def _stop_worker_process(process: BaseProcess, *, operation: str) -> None:
    if not process.is_alive():
        _signal_worker_group(process, signal.SIGKILL)
        await _join_worker_process(process, _WORKER_STOP_TIMEOUT_SECONDS)
        if process.exitcode is None:
            raise RuntimeError(f"Profilarr {operation} worker could not be reaped")
        return
    _signal_worker_group(process, signal.SIGTERM)
    await _join_worker_process(process, _WORKER_STOP_TIMEOUT_SECONDS)
    _signal_worker_group(process, signal.SIGKILL)
    if process.is_alive():
        await _join_worker_process(process, _WORKER_STOP_TIMEOUT_SECONDS)
    if process.is_alive() or process.exitcode is None:
        raise RuntimeError(f"Profilarr {operation} worker could not be stopped")


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
    result: tuple[str, str, int, int] | None,
    *,
    operation: str,
) -> tuple[int, int]:
    if result is None:
        raise RuntimeError(f"Profilarr {operation} worker returned no result")
    kind, message, ref_count, table_count = result
    if kind == "ok":
        return ref_count, table_count
    safe_message = message or f"Profilarr {operation} failed"
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
) -> tuple[int, int]:
    try:
        await _join_worker_process(process, timeout_seconds)
        if process.is_alive():
            await _stop_worker_process_before_return(process, operation=operation)
            raise TimeoutError(f"Profilarr {operation} timed out")
        result: tuple[str, str, int, int] | None = None
        if connection.poll():
            received = connection.recv()
            if (
                isinstance(received, tuple)
                and len(received) == 4
                and isinstance(received[0], str)
                and isinstance(received[1], str)
                and isinstance(received[2], int)
                and isinstance(received[3], int)
            ):
                result = received
        counts = _raise_worker_result(result, operation=operation)
        if process.exitcode != 0:
            raise RuntimeError(f"Profilarr {operation} worker failed")
        return counts
    except asyncio.CancelledError:
        await _stop_worker_process_before_return(process, operation=operation)
        raise
    except BaseException:
        if process.is_alive():
            await _stop_worker_process_before_return(process, operation=operation)
        raise
    finally:
        connection.close()


async def _probe_source_async(config: Dict[str, Any]) -> None:
    database = Path(config["database_path"])
    repository = Path(config["repository_path"])
    before = _source_mount_identities(database, repository)
    process, connection = _start_probe_process(database, repository)
    counts = await _await_worker(
        process,
        connection,
        operation="source probe",
        timeout_seconds=_PROBE_WORKER_TIMEOUT_SECONDS,
    )
    if counts != (0, 0):
        raise RuntimeError("Profilarr source probe worker returned an invalid result")
    if _source_mount_identities(database, repository) != before:
        raise RuntimeError("Profilarr source mount identity changed during probe")


def _copy_zip_member(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    destination: Path,
) -> None:
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with archive.open(info, "r") as source, os.fdopen(descriptor, "wb") as target:
            shutil.copyfileobj(source, target, length=1024 * 1024)
            target.flush()
            os.fsync(target.fileno())
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


def _validate_manifest(manifest: object) -> dict[str, object]:
    if not isinstance(manifest, dict):
        raise RuntimeError("Profilarr artifact manifest is invalid")
    required = {
        "format_version",
        "validation_version",
        "profilarr_version",
        "source_commit",
        "image_tag",
        "image_digest",
        "created_at",
        "tools",
        "limits",
        "database",
        "bundle",
        "git",
    }
    if set(manifest) != required:
        raise RuntimeError("Profilarr artifact manifest contract is invalid")
    if (
        manifest.get("format_version") != 1
        or manifest.get("validation_version") != 1
        or manifest.get("profilarr_version") != "1.1.5"
        or manifest.get("source_commit") != "21c8eaeb93241588323672866854275ff7dbed67"
        or manifest.get("image_tag") != "santiagosayshey/profilarr:v1.1.5"
        or manifest.get("image_digest")
        != "sha256:4d37d6b2039697c842211d0879d4d6df19c1dcbd22a962ed67ba3de8f81dfdad"
    ):
        raise RuntimeError("Profilarr artifact version is incompatible")
    created_at = manifest.get("created_at")
    if not isinstance(created_at, str):
        raise RuntimeError("Profilarr artifact creation time is invalid")
    try:
        parsed_created_at = datetime.fromisoformat(created_at)
    except ValueError as exc:
        raise RuntimeError("Profilarr artifact creation time is invalid") from exc
    if parsed_created_at.tzinfo is None:
        raise RuntimeError("Profilarr artifact creation time is invalid")
    for key in ("tools", "limits", "database", "bundle", "git"):
        if not isinstance(manifest.get(key), dict):
            raise RuntimeError("Profilarr artifact manifest structure is invalid")
    database = manifest["database"]
    bundle = manifest["bundle"]
    git = manifest["git"]
    tools = manifest["tools"]
    limits = manifest["limits"]
    if (
        not isinstance(tools, dict)
        or set(tools) != {"sqlite", "git"}
        or not isinstance(tools.get("sqlite"), str)
        or not isinstance(tools.get("git"), str)
        or re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,3}", tools["sqlite"]) is None
        or re.fullmatch(r"git version [0-9]+(?:\.[0-9]+){1,3}", tools["git"]) is None
    ):
        raise RuntimeError("Profilarr artifact tool identity is invalid")
    if limits != {
        "compressed_bytes": _MAX_COMPRESSED_BYTES,
        "uncompressed_bytes": _MAX_UNCOMPRESSED_BYTES,
        "expansion_ratio": _MAX_EXPANSION_RATIO,
        "manifest_bytes": _MAX_MANIFEST_BYTES,
        "inventory_files": _MAX_INVENTORY_FILES,
        "yaml_bytes": _MAX_YAML_BYTES,
        "authoritative_bytes": _MAX_AUTHORITATIVE_BYTES,
    }:
        raise RuntimeError("Profilarr artifact limits are incompatible")
    if not isinstance(database, dict) or set(database) != {
        "size",
        "sha256",
        "schema_sha256",
        "integrity",
        "migrations",
        "table_counts",
    }:
        raise RuntimeError("Profilarr artifact database manifest is invalid")
    if not isinstance(bundle, dict) or set(bundle) != {
        "size",
        "sha256",
        "object_format",
    }:
        raise RuntimeError("Profilarr artifact bundle manifest is invalid")
    if not isinstance(git, dict) or set(git) != {
        "object_format",
        "branch",
        "head",
        "refs",
        "ref_sha256",
        "clean",
        "inventory",
    }:
        raise RuntimeError("Profilarr artifact Git manifest is invalid")
    for payload in (database, bundle):
        size = payload.get("size")
        sha256 = payload.get("sha256")
        if (
            isinstance(size, bool)
            or not isinstance(size, int)
            or size <= 0
            or not isinstance(sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", sha256) is None
        ):
            raise RuntimeError("Profilarr artifact payload identity is invalid")
    migrations = database.get("migrations")
    if migrations != [{"version": version, "name": name} for version, name in _EXPECTED_MIGRATIONS]:
        raise RuntimeError("Profilarr artifact migration history is invalid")
    expected_schema_sha256 = _schema_contract_sha256()
    if (
        database.get("schema_sha256") != expected_schema_sha256
        or database.get("integrity") != "ok"
        or bundle.get("object_format") != "sha1"
    ):
        raise RuntimeError("Profilarr artifact structural identity is invalid")
    table_counts = database.get("table_counts")
    if not isinstance(table_counts, dict) or set(table_counts) != set(_EXPECTED_SCHEMA):
        raise RuntimeError("Profilarr artifact table counts are invalid")
    if any(
        isinstance(count, bool) or not isinstance(count, int) or count < 0
        for count in table_counts.values()
    ):
        raise RuntimeError("Profilarr artifact table counts are invalid")
    branch = git.get("branch")
    head = git.get("head")
    if (
        git.get("object_format") != "sha1"
        or not isinstance(branch, str)
        or not branch
        or any(ord(character) < 32 for character in branch)
        or not isinstance(head, str)
        or re.fullmatch(r"[0-9a-f]{40}", head) is None
    ):
        raise RuntimeError("Profilarr artifact Git identity is invalid")
    _manifest_refs(manifest)
    _manifest_inventory(manifest)
    refs = git["refs"]
    expected_ref_sha256 = hashlib.sha256(
        json.dumps(refs, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if git.get("ref_sha256") != expected_ref_sha256 or git.get("clean") is not True:
        raise RuntimeError("Profilarr artifact Git proof is invalid")
    return manifest


def _manifest_refs(manifest: dict[str, object]) -> tuple[tuple[str, str], ...]:
    git = manifest["git"]
    if not isinstance(git, dict):
        raise RuntimeError("Profilarr artifact Git manifest is invalid")
    refs_value = git.get("refs")
    if not isinstance(refs_value, list) or not refs_value or len(refs_value) > _MAX_INVENTORY_FILES:
        raise RuntimeError("Profilarr artifact Git refs are invalid")
    refs: list[tuple[str, str]] = []
    for entry in refs_value:
        if not isinstance(entry, dict) or set(entry) != {"name", "object_id"}:
            raise RuntimeError("Profilarr artifact Git refs are invalid")
        name = entry.get("name")
        object_id = entry.get("object_id")
        if (
            not isinstance(name, str)
            or not name.startswith("refs/")
            or any(ord(char) < 32 for char in name)
            or not isinstance(object_id, str)
            or len(object_id) != 40
            or any(char not in "0123456789abcdef" for char in object_id)
        ):
            raise RuntimeError("Profilarr artifact Git refs are invalid")
        refs.append((name, object_id))
    if len(set(refs)) != len(refs):
        raise RuntimeError("Profilarr artifact Git refs contain duplicates")
    return tuple(sorted(refs))


def _manifest_inventory(manifest: dict[str, object]) -> tuple[_InventoryEntry, ...]:
    git = manifest["git"]
    if not isinstance(git, dict):
        raise RuntimeError("Profilarr artifact Git manifest is invalid")
    inventory_value = git.get("inventory")
    if (
        not isinstance(inventory_value, list)
        or not inventory_value
        or len(inventory_value) > _MAX_INVENTORY_FILES
    ):
        raise RuntimeError("Profilarr artifact inventory is invalid")
    inventory: list[_InventoryEntry] = []
    total_bytes = 0
    for entry in inventory_value:
        if not isinstance(entry, dict) or set(entry) != {"path", "mode", "size", "sha256"}:
            raise RuntimeError("Profilarr artifact inventory is invalid")
        path = entry.get("path")
        mode = entry.get("mode")
        size = entry.get("size")
        sha256 = entry.get("sha256")
        if not isinstance(path, str):
            raise RuntimeError("Profilarr artifact inventory is invalid")
        pure_path = PurePath(path)
        if (
            pure_path.is_absolute()
            or len(pure_path.parts) < 2
            or pure_path.parts[0] not in _AUTHORITATIVE_DIRECTORIES
            or any(part in {"", ".", ".."} for part in pure_path.parts)
            or pure_path.as_posix() != path
            or any(ord(character) < 32 for character in path)
            or isinstance(mode, bool)
            or not isinstance(mode, int)
            or mode != 0o644
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size <= 0
            or size > _MAX_YAML_BYTES
            or not isinstance(sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", sha256) is None
        ):
            raise RuntimeError("Profilarr artifact inventory is invalid")
        total_bytes += size
        if total_bytes > _MAX_AUTHORITATIVE_BYTES:
            raise RuntimeError("Profilarr artifact inventory exceeds safe limits")
        inventory.append(
            _InventoryEntry(
                path=path,
                mode=mode,
                size=size,
                sha256=sha256,
            )
        )
    if inventory != sorted(inventory, key=lambda item: item.path) or len(
        {entry.path for entry in inventory}
    ) != len(inventory):
        raise RuntimeError("Profilarr artifact inventory is invalid")
    return tuple(inventory)


def _validate_artifact_to_workspace(
    artifact_path: Path,
    workspace: Path,
    *,
    expected_size: int | None,
    expected_sha256: str | None,
    bound_descriptor: bool = False,
) -> tuple[dict[str, object], Path, Path]:
    try:
        status = artifact_path.stat() if bound_descriptor else artifact_path.lstat()
    except OSError as exc:
        raise FileNotFoundError("Profilarr restore artifact was not found") from exc
    if not stat.S_ISREG(status.st_mode) or (not bound_descriptor and artifact_path.is_symlink()):
        raise ValueError("Profilarr restore artifact must be a regular file")
    actual_sha256 = _hash_file(artifact_path)
    if expected_size is not None and status.st_size != expected_size:
        raise RuntimeError("Profilarr restore artifact size does not match staged provenance")
    if expected_sha256 is not None and actual_sha256 != expected_sha256:
        raise RuntimeError("Profilarr restore artifact hash does not match staged provenance")
    if status.st_size < 22:
        raise RuntimeError("Profilarr artifact ZIP is invalid")
    with artifact_path.open("rb") as raw_artifact:
        raw_artifact.seek(-22, os.SEEK_END)
        end_record = raw_artifact.read(22)
    if end_record[:4] != b"PK\x05\x06" or end_record[20:22] != b"\x00\x00":
        raise RuntimeError("Profilarr artifact has ambiguous or trailing ZIP data")
    database_path = workspace / "profilarr.db"
    bundle_path = workspace / "repository.bundle"
    try:
        with zipfile.ZipFile(artifact_path) as archive:
            infos = archive.infolist()
            if [info.filename for info in infos] != [
                "profilarr.db",
                "repository.bundle",
                "manifest.json",
            ]:
                raise RuntimeError("Profilarr artifact members are invalid")
            if any(
                info.is_dir()
                or info.flag_bits & 0x1
                or info.compress_type != zipfile.ZIP_DEFLATED
                or stat.S_IFMT(info.external_attr >> 16) != stat.S_IFREG
                for info in infos
            ):
                raise RuntimeError("Profilarr artifact contains unsafe members")
            compressed_bytes = sum(info.compress_size for info in infos)
            uncompressed_bytes = sum(info.file_size for info in infos)
            if (
                any(info.file_size <= 0 or info.compress_size <= 0 for info in infos)
                or compressed_bytes > _MAX_COMPRESSED_BYTES
                or uncompressed_bytes > _MAX_UNCOMPRESSED_BYTES
                or uncompressed_bytes > max(1, compressed_bytes) * _MAX_EXPANSION_RATIO
                or infos[2].file_size > _MAX_MANIFEST_BYTES
            ):
                raise RuntimeError("Profilarr artifact exceeds safe resource limits")
            _copy_zip_member(archive, infos[0], database_path)
            _copy_zip_member(archive, infos[1], bundle_path)
            try:
                manifest_bytes = archive.read(infos[2])
                manifest = _validate_manifest(json.loads(manifest_bytes))
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise RuntimeError("Profilarr artifact manifest is invalid") from exc
            if archive.testzip() is not None:
                raise RuntimeError("Profilarr artifact failed CRC validation")
    except (OSError, zipfile.BadZipFile) as exc:
        raise RuntimeError("Profilarr artifact ZIP is invalid") from exc
    database_manifest = manifest["database"]
    bundle_manifest = manifest["bundle"]
    if not isinstance(database_manifest, dict) or not isinstance(bundle_manifest, dict):
        raise RuntimeError("Profilarr artifact payload manifest is invalid")
    if (
        database_manifest.get("size") != database_path.stat().st_size
        or database_manifest.get("sha256") != _hash_file(database_path)
        or bundle_manifest.get("size") != bundle_path.stat().st_size
        or bundle_manifest.get("sha256") != _hash_file(bundle_path)
    ):
        raise RuntimeError("Profilarr artifact payload hash or size is invalid")
    _validate_database(database_path)
    verification = workspace / "verify.git"
    verification.mkdir(mode=0o700)
    _run_git(verification, "init", "--bare")
    _run_git(verification, "bundle", "verify", str(bundle_path))
    listed: list[tuple[str, str]] = []
    for line in _run_git(verification, "bundle", "list-heads", str(bundle_path)).splitlines():
        try:
            object_id, name = line.split(" ", 1)
        except ValueError as exc:
            raise RuntimeError("Profilarr bundle refs are invalid") from exc
        if name != "HEAD":
            listed.append((name, object_id))
    if tuple(sorted(listed)) != _manifest_refs(manifest):
        raise RuntimeError("Profilarr bundle refs do not match its manifest")
    _reconstruct_repository(
        workspace / "checkout",
        bundle_path,
        manifest,
        database_path,
    )
    return manifest, database_path, bundle_path


def _repository_url_from_database(database_path: Path) -> str | None:
    with sqlite3.connect(f"{database_path.as_uri()}?mode=ro", uri=True) as connection:
        row = connection.execute("SELECT value FROM settings WHERE key = 'gitRepo'").fetchone()
    if row is None or row[0] in {None, ""}:
        return None
    if not _safe_repository_url(row[0]):
        raise RuntimeError("Profilarr linked repository configuration is unsafe")
    return str(row[0])


def _reconstruct_repository(
    destination: Path,
    bundle_path: Path,
    manifest: dict[str, object],
    database_path: Path,
) -> None:
    git_manifest = manifest["git"]
    if not isinstance(git_manifest, dict):
        raise RuntimeError("Profilarr artifact Git manifest is invalid")
    branch = git_manifest.get("branch")
    head = git_manifest.get("head")
    if (
        not isinstance(branch, str)
        or not branch
        or any(ord(char) < 32 for char in branch)
        or not isinstance(head, str)
        or len(head) != 40
    ):
        raise RuntimeError("Profilarr artifact Git checkout is invalid")
    refs = _manifest_refs(manifest)
    if (f"refs/heads/{branch}", head) not in refs:
        raise RuntimeError("Profilarr artifact branch does not match HEAD")
    destination.mkdir(mode=0o700)
    _run_git(destination, "init", f"--initial-branch={branch}")
    _run_git(
        destination,
        "fetch",
        "--update-head-ok",
        "--no-tags",
        str(bundle_path),
        "+refs/*:refs/*",
    )
    _run_git(destination, "symbolic-ref", "HEAD", f"refs/heads/{branch}")
    _run_git(destination, "reset", "--hard", head)
    current_refs: list[tuple[str, str]] = []
    for line in _run_git(
        destination,
        "for-each-ref",
        "--format=%(refname)%09%(objectname)",
    ).splitlines():
        name, object_id = line.split("\t", 1)
        current_refs.append((name, object_id))
    if tuple(sorted(current_refs)) != refs:
        raise RuntimeError("Profilarr restored Git refs are incomplete")
    expected_inventory = _manifest_inventory(manifest)
    if _repository_inventory(destination) != expected_inventory:
        raise RuntimeError("Profilarr restored Git inventory does not match")
    _run_git(destination, "fsck", "--full", "--strict")
    if _run_git(destination, "status", "--porcelain=v2", "--untracked-files=all"):
        raise RuntimeError("Profilarr restored Git worktree is not clean")
    origin = _repository_url_from_database(database_path)
    if origin is not None:
        _run_git(destination, "remote", "add", "origin", origin)
    shutil.rmtree(destination / ".git/hooks", ignore_errors=True)
    shutil.rmtree(destination / ".git/logs", ignore_errors=True)
    for path in sorted((destination, *destination.rglob("*")), reverse=True):
        if path.is_symlink():
            raise RuntimeError("Profilarr restored Git repository contains a symlink")
        os.chmod(path, 0o700 if path.is_dir() else 0o600)


def _directory_flags() -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return flags


def _directory_identity(directory_fd: int) -> tuple[int, int]:
    status = os.fstat(directory_fd)
    if not stat.S_ISDIR(status.st_mode):
        raise ValueError("Profilarr restore-owned path is not a directory")
    return status.st_dev, status.st_ino


def _open_owned_directory(path: Path, expected_identity: tuple[int, int]) -> int:
    descriptor = os.open(path, _directory_flags())
    if _directory_identity(descriptor) != expected_identity:
        os.close(descriptor)
        raise ValueError("Profilarr restore-owned directory changed")
    return descriptor


def _require_parent_path_identity(parent: Path, parent_fd: int) -> tuple[int, int]:
    opened = os.fstat(parent_fd)
    try:
        named = os.stat(parent, follow_symlinks=False)
    except OSError as exc:
        raise RuntimeError("Profilarr restore destination parent changed") from exc
    identity = (opened.st_dev, opened.st_ino)
    if not stat.S_ISDIR(named.st_mode) or (named.st_dev, named.st_ino) != identity:
        raise RuntimeError("Profilarr restore destination parent changed")
    return identity


def _bind_restore_destination(
    destination: Path,
) -> tuple[int, int, tuple[int, int], tuple[int, int]]:
    parent = destination.parent
    parent_fd: int | None = None
    sentinel_fd: int | None = None
    try:
        parent_fd = os.open(parent, _directory_flags())
        parent_identity = _require_parent_path_identity(parent, parent_fd)
        try:
            os.stat(destination.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise FileExistsError("Profilarr restore destination already exists")
        entries = set(os.listdir(parent_fd))
        if RESTORE_SENTINEL_NAME not in entries:
            raise FileNotFoundError("Profilarr restore destination sentinel was not found")
        if entries != {RESTORE_SENTINEL_NAME}:
            raise ValueError("Profilarr restore destination parent must contain only its sentinel")
        sentinel_flags = os.O_RDONLY | os.O_NONBLOCK
        if hasattr(os, "O_NOFOLLOW"):
            sentinel_flags |= os.O_NOFOLLOW
        sentinel_fd = os.open(RESTORE_SENTINEL_NAME, sentinel_flags, dir_fd=parent_fd)
        sentinel_status = os.fstat(sentinel_fd)
        if (
            not stat.S_ISREG(sentinel_status.st_mode)
            or stat.S_IMODE(sentinel_status.st_mode) & 0o077
        ):
            raise ValueError("Profilarr restore destination sentinel is invalid")
        expected_marker = RESTORE_SENTINEL_CONTENT.encode("utf-8")
        if os.read(sentinel_fd, len(expected_marker) + 1) != expected_marker:
            raise ValueError("Profilarr restore destination sentinel is invalid")
        sentinel_identity = (sentinel_status.st_dev, sentinel_status.st_ino)
        if _require_parent_path_identity(parent, parent_fd) != parent_identity:
            raise RuntimeError("Profilarr restore destination parent changed")
        return parent_fd, sentinel_fd, parent_identity, sentinel_identity
    except BaseException:
        if sentinel_fd is not None:
            os.close(sentinel_fd)
        if parent_fd is not None:
            os.close(parent_fd)
        raise


def _same_directory_identity(parent_fd: int, name: str, expected: tuple[int, int]) -> bool:
    try:
        status = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError:
        return False
    return stat.S_ISDIR(status.st_mode) and (status.st_dev, status.st_ino) == expected


def _require_named_directory_identity(
    parent_fd: int,
    name: str,
    expected_identity: tuple[int, int],
) -> None:
    if not _same_directory_identity(parent_fd, name, expected_identity):
        raise ValueError("Profilarr restore-owned directory changed")


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


def _rename_directory_noreplace(
    parent_fd: int,
    source_name: str,
    destination_name: str,
) -> None:
    library = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(library, "renameat2", None)
    if renameat2 is None:
        raise RuntimeError("Profilarr create-only restore requires Linux renameat2")
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
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        raise FileExistsError("Profilarr restore destination already exists")
    raise OSError(error_number, os.strerror(error_number))


def _consume_restore_sentinel(
    parent_fd: int,
    sentinel_fd: int,
    expected_identity: tuple[int, int],
) -> None:
    consumed_name = f".{RESTORE_SENTINEL_NAME}.{uuid.uuid4().hex}.consumed"
    os.lseek(sentinel_fd, 0, os.SEEK_SET)
    marker = os.read(sentinel_fd, len(RESTORE_SENTINEL_CONTENT.encode("utf-8")) + 1)
    if marker != RESTORE_SENTINEL_CONTENT.encode("utf-8"):
        raise ValueError("Profilarr restore destination sentinel changed")
    os.rename(
        RESTORE_SENTINEL_NAME,
        consumed_name,
        src_dir_fd=parent_fd,
        dst_dir_fd=parent_fd,
    )
    current = os.stat(consumed_name, dir_fd=parent_fd, follow_symlinks=False)
    if (current.st_dev, current.st_ino) != expected_identity:
        try:
            os.rename(
                consumed_name,
                RESTORE_SENTINEL_NAME,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
        except OSError:
            pass
        raise ValueError("Profilarr restore destination sentinel changed")
    os.unlink(consumed_name, dir_fd=parent_fd)


def _copy_restored_database(source_path: Path, staging_fd: int) -> Path:
    source_fd = os.open(source_path, os.O_RDONLY)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    destination_fd = os.open("profilarr.db", flags, 0o600, dir_fd=staging_fd)
    digest = hashlib.sha256()
    try:
        while True:
            chunk = os.read(source_fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(destination_fd, view)
                if written <= 0:
                    raise OSError("short write while restoring Profilarr database")
                view = view[written:]
        os.fsync(destination_fd)
    finally:
        os.close(destination_fd)
        os.close(source_fd)
    restored = Path(f"/proc/self/fd/{staging_fd}/profilarr.db")
    if digest.hexdigest() != _hash_file(restored):
        raise RuntimeError("Profilarr restored database hash does not match")
    return restored


def _restore_process_worker(
    artifact_path: Path,
    parent: Path,
    parent_identity: tuple[int, int],
    staging_name: str,
    staging_identity: tuple[int, int],
    validation_name: str,
    validation_identity: tuple[int, int],
    expected_size: int,
    expected_sha256: str,
    expected_artifact_identity: tuple[int, int],
    connection: Connection,
) -> None:
    parent_fd: int | None = None
    staging_fd: int | None = None
    validation_fd: int | None = None
    artifact_fd: int | None = None
    try:
        os.setsid()
        parent_fd = _open_owned_directory(parent, parent_identity)
        if _require_parent_path_identity(parent, parent_fd) != parent_identity:
            raise RuntimeError("Profilarr restore destination parent changed")
        staging_fd = os.open(staging_name, _directory_flags(), dir_fd=parent_fd)
        if _directory_identity(staging_fd) != staging_identity:
            raise ValueError("Profilarr restore staging directory changed")
        validation_fd = os.open(validation_name, _directory_flags(), dir_fd=parent_fd)
        if _directory_identity(validation_fd) != validation_identity:
            raise ValueError("Profilarr restore validation directory changed")
        artifact_flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            artifact_flags |= os.O_NOFOLLOW
        artifact_fd = os.open(artifact_path, artifact_flags)
        artifact_status = os.fstat(artifact_fd)
        if (
            not stat.S_ISREG(artifact_status.st_mode)
            or (
                artifact_status.st_dev,
                artifact_status.st_ino,
            )
            != expected_artifact_identity
        ):
            raise ValueError(
                "Profilarr restore artifact changed from its verified staging identity"
            )
        manifest, database_snapshot, bundle = _validate_artifact_to_workspace(
            Path(f"/proc/self/fd/{artifact_fd}"),
            Path(f"/proc/self/fd/{validation_fd}"),
            expected_size=expected_size,
            expected_sha256=expected_sha256,
            bound_descriptor=True,
        )
        restored_database = _copy_restored_database(database_snapshot, staging_fd)
        _reconstruct_repository(
            Path(f"/proc/self/fd/{staging_fd}/db"),
            bundle,
            manifest,
            restored_database,
        )
        _validate_database(restored_database)
        os.fsync(staging_fd)
        if _require_parent_path_identity(parent, parent_fd) != parent_identity:
            raise RuntimeError("Profilarr restore destination parent changed")
        _require_named_directory_identity(parent_fd, staging_name, staging_identity)
        connection.send(("ok", "", 0, 0))
    except BaseException as exc:
        try:
            connection.send(
                (
                    _worker_error_kind(exc),
                    _worker_error_message(exc, operation="restore"),
                    0,
                    0,
                )
            )
        except (BrokenPipeError, EOFError, OSError):
            pass
        raise SystemExit(1) from None
    finally:
        if artifact_fd is not None:
            os.close(artifact_fd)
        if validation_fd is not None:
            os.close(validation_fd)
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
    validation_name: str,
    validation_identity: tuple[int, int],
    expected_size: int,
    expected_sha256: str,
    expected_artifact_identity: tuple[int, int],
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
            validation_name,
            validation_identity,
            expected_size,
            expected_sha256,
            expected_artifact_identity,
            sending,
        ),
        name="profilarr-restore",
        daemon=True,
    )
    process.start()
    sending.close()
    return process, receiving


async def _materialize_restore(
    artifact_path: Path,
    destination: Path,
    *,
    expected_size: int,
    expected_sha256: str,
) -> None:
    parent = destination.parent
    parent_fd: int | None = None
    staging_name = f".{destination.name}.{uuid.uuid4().hex}.restore.tmp"
    validation_name = f".{destination.name}.{uuid.uuid4().hex}.validation.tmp"
    staging_fd: int | None = None
    validation_fd: int | None = None
    staging_identity: tuple[int, int] | None = None
    validation_identity: tuple[int, int] | None = None
    process: BaseProcess | None = None
    artifact_fd: int | None = None
    sentinel_fd: int | None = None
    sentinel_identity: tuple[int, int] | None = None
    validation_removed = False
    succeeded = False
    try:
        _require_restore_destination_path(str(destination))
        parent_fd, sentinel_fd, parent_identity, sentinel_identity = _bind_restore_destination(
            destination
        )
        artifact_flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            artifact_flags |= os.O_NOFOLLOW
        artifact_fd = os.open(artifact_path, artifact_flags)
        artifact_status = os.fstat(artifact_fd)
        if not stat.S_ISREG(artifact_status.st_mode):
            raise ValueError("Profilarr restore artifact is not a regular file")
        artifact_identity = (artifact_status.st_dev, artifact_status.st_ino)
        if _require_parent_path_identity(parent, parent_fd) != parent_identity:
            raise RuntimeError("Profilarr restore destination parent changed")
        os.mkdir(staging_name, 0o700, dir_fd=parent_fd)
        staging_fd = os.open(staging_name, _directory_flags(), dir_fd=parent_fd)
        staging_identity = _directory_identity(staging_fd)
        os.mkdir(validation_name, 0o700, dir_fd=parent_fd)
        validation_fd = os.open(validation_name, _directory_flags(), dir_fd=parent_fd)
        validation_identity = _directory_identity(validation_fd)
        process, worker_connection = _start_restore_process(
            artifact_path,
            parent,
            parent_identity,
            staging_name,
            staging_identity,
            validation_name,
            validation_identity,
            expected_size,
            expected_sha256,
            artifact_identity,
        )
        current_artifact = os.stat(artifact_path, follow_symlinks=False)
        if (
            not stat.S_ISREG(current_artifact.st_mode)
            or (current_artifact.st_dev, current_artifact.st_ino) != artifact_identity
        ):
            await _stop_worker_process_before_return(process, operation="restore")
            raise ValueError(
                "Profilarr restore artifact changed from its verified staging identity"
            )
        counts = await _await_worker(
            process,
            worker_connection,
            operation="restore",
            timeout_seconds=_RESTORE_WORKER_TIMEOUT_SECONDS,
        )
        if counts != (0, 0):
            raise RuntimeError("Profilarr restore worker returned an invalid result")
        _remove_owned_directory(
            parent_fd,
            validation_fd,
            expected_identity=validation_identity,
            candidate_names=(validation_name,),
        )
        validation_removed = True
        if _require_parent_path_identity(parent, parent_fd) != parent_identity:
            raise RuntimeError("Profilarr restore destination parent changed")
        _require_named_directory_identity(parent_fd, staging_name, staging_identity)
        _rename_directory_noreplace(parent_fd, staging_name, destination.name)
        _require_named_directory_identity(parent_fd, destination.name, staging_identity)
        os.fsync(parent_fd)
        _consume_restore_sentinel(parent_fd, sentinel_fd, sentinel_identity)
        os.fsync(parent_fd)
        if _require_parent_path_identity(parent, parent_fd) != parent_identity:
            raise RuntimeError("Profilarr restore destination parent changed")
        succeeded = True
    finally:
        cleanup_error: BaseException | None = None
        if parent_fd is not None and (process is None or not process.is_alive()):
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
        if staging_fd is not None:
            os.close(staging_fd)
        if artifact_fd is not None:
            os.close(artifact_fd)
        if sentinel_fd is not None:
            os.close(sentinel_fd)
        if parent_fd is not None:
            os.close(parent_fd)
        if cleanup_error is not None:
            raise cleanup_error


def _git_config_has_partial_clone(repository: Path) -> bool:
    return bool(
        _run_git(
            repository,
            "config",
            "--get-regexp",
            "^(extensions\\.partialclone|remote\\..*\\.promisor)$",
            allowed_returncodes=frozenset({0, 1}),
        )
    )


def _source_mount_identities(
    database: Path,
    repository: Path,
) -> tuple[tuple[int, int], tuple[int, int]]:
    database_status = _require_source_mount(database, kind="database")
    repository_status = _require_source_mount(repository, kind="repository")
    if (database_status.st_dev, database_status.st_ino) == (
        repository_status.st_dev,
        repository_status.st_ino,
    ):
        raise ValueError("Profilarr source mounts must be distinct")
    return (
        (database_status.st_dev, database_status.st_ino),
        (repository_status.st_dev, repository_status.st_ino),
    )


def _validate_source_content(database: Path, repository: Path) -> None:
    _validate_database(database)
    _validate_repository(repository)


def _network_interfaces() -> set[str]:
    try:
        return {entry.name for entry in Path("/sys/class/net").iterdir()}
    except OSError as exc:
        raise RuntimeError("Profilarr restore network isolation could not be verified") from exc


def _require_restore_destination_path(value: str) -> Path:
    if os.getenv(ISOLATED_RESTORE_ENV) != "1":
        raise RuntimeError("Profilarr restore is disabled outside an authorized isolated drill")
    if _network_interfaces() != {"lo"}:
        raise RuntimeError("Profilarr restore requires a loopback-only network namespace")
    destination = Path(value)
    parent = destination.parent
    try:
        if parent.is_symlink() or parent.resolve(strict=True) != parent:
            raise ValueError("Profilarr restore destination parent must not use symlinks")
        status = parent.stat()
    except FileNotFoundError as exc:
        raise FileNotFoundError("Profilarr restore destination parent was not found") from exc
    if not stat.S_ISDIR(status.st_mode):
        raise ValueError("Profilarr restore destination parent must be a directory")
    if stat.S_IMODE(status.st_mode) & 0o077:
        raise RuntimeError("Profilarr restore destination parent must be private")
    return destination


def _require_restore_destination(value: str) -> Path:
    destination = _require_restore_destination_path(value)
    parent_fd: int | None = None
    sentinel_fd: int | None = None
    try:
        parent_fd, sentinel_fd, _parent_identity, _sentinel_identity = _bind_restore_destination(
            destination
        )
    except FileNotFoundError as exc:
        raise FileNotFoundError("Profilarr restore destination sentinel was not found") from exc
    finally:
        if sentinel_fd is not None:
            os.close(sentinel_fd)
        if parent_fd is not None:
            os.close(parent_fd)
    return destination


def _secret_safe_error(exc: BaseException) -> str:
    if isinstance(exc, FileNotFoundError):
        return "Profilarr source resource was not found"
    message = str(exc)
    allowed_terms = (
        "alternate",
        "auth",
        "bisect",
        "cherry",
        "clean",
        "column",
        "database",
        "detached",
        "foreign key",
        "Git",
        "HEAD",
        "ignored",
        "integrity",
        "journal",
        "LFS",
        "lock",
        "merge",
        "migration",
        "object",
        "partial",
        "promisor",
        "rebase",
        "repository",
        "schema",
        "shallow",
        "SQLite",
        "submodule",
        "unborn",
        "untracked",
    )
    for term in allowed_terms:
        if term.lower() in message.lower():
            return message
    return "Profilarr probe failed"


class ProfilarrPlugin(BackupPlugin):
    """Back up Profilarr 1.1.5's SQLite and Git control plane."""

    restore_capability = "automatic"

    def __init__(self, name: str, version: str = "0.2.1") -> None:
        super().__init__(name=name, version=version)

    async def validate_config(self, config: Dict[str, Any]) -> bool:
        if not isinstance(config, dict):
            return False
        mode = config.get("mode")
        if mode == "source":
            database_path = _safe_path(config.get("database_path"))
            repository_path = _safe_path(config.get("repository_path"))
            return bool(
                set(config) == _SOURCE_KEYS
                and database_path is not None
                and repository_path is not None
                and _is_beneath(database_path, _SOURCE_ROOT)
                and _is_beneath(repository_path, _SOURCE_ROOT)
                and database_path != repository_path
            )
        if mode == "restore_destination":
            restore_directory = _safe_path(config.get("restore_directory"))
            return bool(
                set(config) == _RESTORE_KEYS
                and restore_directory is not None
                and any(_is_beneath(restore_directory, root) for root in _RESTORE_ROOTS)
            )
        return False

    async def test(self, config: Dict[str, Any]) -> bool:
        if not await self.validate_config(config):
            raise ValueError("Invalid Profilarr configuration")
        if config["mode"] == "restore_destination":
            _require_restore_destination(config["restore_directory"])
            return True
        await _probe_source_async(config)
        return True

    async def backup(self, context: BackupContext) -> Dict[str, Any]:
        if not await self.validate_config(context.config) or context.config["mode"] != "source":
            raise ValueError("Invalid Profilarr backup configuration")
        database = Path(context.config["database_path"])
        repository = Path(context.config["repository_path"])
        await _probe_source_async(context.config)
        capture_mount_identities = _source_mount_identities(database, repository)
        with create_backup_artifact(
            self,
            context,
            prefix="profilarr-control-plane",
            suffix=".profilarr",
            backup_root=BACKUP_BASE_PATH,
        ) as artifact:
            workspace = Path(
                tempfile.mkdtemp(
                    prefix=".profilarr-capture-",
                    dir=artifact.temporary_path.parent,
                )
            )
            os.chmod(workspace, 0o700)
            process: BaseProcess | None = None
            try:
                process, connection = _start_backup_process(
                    database,
                    repository,
                    artifact.temporary_path,
                    workspace,
                    capture_mount_identities,
                )
                ref_count, table_count = await _await_worker(
                    process,
                    connection,
                    operation="backup",
                    timeout_seconds=_BACKUP_WORKER_TIMEOUT_SECONDS,
                )
                if _source_mount_identities(database, repository) != capture_mount_identities:
                    raise RuntimeError("Profilarr source mount identity changed during backup")
            finally:
                if process is None or not process.is_alive():
                    shutil.rmtree(workspace, ignore_errors=False)
            artifact.sidecar_metadata.update(
                {
                    "application_version": "1.1.5",
                    "artifact_format": "profilarr-v1",
                    "validation": "passed",
                    "table_count": table_count,
                    "ref_count": ref_count,
                }
            )
        return {"artifact_path": str(artifact.final_path)}

    async def restore(self, context: RestoreContext) -> Dict[str, Any]:
        if (
            not await self.validate_config(context.config)
            or context.config["mode"] != "restore_destination"
        ):
            raise ValueError("Invalid Profilarr restore configuration")
        destination = _require_restore_destination(context.config["restore_directory"])
        metadata = context.metadata or {}
        expected_size = metadata.get("artifact_bytes")
        expected_sha256 = metadata.get("artifact_sha256")
        if (
            isinstance(expected_size, bool)
            or not isinstance(expected_size, int)
            or expected_size <= 0
            or not isinstance(expected_sha256, str)
            or len(expected_sha256) != 64
        ):
            raise RuntimeError("Profilarr restore requires staged artifact provenance")
        source_target_run_id = metadata.get("source_target_run_id")
        if (
            isinstance(source_target_run_id, bool)
            or not isinstance(source_target_run_id, int)
            or source_target_run_id <= 0
        ):
            raise RuntimeError("Profilarr restore requires source run provenance")
        await _materialize_restore(
            Path(context.artifact_path),
            destination,
            expected_size=expected_size,
            expected_sha256=expected_sha256,
        )
        return {
            "status": "success",
            "message": "All authoritative Profilarr 1.1.5 state was restored",
            "restored_path": str(destination),
        }

    async def get_status(self, context: BackupContext) -> Dict[str, Any]:
        try:
            await self.test(context.config)
        except Exception as exc:
            return {"status": "error", "error": _secret_safe_error(exc)}
        return {"status": "ok"}
