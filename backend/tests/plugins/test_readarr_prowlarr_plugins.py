from __future__ import annotations

import asyncio
import hashlib
import importlib
import io
import json
import os
import shutil
import sqlite3
import stat
import threading
import warnings
import zipfile
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Generator, cast

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.db import get_session
from app.core.plugins.base import BackupContext, RestoreContext
from app.core.plugins.loader import get_plugin, list_plugins
from app.core.plugins.sidecar import write_backup_sidecar
from app.main import app
from app.models import Run, Target, TargetRun
from app.services.restores import RestoreService

READARR_CONTRACT = (
    "readarr",
    "Readarr",
    "0.4.18.2805",
    158,
    "http://readarr.local:8787",
)

PROWLARR_CONTRACT = (
    "prowlarr",
    "Prowlarr",
    "2.4.0.5397",
    44,
    "http://prowlarr.local:9696",
)

SERVICE_CONTRACTS = (
    pytest.param(
        *READARR_CONTRACT,
        id="readarr-0.4.18.2805",
    ),
    pytest.param(
        *PROWLARR_CONTRACT,
        id="prowlarr-2.4.0.5397",
    ),
)

BACKUP_DIRECTORIES = {
    "readarr": "/sources/readarr/backups",
    "prowlarr": "/sources/prowlarr/backups",
}

RESTORE_ORIGINS = {
    "readarr": "http://readarr-restore:8787",
    "prowlarr": "http://prowlarr-restore:9696",
}

FRESH_RESTORE_PATHS = {
    "readarr": (
        "/api/v1/tag",
        "/api/v1/rootfolder",
        "/api/v1/indexer",
        "/api/v1/downloadclient",
        "/api/v1/notification",
    ),
    "prowlarr": (
        "/api/v1/tag",
        "/api/v1/indexer",
        "/api/v1/downloadclient",
        "/api/v1/applications",
        "/api/v1/notification",
    ),
}

REQUIRED_NATIVE_TABLES = {
    "readarr": (
        "Config",
        "RootFolders",
        "Indexers",
        "DownloadClients",
        "Notifications",
        "Tags",
        "Authors",
        "AuthorMetadata",
        "Books",
        "Editions",
        "BookFiles",
        "QualityProfiles",
        "MetadataProfiles",
        "History",
    ),
    "prowlarr": (
        "Config",
        "Indexers",
        "DownloadClients",
        "Notifications",
        "IndexerProxies",
        "Applications",
        "ApplicationIndexerMapping",
        "Tags",
        "AppSyncProfiles",
        "History",
    ),
}


class _DummyScheduler:
    def start(self) -> None:
        return None

    def shutdown(self) -> None:
        return None


class _NoResultConnection:
    def __init__(self) -> None:
        self.closed = False

    def poll(self) -> bool:
        return False

    def recv(self) -> object:
        raise EOFError("No worker result is available")

    def close(self) -> None:
        self.closed = True


class _ResultConnection(_NoResultConnection):
    def __init__(self, result: object) -> None:
        super().__init__()
        self.result = result

    def poll(self) -> bool:
        return True

    def recv(self) -> object:
        return self.result


class _CompletedProcess:
    def __init__(self, *, exitcode: int = 0) -> None:
        self.exitcode = exitcode
        self.join_calls = 0

    def join(self, timeout: float) -> None:
        del timeout
        self.join_calls += 1

    def is_alive(self) -> bool:
        return False


class _BlockingProcess:
    def __init__(self, *, hold_after_terminate: bool) -> None:
        self.exitcode: int | None = None
        self.join_started = threading.Event()
        self.terminate_called = threading.Event()
        self.kill_called = threading.Event()
        self.release = threading.Event()
        self.join_calls = 0
        self._alive = True
        self._hold_after_terminate = hold_after_terminate

    def join(self, timeout: float) -> None:
        self.join_calls += 1
        self.join_started.set()
        released = self.release.wait(timeout)
        if released and (self.terminate_called.is_set() or self.kill_called.is_set()):
            self._alive = False
            self.exitcode = -9 if self.kill_called.is_set() else -15

    def is_alive(self) -> bool:
        return self._alive

    def terminate(self) -> None:
        self.terminate_called.set()
        if not self._hold_after_terminate:
            self.release.set()

    def kill(self) -> None:
        self.kill_called.set()
        self.release.set()


@pytest.fixture
def api_client(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[TestClient, None, None]:
    def override_get_session() -> Generator[Session, None, None]:
        yield db_session

    app.dependency_overrides[get_session] = override_get_session
    monkeypatch.setattr("app.main.init_db", lambda: None, raising=True)
    monkeypatch.setattr("app.main.bootstrap_db", lambda: None, raising=True)
    monkeypatch.setattr(
        "app.main.schedule_jobs_on_startup",
        lambda scheduler, db: None,
        raising=True,
    )
    monkeypatch.setattr("app.main.get_scheduler", lambda: _DummyScheduler(), raising=True)

    @asynccontextmanager
    async def route_only_lifespan(_app):  # type: ignore[no-untyped-def]
        yield

    monkeypatch.setattr(app.router, "lifespan_context", route_only_lifespan)
    with TestClient(app, backend_options={"use_uvloop": True}) as client:
        yield client
    app.dependency_overrides.clear()


def _install_transport(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    transport = httpx.MockTransport(handler)
    original_client = httpx.AsyncClient

    def client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return original_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)


def _native_database_bytes(
    tmp_path: Path,
    *,
    plugin_key: str,
    migration: int,
    missing_table: str | None = None,
    foreign_key_violation: bool = False,
) -> bytes:
    database_path = tmp_path / f"{plugin_key}-native.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute('CREATE TABLE "VersionInfo" ("Version" INTEGER NOT NULL)')
        connection.execute(
            'INSERT INTO "VersionInfo" ("Version") VALUES (?)',
            (migration,),
        )
        selected_tables = set(REQUIRED_NATIVE_TABLES[plugin_key]) - {missing_table}
        for table_name in sorted(selected_tables - {"Config", "Tags"}):
            connection.execute(f'CREATE TABLE "{table_name}" ("Id" INTEGER PRIMARY KEY)')
        if "Tags" in selected_tables:
            connection.execute('CREATE TABLE "Tags" ("Id" INTEGER PRIMARY KEY, "Marker" TEXT)')
            connection.execute(
                'INSERT INTO "Tags" ("Id", "Marker") VALUES (1, ?)',
                (f"{plugin_key}-marker",),
            )
        if "Config" in selected_tables:
            connection.execute(
                'CREATE TABLE "Config" '
                '("Id" INTEGER PRIMARY KEY, "TagId" INTEGER REFERENCES "Tags"("Id"))'
            )
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute(
                'INSERT INTO "Config" ("Id", "TagId") VALUES (1, ?)',
                (999 if foreign_key_violation else 1,),
            )
        assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)

    database_bytes = database_path.read_bytes()
    database_path.unlink()
    return database_bytes


def _regular_member(name: str, *, compression: int = zipfile.ZIP_DEFLATED) -> zipfile.ZipInfo:
    member = zipfile.ZipInfo(name)
    member.create_system = 3
    member.external_attr = (stat.S_IFREG | 0o600) << 16
    member.compress_type = compression
    return member


def _archive_bytes(
    members: list[tuple[zipfile.ZipInfo | str, bytes]],
    *,
    compression: int = zipfile.ZIP_DEFLATED,
) -> bytes:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w", compression=compression) as archive:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            for member, member_payload in members:
                if isinstance(member, str):
                    member = _regular_member(member, compression=compression)
                archive.writestr(member, member_payload)
    return payload.getvalue()


def _native_backup_zip(
    tmp_path: Path,
    *,
    plugin_key: str,
    version: str,
    migration: int,
) -> bytes:
    return _archive_bytes(
        [
            (
                "config.xml",
                b"<Config><ApiKey>restored-synthetic-key</ApiKey></Config>",
            ),
            ("INFO", f"v{version}\n2026-08-16 12:00:00\n".encode()),
            (
                f"{plugin_key}.db",
                _native_database_bytes(
                    tmp_path,
                    plugin_key=plugin_key,
                    migration=migration,
                ),
            ),
        ]
    )


def _streamed_file_bytes(path: Path) -> bytes:
    chunks: list[bytes] = []
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(64 * 1024), b""):
            chunks.append(chunk)
    return b"".join(chunks)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _restore_config(plugin_key: str) -> dict[str, object]:
    return {
        "base_url": RESTORE_ORIGINS[plugin_key],
        "api_key": "destination-key",
        "backup_directory": BACKUP_DIRECTORIES[plugin_key],
    }


def _restore_context(
    plugin_key: str,
    artifact: Path,
    *,
    source_target_id: str = "source-target",
    destination_target_id: str = "isolated-destination-target",
    config: dict[str, object] | None = None,
    metadata: dict[str, object] | None = None,
) -> RestoreContext:
    return RestoreContext(
        job_id=f"{plugin_key}-restore-drill",
        source_target_id=source_target_id,
        destination_target_id=destination_target_id,
        config=config or _restore_config(plugin_key),
        artifact_path=str(artifact),
        metadata=metadata
        or {
            "artifact_bytes": artifact.stat().st_size,
            "artifact_sha256": _sha256_file(artifact),
        },
    )


def _authorize_isolated_restore(
    monkeypatch: pytest.MonkeyPatch,
    plugin_key: str,
) -> None:
    monkeypatch.setenv("HOMELAB_BACKUP_ALLOW_ISOLATED_RESTORE", "1")
    monkeypatch.setenv(
        "HOMELAB_BACKUP_ISOLATED_RESTORE_ALLOWED_ORIGINS",
        RESTORE_ORIGINS[plugin_key],
    )


def _patch_first_zip_header(
    payload: bytes,
    *,
    local_offset: int,
    central_offset: int,
    value: int,
) -> bytes:
    mutated = bytearray(payload)
    local = mutated.index(b"PK\x03\x04")
    central = mutated.index(b"PK\x01\x02")
    mutated[local + local_offset : local + local_offset + 2] = value.to_bytes(2, "little")
    mutated[central + central_offset : central + central_offset + 2] = value.to_bytes(2, "little")
    return bytes(mutated)


def _malformed_native_zip(
    tmp_path: Path,
    *,
    plugin_key: str,
    version: str,
    migration: int,
    case: str,
) -> bytes:
    config = b"<Config><ApiKey>restored-synthetic-key</ApiKey></Config>"
    info = f"v{version}\n2026-08-16 12:00:00\n".encode()
    database = _native_database_bytes(
        tmp_path,
        plugin_key=plugin_key,
        migration=(migration + 1 if case == "wrong-migration" else migration),
        missing_table=(REQUIRED_NATIVE_TABLES[plugin_key][-1] if case == "missing-table" else None),
        foreign_key_violation=case == "foreign-key",
    )
    if case == "malformed-xml":
        config = b"<Config>"
    elif case == "wrong-config-root":
        config = b"<NotConfig><ApiKey>key</ApiKey></NotConfig>"
    elif case == "missing-api-key":
        config = b"<Config />"
    elif case == "empty-api-key":
        config = b"<Config><ApiKey> </ApiKey></Config>"
    elif case == "duplicate-api-key":
        config = b"<Config><ApiKey>one</ApiKey><ApiKey>two</ApiKey></Config>"
    elif case == "wrong-info-version":
        info = b"v0.0.0\n2026-08-16 12:00:00\n"
    elif case == "bad-info-timestamp":
        info = f"v{version}\nnot-a-timestamp\n".encode()
    elif case == "corrupt-sqlite":
        database = b"not a SQLite database"
    elif case == "sqlite-quick-check":
        database = database[: len(database) // 2]

    members: list[tuple[zipfile.ZipInfo | str, bytes]] = [
        ("config.xml", config),
        ("INFO", info),
        (f"{plugin_key}.db", database),
    ]
    if case == "missing-member":
        members = [members[0], members[2]]
    elif case == "config-only":
        members = members[:2]
    elif case == "duplicate-member":
        members.insert(1, ("config.xml", config))
    elif case == "extra-member":
        members.append(("unexpected.txt", b"unexpected"))
    elif case == "nested-member":
        members[0] = ("nested/config.xml", config)
    elif case == "traversal-member":
        members[0] = ("../config.xml", config)
    elif case == "absolute-member":
        members[0] = ("/config.xml", config)
    elif case == "case-mismatched-member":
        members[0] = ("Config.xml", config)
    elif case in {"link-member", "device-member"}:
        file_type = stat.S_IFLNK if case == "link-member" else stat.S_IFCHR
        unsafe = _regular_member("config.xml")
        unsafe.external_attr = (file_type | 0o600) << 16
        members[0] = (unsafe, config)
    elif case in {"hot-wal", "hot-shm", "hot-journal"}:
        suffix = {
            "hot-wal": "-wal",
            "hot-shm": "-shm",
            "hot-journal": "-journal",
        }[case]
        members.append((f"{plugin_key}.db{suffix}", b"hot database residue"))

    stored = case == "crc"
    archive = _archive_bytes(
        members,
        compression=zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED,
    )
    if case == "encrypted-member":
        local = archive.index(b"PK\x03\x04")
        flags = int.from_bytes(archive[local + 6 : local + 8], "little") | 0x1
        archive = _patch_first_zip_header(
            archive,
            local_offset=6,
            central_offset=8,
            value=flags,
        )
    elif case == "unsupported-compression":
        archive = _patch_first_zip_header(
            archive,
            local_offset=8,
            central_offset=10,
            value=99,
        )
    elif case == "crc":
        mutated = bytearray(archive)
        config_offset = mutated.index(config)
        mutated[config_offset] ^= 0x01
        archive = bytes(mutated)
    elif case == "trailing-data":
        archive += b"untrusted trailing bytes"
    return archive


def _source_config(
    plugin_key: str,
    default_url: str,
    backup_directory: str | Path | None = None,
) -> dict[str, object]:
    return {
        "base_url": default_url,
        "api_key": "synthetic-key",
        "backup_directory": str(backup_directory or BACKUP_DIRECTORIES[plugin_key]),
    }


def _backup_context(
    plugin_key: str,
    default_url: str,
    backup_directory: str | Path | None = None,
) -> BackupContext:
    return BackupContext(
        job_id="job-014",
        target_id=f"target-{plugin_key}",
        config=_source_config(plugin_key, default_url, backup_directory),
        metadata={"target_slug": f"{plugin_key}-drill"},
    )


def _install_read_only_mount(
    monkeypatch: pytest.MonkeyPatch,
    backup_directory: Path,
    *,
    plugin_key: str,
    is_mount: bool = True,
    read_only: bool = True,
) -> list[Path]:
    observed: list[Path] = []

    def fake_is_mount(path: str | os.PathLike[str]) -> bool:
        observed.append(Path(path))
        return is_mount and Path(path) == backup_directory

    def fake_statvfs(path: str | os.PathLike[str]) -> SimpleNamespace:
        observed.append(Path(path))
        assert Path(path) == backup_directory
        return SimpleNamespace(f_flag=os.ST_RDONLY if read_only else 0)

    monkeypatch.setattr(os.path, "ismount", fake_is_mount)
    monkeypatch.setattr(os, "statvfs", fake_statvfs)
    monkeypatch.setattr(
        type(get_plugin(plugin_key)),
        "native_backup_mount",
        backup_directory,
    )
    return observed


def _mounted_source_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    plugin_key: str,
    default_url: str,
) -> tuple[dict[str, object], Path]:
    backup_directory = tmp_path / f"{plugin_key}-native-backups"
    backup_directory.mkdir()
    _install_read_only_mount(
        monkeypatch,
        backup_directory,
        plugin_key=plugin_key,
    )
    return _source_config(plugin_key, default_url, backup_directory), backup_directory


def _status_payload(app_name: str, version: str, migration: int) -> dict[str, object]:
    return {
        "appName": app_name,
        "version": version,
        "databaseType": "sqlite",
        "migrationVersion": migration,
    }


def _manual_backup(
    plugin_key: str,
    version: str,
    *,
    backup_id: int,
    filename: str | None = None,
    path: str | None = None,
    observed_at: datetime | None = None,
    size: int = 4096,
) -> dict[str, object]:
    resolved_filename = filename or f"{plugin_key}_backup_v{version}_2026.08.16_12.00.00.zip"
    return {
        "id": backup_id,
        "name": resolved_filename,
        "type": "manual",
        "path": path or f"/backup/manual/{resolved_filename}",
        "size": size,
        "time": (observed_at or datetime.now(timezone.utc)).isoformat(),
    }


def _install_single_archive_backup(
    monkeypatch: pytest.MonkeyPatch,
    *,
    backup_directory: Path,
    archive_bytes: bytes,
    plugin_key: str,
    app_name: str,
    version: str,
    migration: int,
) -> list[httpx.Request]:
    filename = f"{plugin_key}_backup_v{version}_2026.08.16_12.34.56.zip"
    candidate = _manual_backup(
        plugin_key,
        version,
        backup_id=301,
        filename=filename,
        observed_at=datetime.now(timezone.utc) + timedelta(seconds=1),
        size=len(archive_bytes),
    )
    requests: list[httpx.Request] = []
    list_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal list_calls
        requests.append(request)
        if request.method == "GET" and request.url.path == "/api/v1/system/status":
            return httpx.Response(
                200,
                json=_status_payload(app_name, version, migration),
            )
        if request.method == "GET" and request.url.path == "/api/v1/system/backup":
            list_calls += 1
            return httpx.Response(200, json=[] if list_calls == 1 else [candidate])
        if request.method == "POST" and request.url.path == "/api/v1/command":
            (backup_directory / filename).write_bytes(archive_bytes)
            return httpx.Response(201, json={"id": 302})
        if request.method == "GET" and request.url.path == "/api/v1/command/302":
            command: dict[str, object] = {"id": 302, "status": "completed"}
            if plugin_key == "readarr":
                command["result"] = "successful"
            return httpx.Response(200, json=command)
        raise AssertionError(f"Unexpected network request: {request.method} {request.url}")

    _install_transport(monkeypatch, handler)
    return requests


@pytest.mark.parametrize(
    ("plugin_key", "app_name", "version", "migration", "default_url"),
    SERVICE_CONTRACTS,
)
def test_plugin_is_discoverable_with_flat_schema_and_automatic_restore(
    api_client: TestClient,
    plugin_key: str,
    app_name: str,
    version: str,
    migration: int,
    default_url: str,
) -> None:
    del app_name, version, migration

    loader_entry = next(item for item in list_plugins() if item["key"] == plugin_key)
    assert loader_entry == {
        "key": plugin_key,
        "name": plugin_key,
        "version": "0.2.1",
        "restore_capability": "automatic",
    }
    assert get_plugin(plugin_key).restore_capability == "automatic"

    discovery_response = api_client.get("/api/v1/plugins/")
    assert discovery_response.status_code == 200
    api_entry = next(item for item in discovery_response.json() if item["key"] == plugin_key)
    assert api_entry == loader_entry

    schema_response = api_client.get(f"/api/v1/plugins/{plugin_key}/schema")
    assert schema_response.status_code == 200
    schema = schema_response.json()
    assert schema == {
        "type": "object",
        "additionalProperties": False,
        "required": ["base_url", "api_key", "backup_directory"],
        "properties": {
            "base_url": {
                "type": "string",
                "title": "Base URL",
                "default": default_url,
                "minLength": 1,
                "pattern": r"^https?://[^/?#]+$",
            },
            "api_key": {
                "type": "string",
                "title": "API Key",
                "minLength": 1,
            },
            "backup_directory": {
                "type": "string",
                "title": "Backup Directory",
                "default": BACKUP_DIRECTORIES[plugin_key],
                "const": BACKUP_DIRECTORIES[plugin_key],
                "minLength": 1,
                "pattern": "^/.*",
            },
        },
    }
    assert "mode" not in schema["properties"]
    assert "default" not in schema["properties"]["api_key"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("plugin_key", "app_name", "version", "migration", "default_url"),
    SERVICE_CONTRACTS,
)
async def test_loader_plugin_accepts_only_the_exact_flat_source_config(
    plugin_key: str,
    app_name: str,
    version: str,
    migration: int,
    default_url: str,
) -> None:
    del app_name, version, migration
    plugin = get_plugin(plugin_key)

    source_config = _source_config(plugin_key, default_url)
    assert await plugin.validate_config(source_config)

    invalid_configs: tuple[object, ...] = (
        {},
        {"base_url": default_url},
        {"api_key": "synthetic-key"},
        {"base_url": default_url, "api_key": "synthetic-key"},
        {"base_url": "", "api_key": "synthetic-key"},
        {"base_url": default_url, "api_key": ""},
        {"base_url": 123, "api_key": "synthetic-key"},
        {"base_url": default_url, "api_key": 123},
        {
            "base_url": default_url,
            "api_key": "synthetic-key",
            "backup_directory": BACKUP_DIRECTORIES[plugin_key],
            "mode": "source",
        },
        {
            "base_url": default_url,
            "api_key": "synthetic-key",
            "backup_directory": BACKUP_DIRECTORIES[plugin_key],
            "legacy_url": "http://legacy.invalid",
        },
        {"base_url": "readarr.local:8787", "api_key": "synthetic-key"},
        {"base_url": "ftp://service.local", "api_key": "synthetic-key"},
        {
            "base_url": "http://user:password@service.local",
            "api_key": "synthetic-key",
            "backup_directory": BACKUP_DIRECTORIES[plugin_key],
        },
        {
            "base_url": "http://service.local/path",
            "api_key": "synthetic-key",
            "backup_directory": BACKUP_DIRECTORIES[plugin_key],
        },
        {
            "base_url": "http://service.local?token=value",
            "api_key": "synthetic-key",
            "backup_directory": BACKUP_DIRECTORIES[plugin_key],
        },
        {
            "base_url": "http://service.local#fragment",
            "api_key": "synthetic-key",
            "backup_directory": BACKUP_DIRECTORIES[plugin_key],
        },
        {**source_config, "backup_directory": "relative/backups"},
        {**source_config, "backup_directory": "/sources/readarr/../backups"},
        {**source_config, "backup_directory": "/config/private"},
        {**source_config, "backup_directory": "/backups/readarr"},
        {**source_config, "backup_directory": "/sources"},
        {**source_config, "backup_directory": f"/sources/{plugin_key}"},
        {**source_config, "backup_directory": "/mnt"},
        None,
        [],
    )
    for config in invalid_configs:
        assert await plugin.validate_config(config) is False  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("plugin_key", "app_name", "version", "migration", "default_url"),
    SERVICE_CONTRACTS,
)
def test_target_api_persists_only_exact_flat_plugin_config(
    api_client: TestClient,
    plugin_key: str,
    app_name: str,
    version: str,
    migration: int,
    default_url: str,
) -> None:
    del app_name, version, migration
    config = _source_config(plugin_key, default_url)
    serialized = json.dumps(config, sort_keys=True)
    response = api_client.post(
        "/api/v1/targets/",
        json={
            "name": f"{plugin_key} exact target",
            "plugin_name": plugin_key,
            "plugin_config_json": serialized,
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["plugin_name"] == plugin_key
    assert response.json()["plugin_config_json"] == serialized

    invalid_configs: tuple[dict[str, object], ...] = (
        {},
        {"base_url": default_url},
        {"api_key": "synthetic-key"},
        {"base_url": default_url, "api_key": "synthetic-key"},
        {"base_url": "", "api_key": "synthetic-key"},
        {"base_url": default_url, "api_key": ""},
        {**config, "mode": "source"},
        {"base_url": 123, "api_key": "synthetic-key"},
        {"base_url": default_url, "api_key": 123},
        {**config, "backup_directory": "relative/backups"},
    )
    for index, invalid_config in enumerate(invalid_configs):
        invalid_response = api_client.post(
            "/api/v1/targets/",
            json={
                "name": f"{plugin_key} invalid target {index}",
                "plugin_name": plugin_key,
                "plugin_config_json": json.dumps(invalid_config),
            },
        )
        assert invalid_response.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("plugin_key", "app_name", "version", "migration", "default_url"),
    SERVICE_CONTRACTS,
)
async def test_non_destructive_probe_requires_exact_status_and_lists_backups(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    plugin_key: str,
    app_name: str,
    version: str,
    migration: int,
    default_url: str,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "GET"
        assert request.headers["X-Api-Key"] == "synthetic-key"
        assert request.url.params.get("apikey") is None
        if request.url.path == "/api/v1/system/status":
            return httpx.Response(
                200,
                json={
                    "appName": app_name,
                    "version": version,
                    "databaseType": "sqlite",
                    "migrationVersion": migration,
                },
            )
        if request.url.path == "/api/v1/system/backup":
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    _install_transport(monkeypatch, handler)
    config, _ = _mounted_source_config(monkeypatch, tmp_path, plugin_key, default_url)

    assert await get_plugin(plugin_key).test(config)
    assert [request.url.path for request in requests] == [
        "/api/v1/system/status",
        "/api/v1/system/backup",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("plugin_key", "app_name", "version", "migration", "default_url"),
    SERVICE_CONTRACTS,
)
@pytest.mark.parametrize(
    ("status_override", "error_fragment"),
    (
        ({"appName": "WrongApp"}, "application"),
        ({"version": "0.0.0"}, "version"),
        ({"databaseType": "postgresql"}, "sqlite"),
        ({"migrationVersion": -1}, "migration"),
    ),
)
async def test_non_destructive_probe_rejects_status_contract_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    plugin_key: str,
    app_name: str,
    version: str,
    migration: int,
    default_url: str,
    status_override: dict[str, object],
    error_fragment: str,
) -> None:
    status: dict[str, object] = {
        "appName": app_name,
        "version": version,
        "databaseType": "sqlite",
        "migrationVersion": migration,
    }
    status.update(status_override)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/system/status":
            return httpx.Response(200, json=status)
        return httpx.Response(200, json=[])

    _install_transport(monkeypatch, handler)
    config, _ = _mounted_source_config(monkeypatch, tmp_path, plugin_key, default_url)

    with pytest.raises(RuntimeError) as raised:
        await get_plugin(plugin_key).test(config)
    assert error_fragment in str(raised.value).lower()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("plugin_key", "app_name", "version", "migration", "default_url"),
    SERVICE_CONTRACTS,
)
@pytest.mark.parametrize("status_code", (401, 403))
async def test_non_destructive_probe_reports_auth_failure_without_leaking_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    plugin_key: str,
    app_name: str,
    version: str,
    migration: int,
    default_url: str,
    status_code: int,
) -> None:
    del app_name, version, migration

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, text="synthetic-key must not escape")

    _install_transport(monkeypatch, handler)
    config, _ = _mounted_source_config(monkeypatch, tmp_path, plugin_key, default_url)

    with pytest.raises(RuntimeError) as raised:
        await get_plugin(plugin_key).test(config)
    assert str(status_code) in str(raised.value)
    assert "synthetic-key" not in str(raised.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("plugin_key", "app_name", "version", "migration", "default_url"),
    SERVICE_CONTRACTS,
)
async def test_get_status_reports_only_fresh_secret_safe_probe_health(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    plugin_key: str,
    app_name: str,
    version: str,
    migration: int,
    default_url: str,
) -> None:
    healthy = True
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if not healthy:
            return httpx.Response(401, text="synthetic-key must remain secret")
        if request.url.path == "/api/v1/system/status":
            return httpx.Response(200, json=_status_payload(app_name, version, migration))
        if request.url.path == "/api/v1/system/backup":
            return httpx.Response(200, json=[])
        raise AssertionError(f"Unexpected status request: {request.method} {request.url}")

    _install_transport(monkeypatch, handler)
    config, _ = _mounted_source_config(monkeypatch, tmp_path, plugin_key, default_url)
    context = BackupContext(
        job_id=f"{plugin_key}-status",
        target_id=f"{plugin_key}-source",
        config=config,
    )
    plugin = get_plugin(plugin_key)

    assert await plugin.get_status(context) == {"status": "ok"}
    assert [request.url.path for request in requests] == [
        "/api/v1/system/status",
        "/api/v1/system/backup",
    ]

    healthy = False
    failed = await plugin.get_status(context)
    assert failed["status"] == "error"
    assert "status 401" in failed["error"]
    assert "synthetic-key" not in json.dumps(failed)
    assert [request.url.path for request in requests] == [
        "/api/v1/system/status",
        "/api/v1/system/backup",
        "/api/v1/system/status",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("plugin_key", "app_name", "version", "migration", "default_url"),
    SERVICE_CONTRACTS,
)
async def test_non_destructive_probe_does_not_follow_origin_redirect(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    plugin_key: str,
    app_name: str,
    version: str,
    migration: int,
    default_url: str,
) -> None:
    del app_name, version, migration
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            302,
            headers={"location": "https://attacker.invalid/collect"},
        )

    _install_transport(monkeypatch, handler)
    config, _ = _mounted_source_config(monkeypatch, tmp_path, plugin_key, default_url)

    with pytest.raises(RuntimeError, match="302"):
        await get_plugin(plugin_key).test(config)
    assert [request.url.host for request in requests] == [
        default_url.split("//", 1)[1].split(":", 1)[0]
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("plugin_key", "app_name", "version", "migration", "default_url"),
    SERVICE_CONTRACTS,
)
@pytest.mark.parametrize("malformed_endpoint", ("status", "backup"))
async def test_non_destructive_probe_rejects_malformed_vendor_json(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    plugin_key: str,
    app_name: str,
    version: str,
    migration: int,
    default_url: str,
    malformed_endpoint: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/system/status":
            if malformed_endpoint == "status":
                return httpx.Response(200, content=b"{")
            return httpx.Response(
                200,
                json={
                    "appName": app_name,
                    "version": version,
                    "databaseType": "sqlite",
                    "migrationVersion": migration,
                },
            )
        if malformed_endpoint == "backup":
            return httpx.Response(200, json={"records": []})
        return httpx.Response(200, json=[])

    _install_transport(monkeypatch, handler)
    config, _ = _mounted_source_config(monkeypatch, tmp_path, plugin_key, default_url)

    with pytest.raises(RuntimeError, match="response"):
        await get_plugin(plugin_key).test(config)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("plugin_key", "app_name", "version", "migration", "default_url"),
    SERVICE_CONTRACTS,
)
@pytest.mark.parametrize("invalid_member", (42, {}))
async def test_non_destructive_probe_rejects_malformed_backup_list_members(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    plugin_key: str,
    app_name: str,
    version: str,
    migration: int,
    default_url: str,
    invalid_member: object,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/system/status":
            return httpx.Response(
                200,
                json=_status_payload(app_name, version, migration),
            )
        return httpx.Response(200, json=[invalid_member])

    _install_transport(monkeypatch, handler)
    config, _ = _mounted_source_config(monkeypatch, tmp_path, plugin_key, default_url)

    with pytest.raises(RuntimeError, match="backup list response"):
        await get_plugin(plugin_key).test(config)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("plugin_key", "app_name", "version", "migration", "default_url"),
    SERVICE_CONTRACTS,
)
async def test_non_destructive_probe_rejects_malformed_config_before_io(
    monkeypatch: pytest.MonkeyPatch,
    plugin_key: str,
    app_name: str,
    version: str,
    migration: int,
    default_url: str,
) -> None:
    del app_name, version, migration
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(500)

    _install_transport(monkeypatch, handler)

    with pytest.raises(ValueError, match="Invalid configuration"):
        await get_plugin(plugin_key).test(
            {
                "base_url": default_url,
                "api_key": "synthetic-key",
                "backup_directory": BACKUP_DIRECTORIES[plugin_key],
                "mode": "source",
            }
        )
    assert requests == []


def test_same_second_manual_backup_attribution_uses_baseline_identity() -> None:
    observed_at = datetime(2026, 8, 16, 12, 34, 56, tzinfo=timezone.utc)
    baseline = _manual_backup(
        "readarr",
        READARR_CONTRACT[2],
        backup_id=10,
        filename="readarr-baseline.zip",
        observed_at=observed_at,
    )
    fresh = _manual_backup(
        "readarr",
        READARR_CONTRACT[2],
        backup_id=11,
        filename="readarr-fresh.zip",
        observed_at=observed_at,
    )
    plugin = cast(Any, get_plugin("readarr"))
    known = {plugin._backup_identity(baseline)}

    candidates = plugin._new_manual_backups(
        [baseline, fresh],
        known,
        observed_at,
    )

    assert candidates == [fresh]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("plugin_key", "app_name", "version", "migration", "default_url"),
    SERVICE_CONTRACTS,
)
async def test_backup_rechecks_exact_status_and_publishes_attributed_native_archive(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    plugin_key: str,
    app_name: str,
    version: str,
    migration: int,
    default_url: str,
) -> None:
    archive_bytes = _native_backup_zip(
        tmp_path,
        plugin_key=plugin_key,
        version=version,
        migration=migration,
    )
    backup_directory = tmp_path / f"{plugin_key}-native-backups"
    backup_directory.mkdir()
    _install_read_only_mount(
        monkeypatch,
        backup_directory,
        plugin_key=plugin_key,
    )
    baseline = _manual_backup(
        plugin_key,
        version,
        backup_id=10,
        filename=f"{plugin_key}_backup_v{version}_2026.08.16_11.59.59.zip",
        size=len(archive_bytes),
    )
    created = _manual_backup(
        plugin_key,
        version,
        backup_id=11,
        filename=f"{plugin_key}_backup_v{version}_2026.08.16_12.00.01.zip",
        observed_at=datetime.now(timezone.utc) + timedelta(seconds=1),
        size=len(archive_bytes),
    )
    baseline_path = backup_directory / str(baseline["name"])
    created_path = backup_directory / str(created["name"])
    baseline_path.write_bytes(archive_bytes)
    output_root = tmp_path / "published"
    list_calls = 0
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal list_calls
        requests.append(request)
        assert request.headers["X-Api-Key"] == "synthetic-key"
        assert request.url.params.get("apikey") is None
        if request.method == "GET" and request.url.path == "/api/v1/system/status":
            return httpx.Response(
                200,
                json=_status_payload(app_name, version, migration),
            )
        if request.method == "GET" and request.url.path == "/api/v1/system/backup":
            list_calls += 1
            return httpx.Response(
                200,
                json=[baseline] if list_calls == 1 else [created, baseline],
            )
        if request.method == "POST" and request.url.path == "/api/v1/command":
            assert json.loads(request.content) == {"name": "Backup"}
            created_path.write_bytes(archive_bytes)
            return httpx.Response(201, json={"id": 81, "status": "queued"})
        if request.method == "GET" and request.url.path == "/api/v1/command/81":
            command: dict[str, object] = {"id": 81, "status": "completed"}
            if plugin_key == "readarr":
                command["result"] = "successful"
            return httpx.Response(200, json=command)
        if request.method == "DELETE" and request.url.path == "/api/v1/system/backup/11":
            published = [path for path in output_root.rglob("*.zip") if path.is_file()]
            sidecars = [path for path in output_root.rglob("*.meta.json") if path.is_file()]
            assert len(published) == 1
            assert sidecars == [Path(f"{published[0]}.meta.json")]
            assert created_path.is_file()
            assert baseline_path.is_file()
            created_path.unlink()
            return httpx.Response(200)
        return httpx.Response(404)

    _install_transport(monkeypatch, handler)
    plugin = get_plugin(plugin_key)
    setattr(plugin, "backup_root", str(output_root))

    def forbid_whole_file_reads(_path: Path) -> bytes:
        raise AssertionError("backup artifacts must be streamed, not read as one byte string")

    monkeypatch.setattr(Path, "read_bytes", forbid_whole_file_reads)

    result = await plugin.backup(_backup_context(plugin_key, default_url, backup_directory))

    artifact = Path(result["artifact_path"])
    sidecar = Path(f"{artifact}.meta.json")
    assert _streamed_file_bytes(artifact) == archive_bytes
    assert sidecar.is_file()
    assert stat.S_IMODE(artifact.stat().st_mode) == 0o600
    assert stat.S_IMODE(sidecar.stat().st_mode) == 0o600
    sidecar_data = json.loads(sidecar.read_text(encoding="utf-8"))
    assert sidecar_data["plugin_name"] == plugin_key
    assert sidecar_data["target_slug"] == f"{plugin_key}-drill"
    assert sidecar_data["artifact_bytes"] == len(archive_bytes)
    assert sidecar_data["sha256"] == hashlib.sha256(archive_bytes).hexdigest()
    serialized_sidecar = json.dumps(sidecar_data)
    assert "synthetic-key" not in serialized_sidecar
    assert default_url not in serialized_sidecar
    assert str(backup_directory) not in serialized_sidecar
    assert [
        (request.method, request.url.path)
        for request in requests
        if request.url.path.startswith("/api/v1")
    ] == [
        ("GET", "/api/v1/system/status"),
        ("GET", "/api/v1/system/backup"),
        ("POST", "/api/v1/command"),
        ("GET", "/api/v1/command/81"),
        ("GET", "/api/v1/system/backup"),
        ("DELETE", "/api/v1/system/backup/11"),
    ]
    assert (
        sum(
            request.method == "POST" and request.url.path == "/api/v1/command"
            for request in requests
        )
        == 1
    )
    assert baseline_path.is_file()
    assert not created_path.exists()
    assert all(request.url.path.startswith("/api/v1/") for request in requests)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("plugin_key", "app_name", "version", "migration", "default_url"),
    SERVICE_CONTRACTS,
)
async def test_backup_rejects_terminal_command_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    plugin_key: str,
    app_name: str,
    version: str,
    migration: int,
    default_url: str,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/v1/system/status":
            return httpx.Response(
                200,
                json=_status_payload(app_name, version, migration),
            )
        if request.url.path == "/api/v1/system/backup":
            return httpx.Response(200, json=[])
        if request.method == "POST" and request.url.path == "/api/v1/command":
            return httpx.Response(201, json={"id": 82})
        if request.url.path == "/api/v1/command/82":
            return httpx.Response(200, json={"id": 82, "status": "failed"})
        return httpx.Response(404)

    _install_transport(monkeypatch, handler)
    _, backup_directory = _mounted_source_config(monkeypatch, tmp_path, plugin_key, default_url)

    with pytest.raises(RuntimeError, match="failed"):
        await get_plugin(plugin_key).backup(
            _backup_context(plugin_key, default_url, backup_directory)
        )
    assert (requests[0].method, requests[0].url.path) == (
        "GET",
        "/api/v1/system/status",
    )
    assert not any(request.method == "DELETE" for request in requests)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("plugin_key", "app_name", "version", "migration", "default_url"),
    SERVICE_CONTRACTS,
)
async def test_backup_rejects_boolean_command_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    plugin_key: str,
    app_name: str,
    version: str,
    migration: int,
    default_url: str,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/v1/system/status":
            return httpx.Response(200, json=_status_payload(app_name, version, migration))
        if request.url.path == "/api/v1/system/backup":
            return httpx.Response(200, json=[])
        if request.method == "POST" and request.url.path == "/api/v1/command":
            return httpx.Response(201, json={"id": True})
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    _install_transport(monkeypatch, handler)
    _, backup_directory = _mounted_source_config(
        monkeypatch,
        tmp_path,
        plugin_key,
        default_url,
    )

    with pytest.raises(RuntimeError, match="command id"):
        await get_plugin(plugin_key).backup(
            _backup_context(plugin_key, default_url, backup_directory)
        )
    assert not any("/command/True" in request.url.path for request in requests)


@pytest.mark.asyncio
async def test_readarr_backup_rejects_completed_command_without_successful_result(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/v1/system/status":
            return httpx.Response(
                200,
                json=_status_payload("Readarr", "0.4.18.2805", 158),
            )
        if request.url.path == "/api/v1/system/backup":
            return httpx.Response(200, json=[])
        if request.method == "POST" and request.url.path == "/api/v1/command":
            return httpx.Response(201, json={"id": 83})
        if request.url.path == "/api/v1/command/83":
            return httpx.Response(
                200,
                json={"id": 83, "status": "completed", "result": "unsuccessful"},
            )
        return httpx.Response(404)

    _install_transport(monkeypatch, handler)
    _, backup_directory = _mounted_source_config(
        monkeypatch,
        tmp_path,
        "readarr",
        "http://readarr.local:8787",
    )

    with pytest.raises(RuntimeError, match="unsuccessfully"):
        await get_plugin("readarr").backup(
            _backup_context(
                "readarr",
                "http://readarr.local:8787",
                backup_directory,
            )
        )
    assert requests[0].url.path == "/api/v1/system/status"
    assert not any(request.method == "DELETE" for request in requests)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("plugin_key", "app_name", "version", "migration", "default_url"),
    SERVICE_CONTRACTS,
)
@pytest.mark.parametrize("selection_case", ("ambiguous", "missing", "stale"))
async def test_backup_rejects_unattributable_manual_archive(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    plugin_key: str,
    app_name: str,
    version: str,
    migration: int,
    default_url: str,
    selection_case: str,
) -> None:
    archive_bytes = _native_backup_zip(
        tmp_path,
        plugin_key=plugin_key,
        version=version,
        migration=migration,
    )
    backup_directory = tmp_path / f"{plugin_key}-native-backups"
    backup_directory.mkdir()
    _install_read_only_mount(
        monkeypatch,
        backup_directory,
        plugin_key=plugin_key,
    )
    now = datetime.now(timezone.utc)
    fresh = _manual_backup(
        plugin_key,
        version,
        backup_id=91,
        filename=f"{plugin_key}_backup_v{version}_2026.08.16_12.00.01.zip",
        observed_at=now + timedelta(seconds=1),
        size=len(archive_bytes),
    )
    second = _manual_backup(
        plugin_key,
        version,
        backup_id=92,
        filename=f"{plugin_key}_backup_v{version}_2026.08.16_12.00.02.zip",
        observed_at=now + timedelta(seconds=1),
        size=len(archive_bytes),
    )
    stale = _manual_backup(
        plugin_key,
        version,
        backup_id=93,
        filename=f"{plugin_key}_backup_v{version}_2026.08.15_12.00.00.zip",
        observed_at=now - timedelta(days=1),
        size=len(archive_bytes),
    )
    post_trigger_entries = {
        "ambiguous": [fresh, second],
        "missing": [],
        "stale": [stale],
    }[selection_case]
    list_calls = 0
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal list_calls
        requests.append(request)
        if request.url.path == "/api/v1/system/status":
            return httpx.Response(
                200,
                json=_status_payload(app_name, version, migration),
            )
        if request.url.path == "/api/v1/system/backup":
            list_calls += 1
            return httpx.Response(200, json=[] if list_calls == 1 else post_trigger_entries)
        if request.method == "POST" and request.url.path == "/api/v1/command":
            for entry in post_trigger_entries:
                (backup_directory / str(entry["name"])).write_bytes(archive_bytes)
            return httpx.Response(201, json={"id": 84})
        if request.url.path == "/api/v1/command/84":
            command: dict[str, object] = {"id": 84, "status": "completed"}
            if plugin_key == "readarr":
                command["result"] = "successful"
            return httpx.Response(200, json=command)
        return httpx.Response(404)

    _install_transport(monkeypatch, handler)
    plugin = get_plugin(plugin_key)
    setattr(plugin, "backup_root", str(tmp_path))
    setattr(plugin, "backup_deadline_seconds", 0.01)
    setattr(plugin, "poll_interval_seconds", 0.0)

    with pytest.raises(RuntimeError, match="archive|ambiguous|stale|appear"):
        await asyncio.wait_for(
            plugin.backup(_backup_context(plugin_key, default_url, backup_directory)),
            timeout=0.25,
        )
    assert not any(request.method == "DELETE" for request in requests)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("plugin_key", "app_name", "version", "migration", "default_url"),
    SERVICE_CONTRACTS,
)
@pytest.mark.parametrize(
    "source_case",
    (
        "traversal",
        "cross-origin",
        "symlink",
        "nonregular",
        "not-mounted",
        "writable",
        "missing",
        "mismatch",
    ),
)
async def test_backup_refuses_unsafe_or_untrusted_local_source(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    plugin_key: str,
    app_name: str,
    version: str,
    migration: int,
    default_url: str,
    source_case: str,
) -> None:
    archive_bytes = _native_backup_zip(
        tmp_path,
        plugin_key=plugin_key,
        version=version,
        migration=migration,
    )
    backup_directory = tmp_path / f"{plugin_key}-native-backups"
    backup_directory.mkdir()
    _install_read_only_mount(
        monkeypatch,
        backup_directory,
        plugin_key=plugin_key,
        is_mount=source_case != "not-mounted",
        read_only=source_case != "writable",
    )
    filename = f"{plugin_key}_backup_v{version}_2026.08.16_12.00.03.zip"
    candidate_path = {
        "traversal": f"/backup/manual/../{filename}",
        "cross-origin": f"https://attacker.invalid/backup/manual/{filename}",
        "mismatch": "/backup/manual/different.zip",
    }.get(source_case)
    candidate = _manual_backup(
        plugin_key,
        version,
        backup_id=94,
        filename=filename,
        path=candidate_path,
        observed_at=datetime.now(timezone.utc) + timedelta(seconds=1),
        size=len(archive_bytes),
    )
    requests: list[httpx.Request] = []
    list_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal list_calls
        requests.append(request)
        if request.url.path == "/api/v1/system/status":
            return httpx.Response(
                200,
                json=_status_payload(app_name, version, migration),
            )
        if request.url.path == "/api/v1/system/backup":
            list_calls += 1
            return httpx.Response(200, json=[] if list_calls == 1 else [candidate])
        if request.method == "POST" and request.url.path == "/api/v1/command":
            candidate_file = backup_directory / filename
            if source_case == "symlink":
                outside = tmp_path / f"outside-{plugin_key}.zip"
                outside.write_bytes(archive_bytes)
                candidate_file.symlink_to(outside)
            elif source_case == "nonregular":
                candidate_file.mkdir()
            elif source_case != "missing":
                candidate_file.write_bytes(archive_bytes)
            return httpx.Response(201, json={"id": 85})
        if request.url.path == "/api/v1/command/85":
            command: dict[str, object] = {"id": 85, "status": "completed"}
            if plugin_key == "readarr":
                command["result"] = "successful"
            return httpx.Response(200, json=command)
        raise AssertionError(f"Unexpected network request: {request.method} {request.url}")

    _install_transport(monkeypatch, handler)
    plugin = get_plugin(plugin_key)
    setattr(plugin, "backup_root", str(tmp_path))

    with pytest.raises(
        (RuntimeError, FileNotFoundError),
        match="mount|read-only|unsafe|regular|matching|missing|path",
    ):
        await plugin.backup(_backup_context(plugin_key, default_url, backup_directory))
    expected_host = default_url.split("//", 1)[1].split(":", 1)[0]
    assert {request.url.host for request in requests} <= {expected_host}
    assert all(request.url.path.startswith("/api/v1/") for request in requests)
    assert not any(request.method == "DELETE" for request in requests)


ARCHIVE_REJECTION_CASES = (
    "missing-member",
    "config-only",
    "duplicate-member",
    "extra-member",
    "nested-member",
    "traversal-member",
    "absolute-member",
    "case-mismatched-member",
    "link-member",
    "device-member",
    "encrypted-member",
    "unsupported-compression",
    "crc",
    "trailing-data",
    "malformed-xml",
    "wrong-config-root",
    "missing-api-key",
    "empty-api-key",
    "duplicate-api-key",
    "wrong-info-version",
    "bad-info-timestamp",
    "corrupt-sqlite",
    "sqlite-quick-check",
    "foreign-key",
    "wrong-migration",
    "missing-table",
    "hot-wal",
    "hot-shm",
    "hot-journal",
    "archive-size-bound",
    "member-count-bound",
    "compressed-size-bound",
    "uncompressed-size-bound",
    "expansion-ratio-bound",
    "xml-size-bound",
    "database-size-bound",
)

ARCHIVE_BOUND_CONSTANTS = {
    "archive-size-bound": "_MAX_ARCHIVE_BYTES",
    "member-count-bound": "_MAX_ZIP_MEMBERS",
    "compressed-size-bound": "_MAX_COMPRESSED_BYTES",
    "uncompressed-size-bound": "_MAX_UNCOMPRESSED_BYTES",
    "expansion-ratio-bound": "_MAX_EXPANSION_RATIO",
    "xml-size-bound": "_MAX_CONFIG_BYTES",
    "database-size-bound": "_MAX_DATABASE_BYTES",
}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("plugin_key", "app_name", "version", "migration", "default_url"),
    SERVICE_CONTRACTS,
)
@pytest.mark.parametrize("archive_case", ARCHIVE_REJECTION_CASES)
async def test_backup_rejects_malformed_or_unbounded_native_archive(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    plugin_key: str,
    app_name: str,
    version: str,
    migration: int,
    default_url: str,
    archive_case: str,
) -> None:
    mutation = (
        archive_case.removesuffix("-bound") if archive_case.endswith("-bound") else archive_case
    )
    archive_bytes = (
        _native_backup_zip(
            tmp_path,
            plugin_key=plugin_key,
            version=version,
            migration=migration,
        )
        if archive_case in ARCHIVE_BOUND_CONSTANTS
        else _malformed_native_zip(
            tmp_path,
            plugin_key=plugin_key,
            version=version,
            migration=migration,
            case=mutation,
        )
    )
    backup_directory = tmp_path / f"{plugin_key}-strict-native-backups"
    backup_directory.mkdir()
    _install_read_only_mount(
        monkeypatch,
        backup_directory,
        plugin_key=plugin_key,
    )
    output_root = tmp_path / "published"
    if archive_case in ARCHIVE_BOUND_CONSTANTS:
        servarr_module = importlib.import_module("app.core.plugins.servarr")
        monkeypatch.setattr(
            servarr_module,
            ARCHIVE_BOUND_CONSTANTS[archive_case],
            1 if archive_case != "member-count-bound" else 2,
            raising=False,
        )
    requests = _install_single_archive_backup(
        monkeypatch,
        backup_directory=backup_directory,
        archive_bytes=archive_bytes,
        plugin_key=plugin_key,
        app_name=app_name,
        version=version,
        migration=migration,
    )
    plugin = get_plugin(plugin_key)
    setattr(plugin, "backup_root", str(output_root))

    with pytest.raises(RuntimeError):
        await plugin.backup(_backup_context(plugin_key, default_url, backup_directory))

    assert not [path for path in output_root.rglob("*") if path.is_file()]
    assert all(request.url.path.startswith("/api/v1/") for request in requests)
    assert not any(request.method == "DELETE" for request in requests)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("plugin_key", "app_name", "version", "migration", "default_url"),
    SERVICE_CONTRACTS,
)
async def test_backup_cancellation_removes_partial_artifact_and_sidecar(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    plugin_key: str,
    app_name: str,
    version: str,
    migration: int,
    default_url: str,
) -> None:
    archive_bytes = _native_backup_zip(
        tmp_path,
        plugin_key=plugin_key,
        version=version,
        migration=migration,
    )
    backup_directory = tmp_path / f"{plugin_key}-cancel-native-backups"
    backup_directory.mkdir()
    _install_read_only_mount(
        monkeypatch,
        backup_directory,
        plugin_key=plugin_key,
    )
    output_root = tmp_path / "published"
    requests = _install_single_archive_backup(
        monkeypatch,
        backup_directory=backup_directory,
        archive_bytes=archive_bytes,
        plugin_key=plugin_key,
        app_name=app_name,
        version=version,
        migration=migration,
    )
    plugin = get_plugin(plugin_key)
    setattr(plugin, "backup_root", str(output_root))
    servarr_module = importlib.import_module("app.core.plugins.servarr")
    process = _BlockingProcess(hold_after_terminate=False)
    connection = _NoResultConnection()
    validation_roots: list[Path] = []

    def start_blocked_worker(
        _directory: Path,
        _name: str,
        _evidence: object,
        artifact_path: Path,
        validation_root: Path,
        _validation_identity: tuple[int, int],
    ) -> tuple[object, object]:
        artifact_path.write_bytes(b"sensitive cancelled Servarr archive")
        (validation_root / "database.sqlite").write_bytes(b"sensitive SQLite residue")
        validation_roots.append(validation_root)
        return process, connection

    monkeypatch.setattr(servarr_module, "_start_backup_process", start_blocked_worker)
    operation = asyncio.create_task(
        plugin.backup(_backup_context(plugin_key, default_url, backup_directory))
    )
    assert await asyncio.to_thread(process.join_started.wait, 2)
    operation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await operation
    assert not process.is_alive()
    assert process.exitcode == -15
    assert connection.closed
    assert validation_roots and all(not path.exists() for path in validation_roots)
    assert not [path for path in output_root.rglob("*") if path.is_file()]
    assert not any(request.method == "DELETE" for request in requests)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("plugin_key", "app_name", "version", "migration", "default_url"),
    SERVICE_CONTRACTS,
)
@pytest.mark.parametrize(
    "cleanup_failure",
    ("http-500", "unexpected-204", "timeout", "cancellation"),
)
async def test_backup_cleanup_failure_preserves_published_and_source_copies(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    plugin_key: str,
    app_name: str,
    version: str,
    migration: int,
    default_url: str,
    cleanup_failure: str,
) -> None:
    archive_bytes = _native_backup_zip(
        tmp_path,
        plugin_key=plugin_key,
        version=version,
        migration=migration,
    )
    backup_directory = tmp_path / f"{plugin_key}-cleanup-native-backups"
    backup_directory.mkdir()
    _install_read_only_mount(
        monkeypatch,
        backup_directory,
        plugin_key=plugin_key,
    )
    output_root = tmp_path / "published"
    baseline = _manual_backup(
        plugin_key,
        version,
        backup_id=400,
        filename=f"{plugin_key}_backup_v{version}_2026.08.16_12.40.00.zip",
        size=len(archive_bytes),
    )
    created = _manual_backup(
        plugin_key,
        version,
        backup_id=401,
        filename=f"{plugin_key}_backup_v{version}_2026.08.16_12.41.00.zip",
        observed_at=datetime.now(timezone.utc) + timedelta(seconds=1),
        size=len(archive_bytes),
    )
    baseline_path = backup_directory / str(baseline["name"])
    created_path = backup_directory / str(created["name"])
    baseline_path.write_bytes(archive_bytes)
    requests: list[httpx.Request] = []
    list_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal list_calls
        requests.append(request)
        if request.method == "GET" and request.url.path == "/api/v1/system/status":
            return httpx.Response(
                200,
                json=_status_payload(app_name, version, migration),
            )
        if request.method == "GET" and request.url.path == "/api/v1/system/backup":
            list_calls += 1
            return httpx.Response(
                200,
                json=[baseline] if list_calls == 1 else [created, baseline],
            )
        if request.method == "POST" and request.url.path == "/api/v1/command":
            created_path.write_bytes(archive_bytes)
            return httpx.Response(201, json={"id": 402})
        if request.method == "GET" and request.url.path == "/api/v1/command/402":
            command: dict[str, object] = {"id": 402, "status": "completed"}
            if plugin_key == "readarr":
                command["result"] = "successful"
            return httpx.Response(200, json=command)
        if request.method == "DELETE":
            assert request.url.path == "/api/v1/system/backup/401"
            assert request.headers["X-Api-Key"] == "synthetic-key"
            assert request.url.params.get("apikey") is None
            artifacts = [path for path in output_root.rglob("*.zip") if path.is_file()]
            sidecars = [path for path in output_root.rglob("*.meta.json") if path.is_file()]
            assert len(artifacts) == 1
            assert sidecars == [Path(f"{artifacts[0]}.meta.json")]
            if cleanup_failure == "http-500":
                return httpx.Response(500)
            if cleanup_failure == "unexpected-204":
                return httpx.Response(204)
            if cleanup_failure == "timeout":
                raise httpx.ReadTimeout("synthetic cleanup timeout", request=request)
            raise asyncio.CancelledError
        raise AssertionError(f"Unexpected network request: {request.method} {request.url}")

    _install_transport(monkeypatch, handler)
    plugin = get_plugin(plugin_key)
    setattr(plugin, "backup_root", str(output_root))
    operation = plugin.backup(_backup_context(plugin_key, default_url, backup_directory))

    if cleanup_failure == "cancellation":
        with pytest.raises(asyncio.CancelledError):
            await operation
    else:
        with pytest.raises((RuntimeError, httpx.HTTPError)):
            await operation

    delete_requests = [request for request in requests if request.method == "DELETE"]
    assert [(request.method, request.url.path) for request in delete_requests] == [
        ("DELETE", "/api/v1/system/backup/401")
    ]
    assert baseline_path.is_file()
    assert created_path.is_file()
    artifacts = [path for path in output_root.rglob("*.zip") if path.is_file()]
    sidecars = [path for path in output_root.rglob("*.meta.json") if path.is_file()]
    assert len(artifacts) == 1
    assert sidecars == [Path(f"{artifacts[0]}.meta.json")]
    sidecar_text = sidecars[0].read_text(encoding="utf-8")
    assert "synthetic-key" not in sidecar_text
    assert default_url not in sidecar_text
    assert str(backup_directory) not in sidecar_text


@pytest.mark.asyncio
async def test_backup_worker_timeout_escalates_to_kill_reaps_and_removes_private_residue(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plugin_key, app_name, version, migration, default_url = READARR_CONTRACT
    archive_bytes = _native_backup_zip(
        tmp_path,
        plugin_key=plugin_key,
        version=version,
        migration=migration,
    )
    backup_directory = tmp_path / "readarr-timeout-native-backups"
    backup_directory.mkdir()
    _install_read_only_mount(
        monkeypatch,
        backup_directory,
        plugin_key=plugin_key,
    )
    output_root = tmp_path / "published"
    requests = _install_single_archive_backup(
        monkeypatch,
        backup_directory=backup_directory,
        archive_bytes=archive_bytes,
        plugin_key=plugin_key,
        app_name=app_name,
        version=version,
        migration=migration,
    )
    plugin = get_plugin(plugin_key)
    setattr(plugin, "backup_root", str(output_root))
    servarr_module = importlib.import_module("app.core.plugins.servarr")
    assert callable(getattr(servarr_module, "_start_backup_process", None))
    monkeypatch.setattr(servarr_module, "_BACKUP_WORKER_TIMEOUT_SECONDS", 0.01, raising=False)
    monkeypatch.setattr(servarr_module, "_WORKER_STOP_TIMEOUT_SECONDS", 0.01, raising=False)
    process = _BlockingProcess(hold_after_terminate=True)
    connection = _NoResultConnection()
    validation_roots: list[Path] = []

    def start_blocked_worker(
        _directory: Path,
        _name: str,
        _evidence: object,
        artifact_path: Path,
        validation_root: Path,
        _validation_identity: tuple[int, int],
    ) -> tuple[object, object]:
        artifact_path.write_bytes(b"sensitive partial Servarr archive")
        (validation_root / "database.sqlite").write_bytes(b"sensitive SQLite residue")
        validation_roots.append(validation_root)
        return process, connection

    monkeypatch.setattr(
        servarr_module,
        "_start_backup_process",
        start_blocked_worker,
        raising=False,
    )

    with pytest.raises(TimeoutError, match="timed out"):
        await plugin.backup(_backup_context(plugin_key, default_url, backup_directory))

    assert process.terminate_called.is_set()
    assert process.kill_called.is_set()
    assert process.exitcode == -9
    assert not process.is_alive()
    assert process.join_calls >= 3
    assert connection.closed
    assert validation_roots and all(not path.exists() for path in validation_roots)
    assert not [path for path in output_root.rglob("*") if path.is_file()]
    assert list(backup_directory.glob("*.zip"))
    assert not any(request.method == "DELETE" for request in requests)


@pytest.mark.asyncio
async def test_backup_worker_repeated_cancellation_waits_for_kill_reap_and_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plugin_key, app_name, version, migration, default_url = PROWLARR_CONTRACT
    archive_bytes = _native_backup_zip(
        tmp_path,
        plugin_key=plugin_key,
        version=version,
        migration=migration,
    )
    backup_directory = tmp_path / "prowlarr-cancel-native-backups"
    backup_directory.mkdir()
    _install_read_only_mount(
        monkeypatch,
        backup_directory,
        plugin_key=plugin_key,
    )
    output_root = tmp_path / "published"
    requests = _install_single_archive_backup(
        monkeypatch,
        backup_directory=backup_directory,
        archive_bytes=archive_bytes,
        plugin_key=plugin_key,
        app_name=app_name,
        version=version,
        migration=migration,
    )
    plugin = get_plugin(plugin_key)
    setattr(plugin, "backup_root", str(output_root))
    servarr_module = importlib.import_module("app.core.plugins.servarr")
    assert callable(getattr(servarr_module, "_start_backup_process", None))
    monkeypatch.setattr(servarr_module, "_BACKUP_WORKER_TIMEOUT_SECONDS", 30.0, raising=False)
    monkeypatch.setattr(servarr_module, "_WORKER_STOP_TIMEOUT_SECONDS", 0.05, raising=False)
    process = _BlockingProcess(hold_after_terminate=True)
    connection = _NoResultConnection()
    validation_roots: list[Path] = []

    def start_blocked_worker(
        _directory: Path,
        _name: str,
        _evidence: object,
        artifact_path: Path,
        validation_root: Path,
        _validation_identity: tuple[int, int],
    ) -> tuple[object, object]:
        artifact_path.write_bytes(b"sensitive cancelled Servarr archive")
        (validation_root / "database.sqlite").write_bytes(b"sensitive SQLite residue")
        validation_roots.append(validation_root)
        return process, connection

    monkeypatch.setattr(
        servarr_module,
        "_start_backup_process",
        start_blocked_worker,
        raising=False,
    )
    task = asyncio.create_task(
        plugin.backup(_backup_context(plugin_key, default_url, backup_directory))
    )
    worker_started = await asyncio.to_thread(process.join_started.wait, 2)
    if not worker_started:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        pytest.fail("backup did not enter the bounded worker process seam")

    task.cancel()
    assert await asyncio.to_thread(process.terminate_called.wait, 2)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)
    assert process.kill_called.is_set()
    assert process.exitcode == -9
    assert not process.is_alive()
    assert process.join_calls >= 3
    assert connection.closed
    assert validation_roots and all(not path.exists() for path in validation_roots)
    assert not [path for path in output_root.rglob("*") if path.is_file()]
    assert list(backup_directory.glob("*.zip"))
    assert not any(request.method == "DELETE" for request in requests)


@pytest.mark.asyncio
@pytest.mark.parametrize("worker_case", ("nonzero", "no-result", "malformed-result"))
async def test_backup_worker_invalid_completion_fails_closed_without_publication_or_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    worker_case: str,
) -> None:
    plugin_key, app_name, version, migration, default_url = READARR_CONTRACT
    archive_bytes = _native_backup_zip(
        tmp_path,
        plugin_key=plugin_key,
        version=version,
        migration=migration,
    )
    backup_directory = tmp_path / f"readarr-{worker_case}-native-backups"
    backup_directory.mkdir()
    _install_read_only_mount(
        monkeypatch,
        backup_directory,
        plugin_key=plugin_key,
    )
    output_root = tmp_path / "published"
    requests = _install_single_archive_backup(
        monkeypatch,
        backup_directory=backup_directory,
        archive_bytes=archive_bytes,
        plugin_key=plugin_key,
        app_name=app_name,
        version=version,
        migration=migration,
    )
    plugin = get_plugin(plugin_key)
    setattr(plugin, "backup_root", str(output_root))
    servarr_module = importlib.import_module("app.core.plugins.servarr")
    assert callable(getattr(servarr_module, "_start_backup_process", None))
    process = _CompletedProcess(exitcode=1 if worker_case == "nonzero" else 0)
    if worker_case == "no-result":
        connection: _NoResultConnection = _NoResultConnection()
    elif worker_case == "malformed-result":
        connection = _ResultConnection({"kind": "ok", "payload": "untrusted"})
    else:
        connection = _ResultConnection(("ok", "", None))
    validation_roots: list[Path] = []

    def start_invalid_worker(
        _directory: Path,
        _name: str,
        _evidence: object,
        artifact_path: Path,
        validation_root: Path,
        _validation_identity: tuple[int, int],
    ) -> tuple[object, object]:
        artifact_path.write_bytes(b"sensitive invalid worker archive")
        (validation_root / "database.sqlite").write_bytes(b"sensitive SQLite residue")
        validation_roots.append(validation_root)
        return process, connection

    monkeypatch.setattr(
        servarr_module,
        "_start_backup_process",
        start_invalid_worker,
        raising=False,
    )

    with pytest.raises(RuntimeError, match="worker|result"):
        await plugin.backup(_backup_context(plugin_key, default_url, backup_directory))

    assert process.join_calls >= 1
    assert connection.closed
    assert validation_roots and all(not path.exists() for path in validation_roots)
    assert not [path for path in output_root.rglob("*") if path.is_file()]
    assert list(backup_directory.glob("*.zip"))
    assert not any(request.method == "DELETE" for request in requests)


@pytest.mark.asyncio
async def test_backup_worker_refuses_native_source_replacement_during_copy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plugin_key, app_name, version, migration, default_url = READARR_CONTRACT
    archive_bytes = _native_backup_zip(
        tmp_path,
        plugin_key=plugin_key,
        version=version,
        migration=migration,
    )
    backup_directory = tmp_path / "readarr-replaced-native-backups"
    backup_directory.mkdir()
    _install_read_only_mount(
        monkeypatch,
        backup_directory,
        plugin_key=plugin_key,
    )
    output_root = tmp_path / "published"
    requests = _install_single_archive_backup(
        monkeypatch,
        backup_directory=backup_directory,
        archive_bytes=archive_bytes,
        plugin_key=plugin_key,
        app_name=app_name,
        version=version,
        migration=migration,
    )
    plugin = get_plugin(plugin_key)
    setattr(plugin, "backup_root", str(output_root))
    servarr_module = importlib.import_module("app.core.plugins.servarr")
    assert callable(getattr(servarr_module, "_start_backup_process", None))
    validation_roots: list[Path] = []
    source_identities: list[tuple[int, int]] = []

    def start_replacing_worker(
        directory: Path,
        name: str,
        _evidence: object,
        artifact_path: Path,
        validation_root: Path,
        _validation_identity: tuple[int, int],
    ) -> tuple[object, object]:
        source = directory / name
        source_identities.append((source.stat().st_dev, source.stat().st_ino))
        validation_roots.append(validation_root)
        real_copy = shutil.copyfileobj

        def copy_and_replace(
            source_file: Any,
            destination_file: Any,
            length: int = 0,
        ) -> None:
            first = source_file.read(length)
            destination_file.write(first)
            replacement = directory / ".replacement.zip"
            replacement.write_bytes(archive_bytes)
            os.replace(replacement, source)
            real_copy(source_file, destination_file, length=length)

        with monkeypatch.context() as copy_patch:
            copy_patch.setattr(shutil, "copyfileobj", copy_and_replace)
            try:
                cast(Any, plugin)._copy_stable_local_backup(source, artifact_path)
            except RuntimeError as exc:
                return _CompletedProcess(exitcode=1), _ResultConnection(("runtime", str(exc), None))
        raise AssertionError("native source replacement was not detected")

    monkeypatch.setattr(
        servarr_module,
        "_start_backup_process",
        start_replacing_worker,
        raising=False,
    )

    with pytest.raises(RuntimeError, match="changed while copying"):
        await plugin.backup(_backup_context(plugin_key, default_url, backup_directory))

    source = next(backup_directory.glob("*.zip"))
    assert len(source_identities) == 1
    original_device, original_inode = source_identities[0]
    assert source.stat().st_dev == original_device
    assert source.stat().st_ino != original_inode
    assert validation_roots and all(not path.exists() for path in validation_roots)
    assert not [path for path in output_root.rglob("*") if path.is_file()]
    assert not any(request.method == "DELETE" for request in requests)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("plugin_key", "app_name", "version", "migration", "default_url"),
    SERVICE_CONTRACTS,
)
async def test_isolated_restore_uploads_restarts_and_proves_new_exact_process(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    plugin_key: str,
    app_name: str,
    version: str,
    migration: int,
    default_url: str,
) -> None:
    del default_url
    artifact = tmp_path / f"{plugin_key}-restore.zip"
    artifact.write_bytes(
        _native_backup_zip(
            tmp_path,
            plugin_key=plugin_key,
            version=version,
            migration=migration,
        )
    )
    _authorize_isolated_restore(monkeypatch, plugin_key)
    requests: list[httpx.Request] = []
    status_calls = 0
    old_start = "2026-08-16T10:00:00Z"
    new_start = "2026-08-16T10:01:00Z"

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal status_calls
        requests.append(request)
        assert (
            str(request.url.copy_with(path="", query=None)).rstrip("/")
            == RESTORE_ORIGINS[plugin_key]
        )
        if request.method == "GET" and request.url.path == "/api/v1/system/status":
            status_calls += 1
            expected_key = "destination-key" if status_calls == 1 else "restored-synthetic-key"
            assert request.headers["X-Api-Key"] == expected_key
            return httpx.Response(
                200,
                json={
                    **_status_payload(app_name, version, migration),
                    "startTime": old_start if status_calls == 1 else new_start,
                },
            )
        if request.method == "GET" and request.url.path in FRESH_RESTORE_PATHS[plugin_key]:
            assert request.headers["X-Api-Key"] == "destination-key"
            return httpx.Response(200, json=[])
        if request.method == "POST" and request.url.path.endswith("/system/backup/restore/upload"):
            assert request.headers["X-Api-Key"] == "destination-key"
            assert request.headers["Content-Type"].startswith("multipart/form-data; boundary=")
            assert request.content.count(b'name="file"') == 1
            assert f'filename="{artifact.name}"'.encode() in request.content
            return httpx.Response(200, json={"restartRequired": True})
        if request.method == "POST" and request.url.path == "/api/v1/system/restart":
            assert request.headers["X-Api-Key"] == "destination-key"
            assert request.content == b""
            return httpx.Response(200, json={"restarting": True})
        raise AssertionError(f"Unexpected restore request: {request.method} {request.url}")

    _install_transport(monkeypatch, handler)
    plugin = get_plugin(plugin_key)
    setattr(plugin, "restore_deadline_seconds", 0.05)
    setattr(plugin, "restore_poll_interval_seconds", 0.0)

    result = await plugin.restore(_restore_context(plugin_key, artifact))

    assert result["status"] == "success"
    assert result["artifact_bytes"] == artifact.stat().st_size
    assert status_calls == 2
    assert [(request.method, request.url.path) for request in requests] == [
        ("GET", "/api/v1/system/status"),
        *(("GET", path) for path in FRESH_RESTORE_PATHS[plugin_key]),
        ("POST", "/api/v1/system/backup/restore/upload"),
        ("POST", "/api/v1/system/restart"),
        ("GET", "/api/v1/system/status"),
    ]
    assert "restored-synthetic-key" not in json.dumps(result)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("plugin_key", "app_name", "version", "migration", "default_url"),
    SERVICE_CONTRACTS,
)
async def test_isolated_restore_refuses_nonfresh_destination_before_upload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    plugin_key: str,
    app_name: str,
    version: str,
    migration: int,
    default_url: str,
) -> None:
    del default_url
    artifact = tmp_path / f"{plugin_key}-nonfresh.zip"
    artifact.write_bytes(
        _native_backup_zip(
            tmp_path,
            plugin_key=plugin_key,
            version=version,
            migration=migration,
        )
    )
    _authorize_isolated_restore(monkeypatch, plugin_key)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/v1/system/status":
            return httpx.Response(
                200,
                json={
                    **_status_payload(app_name, version, migration),
                    "startTime": "2026-08-16T10:00:00Z",
                },
            )
        if request.url.path == FRESH_RESTORE_PATHS[plugin_key][0]:
            return httpx.Response(200, json=[{"id": 1}])
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    _install_transport(monkeypatch, handler)

    with pytest.raises(RuntimeError, match="not fresh and empty"):
        await get_plugin(plugin_key).restore(_restore_context(plugin_key, artifact))

    assert [(request.method, request.url.path) for request in requests] == [
        ("GET", "/api/v1/system/status"),
        ("GET", FRESH_RESTORE_PATHS[plugin_key][0]),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("plugin_key", "app_name", "version", "migration", "default_url"),
    SERVICE_CONTRACTS,
)
async def test_restore_upload_remains_bound_to_verified_descriptor_after_path_replacement(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    plugin_key: str,
    app_name: str,
    version: str,
    migration: int,
    default_url: str,
) -> None:
    del default_url
    artifact = tmp_path / f"{plugin_key}-descriptor-bound.zip"
    original = _native_backup_zip(
        tmp_path,
        plugin_key=plugin_key,
        version=version,
        migration=migration,
    )
    artifact.write_bytes(original)
    metadata = {
        "artifact_bytes": len(original),
        "artifact_sha256": hashlib.sha256(original).hexdigest(),
    }
    replacement = _archive_bytes(
        [
            (
                "config.xml",
                b"<Config><ApiKey>replacement-synthetic-key</ApiKey></Config>",
            ),
            ("INFO", f"v{version}\n2026-08-16 12:00:00\n".encode()),
            (
                f"{plugin_key}.db",
                _native_database_bytes(
                    tmp_path,
                    plugin_key=plugin_key,
                    migration=migration,
                ),
            ),
        ]
    )
    replacement_path = tmp_path / f"{plugin_key}-replacement.zip"
    replacement_path.write_bytes(replacement)
    _authorize_isolated_restore(monkeypatch, plugin_key)
    plugin = get_plugin(plugin_key)

    async def replace_verified_path(
        _client: httpx.AsyncClient,
        _base_url: str,
        _headers: dict[str, str],
    ) -> None:
        os.replace(replacement_path, artifact)

    monkeypatch.setattr(plugin, "_require_fresh_restore_destination", replace_verified_path)
    status_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal status_calls
        if request.url.path == "/api/v1/system/status":
            status_calls += 1
            expected_key = "destination-key" if status_calls == 1 else "restored-synthetic-key"
            assert request.headers["X-Api-Key"] == expected_key
            return httpx.Response(
                200,
                json={
                    **_status_payload(app_name, version, migration),
                    "startTime": (
                        "2026-08-16T10:00:00Z" if status_calls == 1 else "2026-08-16T10:01:00Z"
                    ),
                },
            )
        if request.url.path.endswith("/system/backup/restore/upload"):
            assert original in request.content
            assert replacement not in request.content
            return httpx.Response(200, json={"restartRequired": True})
        if request.url.path == "/api/v1/system/restart":
            return httpx.Response(200, json={"restarting": True})
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    _install_transport(monkeypatch, handler)
    setattr(plugin, "restore_deadline_seconds", 0.05)
    setattr(plugin, "restore_poll_interval_seconds", 0.0)

    result = await plugin.restore(_restore_context(plugin_key, artifact, metadata=metadata))

    assert result["status"] == "success"
    assert artifact.read_bytes() == replacement


@pytest.mark.asyncio
async def test_same_origin_restore_lock_serializes_targets_and_releases_on_cancellation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    plugin_key = "readarr"
    artifact = tmp_path / "lock-seam.zip"
    artifact.write_bytes(b"lock seam")
    _authorize_isolated_restore(monkeypatch, plugin_key)
    plugin = get_plugin(plugin_key)
    first_entered = asyncio.Event()
    second_entered = asyncio.Event()
    hold_first = asyncio.Event()
    entered: list[str] = []

    async def observe_serialization(
        context: RestoreContext,
        _base_url: str,
        _headers: dict[str, str],
    ) -> dict[str, str]:
        entered.append(context.destination_target_id)
        if context.destination_target_id == "destination-one":
            first_entered.set()
            await hold_first.wait()
        else:
            second_entered.set()
        return {"status": "success"}

    monkeypatch.setattr(plugin, "_restore_without_lock", observe_serialization)
    first = asyncio.create_task(
        plugin.restore(
            _restore_context(
                plugin_key,
                artifact,
                destination_target_id="destination-one",
            )
        )
    )
    await asyncio.wait_for(first_entered.wait(), timeout=1)
    second = asyncio.create_task(
        plugin.restore(
            _restore_context(
                plugin_key,
                artifact,
                destination_target_id="destination-two",
                config={
                    **_restore_config(plugin_key),
                    "base_url": "http://READARR-RESTORE:8787",
                },
            )
        )
    )
    await asyncio.sleep(0.05)
    assert entered == ["destination-one"]
    assert not second_entered.is_set()

    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert await asyncio.wait_for(second, timeout=1) == {"status": "success"}
    assert entered == ["destination-one", "destination-two"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("plugin_key", "app_name", "version", "migration", "default_url"),
    SERVICE_CONTRACTS,
)
@pytest.mark.parametrize(
    "refusal_case",
    (
        "missing-authorization",
        "missing-allowlist",
        "wrong-origin",
        "same-target",
        "invalid-config",
        "inexact-pre-status",
    ),
)
async def test_restore_refuses_unauthorized_or_incompatible_destination_before_mutation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    plugin_key: str,
    app_name: str,
    version: str,
    migration: int,
    default_url: str,
    refusal_case: str,
) -> None:
    del default_url
    artifact = tmp_path / f"{plugin_key}-refusal.zip"
    artifact.write_bytes(
        _native_backup_zip(
            tmp_path,
            plugin_key=plugin_key,
            version=version,
            migration=migration,
        )
    )
    monkeypatch.delenv("HOMELAB_BACKUP_ALLOW_ISOLATED_RESTORE", raising=False)
    monkeypatch.delenv(
        "HOMELAB_BACKUP_ISOLATED_RESTORE_ALLOWED_ORIGINS",
        raising=False,
    )
    if refusal_case != "missing-authorization":
        monkeypatch.setenv("HOMELAB_BACKUP_ALLOW_ISOLATED_RESTORE", "1")
    if refusal_case != "missing-allowlist":
        monkeypatch.setenv(
            "HOMELAB_BACKUP_ISOLATED_RESTORE_ALLOWED_ORIGINS",
            RESTORE_ORIGINS[plugin_key],
        )
    config = _restore_config(plugin_key)
    if refusal_case == "wrong-origin":
        config["base_url"] = "http://unapproved-restore:8787"
    elif refusal_case == "invalid-config":
        config.pop("api_key")
    source_id = "same-target" if refusal_case == "same-target" else "source-target"
    destination_id = "same-target" if refusal_case == "same-target" else "isolated-destination"
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if refusal_case == "inexact-pre-status" and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    **_status_payload("WrongApp", version, migration),
                    "startTime": "2026-08-16T10:00:00Z",
                },
            )
        raise AssertionError("restore mutation or unauthorized I/O was attempted")

    _install_transport(monkeypatch, handler)
    context = _restore_context(
        plugin_key,
        artifact,
        source_target_id=source_id,
        destination_target_id=destination_id,
        config=config,
    )

    with pytest.raises((RuntimeError, ValueError)):
        await get_plugin(plugin_key).restore(context)
    assert not any(request.method == "POST" for request in requests)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("plugin_key", "app_name", "version", "migration", "default_url"),
    SERVICE_CONTRACTS,
)
@pytest.mark.parametrize(
    "artifact_case",
    ("invalid", "size-mismatch", "hash-mismatch", "substituted"),
)
async def test_restore_rejects_invalid_or_substituted_artifact_before_mutation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    plugin_key: str,
    app_name: str,
    version: str,
    migration: int,
    default_url: str,
    artifact_case: str,
) -> None:
    del app_name, default_url
    artifact = tmp_path / f"{plugin_key}-artifact-check.zip"
    original = _native_backup_zip(
        tmp_path,
        plugin_key=plugin_key,
        version=version,
        migration=migration,
    )
    artifact.write_bytes(original)
    metadata: dict[str, object] = {
        "artifact_bytes": len(original),
        "artifact_sha256": hashlib.sha256(original).hexdigest(),
    }
    if artifact_case == "invalid":
        artifact.write_bytes(b"not a native archive")
        metadata = {
            "artifact_bytes": artifact.stat().st_size,
            "artifact_sha256": _sha256_file(artifact),
        }
    elif artifact_case == "size-mismatch":
        metadata["artifact_bytes"] = len(original) + 1
    elif artifact_case == "hash-mismatch":
        metadata["artifact_sha256"] = "0" * 64
    elif artifact_case == "substituted":
        replacement = _archive_bytes(
            [
                ("config.xml", b"<Config><ApiKey>replaced-synthetic-key</ApiKey></Config>"),
                ("INFO", f"v{version}\n2026-08-16 12:00:00\n".encode()),
                (
                    f"{plugin_key}.db",
                    _native_database_bytes(
                        tmp_path,
                        plugin_key=plugin_key,
                        migration=migration,
                    ),
                ),
            ]
        )
        artifact.write_bytes(replacement)
    _authorize_isolated_restore(monkeypatch, plugin_key)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise AssertionError("artifact failure must precede destination I/O")

    _install_transport(monkeypatch, handler)

    with pytest.raises((RuntimeError, ValueError)):
        await get_plugin(plugin_key).restore(
            _restore_context(plugin_key, artifact, metadata=metadata)
        )
    assert requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("plugin_key", "app_name", "version", "migration", "default_url"),
    SERVICE_CONTRACTS,
)
@pytest.mark.parametrize(
    "failure_case",
    (
        "pre-auth",
        "pre-malformed",
        "upload-response",
        "upload-timeout",
        "restart-response",
        "post-auth",
        "post-malformed",
        "post-status-drift",
        "same-start-time",
        "empty-start-time",
        "non-string-start-time",
        "cancellation",
    ),
)
async def test_restore_fails_closed_for_bounded_protocol_failures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    plugin_key: str,
    app_name: str,
    version: str,
    migration: int,
    default_url: str,
    failure_case: str,
) -> None:
    del default_url
    artifact = tmp_path / f"{plugin_key}-protocol-failure.zip"
    artifact.write_bytes(
        _native_backup_zip(
            tmp_path,
            plugin_key=plugin_key,
            version=version,
            migration=migration,
        )
    )
    _authorize_isolated_restore(monkeypatch, plugin_key)
    requests: list[httpx.Request] = []
    status_calls = 0
    old_start = "2026-08-16T11:00:00Z"

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal status_calls
        requests.append(request)
        if request.method == "GET" and request.url.path == "/api/v1/system/status":
            status_calls += 1
            if status_calls == 1:
                if failure_case == "pre-auth":
                    return httpx.Response(401)
                if failure_case == "pre-malformed":
                    return httpx.Response(200, content=b"{")
                return httpx.Response(
                    200,
                    json={
                        **_status_payload(app_name, version, migration),
                        "startTime": old_start,
                    },
                )
            assert request.headers["X-Api-Key"] == "restored-synthetic-key"
            if failure_case == "post-auth":
                return httpx.Response(401)
            if failure_case == "post-malformed":
                return httpx.Response(200, content=b"{")
            if failure_case == "post-status-drift":
                return httpx.Response(
                    200,
                    json={
                        **_status_payload("WrongApp", version, migration),
                        "startTime": "2026-08-16T11:01:00Z",
                    },
                )
            return httpx.Response(
                200,
                json={
                    **_status_payload(app_name, version, migration),
                    "startTime": (
                        ""
                        if failure_case == "empty-start-time"
                        else 42 if failure_case == "non-string-start-time" else old_start
                    ),
                },
            )
        if request.method == "GET" and request.url.path in FRESH_RESTORE_PATHS[plugin_key]:
            return httpx.Response(200, json=[])
        if request.method == "POST" and request.url.path.endswith("/system/backup/restore/upload"):
            if failure_case == "cancellation":
                raise asyncio.CancelledError
            if failure_case == "upload-timeout":
                raise httpx.ReadTimeout("synthetic restore timeout", request=request)
            return httpx.Response(
                200,
                json={"restartRequired": failure_case != "upload-response"},
            )
        if request.method == "POST" and request.url.path == "/api/v1/system/restart":
            return httpx.Response(
                200,
                json={"restarting": failure_case != "restart-response"},
            )
        raise AssertionError(f"Unexpected restore request: {request.method} {request.url}")

    _install_transport(monkeypatch, handler)
    plugin = get_plugin(plugin_key)
    setattr(plugin, "restore_deadline_seconds", 0.01)
    setattr(plugin, "restore_poll_interval_seconds", 0.0)
    operation = plugin.restore(_restore_context(plugin_key, artifact))

    if failure_case == "cancellation":
        with pytest.raises(asyncio.CancelledError):
            await operation
    elif failure_case in {
        "post-auth",
        "post-malformed",
        "same-start-time",
        "empty-start-time",
        "non-string-start-time",
    }:
        with pytest.raises(RuntimeError):
            await asyncio.wait_for(operation, timeout=1.0)
    else:
        with pytest.raises((RuntimeError, ValueError, httpx.HTTPError)):
            await operation

    assert {
        f"{request.url.scheme}://{request.url.host}:{request.url.port}" for request in requests
    } <= {RESTORE_ORIGINS[plugin_key]}
    assert not any(request.url.host == "attacker.invalid" for request in requests)
    if failure_case in {"pre-auth", "pre-malformed"}:
        assert not any(request.method == "POST" for request in requests)


@pytest.mark.parametrize(
    ("plugin_key", "app_name", "version", "migration", "default_url"),
    SERVICE_CONTRACTS,
)
def test_restore_service_stages_verified_artifact_and_records_successful_audit(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    plugin_key: str,
    app_name: str,
    version: str,
    migration: int,
    default_url: str,
) -> None:
    source = Target(
        name=f"{app_name} Source",
        slug=f"{plugin_key}-source",
        plugin_name=plugin_key,
        plugin_config_json=json.dumps(_source_config(plugin_key, default_url)),
    )
    destination = Target(
        name=f"{app_name} Isolated Restore",
        slug=f"{plugin_key}-isolated-restore",
        plugin_name=plugin_key,
        plugin_config_json=json.dumps(_restore_config(plugin_key)),
    )
    db_session.add_all([source, destination])
    db_session.commit()
    artifact_directory = tmp_path / source.slug / "2026-08-16"
    artifact_directory.mkdir(parents=True)
    artifact = artifact_directory / f"{plugin_key}-service-restore.zip"
    artifact.write_bytes(
        _native_backup_zip(
            tmp_path,
            plugin_key=plugin_key,
            version=version,
            migration=migration,
        )
    )
    plugin = get_plugin(plugin_key)
    write_backup_sidecar(
        str(artifact),
        plugin,
        BackupContext(
            job_id=f"{plugin_key}-source-run",
            target_id=str(source.id),
            config=_source_config(plugin_key, default_url),
            metadata={"target_slug": source.slug},
        ),
    )
    artifact_size = artifact.stat().st_size
    artifact_sha = _sha256_file(artifact)
    artifact_inode = artifact.stat().st_ino
    source_run = Run(
        status="success",
        operation="backup",
        started_at=datetime.now(timezone.utc),
        finished_at=datetime.now(timezone.utc),
    )
    db_session.add(source_run)
    db_session.commit()
    source_target_run = TargetRun(
        run_id=source_run.id,
        target_id=source.id,
        status="success",
        operation="backup",
        artifact_path=str(artifact),
        artifact_bytes=artifact_size,
        sha256=artifact_sha,
        source_identity_json=json.dumps(
            {
                "database_backend": "sqlite",
                "database_migration": migration,
            }
        ),
        started_at=source_run.started_at,
        finished_at=source_run.finished_at,
    )
    db_session.add(source_target_run)
    db_session.commit()
    _authorize_isolated_restore(monkeypatch, plugin_key)
    monkeypatch.setenv("BACKUP_BASE_PATH", str(tmp_path))
    status_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal status_calls
        if request.method == "GET" and request.url.path == "/api/v1/system/status":
            status_calls += 1
            return httpx.Response(
                200,
                json={
                    **_status_payload(app_name, version, migration),
                    "startTime": (
                        "2026-08-16T12:00:00Z" if status_calls == 1 else "2026-08-16T12:01:00Z"
                    ),
                },
            )
        if request.method == "GET" and request.url.path in FRESH_RESTORE_PATHS[plugin_key]:
            return httpx.Response(200, json=[])
        if request.method == "POST" and request.url.path.endswith("/system/backup/restore/upload"):
            assert request.content.count(b'name="file"') == 1
            return httpx.Response(200, json={"restartRequired": True})
        if request.method == "POST" and request.url.path == "/api/v1/system/restart":
            return httpx.Response(200, json={"restarting": True})
        raise AssertionError(f"Unexpected restore request: {request.method} {request.url}")

    _install_transport(monkeypatch, handler)
    observed: dict[str, object] = {}
    real_restore = plugin.restore

    async def observe_real_restore(context: RestoreContext) -> dict[str, Any]:
        staged = Path(context.artifact_path)
        observed["path"] = staged
        observed["inode"] = staged.stat().st_ino
        observed["bytes"] = staged.stat().st_size
        observed["sha256"] = _sha256_file(staged)
        observed["metadata"] = dict(context.metadata or {})
        return cast(dict[str, Any], await real_restore(context))

    monkeypatch.setattr(plugin, "restore", observe_real_restore)
    monkeypatch.setattr("app.services.restores.get_plugin", lambda _name: plugin)
    setattr(plugin, "restore_deadline_seconds", 0.05)
    setattr(plugin, "restore_poll_interval_seconds", 0.0)

    result = RestoreService(db_session).restore(
        source_target_run_id=source_target_run.id,
        destination_target_id=destination.id,
        triggered_by=f"isolated_{plugin_key}_restore_test",
    )

    assert result.status == "success"
    assert result.operation == "restore"
    assert result.finished_at is not None
    assert f"isolated_{plugin_key}_restore_test" in (result.logs_text or "")
    assert len(result.target_runs) == 1
    audited = result.target_runs[0]
    assert audited.status == "success"
    assert audited.operation == "restore"
    assert audited.target_id == destination.id
    assert audited.artifact_path == str(artifact)
    assert audited.artifact_bytes == artifact_size
    assert audited.sha256 == artifact_sha
    assert audited.finished_at is not None

    staged = observed["path"]
    assert isinstance(staged, Path)
    assert staged != artifact
    assert observed["inode"] != artifact_inode
    assert observed["bytes"] == artifact_size
    assert observed["sha256"] == artifact_sha
    assert staged.exists() is False
    metadata = observed["metadata"]
    assert isinstance(metadata, dict)
    assert metadata["artifact_bytes"] == artifact_size
    assert metadata["artifact_sha256"] == artifact_sha
    assert metadata["source_target_id"] == source.id
    assert metadata["source_target_slug"] == source.slug
    assert not list(artifact_directory.glob(".homelab-backup-restore-*"))
    assert artifact.stat().st_ino == artifact_inode
    assert _sha256_file(artifact) == artifact_sha
