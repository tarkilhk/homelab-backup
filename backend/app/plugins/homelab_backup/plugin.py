"""Consistent self-backup for the Homelab Backup SQLite database."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import tempfile
import threading
import time
import uuid
import zipfile
from contextlib import suppress
from importlib.metadata import version as installed_package_version
from pathlib import Path, PurePath
from typing import Any, Dict

from app.core.plugins.artifacts import create_backup_artifact
from app.core.plugins.base import BackupContext, BackupPlugin, RestoreContext

BACKUP_BASE_PATH = "/backups"
RESTORE_SENTINEL_NAME = ".homelab-backup-restore-destination"
RESTORE_SENTINEL_CONTENT = "homelab-backup-isolated-restore-v1\n"
_SNAPSHOT_TIMEOUT_SECONDS = 120.0
_MAX_MANIFEST_BYTES = 1024 * 1024
_MAX_DATABASE_BYTES = 8 * 1024**3
_MAX_COMPRESSION_RATIO = 1000
_ARCHIVE_MEMBERS = frozenset({"manifest.json", "homelab_backup.db"})
_FORBIDDEN_RESTORE_ROOTS = (Path("/app/db"), Path("/backups"))

REQUIRED_SCHEMA: dict[str, frozenset[str]] = {
    "groups": frozenset({"id", "name", "description", "created_at", "updated_at"}),
    "tags": frozenset({"id", "slug", "display_name", "created_at", "updated_at"}),
    "targets": frozenset(
        {
            "id",
            "name",
            "slug",
            "plugin_name",
            "plugin_config_json",
            "group_id",
            "created_at",
            "updated_at",
        }
    ),
    "jobs": frozenset(
        {
            "id",
            "tag_id",
            "name",
            "schedule_cron",
            "enabled",
            "retention_policy_json",
            "created_at",
            "updated_at",
        }
    ),
    "runs": frozenset(
        {
            "id",
            "job_id",
            "started_at",
            "finished_at",
            "status",
            "operation",
            "message",
            "logs_text",
        }
    ),
    "target_runs": frozenset(
        {
            "id",
            "run_id",
            "target_id",
            "started_at",
            "finished_at",
            "status",
            "operation",
            "message",
            "artifact_path",
            "artifact_bytes",
            "sha256",
            "logs_text",
        }
    ),
    "group_tags": frozenset({"id", "group_id", "tag_id", "created_at"}),
    "target_tags": frozenset(
        {
            "id",
            "target_id",
            "tag_id",
            "origin",
            "source_group_id",
            "is_auto_tag",
            "created_at",
        }
    ),
    "settings": frozenset({"id", "global_retention_policy_json", "created_at", "updated_at"}),
    "maintenance_jobs": frozenset(
        {
            "id",
            "key",
            "job_type",
            "name",
            "schedule_cron",
            "enabled",
            "config_json",
            "visible_in_ui",
            "created_at",
            "updated_at",
        }
    ),
    "maintenance_runs": frozenset(
        {
            "id",
            "maintenance_job_id",
            "started_at",
            "finished_at",
            "status",
            "message",
            "result_json",
        }
    ),
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


def _validate_database(path: Path) -> None:
    """Validate current Homelab Backup schema and SQLite consistency read-only."""

    try:
        uri = f"{path.as_uri()}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=5.0) as connection:
            quick_check = [row[0] for row in connection.execute("PRAGMA quick_check")]
            if quick_check != ["ok"]:
                raise RuntimeError("Homelab Backup database failed SQLite integrity validation")

            tables = {
                str(row[0])
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            missing_tables = set(REQUIRED_SCHEMA).difference(tables)
            if missing_tables:
                raise RuntimeError("Homelab Backup database is missing required tables")

            for table, required_columns in REQUIRED_SCHEMA.items():
                columns = {
                    str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')
                }
                if not required_columns.issubset(columns):
                    raise RuntimeError("Homelab Backup database is missing required table columns")

            if list(connection.execute("PRAGMA foreign_key_check")):
                raise RuntimeError("Homelab Backup database has foreign-key violations")
    except RuntimeError:
        raise
    except (OSError, sqlite3.DatabaseError) as exc:
        raise RuntimeError("Homelab Backup database is not a usable SQLite database") from exc


def _database_evidence(path: Path) -> dict[str, Any]:
    """Return non-secret structural evidence after validating a snapshot."""

    _validate_database(path)
    uri = f"{path.as_uri()}?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True, timeout=5.0) as connection:
            schema_rows = [
                tuple(str(value or "") for value in row)
                for row in connection.execute(
                    """
                    SELECT type, name, tbl_name, sql
                    FROM sqlite_master
                    WHERE name NOT LIKE 'sqlite_%'
                    ORDER BY type, name, tbl_name, sql
                    """
                )
            ]
            normalized_schema = json.dumps(
                schema_rows,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("utf-8")
            row_counts = {
                table: int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
                for table in sorted(REQUIRED_SCHEMA)
            }
    except (OSError, sqlite3.DatabaseError) as exc:
        raise RuntimeError("Homelab Backup database evidence could not be read") from exc
    return {
        "schema_sha256": hashlib.sha256(normalized_schema).hexdigest(),
        "row_counts": row_counts,
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
    """Create one consistent SQLite snapshot and obey cooperative cancellation."""

    def progress(_status: int, _remaining: int, _total: int) -> None:
        if stop_event.is_set():
            raise RuntimeError("Homelab Backup snapshot was cancelled")
        if time.monotonic() >= deadline:
            raise TimeoutError("Homelab Backup snapshot timed out")

    source_uri = f"{source_path.as_uri()}?mode=ro"
    try:
        descriptor = os.open(
            snapshot_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        os.close(descriptor)
        if os.stat(snapshot_path).st_mode & 0o077:
            raise PermissionError("Homelab Backup snapshot permissions are not private")
        with sqlite3.connect(source_uri, uri=True, timeout=5.0) as source:
            with sqlite3.connect(snapshot_path, timeout=5.0) as destination:
                progress(0, 0, 0)
                source.backup(
                    destination,
                    pages=256,
                    progress=progress,
                    sleep=0.01,
                )
                destination.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except (TimeoutError, RuntimeError):
        raise
    except PermissionError:
        raise
    except (OSError, sqlite3.DatabaseError) as exc:
        raise RuntimeError("Homelab Backup SQLite snapshot failed") from exc


async def _create_snapshot(source_path: Path, snapshot_path: Path) -> None:
    stop_event = threading.Event()
    deadline = time.monotonic() + _SNAPSHOT_TIMEOUT_SECONDS
    worker = asyncio.create_task(
        asyncio.to_thread(
            _snapshot_database,
            source_path,
            snapshot_path,
            stop_event,
            deadline,
        )
    )
    try:
        await asyncio.wait_for(
            asyncio.shield(worker),
            timeout=_SNAPSHOT_TIMEOUT_SECONDS + 1.0,
        )
    except asyncio.TimeoutError as exc:
        stop_event.set()
        with suppress(Exception):
            await asyncio.shield(worker)
        raise TimeoutError("Homelab Backup snapshot timed out") from exc
    except asyncio.CancelledError:
        stop_event.set()
        with suppress(Exception):
            await asyncio.shield(worker)
        raise


def _write_backup_archive(snapshot_path: Path, archive_path: Path) -> None:
    evidence = _database_evidence(snapshot_path)
    manifest = {
        "format_version": 1,
        "application_version": installed_package_version("homelab-backup"),
        "sqlite_version": sqlite3.sqlite_version,
        "database": {
            "filename": "homelab_backup.db",
            "size_bytes": snapshot_path.stat().st_size,
            "sha256": _sha256_file(snapshot_path),
        },
        "schema_sha256": evidence["schema_sha256"],
        "required_tables": sorted(REQUIRED_SCHEMA),
        "row_counts": evidence["row_counts"],
    }
    with zipfile.ZipFile(
        archive_path,
        mode="x",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=6,
    ) as archive:
        archive.write(snapshot_path, arcname="homelab_backup.db")
        archive.writestr(
            "manifest.json",
            json.dumps(manifest, sort_keys=True, separators=(",", ":")),
        )
    os.chmod(archive_path, 0o600)
    if os.stat(archive_path).st_mode & 0o077:
        raise PermissionError("Homelab Backup artifact permissions are not private")


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
            raise ValueError("Restore destination uses a forbidden live or backup path")


def _validate_private_mode(path: Path, *, label: str) -> None:
    if os.stat(path).st_mode & 0o077:
        raise PermissionError(f"Homelab Backup {label} permissions are not private")


def _load_archive_manifest(artifact_path: Path) -> dict[str, Any]:
    """Validate the fixed ZIP envelope and return its strict manifest."""

    if not artifact_path.exists() or not artifact_path.is_file() or artifact_path.is_symlink():
        raise ValueError("Homelab Backup restore artifact is not a regular file")
    try:
        with zipfile.ZipFile(artifact_path) as archive:
            members = archive.infolist()
            names = [member.filename for member in members]
            if len(members) != 2 or set(names) != _ARCHIVE_MEMBERS or len(set(names)) != 2:
                raise ValueError("Homelab Backup archive must contain exactly two members")
            for member in members:
                file_type = (member.external_attr >> 16) & 0o170000
                if file_type not in {0, stat.S_IFREG}:
                    raise ValueError("Homelab Backup archive contains an unsafe member")
                if member.file_size < 0 or member.compress_size < 0:
                    raise ValueError("Homelab Backup archive member size is invalid")
                if (
                    member.file_size > 0
                    and member.compress_size == 0
                    or member.compress_size > 0
                    and member.file_size > member.compress_size * _MAX_COMPRESSION_RATIO
                ):
                    raise ValueError("Homelab Backup archive compression ratio is unsafe")
            by_name = {member.filename: member for member in members}
            if by_name["manifest.json"].file_size > _MAX_MANIFEST_BYTES:
                raise ValueError("Homelab Backup manifest is too large")
            database_size = by_name["homelab_backup.db"].file_size
            if database_size <= 0 or database_size > _MAX_DATABASE_BYTES:
                raise ValueError("Homelab Backup database member size is invalid")
            if archive.testzip() is not None:
                raise ValueError("Homelab Backup archive failed CRC validation")
            manifest_payload = archive.read("manifest.json")
    except ValueError:
        raise
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        raise ValueError("Homelab Backup restore artifact is not a valid ZIP archive") from exc

    try:
        manifest = json.loads(manifest_payload)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("Homelab Backup archive manifest is not valid JSON") from exc
    if not isinstance(manifest, dict) or set(manifest) != {
        "format_version",
        "application_version",
        "sqlite_version",
        "database",
        "schema_sha256",
        "required_tables",
        "row_counts",
    }:
        raise ValueError("Homelab Backup archive manifest contract is invalid")
    if manifest.get("format_version") != 1:
        raise ValueError("Homelab Backup archive format version is unsupported")
    if manifest.get("application_version") != installed_package_version("homelab-backup"):
        raise ValueError("Homelab Backup archive application version does not match")
    if manifest.get("required_tables") != sorted(REQUIRED_SCHEMA):
        raise ValueError("Homelab Backup archive required-table contract does not match")
    if not isinstance(manifest.get("sqlite_version"), str):
        raise ValueError("Homelab Backup archive SQLite version is invalid")
    schema_sha256 = manifest.get("schema_sha256")
    if (
        not isinstance(schema_sha256, str)
        or len(schema_sha256) != 64
        or any(character not in "0123456789abcdef" for character in schema_sha256)
    ):
        raise ValueError("Homelab Backup archive schema digest is invalid")
    database = manifest.get("database")
    if not isinstance(database, dict) or set(database) != {
        "filename",
        "size_bytes",
        "sha256",
    }:
        raise ValueError("Homelab Backup archive database metadata is invalid")
    if database.get("filename") != "homelab_backup.db":
        raise ValueError("Homelab Backup archive database filename is invalid")
    if (
        not isinstance(database.get("size_bytes"), int)
        or isinstance(database.get("size_bytes"), bool)
        or database["size_bytes"] != database_size
    ):
        raise ValueError("Homelab Backup archive database size does not match")
    database_sha256 = database.get("sha256")
    if (
        not isinstance(database_sha256, str)
        or len(database_sha256) != 64
        or any(character not in "0123456789abcdef" for character in database_sha256)
    ):
        raise ValueError("Homelab Backup archive database digest is invalid")
    row_counts = manifest.get("row_counts")
    if not isinstance(row_counts, dict) or set(row_counts) != set(REQUIRED_SCHEMA):
        raise ValueError("Homelab Backup archive row-count contract is invalid")
    if any(
        not isinstance(count, int) or isinstance(count, bool) or count < 0
        for count in row_counts.values()
    ):
        raise ValueError("Homelab Backup archive row counts are invalid")
    return manifest


def _assert_database_matches_manifest(path: Path, manifest: dict[str, Any]) -> None:
    database = manifest["database"]
    if path.stat().st_size != database["size_bytes"]:
        raise ValueError("Homelab Backup database size does not match its manifest")
    if _sha256_file(path) != database["sha256"]:
        raise ValueError("Homelab Backup database digest does not match its manifest")
    evidence = _database_evidence(path)
    if evidence["schema_sha256"] != manifest["schema_sha256"]:
        raise ValueError("Homelab Backup database schema does not match its manifest")
    if evidence["row_counts"] != manifest["row_counts"]:
        raise ValueError("Homelab Backup database row counts do not match its manifest")


def _extract_validated_database(
    artifact_path: Path,
    output_path: Path,
) -> dict[str, Any]:
    manifest = _load_archive_manifest(artifact_path)
    try:
        with zipfile.ZipFile(artifact_path) as archive:
            with archive.open("homelab_backup.db") as source:
                descriptor = os.open(
                    output_path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
                if os.fstat(descriptor).st_mode & 0o077:
                    os.close(descriptor)
                    raise PermissionError(
                        "Homelab Backup validation database permissions are not private"
                    )
                with os.fdopen(descriptor, "wb") as destination:
                    shutil.copyfileobj(source, destination, length=1024 * 1024)
                    destination.flush()
                    os.fsync(destination.fileno())
        _assert_database_matches_manifest(output_path, manifest)
    except Exception:
        output_path.unlink(missing_ok=True)
        raise
    return manifest


def _validate_restore_destination(path: Path, artifact_path: Path) -> None:
    _validate_restore_path_location(path)
    if _path_has_symlink(path):
        raise ValueError("Restore destination must not contain a symbolic link")
    resolved_parent = path.parent.resolve(strict=False)
    resolved_artifact = artifact_path.resolve(strict=True)
    if _is_relative_to(resolved_parent, resolved_artifact.parent) or _is_relative_to(
        resolved_artifact, resolved_parent
    ):
        raise ValueError("Restore destination must not overlap the backup artifact")
    if path.exists():
        raise ValueError("Restore destination database already exists")
    for suffix in ("-wal", "-shm"):
        if Path(f"{path}{suffix}").exists():
            raise ValueError("Restore destination contains SQLite companion files")
    _validate_fresh_destination(path)


def _copy_database_to_destination(
    validated_path: Path,
    destination_path: Path,
    manifest: dict[str, Any],
) -> None:
    staging_path = destination_path.parent / (
        f".{destination_path.name}.{uuid.uuid4().hex}.restore.tmp"
    )
    published = False
    try:
        with validated_path.open("rb") as source:
            descriptor = os.open(staging_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as destination:
                shutil.copyfileobj(source, destination, length=1024 * 1024)
                destination.flush()
                os.fsync(destination.fileno())
        if os.stat(staging_path).st_mode & 0o077:
            raise PermissionError("Restore staging database permissions are not private")
        _assert_database_matches_manifest(staging_path, manifest)
        os.link(staging_path, destination_path)
        published = True
        staging_path.unlink()
        directory_fd = os.open(destination_path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        _assert_database_matches_manifest(destination_path, manifest)
    except FileExistsError as exc:
        raise ValueError("Restore destination database already exists") from exc
    except Exception:
        if published:
            destination_path.unlink(missing_ok=True)
        raise
    finally:
        staging_path.unlink(missing_ok=True)


def _validate_fresh_destination(path: Path) -> None:
    parent = path.parent
    if not parent.exists() or not parent.is_dir():
        raise FileNotFoundError("Restore destination directory was not found")
    if _path_has_symlink(path):
        raise ValueError("Restore destination must not contain a symbolic link")
    sentinel = parent / RESTORE_SENTINEL_NAME
    if not sentinel.exists() or not sentinel.is_file() or sentinel.is_symlink():
        raise FileNotFoundError("Restore destination sentinel was not found")
    try:
        marker = sentinel.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError("Restore destination sentinel is invalid") from exc
    if marker != RESTORE_SENTINEL_CONTENT:
        raise ValueError("Restore destination sentinel is invalid")
    if {entry.name for entry in parent.iterdir()} != {RESTORE_SENTINEL_NAME}:
        raise ValueError("Restore destination directory must be otherwise empty")
    if not os.access(parent, os.W_OK):
        raise PermissionError("Restore destination directory is not writable")


class HomelabBackupPlugin(BackupPlugin):
    """Snapshot and create-only restore of Homelab Backup application state."""

    restore_capability = "partial"

    async def validate_config(self, config: Dict[str, Any]) -> bool:
        if set(config) != {"database_path"}:
            return False
        raw_path = config.get("database_path")
        if not isinstance(raw_path, str) or not raw_path:
            return False
        path = Path(raw_path)
        return (
            path.is_absolute()
            and path.name == "homelab_backup.db"
            and ".." not in PurePath(raw_path).parts
        )

    async def test(self, config: Dict[str, Any]) -> bool:
        if not await self.validate_config(config):
            raise ValueError("Invalid configuration: database_path is required")
        path = Path(config["database_path"])
        if _path_has_symlink(path):
            raise ValueError("Database path must not contain a symbolic link")
        if path.exists():
            if not path.is_file():
                raise FileNotFoundError("Homelab Backup database is not a regular file")
            await asyncio.to_thread(_validate_database, path)
        else:
            _validate_restore_path_location(path)
            await asyncio.to_thread(_validate_fresh_destination, path)
        return True

    async def backup(self, context: BackupContext) -> Dict[str, Any]:
        if not await self.validate_config(context.config):
            raise ValueError("Invalid Homelab Backup backup configuration")
        source_path = Path(context.config["database_path"])
        if _path_has_symlink(source_path):
            raise ValueError("Database path must not contain a symbolic link")
        if not source_path.exists() or not source_path.is_file():
            raise FileNotFoundError("Homelab Backup source database was not found")
        await asyncio.to_thread(_validate_database, source_path)

        with create_backup_artifact(
            self,
            context,
            prefix="homelab-backup-state",
            suffix=".zip",
            backup_root=BACKUP_BASE_PATH,
        ) as artifact:
            snapshot_path = artifact.temporary_path.with_suffix(".snapshot.db")
            try:
                await _create_snapshot(source_path, snapshot_path)
                await asyncio.to_thread(
                    _write_backup_archive,
                    snapshot_path,
                    artifact.temporary_path,
                )
                _validate_private_mode(artifact.temporary_path, label="artifact")
                archive_validation_path = artifact.temporary_path.with_suffix(
                    ".archive-validation.db"
                )
                try:
                    await asyncio.to_thread(
                        _extract_validated_database,
                        artifact.temporary_path,
                        archive_validation_path,
                    )
                finally:
                    archive_validation_path.unlink(missing_ok=True)
            finally:
                snapshot_path.unlink(missing_ok=True)
        if stat_mode := artifact.final_path.stat().st_mode & 0o077:
            artifact.final_path.unlink(missing_ok=True)
            Path(f"{artifact.final_path}.meta.json").unlink(missing_ok=True)
            raise PermissionError(
                f"Homelab Backup artifact permissions are not private ({stat_mode:o})"
            )
        return {"artifact_path": str(artifact.final_path)}

    async def restore(self, context: RestoreContext) -> Dict[str, Any]:
        if not await self.validate_config(context.config):
            raise ValueError("Invalid Homelab Backup restore configuration")
        destination_path = Path(context.config["database_path"])
        artifact_path = Path(context.artifact_path)
        _validate_restore_destination(destination_path, artifact_path)

        with tempfile.TemporaryDirectory(prefix="homelab-backup-restore-") as temp_dir:
            validated_path = Path(temp_dir) / "homelab_backup.db"
            manifest = await asyncio.to_thread(
                _extract_validated_database,
                artifact_path,
                validated_path,
            )
            _validate_restore_destination(destination_path, artifact_path)
            await asyncio.to_thread(
                _copy_database_to_destination,
                validated_path,
                destination_path,
                manifest,
            )
        return {
            "status": "partial",
            "restored_path": str(destination_path),
            "message": (
                "Offline database restored; boot and verify an isolated backend "
                "using the recorded application version"
            ),
        }

    async def get_status(self, context: BackupContext) -> Dict[str, Any]:
        await self.test(context.config)
        return {"status": ("ok" if Path(context.config["database_path"]).exists() else "unknown")}
