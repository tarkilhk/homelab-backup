"""Consistent control-plane backup for SFTPGo v2.7.5 SQLite state."""

from __future__ import annotations

import asyncio
import hashlib
import multiprocessing
import os
import shutil
import sqlite3
import threading
import time
import uuid
from multiprocessing.process import BaseProcess
from pathlib import Path, PurePath
from typing import Any, Dict

from app.core.plugins.artifacts import create_backup_artifact
from app.core.plugins.base import BackupContext, BackupPlugin, RestoreContext

BACKUP_BASE_PATH = "/backups"
RESTORE_SENTINEL_NAME = ".sftpgo-restore-destination"
RESTORE_SENTINEL_CONTENT = "sftpgo-v2.7.5-isolated-restore-v1\n"
SFTPGO_VERSION = "2.7.5"
SFTPGO_SCHEMA_VERSION = 33

_SNAPSHOT_TIMEOUT_SECONDS = 120.0
_SNAPSHOT_STOP_TIMEOUT_SECONDS = 5.0
_RESTORE_TIMEOUT_SECONDS = 120.0
_FORBIDDEN_RESTORE_ROOTS = (
    Path("/backups"),
    Path("/sources/sftpgo"),
    Path("/var/lib/sftpgo"),
)
_TRANSIENT_TABLES = (
    "active_transfers",
    "shared_sessions",
    "tasks",
    "defender_events",
    "defender_hosts",
)
_REQUIRED_SCHEMA: dict[str, frozenset[str]] = {
    "schema_version": frozenset({"id", "version"}),
    "roles": frozenset({"id", "name", "description", "created_at", "updated_at"}),
    "admins": frozenset(
        {
            "id",
            "username",
            "description",
            "password",
            "email",
            "status",
            "permissions",
            "filters",
            "additional_info",
            "last_login",
            "role_id",
            "created_at",
            "updated_at",
        }
    ),
    "active_transfers": frozenset(
        {
            "id",
            "connection_id",
            "transfer_id",
            "transfer_type",
            "username",
            "folder_name",
            "ip",
            "truncated_size",
            "current_ul_size",
            "current_dl_size",
            "created_at",
            "updated_at",
        }
    ),
    "defender_hosts": frozenset({"id", "ip", "ban_time", "updated_at"}),
    "defender_events": frozenset({"id", "date_time", "score", "host_id"}),
    "folders": frozenset(
        {
            "id",
            "name",
            "description",
            "path",
            "used_quota_size",
            "used_quota_files",
            "last_quota_update",
            "filesystem",
        }
    ),
    "groups": frozenset({"id", "name", "description", "created_at", "updated_at", "user_settings"}),
    "shared_sessions": frozenset({"key", "data", "type", "timestamp"}),
    "users": frozenset(
        {
            "id",
            "username",
            "status",
            "expiration_date",
            "description",
            "password",
            "public_keys",
            "home_dir",
            "uid",
            "gid",
            "max_sessions",
            "quota_size",
            "quota_files",
            "permissions",
            "used_quota_size",
            "used_quota_files",
            "last_quota_update",
            "upload_bandwidth",
            "download_bandwidth",
            "last_login",
            "filters",
            "filesystem",
            "additional_info",
            "created_at",
            "updated_at",
            "email",
            "upload_data_transfer",
            "download_data_transfer",
            "total_data_transfer",
            "used_upload_data_transfer",
            "used_download_data_transfer",
            "deleted_at",
            "first_download",
            "first_upload",
            "last_password_change",
            "role_id",
        }
    ),
    "groups_folders_mapping": frozenset(
        {
            "id",
            "folder_id",
            "group_id",
            "virtual_path",
            "quota_size",
            "quota_files",
            "sort_order",
        }
    ),
    "users_groups_mapping": frozenset({"id", "user_id", "group_id", "group_type", "sort_order"}),
    "users_folders_mapping": frozenset(
        {
            "id",
            "user_id",
            "folder_id",
            "virtual_path",
            "quota_size",
            "quota_files",
            "sort_order",
        }
    ),
    "shares": frozenset(
        {
            "id",
            "share_id",
            "name",
            "description",
            "scope",
            "paths",
            "created_at",
            "updated_at",
            "last_use_at",
            "expires_at",
            "password",
            "max_tokens",
            "used_tokens",
            "allow_from",
            "user_id",
            "options",
        }
    ),
    "api_keys": frozenset(
        {
            "id",
            "name",
            "key_id",
            "api_key",
            "scope",
            "created_at",
            "updated_at",
            "last_use_at",
            "expires_at",
            "description",
            "admin_id",
            "user_id",
        }
    ),
    "events_rules": frozenset(
        {
            "id",
            "name",
            "status",
            "description",
            "created_at",
            "updated_at",
            "trigger",
            "conditions",
            "deleted_at",
        }
    ),
    "events_actions": frozenset({"id", "name", "description", "type", "options"}),
    "rules_actions_mapping": frozenset({"id", "rule_id", "action_id", "order", "options"}),
    "tasks": frozenset({"id", "name", "updated_at", "version"}),
    "admins_groups_mapping": frozenset({"id", "admin_id", "group_id", "options", "sort_order"}),
    "ip_lists": frozenset(
        {
            "id",
            "type",
            "ipornet",
            "mode",
            "description",
            "first",
            "last",
            "ip_type",
            "protocols",
            "created_at",
            "updated_at",
            "deleted_at",
        }
    ),
    "configurations": frozenset({"id", "configs"}),
}


def _path_has_symlink(path: Path) -> bool:
    """Return whether any existing component of an absolute path is a symlink."""

    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        if current.is_symlink():
            return True
        if not current.exists():
            break
    return False


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _validate_restore_path_location(path: Path) -> None:
    resolved_path = path.resolve(strict=False)
    for root in _FORBIDDEN_RESTORE_ROOTS:
        if _is_relative_to(resolved_path, root.resolve(strict=False)):
            raise ValueError("SFTPGo restore destination uses a forbidden path")


def _validate_private_mode(path: Path, *, label: str) -> None:
    if os.stat(path).st_mode & 0o077:
        raise PermissionError(f"SFTPGo {label} permissions are not private")


def _database_evidence(
    path: Path,
    *,
    require_transient_empty: bool,
) -> dict[str, Any]:
    """Validate exact v2.7.5 provider state without exposing stored values."""

    try:
        uri = f"{path.as_uri()}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=5.0) as connection:
            if [row[0] for row in connection.execute("PRAGMA quick_check")] != ["ok"]:
                raise RuntimeError("SFTPGo database failed SQLite integrity validation")

            tables = {
                str(row[0])
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            if set(_REQUIRED_SCHEMA).difference(tables):
                raise RuntimeError("SFTPGo database is missing required tables")

            for table, required_columns in _REQUIRED_SCHEMA.items():
                columns = {
                    str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')
                }
                if not required_columns.issubset(columns):
                    raise RuntimeError("SFTPGo database is missing required table columns")

            versions = [
                int(row[0])
                for row in connection.execute("SELECT version FROM schema_version ORDER BY id")
            ]
            if versions != [SFTPGO_SCHEMA_VERSION]:
                raise RuntimeError("SFTPGo database schema version is unsupported")

            admin_count = int(connection.execute("SELECT COUNT(*) FROM admins").fetchone()[0])
            if admin_count < 1:
                raise RuntimeError("SFTPGo database contains no administrator")

            if list(connection.execute("PRAGMA foreign_key_check")):
                raise RuntimeError("SFTPGo database has foreign-key violations")

            transient_counts = {
                table: int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
                for table in _TRANSIENT_TABLES
            }
            if require_transient_empty and any(transient_counts.values()):
                raise RuntimeError("SFTPGo backup contains transient runtime state")

            semantic_counts = {
                table: int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
                for table in (
                    "admins",
                    "users",
                    "groups",
                    "folders",
                    "shares",
                    "api_keys",
                    "roles",
                    "ip_lists",
                    "events_actions",
                    "events_rules",
                )
            }
    except RuntimeError:
        raise
    except (OSError, sqlite3.DatabaseError, TypeError, ValueError) as exc:
        raise RuntimeError("SFTPGo database is not a usable v2.7.5 SQLite database") from exc

    return {
        "schema_version": SFTPGO_SCHEMA_VERSION,
        "admin_count": admin_count,
        "transient_counts": transient_counts,
        "semantic_counts": semantic_counts,
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _snapshot_database(
    source_path: Path,
    snapshot_path: Path,
    stop_event: threading.Event,
    deadline: float,
) -> None:
    """Create a point-in-time standalone snapshot and remove transient rows."""

    def progress(_status: int, _remaining: int, _total: int) -> None:
        if stop_event.is_set():
            raise RuntimeError("SFTPGo snapshot was cancelled")
        if time.monotonic() >= deadline:
            raise TimeoutError("SFTPGo snapshot timed out")

    source_uri = f"{source_path.as_uri()}?mode=ro"
    try:
        descriptor = os.open(
            snapshot_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        os.close(descriptor)
        _validate_private_mode(snapshot_path, label="snapshot")
        with sqlite3.connect(source_uri, uri=True, timeout=5.0) as source:
            with sqlite3.connect(snapshot_path, timeout=5.0) as destination:
                progress(0, 0, 0)
                source.backup(
                    destination,
                    pages=256,
                    progress=progress,
                    sleep=0.01,
                )
                destination.execute("PRAGMA foreign_keys = ON")
                destination.execute("BEGIN IMMEDIATE")
                for table in _TRANSIENT_TABLES:
                    destination.execute(f'DELETE FROM "{table}"')
                destination.commit()
                journal_mode = destination.execute("PRAGMA journal_mode = DELETE").fetchone()
                if not journal_mode or str(journal_mode[0]).lower() != "delete":
                    raise RuntimeError("SFTPGo snapshot could not become standalone")
        for suffix in ("-wal", "-shm"):
            companion = Path(f"{snapshot_path}{suffix}")
            if companion.exists():
                raise RuntimeError("SFTPGo snapshot retained SQLite companion state")
        _database_evidence(snapshot_path, require_transient_empty=True)
    except (TimeoutError, RuntimeError, PermissionError):
        for suffix in ("-wal", "-shm"):
            Path(f"{snapshot_path}{suffix}").unlink(missing_ok=True)
        raise
    except (OSError, sqlite3.DatabaseError) as exc:
        for suffix in ("-wal", "-shm"):
            Path(f"{snapshot_path}{suffix}").unlink(missing_ok=True)
        raise RuntimeError("SFTPGo SQLite snapshot failed") from exc


def _snapshot_process_worker(
    source_path: Path,
    snapshot_path: Path,
    deadline: float,
) -> None:
    """Run the SQLite snapshot in a process the scheduler can stop safely."""

    try:
        _snapshot_database(
            source_path,
            snapshot_path,
            threading.Event(),
            deadline,
        )
    except BaseException:
        raise SystemExit(1) from None


def _start_snapshot_process(
    source_path: Path,
    snapshot_path: Path,
    deadline: float,
) -> BaseProcess:
    context = multiprocessing.get_context("spawn")
    process = context.Process(
        target=_snapshot_process_worker,
        args=(source_path, snapshot_path, deadline),
        name="sftpgo-snapshot",
        daemon=True,
    )
    process.start()
    return process


async def _join_worker_process(
    process: BaseProcess,
    timeout_seconds: float,
) -> None:
    await asyncio.to_thread(process.join, timeout_seconds)


async def _stop_worker_process(process: BaseProcess, *, operation: str) -> None:
    """Terminate, then kill and reap a plugin worker within bounded waits."""

    if not process.is_alive():
        await _join_worker_process(process, _SNAPSHOT_STOP_TIMEOUT_SECONDS)
        if process.exitcode is None:
            raise RuntimeError(f"SFTPGo {operation} worker could not be reaped")
        return

    process.terminate()
    await _join_worker_process(process, _SNAPSHOT_STOP_TIMEOUT_SECONDS)
    if process.is_alive():
        process.kill()
        await _join_worker_process(process, _SNAPSHOT_STOP_TIMEOUT_SECONDS)
    if process.is_alive() or process.exitcode is None:
        raise RuntimeError(f"SFTPGo {operation} worker could not be stopped")


async def _stop_worker_process_before_return(
    process: BaseProcess,
    *,
    operation: str,
) -> None:
    stop_task = asyncio.create_task(_stop_worker_process(process, operation=operation))
    try:
        await asyncio.shield(stop_task)
    except asyncio.CancelledError:
        await asyncio.shield(stop_task)
        raise


def _remove_snapshot_companions(snapshot_path: Path) -> None:
    for suffix in ("-wal", "-shm"):
        Path(f"{snapshot_path}{suffix}").unlink(missing_ok=True)


async def _create_snapshot(source_path: Path, snapshot_path: Path) -> None:
    deadline = time.monotonic() + _SNAPSHOT_TIMEOUT_SECONDS
    process = _start_snapshot_process(source_path, snapshot_path, deadline)
    try:
        await _join_worker_process(
            process,
            _SNAPSHOT_TIMEOUT_SECONDS + 1.0,
        )
        if process.is_alive():
            await _stop_worker_process_before_return(process, operation="snapshot")
            raise TimeoutError("SFTPGo snapshot timed out")
        if process.exitcode != 0:
            raise RuntimeError("SFTPGo SQLite snapshot failed")
    except asyncio.CancelledError:
        await _stop_worker_process_before_return(process, operation="snapshot")
        raise
    except BaseException:
        if process.is_alive():
            await _stop_worker_process_before_return(process, operation="snapshot")
        raise
    finally:
        if not process.is_alive():
            _remove_snapshot_companions(snapshot_path)


def _validate_fresh_destination(path: Path) -> None:
    parent = path.parent
    if not parent.exists() or not parent.is_dir():
        raise FileNotFoundError("SFTPGo restore destination directory was not found")
    if _path_has_symlink(path):
        raise ValueError("SFTPGo restore destination must not contain a symbolic link")
    sentinel = parent / RESTORE_SENTINEL_NAME
    if not sentinel.exists() or not sentinel.is_file() or sentinel.is_symlink():
        raise FileNotFoundError("SFTPGo restore destination sentinel was not found")
    try:
        marker = sentinel.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError("SFTPGo restore destination sentinel is invalid") from exc
    if marker != RESTORE_SENTINEL_CONTENT:
        raise ValueError("SFTPGo restore destination sentinel is invalid")
    if {entry.name for entry in parent.iterdir()} != {RESTORE_SENTINEL_NAME}:
        raise ValueError("SFTPGo restore destination directory must be otherwise empty")
    if not os.access(parent, os.W_OK):
        raise PermissionError("SFTPGo restore destination directory is not writable")


def _validate_restore_destination(path: Path, artifact_path: Path) -> None:
    _validate_restore_path_location(path)
    if _path_has_symlink(path):
        raise ValueError("SFTPGo restore destination must not contain a symbolic link")
    if path.exists():
        raise ValueError("SFTPGo restore destination database already exists")
    for suffix in ("-wal", "-shm"):
        if Path(f"{path}{suffix}").exists():
            raise ValueError("SFTPGo restore destination has SQLite companion files")

    resolved_parent = path.parent.resolve(strict=False)
    resolved_artifact = artifact_path.resolve(strict=True)
    if _is_relative_to(resolved_parent, resolved_artifact.parent) or _is_relative_to(
        resolved_artifact,
        resolved_parent,
    ):
        raise ValueError("SFTPGo restore destination overlaps its backup artifact")
    _validate_fresh_destination(path)


def _copy_database_to_destination(
    source_path: Path,
    destination_path: Path,
    staging_path: Path,
) -> None:
    source_hash = _sha256_file(source_path)
    source_size = source_path.stat().st_size
    published = False
    try:
        with source_path.open("rb") as source:
            descriptor = os.open(staging_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as destination:
                shutil.copyfileobj(source, destination, length=1024 * 1024)
                destination.flush()
                os.fsync(destination.fileno())
        _validate_private_mode(staging_path, label="restore staging database")
        if staging_path.stat().st_size != source_size or _sha256_file(staging_path) != source_hash:
            raise RuntimeError("SFTPGo restored database copy does not match the artifact")
        _database_evidence(staging_path, require_transient_empty=True)
        os.link(staging_path, destination_path)
        published = True
        directory_fd = os.open(destination_path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        _validate_private_mode(destination_path, label="restored database")
        if (
            destination_path.stat().st_size != source_size
            or _sha256_file(destination_path) != source_hash
        ):
            raise RuntimeError("SFTPGo published database does not match the artifact")
        _database_evidence(destination_path, require_transient_empty=True)
    except FileExistsError as exc:
        raise ValueError("SFTPGo restore destination database already exists") from exc
    except Exception:
        if published and staging_path.exists() and destination_path.exists():
            try:
                if os.path.samefile(destination_path, staging_path):
                    destination_path.unlink()
            except OSError:
                pass
        raise


def _restore_process_worker(
    source_path: Path,
    destination_path: Path,
    staging_path: Path,
) -> None:
    try:
        _copy_database_to_destination(source_path, destination_path, staging_path)
    except ValueError:
        raise SystemExit(2) from None
    except BaseException:
        raise SystemExit(1) from None


def _start_restore_process(
    source_path: Path,
    destination_path: Path,
    staging_path: Path,
) -> BaseProcess:
    context = multiprocessing.get_context("spawn")
    process = context.Process(
        target=_restore_process_worker,
        args=(source_path, destination_path, staging_path),
        name="sftpgo-restore",
        daemon=True,
    )
    process.start()
    return process


def _remove_owned_restore_output(destination_path: Path, staging_path: Path) -> None:
    if destination_path.exists() and staging_path.exists():
        try:
            if os.path.samefile(destination_path, staging_path):
                destination_path.unlink()
        except OSError:
            pass
    staging_path.unlink(missing_ok=True)


async def _restore_database_with_deadline(
    source_path: Path,
    destination_path: Path,
) -> None:
    staging_path = destination_path.parent / (
        f".{destination_path.name}.{uuid.uuid4().hex}.restore.tmp"
    )
    process = _start_restore_process(source_path, destination_path, staging_path)
    succeeded = False
    try:
        await _join_worker_process(process, _RESTORE_TIMEOUT_SECONDS)
        if process.is_alive():
            await _stop_worker_process_before_return(process, operation="restore")
            raise TimeoutError("SFTPGo restore timed out")
        if process.exitcode == 2:
            raise ValueError("SFTPGo restore destination database already exists")
        if process.exitcode != 0:
            raise RuntimeError("SFTPGo restore database copy failed")
        succeeded = True
    except asyncio.CancelledError:
        await _stop_worker_process_before_return(process, operation="restore")
        raise
    except BaseException:
        if process.is_alive():
            await _stop_worker_process_before_return(process, operation="restore")
        raise
    finally:
        if not process.is_alive():
            if succeeded:
                staging_path.unlink(missing_ok=True)
            else:
                _remove_owned_restore_output(destination_path, staging_path)


class SFTPGoPlugin(BackupPlugin):
    """Snapshot and create-only restore of SFTPGo v2.7.5 provider state."""

    restore_capability = "partial"

    async def validate_config(self, config: Dict[str, Any]) -> bool:
        if not isinstance(config, dict) or set(config) != {"database_path"}:
            return False
        raw_path = config.get("database_path")
        if not isinstance(raw_path, str) or not raw_path:
            return False
        path = Path(raw_path)
        return (
            path.is_absolute() and path.name == "sftpgo.db" and ".." not in PurePath(raw_path).parts
        )

    async def test(self, config: Dict[str, Any]) -> bool:
        if not await self.validate_config(config):
            raise ValueError("Invalid SFTPGo configuration: database_path is required")
        path = Path(config["database_path"])
        if _path_has_symlink(path):
            raise ValueError("SFTPGo database path must not contain a symbolic link")
        if path.exists():
            if not path.is_file():
                raise ValueError("SFTPGo database path is not a regular file")
            await asyncio.to_thread(
                _database_evidence,
                path,
                require_transient_empty=False,
            )
            return True

        _validate_restore_path_location(path)
        await asyncio.to_thread(_validate_fresh_destination, path)
        return True

    async def backup(self, context: BackupContext) -> Dict[str, Any]:
        if not await self.validate_config(context.config):
            raise ValueError("Invalid SFTPGo backup configuration")
        source_path = Path(context.config["database_path"])
        if _path_has_symlink(source_path):
            raise ValueError("SFTPGo database path must not contain a symbolic link")
        if not source_path.exists() or not source_path.is_file():
            raise FileNotFoundError("SFTPGo source database was not found")
        await asyncio.to_thread(
            _database_evidence,
            source_path,
            require_transient_empty=False,
        )

        with create_backup_artifact(
            self,
            context,
            prefix="sftpgo-v2.7.5-state",
            suffix=".db",
            backup_root=BACKUP_BASE_PATH,
        ) as artifact:
            await _create_snapshot(source_path, artifact.temporary_path)
            _validate_private_mode(artifact.temporary_path, label="artifact")
            await asyncio.to_thread(
                _database_evidence,
                artifact.temporary_path,
                require_transient_empty=True,
            )

        try:
            _validate_private_mode(artifact.final_path, label="artifact")
        except Exception:
            Path(f"{artifact.final_path}.meta.json").unlink(missing_ok=True)
            artifact.final_path.unlink(missing_ok=True)
            raise
        return {"artifact_path": str(artifact.final_path)}

    async def restore(self, context: RestoreContext) -> Dict[str, Any]:
        if not await self.validate_config(context.config):
            raise ValueError("Invalid SFTPGo restore configuration")
        artifact_path = Path(context.artifact_path)
        if not artifact_path.exists() or not artifact_path.is_file() or artifact_path.is_symlink():
            raise ValueError("SFTPGo restore artifact is not a regular file")
        _validate_private_mode(artifact_path, label="restore artifact")
        await asyncio.to_thread(
            _database_evidence,
            artifact_path,
            require_transient_empty=True,
        )

        destination_path = Path(context.config["database_path"])
        _validate_restore_destination(destination_path, artifact_path)
        await _restore_database_with_deadline(
            artifact_path,
            destination_path,
        )
        return {
            "status": "partial",
            "restored_path": str(destination_path),
            "message": (
                "Offline SFTPGo database restored; boot and verify an isolated "
                f"SFTPGo {SFTPGO_VERSION} instance"
            ),
        }

    async def get_status(self, context: BackupContext) -> Dict[str, Any]:
        await self.test(context.config)
        return {"status": ("ok" if Path(context.config["database_path"]).exists() else "unknown")}
