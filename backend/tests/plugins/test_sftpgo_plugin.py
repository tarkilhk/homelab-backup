from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import threading
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.core.plugins import artifacts as artifacts_module
from app.core.plugins.base import BackupContext, RestoreContext
from app.core.plugins.loader import get_plugin, get_plugin_schema_path, list_plugins
from app.core.plugins.sidecar import read_backup_sidecar
from app.main import app
from app.plugins.sftpgo import plugin as sftpgo_module
from app.plugins.sftpgo.plugin import (
    RESTORE_SENTINEL_CONTENT,
    RESTORE_SENTINEL_NAME,
    SFTPGoPlugin,
)

SCHEMA_VERSION = 33
EXACT_SCHEMA_V33: dict[str, tuple[str, ...]] = {
    "schema_version": ("id", "version"),
    "roles": ("id", "name", "description", "created_at", "updated_at"),
    "admins": (
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
    ),
    "active_transfers": (
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
    ),
    "defender_hosts": ("id", "ip", "ban_time", "updated_at"),
    "defender_events": ("id", "date_time", "score", "host_id"),
    "folders": (
        "id",
        "name",
        "description",
        "path",
        "used_quota_size",
        "used_quota_files",
        "last_quota_update",
        "filesystem",
    ),
    "groups": (
        "id",
        "name",
        "description",
        "created_at",
        "updated_at",
        "user_settings",
    ),
    "shared_sessions": ("key", "type", "data", "timestamp"),
    "users": (
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
    ),
    "groups_folders_mapping": (
        "id",
        "folder_id",
        "group_id",
        "virtual_path",
        "quota_size",
        "quota_files",
        "sort_order",
    ),
    "users_groups_mapping": (
        "id",
        "user_id",
        "group_id",
        "group_type",
        "sort_order",
    ),
    "users_folders_mapping": (
        "id",
        "user_id",
        "folder_id",
        "virtual_path",
        "quota_size",
        "quota_files",
        "sort_order",
    ),
    "shares": (
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
    ),
    "api_keys": (
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
    ),
    "events_rules": (
        "id",
        "name",
        "status",
        "description",
        "created_at",
        "updated_at",
        "trigger",
        "conditions",
        "deleted_at",
    ),
    "events_actions": ("id", "name", "description", "type", "options"),
    "rules_actions_mapping": (
        "id",
        "rule_id",
        "action_id",
        "order",
        "options",
    ),
    "tasks": ("id", "name", "updated_at", "version"),
    "admins_groups_mapping": (
        "id",
        "admin_id",
        "group_id",
        "options",
        "sort_order",
    ),
    "ip_lists": (
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
    ),
    "configurations": ("id", "configs"),
}
TRANSIENT_TABLES = (
    "active_transfers",
    "shared_sessions",
    "tasks",
    "defender_events",
    "defender_hosts",
)
ALL_REQUIRED_COLUMNS = tuple(
    (table, column) for table, columns in EXACT_SCHEMA_V33.items() for column in columns
)


@pytest.fixture
def anyio_backend() -> tuple[str, dict[str, bool]]:
    """Use uvloop on this dev VM because its default loop misses thread wakeups."""

    return ("asyncio", {"use_uvloop": True})


def _column_declaration(table: str, column: str) -> str:
    if column == "id":
        return '"id" INTEGER PRIMARY KEY'
    if table == "defender_events" and column == "host_id":
        return '"host_id" INTEGER REFERENCES "defender_hosts"("id")'
    if table == "schema_version" and column == "version":
        return '"version" INTEGER NOT NULL'
    return f'"{column}" TEXT'


def _insert_row(
    connection: sqlite3.Connection,
    table: str,
    values: dict[str, object],
) -> None:
    columns = {str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')}
    selected = [(column, value) for column, value in values.items() if column in columns]
    if not selected:
        connection.execute(f'INSERT INTO "{table}" DEFAULT VALUES')
        return
    names = ", ".join(f'"{column}"' for column, _value in selected)
    placeholders = ", ".join("?" for _item in selected)
    connection.execute(
        f'INSERT INTO "{table}" ({names}) VALUES ({placeholders})',
        tuple(value for _column, value in selected),
    )


def _create_sftpgo_database(
    path: Path,
    *,
    version: int = SCHEMA_VERSION,
    omit_table: str | None = None,
    omit_column: tuple[str, str] | None = None,
    include_admin: bool = True,
    secret: str = "synthetic-sftpgo-secret",
    transient_rows: bool = False,
    foreign_key_violation: bool = False,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        for table, columns in EXACT_SCHEMA_V33.items():
            if table == omit_table:
                continue
            declarations = [
                _column_declaration(table, column)
                for column in columns
                if omit_column != (table, column)
            ]
            connection.execute(f'CREATE TABLE "{table}" ({", ".join(declarations)})')

        if omit_table != "schema_version":
            _insert_row(connection, "schema_version", {"id": 1, "version": version})
        if include_admin and omit_table != "admins":
            _insert_row(
                connection,
                "admins",
                {
                    "id": 1,
                    "username": "local-admin",
                    "password": secret,
                    "permissions": '["*"]',
                    "status": 1,
                },
            )
        if omit_table != "configurations":
            _insert_row(connection, "configurations", {"id": 1, "configs": "{}"})
        if transient_rows:
            for index, table in enumerate(TRANSIENT_TABLES, start=1):
                if table == omit_table:
                    continue
                _insert_row(
                    connection,
                    table,
                    {
                        "id": index,
                        "key": f"runtime-{index}",
                        "type": index,
                        "host_id": None,
                    },
                )
        if foreign_key_violation and omit_table != "defender_events":
            _insert_row(
                connection,
                "defender_events",
                {"id": 999, "host_id": 987654},
            )
        connection.commit()


def _fresh_restore_path(tmp_path: Path, name: str = "restore") -> Path:
    parent = tmp_path / name
    parent.mkdir()
    (parent / RESTORE_SENTINEL_NAME).write_text(
        RESTORE_SENTINEL_CONTENT,
        encoding="utf-8",
    )
    return parent / "sftpgo.db"


async def _create_backup_for_restore(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[SFTPGoPlugin, Path, str]:
    source_path = tmp_path / "source" / "sftpgo.db"
    secret = "restore-only-synthetic-marker"
    _create_sftpgo_database(
        source_path,
        secret=secret,
        transient_rows=True,
    )
    backup_root = tmp_path / "backups"
    monkeypatch.setattr(sftpgo_module, "BACKUP_BASE_PATH", str(backup_root))
    plugin = SFTPGoPlugin(name="sftpgo")
    artifact_path = Path(
        (
            await plugin.backup(
                BackupContext(
                    job_id="1",
                    target_id="2",
                    config={"database_path": str(source_path)},
                    metadata={"target_slug": "sftpgo-source"},
                )
            )
        )["artifact_path"]
    )
    return plugin, artifact_path, secret


def _restore_context(artifact_path: Path, destination_path: Path) -> RestoreContext:
    return RestoreContext(
        job_id="restore-1",
        source_target_id="2",
        destination_target_id="3",
        config={"database_path": str(destination_path)},
        artifact_path=str(artifact_path),
    )


@pytest.mark.anyio
async def test_sftpgo_discovery_schema_and_configuration_contract() -> None:
    plugin = get_plugin("sftpgo")

    assert isinstance(plugin, SFTPGoPlugin)
    assert plugin.restore_capability == "partial"
    assert any(item["key"] == "sftpgo" for item in list_plugins())

    schema_path = get_plugin_schema_path("sftpgo")
    assert schema_path is not None
    schema = json.loads(Path(schema_path).read_text(encoding="utf-8"))
    assert schema["required"] == ["database_path"]
    assert set(schema["properties"]) == {"database_path"}
    assert schema["properties"]["database_path"]["default"] == ("/sources/sftpgo/config/sftpgo.db")
    assert RESTORE_SENTINEL_NAME == ".sftpgo-restore-destination"
    assert RESTORE_SENTINEL_CONTENT == "sftpgo-v2.7.5-isolated-restore-v1\n"

    assert await plugin.validate_config({"database_path": "/safe/isolated/sftpgo.db"})
    for invalid in (
        {},
        {"database_path": None},
        {"database_path": "sftpgo.db"},
        {"database_path": "/safe/isolated/other.db"},
        {"database_path": "/safe/../isolated/sftpgo.db"},
        {"database_path": "/safe/isolated/sftpgo.db", "extra": True},
    ):
        assert not await plugin.validate_config(invalid)


@pytest.mark.anyio
async def test_connectivity_validates_exact_v275_database_and_status(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "sftpgo.db"
    _create_sftpgo_database(database_path)
    plugin = SFTPGoPlugin(name="sftpgo")
    config = {"database_path": str(database_path)}

    assert await plugin.test(config) is True
    assert await plugin.get_status(BackupContext(job_id="1", target_id="1", config=config)) == {
        "status": "ok"
    }


@pytest.mark.anyio
async def test_connectivity_accepts_only_safe_sentinel_marked_fresh_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "restore"
    destination.mkdir()
    database_path = destination / "sftpgo.db"
    plugin = SFTPGoPlugin(name="sftpgo")
    config = {"database_path": str(database_path)}

    with pytest.raises(FileNotFoundError, match="sentinel"):
        await plugin.test(config)

    sentinel = destination / RESTORE_SENTINEL_NAME
    sentinel.write_text("wrong-v1-marker\n", encoding="utf-8")
    with pytest.raises(ValueError, match="sentinel"):
        await plugin.test(config)

    sentinel.write_text(RESTORE_SENTINEL_CONTENT, encoding="utf-8")
    assert await plugin.test(config) is True
    assert await plugin.get_status(
        BackupContext(job_id="1", target_id="restore", config=config)
    ) == {"status": "unknown"}

    (destination / "unexpected").write_text("not empty", encoding="utf-8")
    with pytest.raises(ValueError, match="otherwise empty"):
        await plugin.test(config)
    (destination / "unexpected").unlink()

    monkeypatch.setattr(sftpgo_module.os, "access", lambda *_args: False)
    with pytest.raises(PermissionError, match="not writable"):
        await plugin.test(config)


@pytest.mark.anyio
async def test_connectivity_refuses_forbidden_nonregular_and_symlink_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin = SFTPGoPlugin(name="sftpgo")
    forbidden = tmp_path / "live"
    forbidden.mkdir()
    (forbidden / RESTORE_SENTINEL_NAME).write_text(
        RESTORE_SENTINEL_CONTENT,
        encoding="utf-8",
    )
    monkeypatch.setattr(sftpgo_module, "_FORBIDDEN_RESTORE_ROOTS", (forbidden,))
    with pytest.raises(ValueError, match="forbidden"):
        await plugin.test({"database_path": str(forbidden / "sftpgo.db")})

    directory_path = tmp_path / "directory" / "sftpgo.db"
    directory_path.mkdir(parents=True)
    with pytest.raises(ValueError, match="regular file"):
        await plugin.test({"database_path": str(directory_path)})

    actual_path = tmp_path / "actual" / "sftpgo.db"
    _create_sftpgo_database(actual_path)
    linked_dir = tmp_path / "linked"
    linked_dir.symlink_to(actual_path.parent, target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic link"):
        await plugin.test({"database_path": str(linked_dir / "sftpgo.db")})


@pytest.mark.anyio
@pytest.mark.parametrize("missing_table", tuple(EXACT_SCHEMA_V33))
async def test_connectivity_requires_every_v275_table(
    tmp_path: Path,
    missing_table: str,
) -> None:
    database_path = tmp_path / "sftpgo.db"
    _create_sftpgo_database(database_path, omit_table=missing_table)

    with pytest.raises(RuntimeError):
        await SFTPGoPlugin(name="sftpgo").test({"database_path": str(database_path)})


@pytest.mark.anyio
@pytest.mark.parametrize(
    "missing_column",
    ALL_REQUIRED_COLUMNS,
    ids=lambda item: f"{item[0]}.{item[1]}",
)
async def test_connectivity_requires_every_v275_column(
    tmp_path: Path,
    missing_column: tuple[str, str],
) -> None:
    database_path = tmp_path / "sftpgo.db"
    _create_sftpgo_database(database_path, omit_column=missing_column)

    with pytest.raises(RuntimeError):
        await SFTPGoPlugin(name="sftpgo").test({"database_path": str(database_path)})


@pytest.mark.anyio
async def test_connectivity_rejects_wrong_version_no_admin_integrity_and_foreign_keys(
    tmp_path: Path,
) -> None:
    plugin = SFTPGoPlugin(name="sftpgo")

    wrong_version = tmp_path / "wrong-version" / "sftpgo.db"
    _create_sftpgo_database(wrong_version, version=32)
    with pytest.raises(RuntimeError, match="schema version"):
        await plugin.test({"database_path": str(wrong_version)})

    no_admin = tmp_path / "no-admin" / "sftpgo.db"
    _create_sftpgo_database(no_admin, include_admin=False)
    with pytest.raises(RuntimeError, match="administrator"):
        await plugin.test({"database_path": str(no_admin)})

    corrupt = tmp_path / "corrupt" / "sftpgo.db"
    corrupt.parent.mkdir()
    corrupt.write_bytes(b"not a usable SQLite database")
    with pytest.raises(RuntimeError, match="usable|integrity"):
        await plugin.test({"database_path": str(corrupt)})

    secret = "must-never-appear-in-errors"
    broken_fk = tmp_path / "broken-fk" / "sftpgo.db"
    _create_sftpgo_database(
        broken_fk,
        secret=secret,
        foreign_key_violation=True,
    )
    with pytest.raises(RuntimeError, match="foreign-key") as error:
        await plugin.test({"database_path": str(broken_fk)})
    assert secret not in str(error.value)


@pytest.mark.anyio
async def test_backup_refuses_symlink_and_nonregular_sources_before_artifact_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actual_path = tmp_path / "actual" / "sftpgo.db"
    _create_sftpgo_database(actual_path)
    linked_path = tmp_path / "linked" / "sftpgo.db"
    linked_path.parent.mkdir()
    linked_path.symlink_to(actual_path)
    directory_path = tmp_path / "directory" / "sftpgo.db"
    directory_path.mkdir(parents=True)
    backup_root = tmp_path / "backups"
    monkeypatch.setattr(sftpgo_module, "BACKUP_BASE_PATH", str(backup_root))
    plugin = SFTPGoPlugin(name="sftpgo")

    for source_path, message in (
        (linked_path, "symbolic link"),
        (directory_path, "not found|regular file"),
    ):
        with pytest.raises((FileNotFoundError, ValueError), match=message):
            await plugin.backup(
                BackupContext(
                    job_id="unsafe-source",
                    target_id="unsafe-source",
                    config={"database_path": str(source_path)},
                    metadata={"target_slug": "unsafe-source"},
                )
            )
    assert not list(backup_root.rglob("*"))


@pytest.mark.anyio
async def test_backup_creates_live_private_standalone_snapshot_sidecar_and_scrubs_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "source" / "sftpgo.db"
    secret = "backup-persistent-secret"
    _create_sftpgo_database(
        database_path,
        secret=secret,
        transient_rows=True,
    )
    writer = sqlite3.connect(database_path)
    assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    _insert_row(writer, "users", {"id": 77, "username": "committed-user"})
    writer.commit()

    backup_root = tmp_path / "backups"
    monkeypatch.setattr(sftpgo_module, "BACKUP_BASE_PATH", str(backup_root))
    plugin = SFTPGoPlugin(name="sftpgo")
    context = BackupContext(
        job_id="11",
        target_id="22",
        config={"database_path": str(database_path)},
        metadata={"target_slug": "sftpgo-primary"},
    )
    try:
        artifact_path = Path((await plugin.backup(context))["artifact_path"])
    finally:
        writer.close()

    assert artifact_path.is_file()
    assert artifact_path.suffix == ".db"
    assert stat.S_IMODE(artifact_path.stat().st_mode) == 0o600
    assert artifact_path.is_relative_to(backup_root / "sftpgo-primary")
    assert not Path(f"{artifact_path}-wal").exists()
    assert not Path(f"{artifact_path}-shm").exists()
    sidecar = read_backup_sidecar(str(artifact_path))
    assert sidecar is not None
    assert sidecar["plugin_name"] == "sftpgo"

    with sqlite3.connect(artifact_path) as snapshot:
        assert snapshot.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert list(snapshot.execute("PRAGMA foreign_key_check")) == []
        assert snapshot.execute("SELECT version FROM schema_version").fetchone()[0] == 33
        assert snapshot.execute("SELECT COUNT(*) FROM admins").fetchone()[0] == 1
        assert (
            snapshot.execute(
                "SELECT COUNT(*) FROM users WHERE username = 'committed-user'"
            ).fetchone()[0]
            == 1
        )
        assert snapshot.execute("SELECT password FROM admins").fetchone()[0] == secret
        for table in TRANSIENT_TABLES:
            assert snapshot.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] == 0

    with sqlite3.connect(database_path) as source:
        for table in TRANSIENT_TABLES:
            assert source.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] == 1


@pytest.mark.anyio
async def test_two_backups_are_unique_and_capture_later_committed_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "source" / "sftpgo.db"
    _create_sftpgo_database(database_path)
    backup_root = tmp_path / "backups"
    monkeypatch.setattr(sftpgo_module, "BACKUP_BASE_PATH", str(backup_root))
    plugin = SFTPGoPlugin(name="sftpgo")
    context = BackupContext(
        job_id="unique",
        target_id="unique",
        config={"database_path": str(database_path)},
        metadata={"target_slug": "unique"},
    )

    first = Path((await plugin.backup(context))["artifact_path"])
    with sqlite3.connect(database_path) as connection:
        _insert_row(connection, "users", {"id": 88, "username": "later-user"})
    second = Path((await plugin.backup(context))["artifact_path"])

    assert first != second
    with sqlite3.connect(first) as snapshot:
        assert snapshot.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
    with sqlite3.connect(second) as snapshot:
        assert snapshot.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1


@pytest.mark.anyio
async def test_backup_cleans_artifact_when_sidecar_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "source" / "sftpgo.db"
    _create_sftpgo_database(database_path)
    backup_root = tmp_path / "backups"
    monkeypatch.setattr(sftpgo_module, "BACKUP_BASE_PATH", str(backup_root))

    def fail_sidecar(*_args: object, **_kwargs: object) -> None:
        raise OSError("injected sidecar failure")

    monkeypatch.setattr(artifacts_module, "write_backup_sidecar", fail_sidecar)
    with pytest.raises(OSError, match="sidecar failure"):
        await SFTPGoPlugin(name="sftpgo").backup(
            BackupContext(
                job_id="sidecar",
                target_id="sidecar",
                config={"database_path": str(database_path)},
                metadata={"target_slug": "sidecar"},
            )
        )
    assert not list(backup_root.rglob("*.db"))
    assert not list(backup_root.rglob("*.meta.json"))
    assert not list(backup_root.rglob("*.tmp"))


@pytest.mark.anyio
async def test_backup_timeout_stops_worker_and_cleans_every_partial_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "source" / "sftpgo.db"
    _create_sftpgo_database(database_path)
    backup_root = tmp_path / "backups"

    class BlockingProcess:
        exitcode: int | None = None

        def __init__(self) -> None:
            self.alive = True
            self.terminated = False
            self.released = threading.Event()

        def join(self, timeout: float) -> None:
            self.released.wait(timeout)
            if self.terminated:
                self.alive = False
                self.exitcode = -15

        def is_alive(self) -> bool:
            return self.alive

        def terminate(self) -> None:
            self.terminated = True
            self.released.set()

        def kill(self) -> None:
            self.terminated = True
            self.released.set()

    process = BlockingProcess()

    monkeypatch.setattr(sftpgo_module, "BACKUP_BASE_PATH", str(backup_root))
    monkeypatch.setattr(sftpgo_module, "_SNAPSHOT_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(
        sftpgo_module,
        "_start_snapshot_process",
        lambda *_args: process,
    )
    with pytest.raises(TimeoutError, match="timed out"):
        await SFTPGoPlugin(name="sftpgo").backup(
            BackupContext(
                job_id="timeout",
                target_id="timeout",
                config={"database_path": str(database_path)},
                metadata={"target_slug": "timeout"},
            )
        )
    assert process.terminated
    assert not process.is_alive()
    assert not list(backup_root.rglob("*.db"))
    assert not list(backup_root.rglob("*.meta.json"))
    assert not list(backup_root.rglob("*.tmp"))


@pytest.mark.anyio
async def test_snapshot_stop_escalates_to_kill_and_reaps_stubborn_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StubbornProcess:
        exitcode: int | None = None

        def __init__(self) -> None:
            self.alive = True
            self.terminated = False
            self.killed = False

        def join(self, timeout: float) -> None:
            if self.killed:
                self.alive = False
                self.exitcode = -9
            else:
                threading.Event().wait(timeout)

        def is_alive(self) -> bool:
            return self.alive

        def terminate(self) -> None:
            self.terminated = True

        def kill(self) -> None:
            self.killed = True

    process: Any = StubbornProcess()
    monkeypatch.setattr(sftpgo_module, "_SNAPSHOT_STOP_TIMEOUT_SECONDS", 0.01)

    await sftpgo_module._stop_worker_process(process, operation="snapshot")

    assert process.terminated
    assert process.killed
    assert not process.is_alive()
    assert process.exitcode == -9


@pytest.mark.anyio
async def test_backup_cancellation_stops_worker_and_cleans_every_partial_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "source" / "sftpgo.db"
    _create_sftpgo_database(database_path)
    backup_root = tmp_path / "backups"
    started = threading.Event()

    class BlockingProcess:
        exitcode: int | None = None

        def __init__(self) -> None:
            self.alive = True
            self.terminated = False
            self.released = threading.Event()

        def join(self, timeout: float) -> None:
            started.set()
            self.released.wait(timeout)
            if self.terminated:
                self.alive = False
                self.exitcode = -15

        def is_alive(self) -> bool:
            return self.alive

        def terminate(self) -> None:
            self.terminated = True
            self.released.set()

        def kill(self) -> None:
            self.terminated = True
            self.released.set()

    process = BlockingProcess()

    monkeypatch.setattr(sftpgo_module, "BACKUP_BASE_PATH", str(backup_root))

    def start_with_partial_files(
        _source_path: Path,
        snapshot_path: Path,
        _deadline: float,
    ) -> BlockingProcess:
        snapshot_path.write_bytes(b"credential-bearing partial")
        Path(f"{snapshot_path}-wal").write_bytes(b"partial WAL")
        Path(f"{snapshot_path}-shm").write_bytes(b"partial SHM")
        return process

    monkeypatch.setattr(sftpgo_module, "_start_snapshot_process", start_with_partial_files)
    task = asyncio.create_task(
        SFTPGoPlugin(name="sftpgo").backup(
            BackupContext(
                job_id="cancel",
                target_id="cancel",
                config={"database_path": str(database_path)},
                metadata={"target_slug": "cancel"},
            )
        )
    )
    assert await asyncio.to_thread(started.wait, 2.0)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert process.terminated
    assert not process.is_alive()
    assert not list(backup_root.rglob("*.db"))
    assert not list(backup_root.rglob("*.meta.json"))
    assert not list(backup_root.rglob("*.tmp"))
    assert not [path for path in backup_root.rglob("*") if path.is_file()]


@pytest.mark.anyio
@pytest.mark.parametrize("variant", ("corrupt", "wrong-version", "symlink", "public"))
async def test_restore_rejects_corrupt_wrong_version_symlink_and_public_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    variant: str,
) -> None:
    plugin, artifact_path, _secret = await _create_backup_for_restore(tmp_path, monkeypatch)
    unsafe_path = tmp_path / "unsafe" / "artifact.db"
    unsafe_path.parent.mkdir()
    if variant == "symlink":
        unsafe_path.symlink_to(artifact_path)
    else:
        unsafe_path.write_bytes(artifact_path.read_bytes())
        unsafe_path.chmod(0o600)
        if variant == "corrupt":
            unsafe_path.write_bytes(b"not a SQLite artifact")
        elif variant == "wrong-version":
            with sqlite3.connect(unsafe_path) as connection:
                connection.execute("UPDATE schema_version SET version = 32")
        elif variant == "public":
            unsafe_path.chmod(0o644)
    destination_path = _fresh_restore_path(tmp_path)

    with pytest.raises((PermissionError, RuntimeError, ValueError)):
        await plugin.restore(_restore_context(unsafe_path, destination_path))
    assert not destination_path.exists()
    assert {item.name for item in destination_path.parent.iterdir()} == {RESTORE_SENTINEL_NAME}


@pytest.mark.anyio
@pytest.mark.parametrize("collision", ("database", "-wal", "-shm"))
async def test_restore_refuses_existing_database_and_companion_collisions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    collision: str,
) -> None:
    plugin, artifact_path, _secret = await _create_backup_for_restore(tmp_path, monkeypatch)
    destination_path = _fresh_restore_path(tmp_path)
    collision_path = (
        destination_path if collision == "database" else Path(f"{destination_path}{collision}")
    )
    collision_path.write_bytes(b"existing-state")

    with pytest.raises(ValueError, match="exists|companion"):
        await plugin.restore(_restore_context(artifact_path, destination_path))
    assert collision_path.read_bytes() == b"existing-state"


@pytest.mark.anyio
async def test_restore_refuses_overlap_forbidden_and_symlink_destinations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin, artifact_path, _secret = await _create_backup_for_restore(tmp_path, monkeypatch)

    overlap_path = artifact_path.parent / "sftpgo.db"
    with pytest.raises(ValueError, match="overlaps"):
        await plugin.restore(_restore_context(artifact_path, overlap_path))

    forbidden = tmp_path / "forbidden"
    forbidden.mkdir()
    (forbidden / RESTORE_SENTINEL_NAME).write_text(
        RESTORE_SENTINEL_CONTENT,
        encoding="utf-8",
    )
    monkeypatch.setattr(sftpgo_module, "_FORBIDDEN_RESTORE_ROOTS", (forbidden,))
    with pytest.raises(ValueError, match="forbidden"):
        await plugin.restore(_restore_context(artifact_path, forbidden / "sftpgo.db"))

    actual = tmp_path / "actual-restore"
    actual.mkdir()
    (actual / RESTORE_SENTINEL_NAME).write_text(
        RESTORE_SENTINEL_CONTENT,
        encoding="utf-8",
    )
    linked = tmp_path / "linked-restore"
    linked.symlink_to(actual, target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic link"):
        await plugin.restore(_restore_context(artifact_path, linked / "sftpgo.db"))


@pytest.mark.anyio
async def test_restore_create_only_atomic_copy_revalidates_and_reports_partial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin, artifact_path, secret = await _create_backup_for_restore(tmp_path, monkeypatch)
    destination_path = _fresh_restore_path(tmp_path)
    artifact_digest = hashlib.sha256(artifact_path.read_bytes()).hexdigest()

    result = await plugin.restore(_restore_context(artifact_path, destination_path))

    assert result["status"] == "partial"
    assert result["restored_path"] == str(destination_path)
    assert "isolated" in result["message"]
    assert "2.7.5" in result["message"]
    assert destination_path.read_bytes() == artifact_path.read_bytes()
    assert hashlib.sha256(destination_path.read_bytes()).hexdigest() == artifact_digest
    assert stat.S_IMODE(destination_path.stat().st_mode) == 0o600
    assert not list(destination_path.parent.glob(".*.restore.tmp"))
    with sqlite3.connect(destination_path) as restored:
        assert restored.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert list(restored.execute("PRAGMA foreign_key_check")) == []
        assert restored.execute("SELECT password FROM admins").fetchone()[0] == secret
        for table in TRANSIENT_TABLES:
            assert restored.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] == 0


@pytest.mark.anyio
@pytest.mark.parametrize("publish_before_block", (False, True))
async def test_restore_cancellation_stops_worker_and_removes_only_owned_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    publish_before_block: bool,
) -> None:
    plugin, artifact_path, _secret = await _create_backup_for_restore(tmp_path, monkeypatch)
    destination_path = _fresh_restore_path(tmp_path)
    started = threading.Event()

    class BlockingProcess:
        exitcode: int | None = None

        def __init__(self) -> None:
            self.alive = True
            self.terminated = False
            self.released = threading.Event()

        def join(self, timeout: float) -> None:
            started.set()
            self.released.wait(timeout)
            if self.terminated:
                self.alive = False
                self.exitcode = -15

        def is_alive(self) -> bool:
            return self.alive

        def terminate(self) -> None:
            self.terminated = True
            self.released.set()

        def kill(self) -> None:
            self.terminated = True
            self.released.set()

    process = BlockingProcess()

    def start_blocked_restore(
        source_path: Path,
        child_destination: Path,
        staging_path: Path,
    ) -> BlockingProcess:
        shutil.copy2(source_path, staging_path)
        staging_path.chmod(0o600)
        if publish_before_block:
            os.link(staging_path, child_destination)
        return process

    monkeypatch.setattr(sftpgo_module, "_start_restore_process", start_blocked_restore)
    task = asyncio.create_task(plugin.restore(_restore_context(artifact_path, destination_path)))
    assert await asyncio.to_thread(started.wait, 2.0)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert process.terminated
    assert not process.is_alive()
    assert not destination_path.exists()
    assert not list(destination_path.parent.glob(".*.restore.tmp"))


@pytest.mark.anyio
async def test_restore_timeout_stops_worker_and_rolls_back_published_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin, artifact_path, _secret = await _create_backup_for_restore(tmp_path, monkeypatch)
    destination_path = _fresh_restore_path(tmp_path)

    class BlockingProcess:
        exitcode: int | None = None

        def __init__(self) -> None:
            self.alive = True
            self.terminated = False
            self.released = threading.Event()

        def join(self, timeout: float) -> None:
            self.released.wait(timeout)
            if self.terminated:
                self.alive = False
                self.exitcode = -15

        def is_alive(self) -> bool:
            return self.alive

        def terminate(self) -> None:
            self.terminated = True
            self.released.set()

        def kill(self) -> None:
            self.terminated = True
            self.released.set()

    process = BlockingProcess()

    def start_blocked_restore(
        source_path: Path,
        child_destination: Path,
        staging_path: Path,
    ) -> BlockingProcess:
        shutil.copy2(source_path, staging_path)
        staging_path.chmod(0o600)
        os.link(staging_path, child_destination)
        return process

    monkeypatch.setattr(sftpgo_module, "_RESTORE_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(sftpgo_module, "_start_restore_process", start_blocked_restore)

    with pytest.raises(TimeoutError, match="timed out"):
        await plugin.restore(_restore_context(artifact_path, destination_path))

    assert process.terminated
    assert not process.is_alive()
    assert not destination_path.exists()
    assert not list(destination_path.parent.glob(".*.restore.tmp"))


@pytest.mark.anyio
async def test_restore_preserves_racing_destination_and_cleans_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin, artifact_path, _secret = await _create_backup_for_restore(tmp_path, monkeypatch)
    destination_path = _fresh_restore_path(tmp_path)
    original_link = sftpgo_module.os.link

    def race_link(source: Path, destination: Path) -> None:
        Path(destination).write_bytes(b"racing-writer")
        original_link(source, destination)

    monkeypatch.setattr(sftpgo_module.os, "link", race_link)
    staging_path = destination_path.parent / ".race.restore.tmp"
    with pytest.raises(ValueError, match="already exists"):
        await asyncio.to_thread(
            sftpgo_module._copy_database_to_destination,
            artifact_path,
            destination_path,
            staging_path,
        )
    staging_path.unlink(missing_ok=True)
    assert destination_path.read_bytes() == b"racing-writer"
    assert not list(destination_path.parent.glob(".*.restore.tmp"))


@pytest.mark.anyio
async def test_restore_removes_destination_after_post_publish_validation_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin, artifact_path, _secret = await _create_backup_for_restore(tmp_path, monkeypatch)
    destination_path = _fresh_restore_path(tmp_path)
    original_link = sftpgo_module.os.link

    def publish_corrupt(source: Path, destination: Path) -> None:
        original_link(source, destination)
        Path(destination).write_bytes(b"injected post-publication corruption")

    monkeypatch.setattr(sftpgo_module.os, "link", publish_corrupt)
    staging_path = destination_path.parent / ".corrupt.restore.tmp"
    with pytest.raises(RuntimeError, match="published|usable"):
        await asyncio.to_thread(
            sftpgo_module._copy_database_to_destination,
            artifact_path,
            destination_path,
            staging_path,
        )
    staging_path.unlink(missing_ok=True)
    assert not destination_path.exists()
    assert not list(destination_path.parent.glob(".*.restore.tmp"))


@pytest.mark.anyio
async def test_restore_preserves_writer_replacement_after_plugin_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _plugin, artifact_path, _secret = await _create_backup_for_restore(tmp_path, monkeypatch)
    destination_path = _fresh_restore_path(tmp_path)
    staging_path = destination_path.parent / ".replaced.restore.tmp"
    original_link = sftpgo_module.os.link

    def replace_after_publish(source: Path, destination: Path) -> None:
        original_link(source, destination)
        Path(destination).unlink()
        Path(destination).write_bytes(b"racing-writer-replacement")

    monkeypatch.setattr(sftpgo_module.os, "link", replace_after_publish)

    with pytest.raises((PermissionError, RuntimeError)):
        await asyncio.to_thread(
            sftpgo_module._copy_database_to_destination,
            artifact_path,
            destination_path,
            staging_path,
        )

    assert destination_path.read_bytes() == b"racing-writer-replacement"
    staging_path.unlink(missing_ok=True)


@pytest.mark.anyio
async def test_restore_cleans_staging_when_atomic_publish_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin, artifact_path, _secret = await _create_backup_for_restore(tmp_path, monkeypatch)
    destination_path = _fresh_restore_path(tmp_path)

    def fail_link(_source: Path, _destination: Path) -> None:
        raise OSError("injected link failure")

    monkeypatch.setattr(sftpgo_module.os, "link", fail_link)
    staging_path = destination_path.parent / ".failed.restore.tmp"
    with pytest.raises(OSError, match="link failure"):
        await asyncio.to_thread(
            sftpgo_module._copy_database_to_destination,
            artifact_path,
            destination_path,
            staging_path,
        )
    staging_path.unlink(missing_ok=True)
    assert not destination_path.exists()
    assert not list(destination_path.parent.glob(".*.restore.tmp"))


@pytest.mark.anyio
async def test_plugin_api_exposes_schema_partial_capability_and_secret_safe_test(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "sftpgo.db"
    _create_sftpgo_database(database_path)
    secret = "api-secret-must-be-redacted"
    broken_path = tmp_path / "broken" / "sftpgo.db"
    _create_sftpgo_database(
        broken_path,
        secret=secret,
        foreign_key_violation=True,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        plugins_response = await client.get("/api/v1/plugins/")
        schema_response = await client.get("/api/v1/plugins/sftpgo/schema")
        test_response = await client.post(
            "/api/v1/plugins/sftpgo/test",
            json={"database_path": str(database_path)},
        )
        invalid_response = await client.post(
            "/api/v1/plugins/sftpgo/test",
            json={"database_path": "relative.db", "password": secret},
        )
        corrupt_response = await client.post(
            "/api/v1/plugins/sftpgo/test",
            json={"database_path": str(broken_path)},
        )

    assert plugins_response.status_code == 200
    assert any(
        item["key"] == "sftpgo" and item["restore_capability"] == "partial"
        for item in plugins_response.json()
    )
    assert schema_response.status_code == 200
    assert set(schema_response.json()["properties"]) == {"database_path"}
    assert test_response.json() == {"ok": True}
    assert invalid_response.json()["ok"] is False
    assert corrupt_response.json()["ok"] is False
    assert secret not in invalid_response.text
    assert secret not in corrupt_response.text
