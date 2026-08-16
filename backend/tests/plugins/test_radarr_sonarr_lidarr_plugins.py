from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import sqlite3
import stat
import threading
import zipfile
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Generator, cast

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.db import get_session
from app.core.plugins import servarr as servarr_module
from app.core.plugins.base import BackupContext, RestoreContext
from app.core.plugins.loader import get_plugin
from app.core.plugins.sidecar import write_backup_sidecar
from app.main import app
from app.models import Run, Target, TargetRun
from app.services.restores import RestoreService

SERVICE_CONTRACTS = (
    pytest.param(
        "radarr",
        "http://radarr.local:7878",
        "/sources/radarr/backups",
        id="radarr-6.3.0.10514",
    ),
    pytest.param(
        "sonarr",
        "http://sonarr.local:8989",
        "/sources/sonarr/backups",
        id="sonarr-4.0.19.2979",
    ),
    pytest.param(
        "lidarr",
        "http://lidarr.local:8686",
        "/sources/lidarr/backups",
        id="lidarr-3.1.0.4875",
    ),
)


@dataclass(frozen=True)
class _IdentityContract:
    plugin_key: str
    app_name: str
    api_prefix: str
    version: str
    package_version: str
    migration: int
    base_url: str


class _NoWorkerResultConnection:
    def __init__(self) -> None:
        self.closed = False

    def recv(self) -> object:
        raise EOFError("No worker result is available")

    def close(self) -> None:
        self.closed = True


class _BlockingWorkerProcess:
    def __init__(self) -> None:
        self.exitcode: int | None = None
        self.join_started = threading.Event()
        self.terminate_called = threading.Event()
        self.kill_called = threading.Event()
        self.release = threading.Event()
        self.join_calls = 0
        self._alive = True

    def join(self, timeout: float) -> None:
        self.join_calls += 1
        self.join_started.set()
        if self.release.wait(timeout):
            self._alive = False
            self.exitcode = -9 if self.kill_called.is_set() else -15

    def is_alive(self) -> bool:
        return self._alive

    def terminate(self) -> None:
        self.terminate_called.set()

    def kill(self) -> None:
        self.kill_called.set()
        self.release.set()


RADARR_IDENTITY = _IdentityContract(
    plugin_key="radarr",
    app_name="Radarr",
    api_prefix="/api/v3",
    version="6.3.0.10514",
    package_version="6.3.0.10514-ls313",
    migration=242,
    base_url="http://radarr.local:7878",
)


IDENTITY_CONTRACTS = (
    pytest.param(
        RADARR_IDENTITY,
        id="radarr-6.3.0.10514",
    ),
    pytest.param(
        _IdentityContract(
            plugin_key="sonarr",
            app_name="Sonarr",
            api_prefix="/api/v3",
            version="4.0.19.2979",
            package_version="4.0.19.2979-ls320",
            migration=217,
            base_url="http://sonarr.local:8989",
        ),
        id="sonarr-4.0.19.2979",
    ),
    pytest.param(
        _IdentityContract(
            plugin_key="lidarr",
            app_name="Lidarr",
            api_prefix="/api/v1",
            version="3.1.0.4875",
            package_version="3.1.0.4875-ls38",
            migration=80,
            base_url="http://lidarr.local:8686",
        ),
        id="lidarr-3.1.0.4875",
    ),
)

REQUIRED_TABLES = {
    "radarr": (
        "Config",
        "RootFolders",
        "Indexers",
        "DownloadClients",
        "Notifications",
        "Tags",
        "Movies",
        "MovieMetadata",
        "MovieFiles",
        "QualityProfiles",
        "CustomFormats",
        "ImportLists",
        "History",
    ),
    "sonarr": (
        "Config",
        "RootFolders",
        "Indexers",
        "DownloadClients",
        "Notifications",
        "Tags",
        "Series",
        "Episodes",
        "EpisodeFiles",
        "QualityProfiles",
        "CustomFormats",
        "ImportLists",
        "History",
    ),
    "lidarr": (
        "Config",
        "RootFolders",
        "Indexers",
        "DownloadClients",
        "Notifications",
        "Tags",
        "Artists",
        "ArtistMetadata",
        "Albums",
        "AlbumReleases",
        "Tracks",
        "TrackFiles",
        "QualityProfiles",
        "MetadataProfiles",
        "CustomFormats",
        "ImportLists",
        "History",
    ),
}

FRESH_RESOURCE_PATHS = {
    "radarr": ("tag", "rootfolder", "indexer", "downloadclient", "notification", "movie"),
    "sonarr": ("tag", "rootfolder", "indexer", "downloadclient", "notification", "series"),
    "lidarr": ("tag", "rootfolder", "indexer", "downloadclient", "notification", "artist"),
}


class _DummyScheduler:
    def start(self) -> None:
        return None

    def shutdown(self) -> None:
        return None


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


def _native_archive(
    tmp_path: Path,
    *,
    plugin_key: str,
    version: str,
    migration: int,
    include_required_tables: bool = True,
    info_bytes: bytes | None = None,
    tag_labels: tuple[str, ...] = (),
    extra_schema_objects: int = 0,
) -> bytes:
    database_path = tmp_path / f"{plugin_key}-fixture.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute('CREATE TABLE "VersionInfo" ("Version" INTEGER NOT NULL)')
        connection.execute(
            'INSERT INTO "VersionInfo" ("Version") VALUES (?)',
            (migration,),
        )
        if include_required_tables:
            for table_name in REQUIRED_TABLES[plugin_key]:
                if table_name == "Tags":
                    connection.execute(
                        'CREATE TABLE "Tags" ' '("Id" INTEGER PRIMARY KEY, "Label" TEXT NOT NULL)'
                    )
                    connection.executemany(
                        'INSERT INTO "Tags" ("Label") VALUES (?)',
                        ((label,) for label in tag_labels),
                    )
                else:
                    connection.execute(f'CREATE TABLE "{table_name}" ("Id" INTEGER PRIMARY KEY)')
        for index in range(extra_schema_objects):
            connection.execute(
                f'CREATE VIEW "SyntheticBound{index}" AS SELECT "Version" FROM "VersionInfo"'
            )
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "config.xml",
            b"<Config><ApiKey>restored-synthetic-key</ApiKey></Config>",
        )
        archive.writestr(
            "INFO",
            info_bytes or f"v{version}\n2026-08-16 12:00:00\n".encode(),
        )
        archive.write(database_path, arcname=f"{plugin_key}.db")
    database_path.unlink()
    return payload.getvalue()


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("plugin_key", "base_url", "backup_directory"),
    SERVICE_CONTRACTS,
)
async def test_exact_servarr_trio_requires_fixed_native_backup_mount(
    plugin_key: str,
    base_url: str,
    backup_directory: str,
) -> None:
    plugin = get_plugin(plugin_key)
    exact_config = {
        "base_url": base_url,
        "api_key": "synthetic-api-key",
        "backup_directory": backup_directory,
    }

    assert await plugin.validate_config(exact_config)
    assert not await plugin.validate_config({"base_url": base_url, "api_key": "synthetic-api-key"})
    assert not await plugin.validate_config(
        {**exact_config, "backup_directory": f"{backup_directory}/nested"}
    )
    assert not await plugin.validate_config({**exact_config, "api_key": " synthetic-api-key"})
    assert not await plugin.validate_config({**exact_config, "api_key": "synthetic-api-key "})
    for unsafe_key in (
        "synthetic api key",
        "synthetic\napi-key",
        "synthetic\x7fapi-key",
    ):
        assert not await plugin.validate_config({**exact_config, "api_key": unsafe_key})


@pytest.mark.parametrize(
    ("plugin_key", "base_url", "backup_directory"),
    SERVICE_CONTRACTS,
)
def test_public_schema_exposes_only_exact_servarr_trio_contract(
    api_client: TestClient,
    plugin_key: str,
    base_url: str,
    backup_directory: str,
) -> None:
    response = api_client.get(f"/api/v1/plugins/{plugin_key}/schema")

    assert response.status_code == 200
    assert response.json() == {
        "type": "object",
        "additionalProperties": False,
        "required": ["base_url", "api_key", "backup_directory"],
        "properties": {
            "base_url": {
                "type": "string",
                "title": "Base URL",
                "default": base_url,
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
                "default": backup_directory,
                "const": backup_directory,
                "minLength": 1,
                "pattern": "^/.*",
            },
        },
    }


@pytest.mark.parametrize(
    ("plugin_key", "base_url", "backup_directory"),
    SERVICE_CONTRACTS,
)
def test_public_discovery_and_target_api_persist_exact_automatic_contract(
    api_client: TestClient,
    plugin_key: str,
    base_url: str,
    backup_directory: str,
) -> None:
    discovery = api_client.get("/api/v1/plugins/")
    assert discovery.status_code == 200
    entry = next(item for item in discovery.json() if item["key"] == plugin_key)
    assert entry["restore_capability"] == "automatic"

    config = {
        "base_url": base_url,
        "api_key": "synthetic-api-key",
        "backup_directory": backup_directory,
    }
    response = api_client.post(
        "/api/v1/targets/",
        json={
            "name": f"{plugin_key} exact target",
            "plugin_name": plugin_key,
            "plugin_config_json": json.dumps(config, sort_keys=True),
        },
    )
    assert response.status_code == 201, response.text
    assert json.loads(response.json()["plugin_config_json"]) == config

    invalid = api_client.post(
        "/api/v1/targets/",
        json={
            "name": f"{plugin_key} mount-less target",
            "plugin_name": plugin_key,
            "plugin_config_json": json.dumps(
                {"base_url": base_url, "api_key": "synthetic-api-key"}
            ),
        },
    )
    assert invalid.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize("contract", IDENTITY_CONTRACTS)
async def test_probe_rejects_wrong_exact_package_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    contract: _IdentityContract,
) -> None:
    plugin_key = contract.plugin_key
    app_name = contract.app_name
    api_prefix = contract.api_prefix
    version = contract.version
    package_version = contract.package_version
    migration = contract.migration
    base_url = contract.base_url
    plugin = get_plugin(plugin_key)
    backup_directory = tmp_path / "native-backups"
    backup_directory.mkdir()
    monkeypatch.setattr(type(plugin), "native_backup_mount", backup_directory)
    monkeypatch.setattr(
        os.path,
        "ismount",
        lambda path: Path(path) == backup_directory,
    )
    monkeypatch.setattr(
        os,
        "statvfs",
        lambda path: SimpleNamespace(f_flag=os.ST_RDONLY),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"{api_prefix}/system/status":
            return httpx.Response(
                200,
                json={
                    "appName": app_name,
                    "version": version,
                    "packageVersion": f"wrong-{package_version}",
                    "databaseType": "sqlite",
                    "migrationVersion": migration,
                    "startTime": "2026-08-16T00:00:00Z",
                },
            )
        if request.url.path == f"{api_prefix}/system/backup":
            return httpx.Response(200, json=[])
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    _install_transport(monkeypatch, handler)

    with pytest.raises(RuntimeError, match="package"):
        await plugin.test(
            {
                "base_url": base_url,
                "api_key": "synthetic-api-key",
                "backup_directory": str(backup_directory),
            }
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("contract", IDENTITY_CONTRACTS)
async def test_probe_requires_parseable_process_start_time(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    contract: _IdentityContract,
) -> None:
    plugin_key = contract.plugin_key
    app_name = contract.app_name
    api_prefix = contract.api_prefix
    version = contract.version
    package_version = contract.package_version
    migration = contract.migration
    base_url = contract.base_url
    plugin = get_plugin(plugin_key)
    backup_directory = tmp_path / "native-backups"
    backup_directory.mkdir()
    monkeypatch.setattr(type(plugin), "native_backup_mount", backup_directory)
    monkeypatch.setattr(os.path, "ismount", lambda path: Path(path) == backup_directory)
    monkeypatch.setattr(
        os,
        "statvfs",
        lambda path: SimpleNamespace(f_flag=os.ST_RDONLY),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"{api_prefix}/system/status":
            return httpx.Response(
                200,
                json={
                    "appName": app_name,
                    "version": version,
                    "packageVersion": package_version,
                    "databaseType": "sqlite",
                    "migrationVersion": migration,
                    "startTime": "not-a-time",
                },
            )
        if request.url.path == f"{api_prefix}/system/backup":
            return httpx.Response(200, json=[])
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    _install_transport(monkeypatch, handler)

    with pytest.raises(RuntimeError, match="start time"):
        await plugin.test(
            {
                "base_url": base_url,
                "api_key": "synthetic-api-key",
                "backup_directory": str(backup_directory),
            }
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("contract", IDENTITY_CONTRACTS)
async def test_probe_and_status_accept_exact_identity_and_safe_existing_native_entry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    contract: _IdentityContract,
) -> None:
    plugin_key = contract.plugin_key
    app_name = contract.app_name
    api_prefix = contract.api_prefix
    version = contract.version
    package_version = contract.package_version
    migration = contract.migration
    base_url = contract.base_url
    plugin = get_plugin(plugin_key)
    backup_directory = tmp_path / "native-backups"
    backup_directory.mkdir()
    monkeypatch.setattr(type(plugin), "native_backup_mount", backup_directory)
    monkeypatch.setattr(os.path, "ismount", lambda path: Path(path) == backup_directory)
    monkeypatch.setattr(
        os,
        "statvfs",
        lambda path: SimpleNamespace(f_flag=os.ST_RDONLY),
    )
    filename = f"{plugin_key}-existing.zip"
    (backup_directory / filename).write_bytes(b"not-read-by-probe")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["X-Api-Key"] == "synthetic-api-key"
        assert "apikey" not in request.url.params
        if request.url.path == f"{api_prefix}/system/status":
            return httpx.Response(
                200,
                json={
                    "appName": app_name,
                    "version": version,
                    "packageVersion": package_version,
                    "databaseType": "sqlite",
                    "migrationVersion": migration,
                    "startTime": "2026-08-16T00:00:00Z",
                },
            )
        if request.url.path == f"{api_prefix}/system/backup":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 61,
                        "name": filename,
                        "path": f"/backup/manual/{filename}",
                        "type": "manual",
                        "size": 17,
                        "time": "2026-08-16T00:00:00Z",
                    }
                ],
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    _install_transport(monkeypatch, handler)
    config = {
        "base_url": base_url,
        "api_key": "synthetic-api-key",
        "backup_directory": str(backup_directory),
    }

    assert await plugin.test(config) is True
    assert await plugin.get_status(
        BackupContext(
            job_id="job-022-status",
            target_id=f"target-{plugin_key}",
            config=config,
        )
    ) == {"status": "ok"}
    assert [(request.method, request.url.path) for request in requests] == [
        ("GET", f"{api_prefix}/system/status"),
        ("GET", f"{api_prefix}/system/backup"),
        ("GET", f"{api_prefix}/system/status"),
        ("GET", f"{api_prefix}/system/backup"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("contract", IDENTITY_CONTRACTS)
@pytest.mark.parametrize(
    ("backup_name", "backup_path"),
    (
        ("unsafe.zip", "/backup/manual/nested/unsafe.zip"),
        ("unsafe\\name.zip", "/backup/manual/unsafe\\name.zip"),
        ("unsafe\nname.zip", "/backup/manual/unsafe\nname.zip"),
    ),
)
async def test_probe_rejects_noncanonical_native_backup_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    contract: _IdentityContract,
    backup_name: str,
    backup_path: str,
) -> None:
    plugin_key = contract.plugin_key
    app_name = contract.app_name
    api_prefix = contract.api_prefix
    version = contract.version
    package_version = contract.package_version
    migration = contract.migration
    base_url = contract.base_url
    plugin = get_plugin(plugin_key)
    backup_directory = tmp_path / "native-backups"
    backup_directory.mkdir()
    (backup_directory / "safe.zip").write_bytes(b"safe-path-fixture")
    (backup_directory / backup_name).write_bytes(b"unsafe-path-fixture")
    monkeypatch.setattr(type(plugin), "native_backup_mount", backup_directory)
    monkeypatch.setattr(os.path, "ismount", lambda path: Path(path) == backup_directory)
    monkeypatch.setattr(
        os,
        "statvfs",
        lambda path: SimpleNamespace(f_flag=os.ST_RDONLY),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"{api_prefix}/system/status":
            return httpx.Response(
                200,
                json={
                    "appName": app_name,
                    "version": version,
                    "packageVersion": package_version,
                    "databaseType": "sqlite",
                    "migrationVersion": migration,
                    "startTime": "2026-08-16T00:00:00Z",
                },
            )
        if request.url.path == f"{api_prefix}/system/backup":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 61,
                        "name": "safe.zip",
                        "path": "/backup/manual/safe.zip",
                        "type": "manual",
                        "size": 17,
                        "time": "2026-08-16T00:00:00Z",
                    },
                    {
                        "id": 62,
                        "name": backup_name,
                        "path": backup_path,
                        "type": "manual",
                        "size": 19,
                        "time": "2026-08-16T00:00:00Z",
                    },
                ],
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    _install_transport(monkeypatch, handler)

    with pytest.raises(RuntimeError, match="unsafe path"):
        await plugin.test(
            {
                "base_url": base_url,
                "api_key": "synthetic-api-key",
                "backup_directory": str(backup_directory),
            }
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("contract", IDENTITY_CONTRACTS)
async def test_probe_rejects_backup_list_entries_with_extra_fields(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    contract: _IdentityContract,
) -> None:
    plugin = get_plugin(contract.plugin_key)
    backup_directory = tmp_path / "native-backups"
    backup_directory.mkdir()
    filename = "unexpected-shape.zip"
    (backup_directory / filename).write_bytes(b"unexpected-shape")
    monkeypatch.setattr(type(plugin), "native_backup_mount", backup_directory)
    monkeypatch.setattr(os.path, "ismount", lambda path: Path(path) == backup_directory)
    monkeypatch.setattr(
        os,
        "statvfs",
        lambda path: SimpleNamespace(f_flag=os.ST_RDONLY),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"{contract.api_prefix}/system/status":
            return httpx.Response(
                200,
                json={
                    "appName": contract.app_name,
                    "version": contract.version,
                    "packageVersion": contract.package_version,
                    "databaseType": "sqlite",
                    "migrationVersion": contract.migration,
                    "startTime": "2026-08-16T00:00:00Z",
                },
            )
        if request.url.path == f"{contract.api_prefix}/system/backup":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 63,
                        "name": filename,
                        "path": f"/backup/manual/{filename}",
                        "type": "manual",
                        "size": 16,
                        "time": "2026-08-16T00:00:00Z",
                        "unexpected": "field",
                    }
                ],
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    _install_transport(monkeypatch, handler)

    with pytest.raises(RuntimeError, match="backup list response is invalid"):
        await plugin.test(
            {
                "base_url": contract.base_url,
                "api_key": "synthetic-api-key",
                "backup_directory": str(backup_directory),
            }
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("contract", IDENTITY_CONTRACTS)
@pytest.mark.parametrize("case", ("collision", "size-mismatch"))
async def test_probe_binds_every_manual_entry_to_one_exact_native_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    contract: _IdentityContract,
    case: str,
) -> None:
    plugin = get_plugin(contract.plugin_key)
    backup_directory = tmp_path / "native-backups"
    backup_directory.mkdir()
    filename = "bound-native.zip"
    native_bytes = b"bound-native-file"
    (backup_directory / filename).write_bytes(native_bytes)
    monkeypatch.setattr(type(plugin), "native_backup_mount", backup_directory)
    monkeypatch.setattr(os.path, "ismount", lambda path: Path(path) == backup_directory)
    monkeypatch.setattr(
        os,
        "statvfs",
        lambda path: SimpleNamespace(f_flag=os.ST_RDONLY),
    )
    entry = {
        "id": 64,
        "name": filename,
        "path": f"/backup/manual/{filename}",
        "type": "manual",
        "size": len(native_bytes) + int(case == "size-mismatch"),
        "time": "2026-08-16T00:00:00Z",
    }
    backups = [entry]
    if case == "collision":
        backups.append(
            {
                **entry,
                "id": 65,
                "time": "2026-08-16T00:00:01Z",
            }
        )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"{contract.api_prefix}/system/status":
            return httpx.Response(
                200,
                json={
                    "appName": contract.app_name,
                    "version": contract.version,
                    "packageVersion": contract.package_version,
                    "databaseType": "sqlite",
                    "migrationVersion": contract.migration,
                    "startTime": "2026-08-16T00:00:00Z",
                },
            )
        if request.url.path == f"{contract.api_prefix}/system/backup":
            return httpx.Response(200, json=backups)
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    _install_transport(monkeypatch, handler)

    with pytest.raises(RuntimeError, match="collision|size"):
        await plugin.test(
            {
                "base_url": contract.base_url,
                "api_key": "synthetic-api-key",
                "backup_directory": str(backup_directory),
            }
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("plugin_key", "base_url", "backup_directory"),
    SERVICE_CONTRACTS,
)
async def test_probe_does_not_follow_cross_origin_redirect_with_api_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    plugin_key: str,
    base_url: str,
    backup_directory: str,
) -> None:
    del backup_directory
    plugin = get_plugin(plugin_key)
    mounted_directory = tmp_path / "native-backups"
    mounted_directory.mkdir()
    monkeypatch.setattr(type(plugin), "native_backup_mount", mounted_directory)
    monkeypatch.setattr(os.path, "ismount", lambda path: Path(path) == mounted_directory)
    monkeypatch.setattr(
        os,
        "statvfs",
        lambda path: SimpleNamespace(f_flag=os.ST_RDONLY),
    )
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(302, headers={"location": "https://attacker.invalid/collect"})

    _install_transport(monkeypatch, handler)

    with pytest.raises(RuntimeError, match="status 302"):
        await plugin.test(
            {
                "base_url": base_url,
                "api_key": "synthetic-api-key",
                "backup_directory": str(mounted_directory),
            }
        )
    assert len(requests) == 1
    assert requests[0].url.host != "attacker.invalid"


@pytest.mark.asyncio
@pytest.mark.parametrize("contract", IDENTITY_CONTRACTS)
async def test_backup_rejects_archive_missing_required_control_plane_tables(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    contract: _IdentityContract,
) -> None:
    caplog.set_level(logging.INFO)
    plugin_key = contract.plugin_key
    app_name = contract.app_name
    api_prefix = contract.api_prefix
    version = contract.version
    package_version = contract.package_version
    migration = contract.migration
    base_url = contract.base_url
    plugin = get_plugin(plugin_key)
    backup_directory = tmp_path / "native-backups"
    backup_directory.mkdir()
    monkeypatch.setattr(type(plugin), "native_backup_mount", backup_directory)
    monkeypatch.setattr(os.path, "ismount", lambda path: Path(path) == backup_directory)
    monkeypatch.setattr(
        os,
        "statvfs",
        lambda path: SimpleNamespace(f_flag=os.ST_RDONLY),
    )
    monkeypatch.setattr(plugin, "backup_root", str(tmp_path / "published"))
    archive_bytes = _native_archive(
        tmp_path,
        plugin_key=plugin_key,
        version=version,
        migration=migration,
        include_required_tables=False,
    )
    filename = f"{plugin_key}_backup_v{version}_2026.08.16_12.00.01.zip"
    candidate = {
        "id": 41,
        "name": filename,
        "path": f"/backup/manual/{filename}",
        "type": "manual",
        "size": len(archive_bytes),
        "time": (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat(),
    }
    list_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal list_calls
        if request.url.path == f"{api_prefix}/system/status":
            return httpx.Response(
                200,
                json={
                    "appName": app_name,
                    "version": version,
                    "packageVersion": package_version,
                    "databaseType": "sqlite",
                    "migrationVersion": migration,
                    "startTime": "2026-08-16T00:00:00Z",
                },
            )
        if request.method == "GET" and request.url.path == f"{api_prefix}/system/backup":
            list_calls += 1
            return httpx.Response(200, json=[] if list_calls == 1 else [candidate])
        if request.method == "POST" and request.url.path == f"{api_prefix}/command":
            (backup_directory / filename).write_bytes(archive_bytes)
            return httpx.Response(201, json={"id": 42})
        if request.url.path == f"{api_prefix}/command/42":
            return httpx.Response(
                200,
                json={"id": 42, "status": "completed", "result": "successful"},
            )
        if request.method == "DELETE":
            return httpx.Response(200)
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    _install_transport(monkeypatch, handler)

    with pytest.raises(RuntimeError, match="required database tables"):
        await plugin.backup(
            BackupContext(
                job_id="job-022-missing-tables",
                target_id=f"target-{plugin_key}",
                config={
                    "base_url": base_url,
                    "api_key": "synthetic-api-key",
                    "backup_directory": str(backup_directory),
                },
                metadata={"target_slug": f"{plugin_key}-drill"},
            )
        )
    assert f"{app_name} backup started" in caplog.text
    assert f"{app_name} backup failed" in caplog.text
    assert "duration_seconds" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("contract", IDENTITY_CONTRACTS)
async def test_restore_refuses_nonempty_destination_before_upload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    contract: _IdentityContract,
) -> None:
    plugin_key = contract.plugin_key
    app_name = contract.app_name
    api_prefix = contract.api_prefix
    version = contract.version
    package_version = contract.package_version
    migration = contract.migration
    base_url = contract.base_url
    plugin = get_plugin(plugin_key)
    destination_url = base_url.replace(".local", "-restore.local")
    artifact = tmp_path / f"{plugin_key}-backup.zip"
    artifact.write_bytes(
        _native_archive(
            tmp_path,
            plugin_key=plugin_key,
            version=version,
            migration=migration,
        )
    )
    monkeypatch.setenv("HOMELAB_BACKUP_ALLOW_ISOLATED_RESTORE", "1")
    monkeypatch.setenv(
        "HOMELAB_BACKUP_ISOLATED_RESTORE_ALLOWED_ORIGINS",
        destination_url,
    )
    mutation_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == f"{api_prefix}/system/status":
            return httpx.Response(
                200,
                json={
                    "appName": app_name,
                    "version": version,
                    "packageVersion": package_version,
                    "databaseType": "sqlite",
                    "migrationVersion": migration,
                    "startTime": "2026-08-16T00:00:00Z",
                },
            )
        if request.method == "GET" and request.url.path == (
            f"{api_prefix}/{FRESH_RESOURCE_PATHS[plugin_key][0]}"
        ):
            return httpx.Response(200, json=[{"id": 1}])
        if request.method != "GET":
            mutation_requests.append(request)
            return httpx.Response(500)
        return httpx.Response(200, json=[])

    _install_transport(monkeypatch, handler)
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()

    with pytest.raises(RuntimeError, match="not fresh and empty"):
        await plugin.restore(
            RestoreContext(
                job_id="job-022-nonfresh",
                source_target_id=f"source-{plugin_key}",
                destination_target_id=f"destination-{plugin_key}",
                config={
                    "base_url": destination_url,
                    "api_key": "destination-synthetic-key",
                    "backup_directory": {
                        "radarr": "/sources/radarr/backups",
                        "sonarr": "/sources/sonarr/backups",
                        "lidarr": "/sources/lidarr/backups",
                    }[plugin_key],
                },
                artifact_path=str(artifact),
                metadata={
                    "artifact_bytes": artifact.stat().st_size,
                    "artifact_sha256": digest,
                },
            )
        )
    assert mutation_requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("contract", IDENTITY_CONTRACTS)
async def test_restore_rejects_info_without_exact_final_newline_before_network(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    contract: _IdentityContract,
) -> None:
    plugin_key = contract.plugin_key
    version = contract.version
    migration = contract.migration
    base_url = contract.base_url
    plugin = get_plugin(plugin_key)
    destination_url = base_url.replace(".local", "-restore.local")
    artifact = tmp_path / f"{plugin_key}-missing-info-newline.zip"
    artifact.write_bytes(
        _native_archive(
            tmp_path,
            plugin_key=plugin_key,
            version=version,
            migration=migration,
            info_bytes=f"v{version}\n2026-08-16 12:00:00".encode(),
        )
    )
    monkeypatch.setenv("HOMELAB_BACKUP_ALLOW_ISOLATED_RESTORE", "1")
    monkeypatch.setenv(
        "HOMELAB_BACKUP_ISOLATED_RESTORE_ALLOWED_ORIGINS",
        destination_url,
    )
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(500)

    _install_transport(monkeypatch, handler)
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()

    with pytest.raises(RuntimeError, match="INFO"):
        await plugin.restore(
            RestoreContext(
                job_id="job-022-info-newline",
                source_target_id=f"source-{plugin_key}",
                destination_target_id=f"destination-{plugin_key}",
                config={
                    "base_url": destination_url,
                    "api_key": "destination-synthetic-key",
                    "backup_directory": {
                        "radarr": "/sources/radarr/backups",
                        "sonarr": "/sources/sonarr/backups",
                        "lidarr": "/sources/lidarr/backups",
                    }[plugin_key],
                },
                artifact_path=str(artifact),
                metadata={
                    "artifact_bytes": artifact.stat().st_size,
                    "artifact_sha256": digest,
                },
            )
        )
    assert requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("contract", IDENTITY_CONTRACTS)
async def test_restore_bounds_sqlite_schema_inventory_before_network(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    contract: _IdentityContract,
) -> None:
    plugin = get_plugin(contract.plugin_key)
    destination_url = contract.base_url.replace(".local", "-restore.local")
    artifact = tmp_path / f"{contract.plugin_key}-schema-bound.zip"
    artifact.write_bytes(
        _native_archive(
            tmp_path,
            plugin_key=contract.plugin_key,
            version=contract.version,
            migration=contract.migration,
            extra_schema_objects=2050,
        )
    )
    monkeypatch.setenv("HOMELAB_BACKUP_ALLOW_ISOLATED_RESTORE", "1")
    monkeypatch.setenv(
        "HOMELAB_BACKUP_ISOLATED_RESTORE_ALLOWED_ORIGINS",
        destination_url,
    )
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(500)

    _install_transport(monkeypatch, handler)
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()

    with pytest.raises(RuntimeError, match="schema object"):
        await plugin.restore(
            RestoreContext(
                job_id="job-022-schema-bound",
                source_target_id=f"source-{contract.plugin_key}",
                destination_target_id=f"destination-{contract.plugin_key}",
                config={
                    "base_url": destination_url,
                    "api_key": "destination-synthetic-key",
                    "backup_directory": {
                        "radarr": "/sources/radarr/backups",
                        "sonarr": "/sources/sonarr/backups",
                        "lidarr": "/sources/lidarr/backups",
                    }[contract.plugin_key],
                },
                artifact_path=str(artifact),
                metadata={
                    "artifact_bytes": artifact.stat().st_size,
                    "artifact_sha256": digest,
                },
            )
        )
    assert requests == []


@pytest.mark.asyncio
async def test_restore_worker_timeout_kills_reaps_and_cleans_private_validation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    contract = RADARR_IDENTITY
    plugin = get_plugin(contract.plugin_key)
    destination_url = contract.base_url.replace(".local", "-restore.local")
    artifact = tmp_path / "radarr-worker-timeout.zip"
    artifact.write_bytes(
        _native_archive(
            tmp_path,
            plugin_key=contract.plugin_key,
            version=contract.version,
            migration=contract.migration,
        )
    )
    monkeypatch.setenv("HOMELAB_BACKUP_ALLOW_ISOLATED_RESTORE", "1")
    monkeypatch.setenv(
        "HOMELAB_BACKUP_ISOLATED_RESTORE_ALLOWED_ORIGINS",
        destination_url,
    )
    monkeypatch.setattr(servarr_module, "_RESTORE_WORKER_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(servarr_module, "_WORKER_STOP_TIMEOUT_SECONDS", 0.01)
    process = _BlockingWorkerProcess()
    connection = _NoWorkerResultConnection()
    validation_roots: list[Path] = []

    def start_blocked_worker(
        _plugin_name: str,
        _artifact_path: Path,
        _expected_size: int,
        _expected_sha256: str,
        validation_root: Path,
        _validation_identity: tuple[int, int],
    ) -> tuple[object, object]:
        (validation_root / "database.sqlite").write_bytes(b"private SQLite residue")
        validation_roots.append(validation_root)
        return process, connection

    monkeypatch.setattr(servarr_module, "_start_restore_process", start_blocked_worker)
    requests: list[httpx.Request] = []

    def reject_request(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(500)

    _install_transport(monkeypatch, reject_request)

    with pytest.raises(TimeoutError, match="worker timed out"):
        await plugin.restore(
            RestoreContext(
                job_id="job-022-restore-timeout",
                source_target_id="source-radarr",
                destination_target_id="destination-radarr",
                config={
                    "base_url": destination_url,
                    "api_key": "destination-synthetic-key",
                    "backup_directory": "/sources/radarr/backups",
                },
                artifact_path=str(artifact),
                metadata={
                    "artifact_bytes": artifact.stat().st_size,
                    "artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
                },
            )
        )

    assert process.terminate_called.is_set()
    assert process.kill_called.is_set()
    assert process.exitcode == -9
    assert process.join_calls >= 3
    assert connection.closed
    assert validation_roots and all(not root.exists() for root in validation_roots)
    assert requests == []


@pytest.mark.asyncio
async def test_restore_worker_repeated_cancellation_waits_for_reap_and_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    contract = RADARR_IDENTITY
    plugin = get_plugin(contract.plugin_key)
    destination_url = contract.base_url.replace(".local", "-restore.local")
    artifact = tmp_path / "radarr-worker-cancel.zip"
    artifact.write_bytes(
        _native_archive(
            tmp_path,
            plugin_key=contract.plugin_key,
            version=contract.version,
            migration=contract.migration,
        )
    )
    monkeypatch.setenv("HOMELAB_BACKUP_ALLOW_ISOLATED_RESTORE", "1")
    monkeypatch.setenv(
        "HOMELAB_BACKUP_ISOLATED_RESTORE_ALLOWED_ORIGINS",
        destination_url,
    )
    monkeypatch.setattr(servarr_module, "_RESTORE_WORKER_TIMEOUT_SECONDS", 30.0)
    monkeypatch.setattr(servarr_module, "_WORKER_STOP_TIMEOUT_SECONDS", 0.01)
    process = _BlockingWorkerProcess()
    connection = _NoWorkerResultConnection()
    validation_roots: list[Path] = []

    def start_blocked_worker(
        _plugin_name: str,
        _artifact_path: Path,
        _expected_size: int,
        _expected_sha256: str,
        validation_root: Path,
        _validation_identity: tuple[int, int],
    ) -> tuple[object, object]:
        (validation_root / "database.sqlite").write_bytes(b"private SQLite residue")
        validation_roots.append(validation_root)
        return process, connection

    monkeypatch.setattr(servarr_module, "_start_restore_process", start_blocked_worker)
    context = RestoreContext(
        job_id="job-022-restore-cancel",
        source_target_id="source-radarr",
        destination_target_id="destination-radarr",
        config={
            "base_url": destination_url,
            "api_key": "destination-synthetic-key",
            "backup_directory": "/sources/radarr/backups",
        },
        artifact_path=str(artifact),
        metadata={
            "artifact_bytes": artifact.stat().st_size,
            "artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
        },
    )
    task = asyncio.create_task(plugin.restore(context))
    assert await asyncio.to_thread(process.join_started.wait, 2)
    task.cancel()
    assert await asyncio.to_thread(process.terminate_called.wait, 2)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)
    assert process.kill_called.is_set()
    assert process.exitcode == -9
    assert connection.closed
    assert validation_roots and all(not root.exists() for root in validation_roots)


@pytest.mark.asyncio
async def test_restore_rejects_oversized_semantic_label_before_network(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    contract = RADARR_IDENTITY
    plugin = get_plugin(contract.plugin_key)
    destination_url = contract.base_url.replace(".local", "-restore.local")
    artifact = tmp_path / "radarr-oversized-label.zip"
    artifact.write_bytes(
        _native_archive(
            tmp_path,
            plugin_key=contract.plugin_key,
            version=contract.version,
            migration=contract.migration,
            tag_labels=("x" * 4097,),
        )
    )
    monkeypatch.setenv("HOMELAB_BACKUP_ALLOW_ISOLATED_RESTORE", "1")
    monkeypatch.setenv(
        "HOMELAB_BACKUP_ISOLATED_RESTORE_ALLOWED_ORIGINS",
        destination_url,
    )
    requests: list[httpx.Request] = []

    def reject_request(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(500)

    _install_transport(monkeypatch, reject_request)

    with pytest.raises(RuntimeError, match="semantic labels.*byte limit"):
        await plugin.restore(
            RestoreContext(
                job_id="job-022-label-bound",
                source_target_id="source-radarr",
                destination_target_id="destination-radarr",
                config={
                    "base_url": destination_url,
                    "api_key": "destination-synthetic-key",
                    "backup_directory": "/sources/radarr/backups",
                },
                artifact_path=str(artifact),
                metadata={
                    "artifact_bytes": artifact.stat().st_size,
                    "artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
                },
            )
        )
    assert requests == []


@pytest.mark.asyncio
async def test_restore_bounds_fresh_resource_response_before_mutation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    contract = RADARR_IDENTITY
    plugin = get_plugin(contract.plugin_key)
    destination_url = contract.base_url.replace(".local", "-restore.local")
    artifact = tmp_path / "radarr-resource-response-bound.zip"
    artifact.write_bytes(
        _native_archive(
            tmp_path,
            plugin_key=contract.plugin_key,
            version=contract.version,
            migration=contract.migration,
        )
    )
    monkeypatch.setenv("HOMELAB_BACKUP_ALLOW_ISOLATED_RESTORE", "1")
    monkeypatch.setenv(
        "HOMELAB_BACKUP_ISOLATED_RESTORE_ALLOWED_ORIGINS",
        destination_url,
    )
    monkeypatch.setattr(servarr_module, "_MAX_RESTORE_API_BYTES", 1)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == f"{contract.api_prefix}/system/status":
            return httpx.Response(
                200,
                json={
                    "appName": contract.app_name,
                    "version": contract.version,
                    "packageVersion": contract.package_version,
                    "databaseType": "sqlite",
                    "migrationVersion": contract.migration,
                    "startTime": "2026-08-16T00:00:00Z",
                },
            )
        if request.method == "GET":
            return httpx.Response(200, json=[])
        return httpx.Response(500)

    _install_transport(monkeypatch, handler)

    with pytest.raises(RuntimeError, match="response size limit"):
        await plugin.restore(
            RestoreContext(
                job_id="job-022-api-response-bound",
                source_target_id="source-radarr",
                destination_target_id="destination-radarr",
                config={
                    "base_url": destination_url,
                    "api_key": "destination-synthetic-key",
                    "backup_directory": "/sources/radarr/backups",
                },
                artifact_path=str(artifact),
                metadata={
                    "artifact_bytes": artifact.stat().st_size,
                    "artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
                },
            )
        )
    assert requests
    assert all(request.method == "GET" for request in requests)


@pytest.mark.asyncio
@pytest.mark.parametrize("contract", IDENTITY_CONTRACTS)
async def test_backup_publishes_exact_private_artifact_sidecar_and_cleans_native_copy(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    contract: _IdentityContract,
) -> None:
    caplog.set_level(logging.INFO)
    plugin_key = contract.plugin_key
    app_name = contract.app_name
    api_prefix = contract.api_prefix
    version = contract.version
    package_version = contract.package_version
    migration = contract.migration
    base_url = contract.base_url
    plugin = get_plugin(plugin_key)
    backup_directory = tmp_path / "native-backups"
    backup_directory.mkdir()
    monkeypatch.setattr(type(plugin), "native_backup_mount", backup_directory)
    monkeypatch.setattr(os.path, "ismount", lambda path: Path(path) == backup_directory)
    monkeypatch.setattr(
        os,
        "statvfs",
        lambda path: SimpleNamespace(f_flag=os.ST_RDONLY),
    )
    output_root = tmp_path / "published"
    monkeypatch.setattr(plugin, "backup_root", str(output_root))
    archive_bytes = _native_archive(
        tmp_path,
        plugin_key=plugin_key,
        version=version,
        migration=migration,
    )
    filename = f"{plugin_key}_backup_v{version}_2026.08.16_12.00.01.zip"
    native_path = backup_directory / filename
    candidate = {
        "id": 51,
        "name": filename,
        "path": f"/backup/manual/{filename}",
        "type": "manual",
        "size": len(archive_bytes),
        "time": (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat(),
    }
    list_calls = 0
    delete_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal delete_calls, list_calls
        assert request.headers["X-Api-Key"] == "synthetic-api-key"
        if request.url.path == f"{api_prefix}/system/status":
            return httpx.Response(
                200,
                json={
                    "appName": app_name,
                    "version": version,
                    "packageVersion": package_version,
                    "databaseType": "sqlite",
                    "migrationVersion": migration,
                    "startTime": "2026-08-16T00:00:00Z",
                },
            )
        if request.method == "GET" and request.url.path == f"{api_prefix}/system/backup":
            list_calls += 1
            return httpx.Response(200, json=[] if list_calls == 1 else [candidate])
        if request.method == "POST" and request.url.path == f"{api_prefix}/command":
            native_path.write_bytes(archive_bytes)
            return httpx.Response(201, json={"id": 52})
        if request.url.path == f"{api_prefix}/command/52":
            return httpx.Response(
                200,
                json={"id": 52, "status": "completed", "result": "successful"},
            )
        if request.method == "DELETE" and request.url.path == (f"{api_prefix}/system/backup/51"):
            published = list(output_root.rglob("*.zip"))
            assert len(published) == 1
            assert Path(f"{published[0]}.meta.json").is_file()
            native_path.unlink()
            delete_calls += 1
            return httpx.Response(200)
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    _install_transport(monkeypatch, handler)

    result = await plugin.backup(
        BackupContext(
            job_id="job-022-happy-backup",
            target_id=f"target-{plugin_key}",
            config={
                "base_url": base_url,
                "api_key": "synthetic-api-key",
                "backup_directory": str(backup_directory),
            },
            metadata={"target_slug": f"{plugin_key}-drill"},
        )
    )

    artifact = Path(result["artifact_path"])
    sidecar = Path(f"{artifact}.meta.json")
    assert artifact.read_bytes() == archive_bytes
    assert stat.S_IMODE(artifact.stat().st_mode) == 0o600
    assert stat.S_IMODE(sidecar.stat().st_mode) == 0o600
    sidecar_data = json.loads(sidecar.read_text(encoding="utf-8"))
    assert sidecar_data["package_version"] == package_version
    assert sidecar_data["artifact_bytes"] == len(archive_bytes)
    assert sidecar_data["sha256"] == hashlib.sha256(archive_bytes).hexdigest()
    serialized_sidecar = json.dumps(sidecar_data)
    assert "synthetic-api-key" not in serialized_sidecar
    assert base_url not in serialized_sidecar
    assert str(backup_directory) not in serialized_sidecar
    assert delete_calls == 1
    assert not native_path.exists()
    assert f"{app_name} backup started" in caplog.text
    assert f"{app_name} backup succeeded" in caplog.text
    assert str(artifact) in caplog.text
    assert "duration_seconds" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("contract", IDENTITY_CONTRACTS)
@pytest.mark.parametrize("content_matches", (True, False), ids=("match", "mismatch"))
async def test_restore_waits_for_restored_key_and_verifies_archive_content(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    contract: _IdentityContract,
    content_matches: bool,
) -> None:
    caplog.set_level(logging.INFO)
    plugin_key = contract.plugin_key
    app_name = contract.app_name
    api_prefix = contract.api_prefix
    version = contract.version
    package_version = contract.package_version
    migration = contract.migration
    base_url = contract.base_url
    plugin = get_plugin(plugin_key)
    destination_url = base_url.replace(".local", "-restore.local")
    restored_label = f"restored-{plugin_key}-marker"
    artifact = tmp_path / f"{plugin_key}-backup.zip"
    artifact.write_bytes(
        _native_archive(
            tmp_path,
            plugin_key=plugin_key,
            version=version,
            migration=migration,
            tag_labels=(restored_label,),
        )
    )
    monkeypatch.setenv("HOMELAB_BACKUP_ALLOW_ISOLATED_RESTORE", "1")
    monkeypatch.setenv(
        "HOMELAB_BACKUP_ISOLATED_RESTORE_ALLOWED_ORIGINS",
        destination_url,
    )
    monkeypatch.setattr(plugin, "restore_poll_interval_seconds", 0.0)
    observed_fresh_paths: list[str] = []
    status_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal status_calls
        if request.method == "GET" and request.url.path == f"{api_prefix}/system/status":
            status_calls += 1
            expected_key = (
                "destination-synthetic-key" if status_calls == 1 else "restored-synthetic-key"
            )
            assert request.headers["X-Api-Key"] == expected_key
            return httpx.Response(
                200,
                json={
                    "appName": app_name,
                    "version": version,
                    "packageVersion": package_version,
                    "databaseType": "sqlite",
                    "migrationVersion": migration,
                    "startTime": (
                        "2026-08-16T00:00:00Z" if status_calls == 1 else "2026-08-16T00:01:00Z"
                    ),
                },
            )
        if request.method == "GET" and request.url.path.startswith(f"{api_prefix}/"):
            observed_fresh_paths.append(request.url.path.removeprefix(f"{api_prefix}/"))
            if request.url.path == f"{api_prefix}/tag" and status_calls > 1:
                label = restored_label if content_matches else "unexpected-restored-marker"
                return httpx.Response(200, json=[{"id": 1, "label": label}])
            return httpx.Response(200, json=[])
        if request.method == "POST" and request.url.path == (
            f"{api_prefix}/system/backup/restore/upload"
        ):
            assert request.headers["X-Api-Key"] == "destination-synthetic-key"
            assert request.content.count(b'name="file"') == 1
            assert b"config.xml" in request.content
            return httpx.Response(200, json={"restartRequired": True})
        if request.method == "POST" and request.url.path == f"{api_prefix}/system/restart":
            assert request.headers["X-Api-Key"] == "destination-synthetic-key"
            return httpx.Response(200, json={"restarting": True})
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    _install_transport(monkeypatch, handler)
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()

    context = RestoreContext(
        job_id="job-022-happy-restore",
        source_target_id=f"source-{plugin_key}",
        destination_target_id=f"destination-{plugin_key}",
        config={
            "base_url": destination_url,
            "api_key": "destination-synthetic-key",
            "backup_directory": {
                "radarr": "/sources/radarr/backups",
                "sonarr": "/sources/sonarr/backups",
                "lidarr": "/sources/lidarr/backups",
            }[plugin_key],
        },
        artifact_path=str(artifact),
        metadata={
            "artifact_bytes": artifact.stat().st_size,
            "artifact_sha256": digest,
        },
    )

    if not content_matches:
        with pytest.raises(RuntimeError, match="restored content"):
            await plugin.restore(context)
        assert observed_fresh_paths == list(FRESH_RESOURCE_PATHS[plugin_key]) + ["tag"]
        assert status_calls == 2
        assert f"{app_name} restore started" in caplog.text
        assert f"{app_name} restore failed" in caplog.text
        assert "duration_seconds" in caplog.text
        return

    result = await plugin.restore(context)

    assert result["status"] == "success"
    assert result["artifact_path"] == str(artifact)
    assert result["artifact_bytes"] == artifact.stat().st_size
    assert observed_fresh_paths == list(FRESH_RESOURCE_PATHS[plugin_key]) * 2
    assert status_calls == 2
    assert f"{app_name} restore started" in caplog.text
    assert f"{app_name} restore succeeded" in caplog.text
    assert str(artifact) in caplog.text
    assert "duration_seconds" in caplog.text


@pytest.mark.parametrize("contract", IDENTITY_CONTRACTS)
def test_restore_service_stages_verified_artifact_and_records_successful_audit(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    contract: _IdentityContract,
) -> None:
    plugin_key = contract.plugin_key
    app_name = contract.app_name
    api_prefix = contract.api_prefix
    version = contract.version
    package_version = contract.package_version
    migration = contract.migration
    base_url = contract.base_url
    backup_directory = {
        "radarr": "/sources/radarr/backups",
        "sonarr": "/sources/sonarr/backups",
        "lidarr": "/sources/lidarr/backups",
    }[plugin_key]
    destination_url = base_url.replace(".local", "-restore.local")
    source_config = {
        "base_url": base_url,
        "api_key": "source-synthetic-key",
        "backup_directory": backup_directory,
    }
    destination_config = {
        "base_url": destination_url,
        "api_key": "destination-synthetic-key",
        "backup_directory": backup_directory,
    }
    source = Target(
        name=f"{app_name} Source",
        slug=f"{plugin_key}-source",
        plugin_name=plugin_key,
        plugin_config_json=json.dumps(source_config),
    )
    destination = Target(
        name=f"{app_name} Restore",
        slug=f"{plugin_key}-restore",
        plugin_name=plugin_key,
        plugin_config_json=json.dumps(destination_config),
    )
    db_session.add_all([source, destination])
    db_session.commit()
    artifact_directory = tmp_path / source.slug / "2026-08-16"
    artifact_directory.mkdir(parents=True)
    artifact = artifact_directory / f"{plugin_key}-backup.zip"
    artifact.write_bytes(
        _native_archive(
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
            job_id="job-022-source",
            target_id=str(source.id),
            config=source_config,
            metadata={"target_slug": source.slug},
        ),
    )
    artifact_size = artifact.stat().st_size
    artifact_sha256 = hashlib.sha256(artifact.read_bytes()).hexdigest()
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
        sha256=artifact_sha256,
        source_identity_json=json.dumps({"base_url": base_url}),
        started_at=source_run.started_at,
        finished_at=source_run.finished_at,
    )
    db_session.add(source_target_run)
    db_session.commit()
    monkeypatch.setenv("BACKUP_BASE_PATH", str(tmp_path))
    monkeypatch.setenv("HOMELAB_BACKUP_ALLOW_ISOLATED_RESTORE", "1")
    monkeypatch.setenv(
        "HOMELAB_BACKUP_ISOLATED_RESTORE_ALLOWED_ORIGINS",
        destination_url,
    )
    monkeypatch.setattr(plugin, "restore_poll_interval_seconds", 0.0)
    status_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal status_calls
        if request.method == "GET" and request.url.path == f"{api_prefix}/system/status":
            status_calls += 1
            return httpx.Response(
                200,
                json={
                    "appName": app_name,
                    "version": version,
                    "packageVersion": package_version,
                    "databaseType": "sqlite",
                    "migrationVersion": migration,
                    "startTime": (
                        "2026-08-16T00:00:00Z" if status_calls == 1 else "2026-08-16T00:01:00Z"
                    ),
                },
            )
        if request.method == "GET" and request.url.path.startswith(f"{api_prefix}/"):
            return httpx.Response(200, json=[])
        if request.method == "POST" and request.url.path.endswith("/system/backup/restore/upload"):
            return httpx.Response(200, json={"restartRequired": True})
        if request.method == "POST" and request.url.path.endswith("/system/restart"):
            return httpx.Response(200, json={"restarting": True})
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    _install_transport(monkeypatch, handler)
    observed: dict[str, object] = {}
    real_restore = plugin.restore

    async def observe_real_restore(context: RestoreContext) -> dict[str, Any]:
        staged = Path(context.artifact_path)
        observed["path"] = staged
        observed["inode"] = staged.stat().st_ino
        observed["mode"] = stat.S_IMODE(staged.stat().st_mode)
        observed["metadata"] = dict(context.metadata or {})
        return cast(dict[str, Any], await real_restore(context))

    monkeypatch.setattr(plugin, "restore", observe_real_restore)
    monkeypatch.setattr("app.services.restores.get_plugin", lambda _name: plugin)

    result = RestoreService(db_session).restore(
        source_target_run_id=source_target_run.id,
        destination_target_id=destination.id,
        triggered_by=f"isolated_{plugin_key}_restore_test",
    )

    assert result.status == "success"
    assert len(result.target_runs) == 1
    audited = result.target_runs[0]
    assert audited.status == "success"
    assert audited.operation == "restore"
    assert audited.artifact_path == str(artifact)
    assert audited.artifact_bytes == artifact_size
    assert audited.sha256 == artifact_sha256
    staged = observed["path"]
    assert isinstance(staged, Path)
    assert staged != artifact
    assert observed["inode"] != artifact_inode
    assert observed["mode"] == 0o600
    assert not staged.exists()
    metadata = observed["metadata"]
    assert isinstance(metadata, dict)
    assert metadata["artifact_bytes"] == artifact_size
    assert metadata["artifact_sha256"] == artifact_sha256
    assert metadata["source_database_identity"] == {"base_url": base_url}
    assert artifact.stat().st_ino == artifact_inode
    assert hashlib.sha256(artifact.read_bytes()).hexdigest() == artifact_sha256
    assert not list(artifact_directory.glob(".homelab-backup-restore-*"))
