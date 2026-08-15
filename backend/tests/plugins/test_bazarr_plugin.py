from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import os
import sqlite3
import stat
import threading
import warnings
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
import yaml  # type: ignore[import-untyped]
from sqlalchemy.orm import Session

from app.core.plugins.base import BackupContext, RestoreContext
from app.core.plugins.loader import get_plugin, get_plugin_schema_path, list_plugins
from app.core.plugins.sidecar import write_backup_sidecar
from app.main import app
from app.models import Run, Target, TargetRun
from app.services.restores import RestoreService

SOURCE_CONFIG: dict[str, object] = {
    "mode": "source",
    "base_url": "http://bazarr.local:6767",
    "api_key": "synthetic-bazarr-api-key",
    "backup_directory": "/sources/bazarr/backups",
}

RESTORE_DESTINATION_CONFIG: dict[str, object] = {
    "mode": "restore_destination",
    "restore_directory": "/tmp/homelab-backup-isolated-restore/bazarr-alpha",
}

RESTORE_SENTINEL_NAME = ".bazarr-restore-destination"
RESTORE_SENTINEL_CONTENT = "bazarr-v1.5.6-isolated-restore-v1\n"

STATUS_RESPONSE = {
    "data": {
        "bazarr_version": "1.5.6",
        "package_version": "v1.5.6-ls349 by linuxserver.io",
        "database_engine": "Sqlite 3.51.2",
        "database_migration": "df76a4410347",
        "timezone": "Etc/UTC",
    }
}

BACKUPS_RESPONSE = {
    "data": [
        {
            "date": "Aug 16 2026",
            "filename": "bazarr_backup_v1.5.6_2026.08.16_12.34.56.zip",
            "size": "12.3 KiB",
            "type": "backup",
        }
    ]
}

BASELINE_BACKUP_NAME = "bazarr_backup_v1.5.6_2026.08.15_12.34.56.zip"
NEW_BACKUP_NAME = "bazarr_backup_v1.5.6_2026.08.16_12.34.56.zip"
SECOND_NEW_BACKUP_NAME = "bazarr_backup_v1.5.6_2026.08.16_12.34.57.zip"

EXPECTED_BAZARR_TABLES = {
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

EXPECTED_CONFIG_SECTIONS = {
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


class _NoResultConnection:
    def __init__(self) -> None:
        self.closed = False

    def poll(self) -> bool:
        return False

    def recv(self) -> tuple[str, str, object]:
        raise AssertionError("No worker result is available")

    def close(self) -> None:
        self.closed = True


class _ResultConnection(_NoResultConnection):
    def __init__(self, result: tuple[str, str, object]) -> None:
        super().__init__()
        self.result = result

    def poll(self) -> bool:
        return True

    def recv(self) -> tuple[str, str, object]:
        return self.result


class _CompletedProcess:
    exitcode = 1

    def join(self, timeout: float) -> None:
        del timeout

    def is_alive(self) -> bool:
        return False


class _BlockingProcess:
    def __init__(self, *, hold_after_terminate: bool = False) -> None:
        self.exitcode: int | None = None
        self.join_started = threading.Event()
        self.terminate_called = threading.Event()
        self.kill_called = threading.Event()
        self.release = threading.Event()
        self._alive = True
        self._hold_after_terminate = hold_after_terminate

    def join(self, timeout: float) -> None:
        self.join_started.set()
        released = self.release.wait(timeout)
        if released and self.terminate_called.is_set():
            self._alive = False
            self.exitcode = -15

    def is_alive(self) -> bool:
        return self._alive

    def terminate(self) -> None:
        self.terminate_called.set()
        if not self._hold_after_terminate:
            self.release.set()

    def kill(self) -> None:
        self.terminate_called.set()
        self.kill_called.set()
        self.release.set()


def _plugin_class() -> type[Any]:
    plugin_class = importlib.import_module("app.plugins.bazarr.plugin").BazarrPlugin
    return cast(type[Any], plugin_class)


def _source_config(backup_directory: Path) -> dict[str, object]:
    return {**SOURCE_CONFIG, "backup_directory": str(backup_directory)}


def _backup_entry(filename: str) -> dict[str, str]:
    return {
        "date": "Aug 16 2026",
        "filename": filename,
        "size": "12.3 KiB",
        "type": "backup",
    }


def _backup_list(*filenames: str) -> dict[str, list[dict[str, str]]]:
    return {"data": [_backup_entry(filename) for filename in filenames]}


def _exact_database_bytes(
    path: Path,
    marker: str,
    *,
    tables: set[str] | None = None,
    migration: str = "df76a4410347",
    foreign_key_violation: bool = False,
) -> bytes:
    database_path = path.with_name(f".{path.name}.sqlite-fixture")
    selected_tables = tables if tables is not None else EXPECTED_BAZARR_TABLES
    with sqlite3.connect(database_path) as connection:
        for table in sorted(selected_tables):
            if table == "alembic_version":
                connection.execute("CREATE TABLE alembic_version (version_num TEXT PRIMARY KEY)")
            elif table == "system":
                connection.execute("CREATE TABLE system (id INTEGER PRIMARY KEY, marker TEXT)")
            elif table == "table_shows":
                connection.execute("CREATE TABLE table_shows (id INTEGER PRIMARY KEY)")
            elif table == "table_episodes":
                connection.execute(
                    "CREATE TABLE table_episodes "
                    "(id INTEGER PRIMARY KEY, show_id INTEGER REFERENCES table_shows(id))"
                )
            else:
                connection.execute(f'CREATE TABLE "{table}" (id INTEGER PRIMARY KEY)')
        if "alembic_version" in selected_tables:
            connection.execute("INSERT INTO alembic_version VALUES (?)", (migration,))
        if "system" in selected_tables:
            connection.execute("INSERT INTO system (marker) VALUES (?)", (marker,))
        if foreign_key_violation and "table_episodes" in selected_tables:
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute("INSERT INTO table_episodes VALUES (1, 999)")
        connection.commit()
    payload = database_path.read_bytes()
    database_path.unlink()
    return payload


def _exact_config(marker: str) -> dict[str, object]:
    config: dict[str, object] = {section: {} for section in EXPECTED_CONFIG_SECTIONS}
    config["general"] = {"instance_name": f"Synthetic Bazarr {marker}"}
    config["postgresql"] = {"enabled": False}
    return config


def _write_exact_native_zip(
    path: Path,
    marker: str,
    *,
    database: bytes | None = None,
    config: object | None = None,
) -> None:
    database_payload = database or _exact_database_bytes(path, marker)
    config_payload = yaml.safe_dump(
        _exact_config(marker) if config is None else config,
        sort_keys=True,
    ).encode()
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in (
            ("bazarr.db", database_payload),
            ("config.yaml", config_payload),
        ):
            info = zipfile.ZipInfo(name)
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o600) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, payload)


def _write_archive_members(
    path: Path,
    members: list[tuple[zipfile.ZipInfo | str, bytes]],
) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            for member, payload in members:
                if isinstance(member, str):
                    info = zipfile.ZipInfo(member)
                    info.create_system = 3
                    info.external_attr = (stat.S_IFREG | 0o600) << 16
                    info.compress_type = zipfile.ZIP_DEFLATED
                    member = info
                archive.writestr(member, payload)


def _valid_member_payloads(path: Path) -> tuple[bytes, bytes]:
    _write_exact_native_zip(path, "validation")
    with zipfile.ZipFile(path) as archive:
        return archive.read("bazarr.db"), archive.read("config.yaml")


def _patch_first_zip_flag(path: Path, *, offset: int, mask: int) -> None:
    payload = bytearray(path.read_bytes())
    local = payload.index(b"PK\x03\x04")
    central = payload.index(b"PK\x01\x02")
    local_flag = int.from_bytes(payload[local + offset : local + offset + 2], "little") | mask
    central_flag = (
        int.from_bytes(payload[central + offset + 2 : central + offset + 4], "little") | mask
    )
    payload[local + offset : local + offset + 2] = local_flag.to_bytes(2, "little")
    payload[central + offset + 2 : central + offset + 4] = central_flag.to_bytes(2, "little")
    path.write_bytes(payload)


def _streamed_bytes(path: Path) -> bytes:
    chunks: list[bytes] = []
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(64 * 1024), b""):
            chunks.append(chunk)
    return b"".join(chunks)


def _backup_context(backup_directory: Path, backup_root: Path) -> BackupContext:
    return BackupContext(
        job_id="bazarr-backup",
        target_id="bazarr-source",
        config=_source_config(backup_directory),
        metadata={"target_slug": "bazarr-source", "backup_root": str(backup_root)},
    )


def _prepare_restore_destination(
    tmp_path: Path,
    *,
    name: str = "bazarr-restore",
) -> tuple[Path, Path, Path]:
    parent = tmp_path / f"{name}-parent"
    parent.mkdir(mode=0o700)
    sentinel = parent / RESTORE_SENTINEL_NAME
    sentinel.write_text(RESTORE_SENTINEL_CONTENT, encoding="utf-8")
    sentinel.chmod(0o600)
    return parent, parent / name, sentinel


def _restore_context(artifact: Path, destination: Path) -> RestoreContext:
    return RestoreContext(
        job_id="bazarr-restore",
        source_target_id="bazarr-source",
        destination_target_id="bazarr-restore",
        config={
            "mode": "restore_destination",
            "restore_directory": str(destination),
        },
        artifact_path=str(artifact),
        metadata={
            "source_target_slug": "bazarr-source",
            "artifact_bytes": artifact.stat().st_size,
            "artifact_sha256": _sha256(artifact),
        },
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _configure_fast_backup_poll(
    monkeypatch: pytest.MonkeyPatch,
    backup_root: Path,
) -> None:
    plugin_module = importlib.import_module("app.plugins.bazarr.plugin")
    monkeypatch.setattr(plugin_module, "BACKUP_BASE_PATH", str(backup_root), raising=False)
    monkeypatch.setattr(plugin_module, "_BACKUP_DEADLINE_SECONDS", 1.0, raising=False)
    monkeypatch.setattr(plugin_module, "_POLL_INTERVAL_SECONDS", 0.0, raising=False)
    monkeypatch.setattr(plugin_module, "_STABILITY_OBSERVATIONS", 2, raising=False)


def _published_files(backup_root: Path) -> list[Path]:
    return [path for path in backup_root.rglob("*") if path.is_file()]


def _database_after_sql(path: Path, payload: bytes, statement: str) -> bytes:
    database_path = path.with_name(f".{path.name}.mutated.sqlite")
    database_path.write_bytes(payload)
    with sqlite3.connect(database_path) as connection:
        connection.executescript(statement)
        connection.commit()
    result = database_path.read_bytes()
    database_path.unlink()
    return result


def _write_malformed_native_zip(path: Path, case: str) -> None:
    database, config = _valid_member_payloads(path)
    if case == "missing-member":
        _write_archive_members(path, [("bazarr.db", database)])
    elif case == "duplicate-member":
        _write_archive_members(
            path,
            [("bazarr.db", database), ("config.yaml", config), ("config.yaml", config)],
        )
    elif case == "extra-member":
        _write_archive_members(
            path,
            [("bazarr.db", database), ("config.yaml", config), ("extra", b"no")],
        )
    elif case in {"nested-member", "absolute-member", "traversal-member"}:
        config_name = {
            "nested-member": "nested/config.yaml",
            "absolute-member": "/config.yaml",
            "traversal-member": "../config.yaml",
        }[case]
        _write_archive_members(path, [("bazarr.db", database), (config_name, config)])
    elif case in {"link-member", "device-member"}:
        unsafe = zipfile.ZipInfo("config.yaml")
        unsafe.create_system = 3
        unsafe.external_attr = (
            (stat.S_IFLNK | 0o777) if case == "link-member" else (stat.S_IFCHR | 0o600)
        ) << 16
        _write_archive_members(path, [("bazarr.db", database), (unsafe, b"target")])
    elif case == "encrypted-member":
        _patch_first_zip_flag(path, offset=6, mask=0x1)
    elif case == "crc":
        payload = bytearray(path.read_bytes())
        local = payload.index(b"PK\x03\x04")
        central = payload.index(b"PK\x01\x02")
        payload[local + 14 : local + 18] = b"\x00\x00\x00\x00"
        payload[central + 16 : central + 20] = b"\x00\x00\x00\x00"
        path.write_bytes(payload)
    elif case == "trailing-data":
        with path.open("ab") as archive:
            archive.write(b"ambiguous-secret-trailer")
    elif case in {"unsafe-yaml", "yaml-structure", "postgresql"}:
        if case == "unsafe-yaml":
            config = b"!!python/object/apply:os.system ['must-not-run']\n"
        else:
            parsed = _exact_config(case)
            if case == "yaml-structure":
                parsed.pop("auth")
            else:
                parsed["postgresql"] = {"enabled": True}
            config = yaml.safe_dump(parsed, sort_keys=True).encode()
        _write_archive_members(path, [("bazarr.db", database), ("config.yaml", config)])
    elif case in {
        "sqlite-header",
        "sqlite-integrity",
        "sqlite-foreign-key",
        "sqlite-table-set",
        "sqlite-migration",
        "sqlite-temp-residue",
        "sqlite-row-count",
    }:
        if case == "sqlite-header":
            database = b"not a sqlite database"
        elif case == "sqlite-integrity":
            corrupted = bytearray(database)
            corrupted[100:116] = b"\xff" * 16
            database = bytes(corrupted)
        elif case == "sqlite-foreign-key":
            database = _exact_database_bytes(path, case, foreign_key_violation=True)
        elif case == "sqlite-table-set":
            database = _exact_database_bytes(
                path,
                case,
                tables=EXPECTED_BAZARR_TABLES - {"table_history"},
            )
        elif case == "sqlite-migration":
            database = _exact_database_bytes(path, case, migration="unexpected-head")
        elif case == "sqlite-temp-residue":
            database = _exact_database_bytes(
                path,
                case,
                tables=EXPECTED_BAZARR_TABLES | {"_alembic_tmp_table_shows"},
            )
        else:
            database = _database_after_sql(
                path,
                database,
                ";".join(
                    f"INSERT INTO system (marker) VALUES ('row-{index}')" for index in range(20)
                ),
            )
        _write_archive_members(path, [("bazarr.db", database), ("config.yaml", config)])
    elif case == "ratio-limit":
        parsed = _exact_config(case)
        parsed["general"] = {"padding": "0" * (1024 * 1024)}
        _write_archive_members(
            path,
            [
                ("bazarr.db", database),
                ("config.yaml", yaml.safe_dump(parsed, sort_keys=True).encode()),
            ],
        )


def _install_single_candidate_transport(
    monkeypatch: pytest.MonkeyPatch,
    backup_directory: Path,
    writer: Any,
) -> None:
    triggered = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal triggered
        if request.method == "GET" and request.url.path == "/api/system/status":
            return httpx.Response(200, json=STATUS_RESPONSE)
        if request.method == "POST" and request.url.path == "/api/system/backups":
            assert not triggered
            triggered = True
            writer(backup_directory / NEW_BACKUP_NAME)
            return httpx.Response(204)
        if request.method == "GET" and request.url.path == "/api/system/backups":
            names = (
                (BASELINE_BACKUP_NAME, NEW_BACKUP_NAME) if triggered else (BASELINE_BACKUP_NAME,)
            )
            return httpx.Response(200, json=_backup_list(*names))
        raise AssertionError(f"Unexpected Bazarr request: {request.method} {request.url}")

    _install_http_transport(monkeypatch, handler)


def _install_http_transport(
    monkeypatch: pytest.MonkeyPatch,
    handler: Any,
) -> None:
    transport = httpx.MockTransport(handler)
    original_client = httpx.AsyncClient

    def client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return original_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)


def _install_read_only_mount(
    monkeypatch: pytest.MonkeyPatch,
    backup_directory: Path,
    *,
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
    return observed


@pytest.fixture
def anyio_backend() -> tuple[str, dict[str, bool]]:
    return ("asyncio", {"use_uvloop": True})


@pytest.fixture(autouse=True)
def isolated_bazarr_restore_network(monkeypatch: pytest.MonkeyPatch) -> None:
    plugin_module = importlib.import_module("app.plugins.bazarr.plugin")
    monkeypatch.setattr(plugin_module, "_network_interfaces", lambda: {"lo"})


@pytest.mark.asyncio
async def test_bazarr_discovery_schema_and_partial_restore_contract() -> None:
    plugin_class = _plugin_class()
    plugin = get_plugin("bazarr")

    assert isinstance(plugin, plugin_class)
    assert plugin.restore_capability == "partial"
    assert any(
        item["key"] == "bazarr" and item["restore_capability"] == "partial"
        for item in list_plugins()
    )

    schema_path = get_plugin_schema_path("bazarr")
    assert schema_path is not None
    schema = json.loads(Path(schema_path).read_text(encoding="utf-8"))
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["mode"]
    assert set(schema["properties"]) == {
        "mode",
        "base_url",
        "api_key",
        "backup_directory",
        "restore_directory",
    }
    assert schema["properties"]["mode"]["enum"] == [
        "source",
        "restore_destination",
    ]
    assert schema["properties"]["mode"]["default"] == "source"
    assert all(
        schema["properties"][key]["type"] == "string"
        for key in ("base_url", "api_key", "backup_directory", "restore_directory")
    )
    assert schema["properties"]["base_url"]["format"] == "uri"
    assert "default" not in schema["properties"]["api_key"]
    required_by_mode = {
        branch["if"]["properties"]["mode"]["const"]: set(branch["then"]["required"])
        for branch in schema["allOf"]
    }
    assert required_by_mode == {
        "source": {"base_url", "api_key", "backup_directory"},
        "restore_destination": {"restore_directory"},
    }


@pytest.mark.asyncio
async def test_bazarr_configuration_is_strict_and_mode_aware() -> None:
    plugin = _plugin_class()(name="bazarr")

    assert await plugin.validate_config(dict(SOURCE_CONFIG)) is True
    assert await plugin.validate_config(dict(RESTORE_DESTINATION_CONFIG)) is True

    invalid_configs: tuple[object, ...] = (
        None,
        [],
        {},
        {key: value for key, value in SOURCE_CONFIG.items() if key != "mode"},
        {**SOURCE_CONFIG, "mode": "legacy"},
        {key: value for key, value in SOURCE_CONFIG.items() if key != "base_url"},
        {key: value for key, value in SOURCE_CONFIG.items() if key != "api_key"},
        {key: value for key, value in SOURCE_CONFIG.items() if key != "backup_directory"},
        {**SOURCE_CONFIG, "base_url": "bazarr.local:6767"},
        {**SOURCE_CONFIG, "base_url": "http://user:password@bazarr.local:6767"},
        {**SOURCE_CONFIG, "base_url": "http://bazarr.local:6767/?token=secret"},
        {**SOURCE_CONFIG, "base_url": "http://bazarr.local:6767/#fragment"},
        {**SOURCE_CONFIG, "base_url": "http://bazarr.local:6767\n.invalid"},
        {**SOURCE_CONFIG, "api_key": ""},
        {**SOURCE_CONFIG, "api_key": 123},
        {**SOURCE_CONFIG, "backup_directory": "relative/backups"},
        {**SOURCE_CONFIG, "backup_directory": "/config"},
        {**SOURCE_CONFIG, "backup_directory": "/sources/bazarr/../backups"},
        {**SOURCE_CONFIG, "restore_directory": "/tmp/restore"},
        {**SOURCE_CONFIG, "legacy_path": "/sources/bazarr/backups"},
        {
            key: value
            for key, value in RESTORE_DESTINATION_CONFIG.items()
            if key != "restore_directory"
        },
        {**RESTORE_DESTINATION_CONFIG, "restore_directory": "relative/restore"},
        {**RESTORE_DESTINATION_CONFIG, "restore_directory": "/config"},
        {
            **RESTORE_DESTINATION_CONFIG,
            "restore_directory": "/tmp/isolated/../bazarr",
        },
        {**RESTORE_DESTINATION_CONFIG, "base_url": "http://bazarr.local:6767"},
        {**RESTORE_DESTINATION_CONFIG, "legacy_path": "/tmp/restore"},
    )
    for config in invalid_configs:
        assert await plugin.validate_config(config) is False  # type: ignore[arg-type]


@pytest.mark.anyio
async def test_bazarr_public_api_exposes_flat_mode_aware_schema() -> None:
    _plugin_class()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        plugins_response = await client.get("/api/v1/plugins/")
        schema_response = await client.get("/api/v1/plugins/bazarr/schema")

    assert plugins_response.status_code == 200
    assert any(
        item["key"] == "bazarr" and item["restore_capability"] == "partial"
        for item in plugins_response.json()
    )
    assert schema_response.status_code == 200
    schema = schema_response.json()
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["mode"]
    assert set(schema["properties"]) == {
        "mode",
        "base_url",
        "api_key",
        "backup_directory",
        "restore_directory",
    }


@pytest.mark.asyncio
async def test_source_probe_requires_exact_version_sqlite_and_native_backup_list(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backup_directory = tmp_path / "native-backups"
    backup_directory.mkdir()
    existing = backup_directory / "existing-marker"
    existing.write_bytes(b"source-must-remain-unchanged")
    mount_observations = _install_read_only_mount(
        monkeypatch,
        backup_directory,
    )
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "GET"
        assert request.url.scheme == "http"
        assert request.url.host == "bazarr.local"
        assert request.url.port == 6767
        assert request.headers["X-API-KEY"] == SOURCE_CONFIG["api_key"]
        assert str(SOURCE_CONFIG["api_key"]) not in str(request.url)
        if request.url.path == "/api/system/status":
            return httpx.Response(200, json=STATUS_RESPONSE)
        if request.url.path == "/api/system/backups":
            return httpx.Response(200, json=BACKUPS_RESPONSE)
        raise AssertionError(f"Unexpected Bazarr probe path: {request.url.path}")

    _install_http_transport(monkeypatch, handler)

    assert await _plugin_class()(name="bazarr").test(_source_config(backup_directory)) is True
    assert [request.url.path for request in requests] == [
        "/api/system/status",
        "/api/system/backups",
    ]
    assert mount_observations
    assert set(mount_observations) == {backup_directory}
    assert existing.read_bytes() == b"source-must-remain-unchanged"
    assert set(backup_directory.iterdir()) == {existing}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("is_mount", "read_only", "expected_exception", "message"),
    (
        (False, True, RuntimeError, "mount"),
        (True, False, RuntimeError, "read-only"),
    ),
)
async def test_source_probe_requires_a_genuine_read_only_mount(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    is_mount: bool,
    read_only: bool,
    expected_exception: type[Exception],
    message: str,
) -> None:
    backup_directory = tmp_path / "native-backups"
    backup_directory.mkdir()
    _install_read_only_mount(
        monkeypatch,
        backup_directory,
        is_mount=is_mount,
        read_only=read_only,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/system/status":
            return httpx.Response(200, json=STATUS_RESPONSE)
        if request.url.path == "/api/system/backups":
            return httpx.Response(200, json=BACKUPS_RESPONSE)
        raise AssertionError(f"Unexpected Bazarr probe path: {request.url.path}")

    _install_http_transport(monkeypatch, handler)

    with pytest.raises(expected_exception, match=message):
        await _plugin_class()(name="bazarr").test(_source_config(backup_directory))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "expected_exception", "message"),
    (
        ("authentication", RuntimeError, "authentication"),
        ("authentication-forbidden", RuntimeError, "authentication"),
        ("network", ConnectionError, "connect"),
        ("timeout", ConnectionError, "connect"),
        ("wrong-version", RuntimeError, "version"),
        ("wrong-image", RuntimeError, "package"),
        ("postgresql", RuntimeError, "SQLite"),
        ("wrong-migration", RuntimeError, "migration"),
        ("malformed-status", RuntimeError, "response"),
        ("malformed-backups", RuntimeError, "response"),
        ("server-error", RuntimeError, "status 500"),
    ),
)
async def test_source_probe_maps_failures_without_disclosing_the_api_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    expected_exception: type[Exception],
    message: str,
) -> None:
    backup_directory = tmp_path / "native-backups"
    backup_directory.mkdir()
    _install_read_only_mount(monkeypatch, backup_directory)
    secret = str(SOURCE_CONFIG["api_key"])

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.host == "bazarr.local"
        assert request.headers["X-API-KEY"] == secret
        if case == "network":
            raise httpx.ConnectError(f"connection failed near {secret}", request=request)
        if case == "timeout":
            raise httpx.ReadTimeout(f"read timed out near {secret}", request=request)
        if case == "authentication":
            return httpx.Response(401, json={"error": secret})
        if case == "authentication-forbidden":
            return httpx.Response(403, json={"error": secret})
        if case == "server-error":
            return httpx.Response(500, text=f"server failure near {secret}")
        if request.url.path == "/api/system/status":
            if case == "wrong-version":
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            **STATUS_RESPONSE["data"],
                            "bazarr_version": "1.5.7",
                        }
                    },
                )
            if case == "wrong-image":
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            **STATUS_RESPONSE["data"],
                            "package_version": "v1.5.6-ls350 by linuxserver.io",
                        }
                    },
                )
            if case == "postgresql":
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            **STATUS_RESPONSE["data"],
                            "database_engine": "Postgresql 18.6",
                        }
                    },
                )
            if case == "wrong-migration":
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            **STATUS_RESPONSE["data"],
                            "database_migration": "unexpected-head",
                        }
                    },
                )
            if case == "malformed-status":
                return httpx.Response(
                    200,
                    json={"data": {"bazarr_version": "1.5.6"}},
                )
            return httpx.Response(200, json=STATUS_RESPONSE)
        if request.url.path == "/api/system/backups":
            if case == "malformed-backups":
                return httpx.Response(
                    200,
                    json={
                        "data": [
                            {
                                "filename": "bazarr_backup_v1.5.6_invalid.zip",
                                "unexpected": secret,
                            }
                        ]
                    },
                )
            return httpx.Response(200, json=BACKUPS_RESPONSE)
        raise AssertionError(f"Unexpected Bazarr probe path: {request.url.path}")

    _install_http_transport(monkeypatch, handler)

    with pytest.raises(expected_exception, match=message) as error:
        await _plugin_class()(name="bazarr").test(_source_config(backup_directory))
    assert secret not in str(error.value)


@pytest.mark.asyncio
async def test_source_probe_propagates_cancellation_without_source_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backup_directory = tmp_path / "native-backups"
    backup_directory.mkdir()
    existing = backup_directory / "existing.zip"
    existing.write_bytes(b"must-remain")
    _install_read_only_mount(monkeypatch, backup_directory)

    def cancel(_request: httpx.Request) -> httpx.Response:
        raise asyncio.CancelledError

    _install_http_transport(monkeypatch, cancel)

    with pytest.raises(asyncio.CancelledError):
        await _plugin_class()(name="bazarr").test(_source_config(backup_directory))

    assert existing.read_bytes() == b"must-remain"
    assert set(backup_directory.iterdir()) == {existing}


@pytest.mark.asyncio
async def test_source_probe_does_not_follow_redirects_or_change_origin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backup_directory = tmp_path / "native-backups"
    backup_directory.mkdir()
    _install_read_only_mount(monkeypatch, backup_directory)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/system/status":
            return httpx.Response(200, json=STATUS_RESPONSE)
        return httpx.Response(
            302,
            headers={"location": "https://attacker.invalid/collect"},
        )

    _install_http_transport(monkeypatch, handler)

    with pytest.raises(RuntimeError, match="status 302"):
        await _plugin_class()(name="bazarr").test(_source_config(backup_directory))
    assert [request.url.host for request in requests] == [
        "bazarr.local",
        "bazarr.local",
    ]
    assert [request.url.path for request in requests] == [
        "/api/system/status",
        "/api/system/backups",
    ]


@pytest.mark.asyncio
async def test_restore_destination_probe_is_create_only_and_never_uses_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOMELAB_BACKUP_ALLOW_ISOLATED_RESTORE", "1")
    parent = tmp_path / "isolated-parent"
    parent.mkdir(mode=0o700)
    sentinel = parent / RESTORE_SENTINEL_NAME
    sentinel.write_text(RESTORE_SENTINEL_CONTENT, encoding="utf-8")
    sentinel.chmod(0o600)
    destination = parent / "bazarr-alpha"

    def no_http_client(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("restore-destination probe must not create an HTTP client")

    monkeypatch.setattr(httpx, "AsyncClient", no_http_client)

    assert (
        await _plugin_class()(name="bazarr").test(
            {"mode": "restore_destination", "restore_directory": str(destination)}
        )
        is True
    )
    assert not destination.exists()
    assert set(parent.iterdir()) == {sentinel}


@pytest.mark.asyncio
async def test_restore_destination_probe_rejects_a_networked_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOMELAB_BACKUP_ALLOW_ISOLATED_RESTORE", "1")
    plugin_module = importlib.import_module("app.plugins.bazarr.plugin")
    monkeypatch.setattr(plugin_module, "_network_interfaces", lambda: {"lo", "eth0"})
    _parent, destination, _sentinel = _prepare_restore_destination(tmp_path)

    with pytest.raises(RuntimeError, match="loopback-only"):
        await _plugin_class()(name="bazarr").test(
            {"mode": "restore_destination", "restore_directory": str(destination)}
        )


@pytest.mark.asyncio
async def test_restore_destination_probe_requires_exact_sentinel_and_absent_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOMELAB_BACKUP_ALLOW_ISOLATED_RESTORE", "1")
    parent = tmp_path / "isolated-parent"
    parent.mkdir(mode=0o700)
    sentinel = parent / RESTORE_SENTINEL_NAME
    destination = parent / "bazarr-alpha"
    config = {"mode": "restore_destination", "restore_directory": str(destination)}

    def no_http_client(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("restore-destination probe must not create an HTTP client")

    monkeypatch.setattr(httpx, "AsyncClient", no_http_client)
    plugin = _plugin_class()(name="bazarr")

    with pytest.raises(FileNotFoundError, match="sentinel"):
        await plugin.test(config)

    sentinel.write_text("wrong-sentinel\n", encoding="utf-8")
    sentinel.chmod(0o600)
    with pytest.raises(ValueError, match="sentinel"):
        await plugin.test(config)

    sentinel.write_text(RESTORE_SENTINEL_CONTENT, encoding="utf-8")
    destination.mkdir()
    with pytest.raises(FileExistsError, match="destination"):
        await plugin.test(config)


@pytest.mark.asyncio
async def test_get_status_reports_only_observed_probe_health(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backup_directory = tmp_path / "native-backups"
    backup_directory.mkdir()
    _install_read_only_mount(monkeypatch, backup_directory)
    healthy = True
    secret = str(SOURCE_CONFIG["api_key"])

    def handler(request: httpx.Request) -> httpx.Response:
        if not healthy:
            return httpx.Response(401, json={"error": secret})
        if request.url.path == "/api/system/status":
            return httpx.Response(200, json=STATUS_RESPONSE)
        if request.url.path == "/api/system/backups":
            return httpx.Response(200, json=BACKUPS_RESPONSE)
        raise AssertionError(f"Unexpected Bazarr probe path: {request.url.path}")

    _install_http_transport(monkeypatch, handler)
    plugin = _plugin_class()(name="bazarr")
    context = BackupContext(
        job_id="status",
        target_id="bazarr-source",
        config=_source_config(backup_directory),
    )

    assert await plugin.get_status(context) == {"status": "ok"}

    healthy = False
    failed_status = await plugin.get_status(context)
    assert failed_status["status"] == "error"
    assert "authentication" in failed_status["error"]
    assert secret not in json.dumps(failed_status)


@pytest.mark.asyncio
async def test_backup_attributes_one_stable_shared_native_zip_and_publishes_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backup_directory = tmp_path / "native-backups"
    backup_directory.mkdir()
    baseline_path = backup_directory / BASELINE_BACKUP_NAME
    _write_exact_native_zip(baseline_path, "baseline")
    new_path = backup_directory / NEW_BACKUP_NAME
    backup_root = tmp_path / "published"
    _install_read_only_mount(monkeypatch, backup_directory)
    _configure_fast_backup_poll(monkeypatch, backup_root)
    requests: list[httpx.Request] = []
    backup_get_count = 0
    triggered = False
    expected_new_bytes: bytes | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal backup_get_count, expected_new_bytes, triggered
        requests.append(request)
        assert request.url.scheme == "http"
        assert request.url.host == "bazarr.local"
        assert request.url.port == 6767
        assert request.headers["X-API-KEY"] == SOURCE_CONFIG["api_key"]
        assert str(SOURCE_CONFIG["api_key"]) not in str(request.url)
        if request.method == "GET" and request.url.path == "/api/system/status":
            return httpx.Response(200, json=STATUS_RESPONSE)
        if request.method == "GET" and request.url.path == "/api/system/backups":
            backup_get_count += 1
            names = (
                (BASELINE_BACKUP_NAME, NEW_BACKUP_NAME) if triggered else (BASELINE_BACKUP_NAME,)
            )
            return httpx.Response(200, json=_backup_list(*names))
        if request.method == "POST" and request.url.path == "/api/system/backups":
            assert not triggered, "backup must send exactly one POST"
            assert request.content == b""
            triggered = True
            _write_exact_native_zip(new_path, "new-state")
            expected_new_bytes = _streamed_bytes(new_path)
            return httpx.Response(204)
        raise AssertionError(f"Unexpected Bazarr backup request: {request.method} {request.url}")

    _install_http_transport(monkeypatch, handler)

    result = await _plugin_class()(name="bazarr").backup(
        _backup_context(backup_directory, backup_root)
    )

    artifact_path = Path(result["artifact_path"])
    assert artifact_path.is_file()
    assert artifact_path.is_relative_to(backup_root / "bazarr-source")
    assert expected_new_bytes is not None
    assert _streamed_bytes(new_path) == expected_new_bytes
    assert _streamed_bytes(artifact_path) == expected_new_bytes
    assert stat.S_IMODE(artifact_path.stat().st_mode) == 0o600
    sidecar = json.loads(Path(f"{artifact_path}.meta.json").read_text(encoding="utf-8"))
    assert sidecar["sha256"] == _sha256(artifact_path)
    assert sidecar["artifact_bytes"] == artifact_path.stat().st_size
    assert sidecar["application_version"] == "1.5.6"
    assert sidecar["package_version"] == "v1.5.6-ls349 by linuxserver.io"
    assert sidecar["database_backend"] == "sqlite"
    assert sidecar["validation"] == "passed"
    assert set(sidecar["table_counts"]) == EXPECTED_BAZARR_TABLES
    assert all(isinstance(count, int) and count >= 0 for count in sidecar["table_counts"].values())
    assert str(SOURCE_CONFIG["api_key"]) not in json.dumps(sidecar)
    assert baseline_path.is_file()
    assert new_path.is_file()
    assert [request.method for request in requests] == [
        "GET",
        "GET",
        "POST",
        "GET",
        "GET",
    ]
    assert [request.url.path for request in requests] == [
        "/api/system/status",
        "/api/system/backups",
        "/api/system/backups",
        "/api/system/backups",
        "/api/system/backups",
    ]
    assert backup_get_count == 3
    assert {request.url.host for request in requests} == {"bazarr.local"}


@pytest.mark.asyncio
async def test_backup_refuses_temporary_path_replacement_after_worker_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backup_directory = tmp_path / "native-backups"
    backup_directory.mkdir()
    _write_exact_native_zip(backup_directory / BASELINE_BACKUP_NAME, "baseline")
    candidate = backup_directory / NEW_BACKUP_NAME
    backup_root = tmp_path / "published"
    relocated = tmp_path / "validated-relocated.zip"
    _install_read_only_mount(monkeypatch, backup_directory)
    _configure_fast_backup_poll(monkeypatch, backup_root)
    triggered = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal triggered
        if request.method == "GET" and request.url.path == "/api/system/status":
            return httpx.Response(200, json=STATUS_RESPONSE)
        if request.method == "GET" and request.url.path == "/api/system/backups":
            names = (
                (BASELINE_BACKUP_NAME, NEW_BACKUP_NAME) if triggered else (BASELINE_BACKUP_NAME,)
            )
            return httpx.Response(200, json=_backup_list(*names))
        if request.method == "POST" and request.url.path == "/api/system/backups":
            triggered = True
            _write_exact_native_zip(candidate, "verified-source")
            return httpx.Response(204)
        raise AssertionError(f"Unexpected Bazarr backup request: {request.method} {request.url}")

    _install_http_transport(monkeypatch, handler)
    plugin_module = importlib.import_module("app.plugins.bazarr.plugin")

    def replace_after_validation(
        directory: Path,
        name: str,
        evidence: Any,
        artifact_path: Path,
        validation_root: Path,
        _validation_identity: tuple[int, int],
    ) -> tuple[object, object]:
        plugin_module._copy_stable_source(directory, name, evidence, artifact_path)
        archive_evidence = plugin_module._validate_native_archive(
            artifact_path,
            validation_root=validation_root,
        )
        artifact_path.rename(relocated)
        _write_exact_native_zip(artifact_path, "unvalidated-replacement")
        completed = _CompletedProcess()
        completed.exitcode = 0
        return completed, _ResultConnection(("ok", "", archive_evidence))

    monkeypatch.setattr(plugin_module, "_start_backup_process", replace_after_validation)

    with pytest.raises(RuntimeError, match="changed before publication"):
        await _plugin_class()(name="bazarr").backup(_backup_context(backup_directory, backup_root))

    assert relocated.is_file()
    assert _published_files(backup_root) == []
    assert not list(backup_root.rglob("*.meta.json"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "message"),
    (
        ("post-failure", "status 500"),
        ("no-candidate", "deadline|timed out"),
        ("multiple", "multiple"),
        ("api-only", "API.*local|matching"),
        ("local-only", "API.*local|matching"),
        ("wrong-version", "version|filename"),
        ("wrong-name", "filename"),
        ("symlink", "link"),
        ("nonregular", "regular"),
        ("escape", "escape|filename"),
        ("same-name-overwrite", "deadline|timed out"),
        ("disappeared", "API.*local|matching"),
    ),
)
async def test_backup_fails_closed_when_trigger_or_attribution_is_not_unique(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    message: str,
) -> None:
    backup_directory = tmp_path / "native-backups"
    backup_directory.mkdir()
    _write_exact_native_zip(
        backup_directory / BASELINE_BACKUP_NAME,
        "baseline",
    )
    backup_root = tmp_path / "published"
    _install_read_only_mount(monkeypatch, backup_directory)
    _configure_fast_backup_poll(monkeypatch, backup_root)
    triggered = False
    post_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal triggered, post_count
        assert request.url.host == "bazarr.local"
        assert request.headers["X-API-KEY"] == SOURCE_CONFIG["api_key"]
        if request.method == "GET" and request.url.path == "/api/system/status":
            return httpx.Response(200, json=STATUS_RESPONSE)
        if request.method == "POST" and request.url.path == "/api/system/backups":
            post_count += 1
            assert post_count == 1
            triggered = True
            if case == "post-failure":
                return httpx.Response(500)
            if case == "multiple":
                _write_exact_native_zip(
                    backup_directory / NEW_BACKUP_NAME,
                    "first",
                )
                _write_exact_native_zip(
                    backup_directory / SECOND_NEW_BACKUP_NAME,
                    "second",
                )
            elif case in {"local-only", "wrong-version", "wrong-name"}:
                filename = {
                    "local-only": NEW_BACKUP_NAME,
                    "wrong-version": ("bazarr_backup_v1.5.7_2026.08.16_12.34.56.zip"),
                    "wrong-name": "bazarr-backup-latest.zip",
                }[case]
                _write_exact_native_zip(backup_directory / filename, case)
            elif case == "symlink":
                outside = tmp_path / "outside.zip"
                _write_exact_native_zip(outside, "outside")
                (backup_directory / NEW_BACKUP_NAME).symlink_to(outside)
            elif case == "nonregular":
                (backup_directory / NEW_BACKUP_NAME).mkdir()
            elif case == "same-name-overwrite":
                _write_exact_native_zip(
                    backup_directory / BASELINE_BACKUP_NAME,
                    "overwritten-baseline",
                )
            elif case == "disappeared":
                _write_exact_native_zip(backup_directory / NEW_BACKUP_NAME, "disappeared")
            return httpx.Response(204)
        if request.method == "GET" and request.url.path == "/api/system/backups":
            names: tuple[str, ...]
            if not triggered or case in {"no-candidate", "local-only", "same-name-overwrite"}:
                names = (BASELINE_BACKUP_NAME,)
            elif case == "multiple":
                names = (
                    BASELINE_BACKUP_NAME,
                    NEW_BACKUP_NAME,
                    SECOND_NEW_BACKUP_NAME,
                )
            elif case == "wrong-version":
                names = (
                    BASELINE_BACKUP_NAME,
                    "bazarr_backup_v1.5.7_2026.08.16_12.34.56.zip",
                )
            elif case == "wrong-name":
                names = (BASELINE_BACKUP_NAME, "bazarr-backup-latest.zip")
            elif case == "escape":
                names = (BASELINE_BACKUP_NAME, f"../{NEW_BACKUP_NAME}")
            elif case == "disappeared":
                (backup_directory / NEW_BACKUP_NAME).unlink(missing_ok=True)
                names = (BASELINE_BACKUP_NAME, NEW_BACKUP_NAME)
            else:
                names = (BASELINE_BACKUP_NAME, NEW_BACKUP_NAME)
            return httpx.Response(200, json=_backup_list(*names))
        raise AssertionError(f"Unexpected Bazarr backup request: {request.method} {request.url}")

    _install_http_transport(monkeypatch, handler)

    with pytest.raises(RuntimeError, match=message):
        await _plugin_class()(name="bazarr").backup(_backup_context(backup_directory, backup_root))

    assert post_count == 1
    assert _published_files(backup_root) == []
    assert not list(backup_root.rglob("*.meta.json"))


@pytest.mark.asyncio
async def test_backup_rejects_candidate_created_before_trigger_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backup_directory = tmp_path / "native-backups"
    backup_directory.mkdir()
    _write_exact_native_zip(backup_directory / BASELINE_BACKUP_NAME, "baseline")
    candidate = backup_directory / NEW_BACKUP_NAME
    backup_root = tmp_path / "published"
    _install_read_only_mount(monkeypatch, backup_directory)
    _configure_fast_backup_poll(monkeypatch, backup_root)
    plugin_module = importlib.import_module("app.plugins.bazarr.plugin")
    triggered = False

    def establish_boundary_after_ambiguous_candidate() -> int:
        _write_exact_native_zip(candidate, "ambiguous-pre-trigger-state")
        return candidate.stat().st_ctime_ns + 1

    monkeypatch.setattr(plugin_module.time, "time_ns", establish_boundary_after_ambiguous_candidate)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal triggered
        if request.method == "GET" and request.url.path == "/api/system/status":
            return httpx.Response(200, json=STATUS_RESPONSE)
        if request.method == "POST" and request.url.path == "/api/system/backups":
            triggered = True
            return httpx.Response(204)
        if request.method == "GET" and request.url.path == "/api/system/backups":
            names = (
                (BASELINE_BACKUP_NAME, NEW_BACKUP_NAME) if triggered else (BASELINE_BACKUP_NAME,)
            )
            return httpx.Response(200, json=_backup_list(*names))
        raise AssertionError(f"Unexpected Bazarr backup request: {request.method} {request.url}")

    _install_http_transport(monkeypatch, handler)

    with pytest.raises(RuntimeError, match="predates.*trigger|overlap|collision"):
        await _plugin_class()(name="bazarr").backup(_backup_context(backup_directory, backup_root))

    assert _published_files(backup_root) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "message"),
    (
        ("missing-member", "members"),
        ("duplicate-member", "duplicate|members"),
        ("extra-member", "members"),
        ("nested-member", "root|members"),
        ("absolute-member", "path|members"),
        ("traversal-member", "path|members"),
        ("link-member", "regular|link"),
        ("device-member", "regular"),
        ("encrypted-member", "encrypted"),
        ("crc", "CRC|corrupt"),
        ("trailing-data", "trailing|ambiguous"),
        ("member-limit", "member.*limit|safety limit"),
        ("compressed-limit", "compressed.*limit|safety limit"),
        ("uncompressed-limit", "uncompressed.*limit|safety limit"),
        ("ratio-limit", "ratio|safety limit"),
        ("unsafe-yaml", "YAML|configuration"),
        ("yaml-structure", "structure|configuration"),
        ("postgresql", "PostgreSQL|SQLite"),
        ("sqlite-header", "SQLite"),
        ("sqlite-integrity", "integrity|quick_check|SQLite"),
        ("sqlite-foreign-key", "foreign key"),
        ("sqlite-table-set", "table"),
        ("sqlite-migration", "migration"),
        ("sqlite-temp-residue", "migration|temporary|table"),
        ("sqlite-row-count", "row|safety limit"),
    ),
)
async def test_backup_rejects_untrusted_or_incompatible_native_archives(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    message: str,
) -> None:
    backup_directory = tmp_path / "native-backups"
    backup_directory.mkdir()
    _write_exact_native_zip(
        backup_directory / BASELINE_BACKUP_NAME,
        "baseline",
    )
    backup_root = tmp_path / "published"
    _install_read_only_mount(monkeypatch, backup_directory)
    _configure_fast_backup_poll(monkeypatch, backup_root)
    plugin_module = importlib.import_module("app.plugins.bazarr.plugin")
    if case == "member-limit":
        monkeypatch.setattr(plugin_module, "_MAX_ZIP_MEMBERS", 1, raising=False)
    elif case == "compressed-limit":
        monkeypatch.setattr(plugin_module, "_MAX_COMPRESSED_BYTES", 1, raising=False)
    elif case == "uncompressed-limit":
        monkeypatch.setattr(plugin_module, "_MAX_UNCOMPRESSED_BYTES", 1, raising=False)
    elif case == "ratio-limit":
        monkeypatch.setattr(plugin_module, "_MAX_EXPANSION_RATIO", 1, raising=False)
    elif case == "sqlite-row-count":
        monkeypatch.setattr(plugin_module, "_MAX_TABLE_ROWS", 5, raising=False)

    process_local_limit_cases = {
        "member-limit",
        "compressed-limit",
        "uncompressed-limit",
        "sqlite-row-count",
    }
    if case in process_local_limit_cases:
        candidate = backup_directory / NEW_BACKUP_NAME
        _write_malformed_native_zip(candidate, case)
        with pytest.raises(RuntimeError, match=message):
            plugin_module._validate_native_archive(candidate)
        assert _published_files(backup_root) == []
        return

    _install_single_candidate_transport(
        monkeypatch,
        backup_directory,
        lambda path: _write_malformed_native_zip(path, case),
    )

    with pytest.raises(RuntimeError, match=message):
        await _plugin_class()(name="bazarr").backup(_backup_context(backup_directory, backup_root))

    assert _published_files(backup_root) == []
    assert not list(backup_root.rglob("*.meta.json"))
    assert not list(backup_root.rglob("*.tmp"))


@pytest.mark.asyncio
async def test_backup_detects_source_mutation_during_copy_and_cleans_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backup_directory = tmp_path / "native-backups"
    backup_directory.mkdir()
    _write_exact_native_zip(
        backup_directory / BASELINE_BACKUP_NAME,
        "baseline",
    )
    backup_root = tmp_path / "published"
    _install_read_only_mount(monkeypatch, backup_directory)
    _configure_fast_backup_poll(monkeypatch, backup_root)
    candidate = backup_directory / NEW_BACKUP_NAME
    _write_exact_native_zip(candidate, "mutable")
    destination = backup_root / "copy.tmp"
    backup_root.mkdir()
    plugin_module = importlib.import_module("app.plugins.bazarr.plugin")
    evidence = plugin_module._file_evidence(candidate.stat())
    original_read = os.read
    mutated = False

    def mutate_after_first_source_read(file_descriptor: int, size: int) -> bytes:
        nonlocal mutated
        chunk = original_read(file_descriptor, size)
        try:
            opened_path = Path(f"/proc/self/fd/{file_descriptor}").resolve(strict=True)
        except OSError:
            opened_path = Path("/not-the-source")
        if chunk and opened_path == candidate and not mutated:
            mutated = True
            with candidate.open("ab") as source:
                source.write(b"changed-during-copy")
        return chunk

    monkeypatch.setattr(os, "read", mutate_after_first_source_read)

    with pytest.raises(RuntimeError, match="changed while.*copied|mutation"):
        plugin_module._copy_stable_source(
            backup_directory,
            candidate.name,
            evidence,
            destination,
        )

    assert mutated is True
    destination.unlink(missing_ok=True)
    assert not list(backup_root.rglob("*.meta.json"))
    assert not list(backup_root.rglob("*.tmp"))


@pytest.mark.asyncio
async def test_restore_materializes_exact_private_files_and_returns_partial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOMELAB_BACKUP_ALLOW_ISOLATED_RESTORE", "1")
    artifact = tmp_path / "restore-service-staged-bazarr.zip"
    _write_exact_native_zip(artifact, "restore")
    parent, destination, sentinel = _prepare_restore_destination(tmp_path)
    fsynced_modes: list[int] = []
    original_fsync = os.fsync

    def observe_fsync(file_descriptor: int) -> None:
        fsynced_modes.append(stat.S_IMODE(os.fstat(file_descriptor).st_mode))
        original_fsync(file_descriptor)

    monkeypatch.setattr(os, "fsync", observe_fsync)

    result = await _plugin_class()(name="bazarr").restore(_restore_context(artifact, destination))

    assert result["status"] == "partial"
    assert "media" in result["message"].lower()
    assert "sonarr" in result["message"].lower()
    assert "radarr" in result["message"].lower()
    assert sentinel.exists() is False
    assert set(parent.iterdir()) == {destination}
    assert stat.S_IMODE(destination.stat().st_mode) == 0o700
    assert stat.S_IMODE((destination / "config").stat().st_mode) == 0o700
    assert stat.S_IMODE((destination / "db").stat().st_mode) == 0o700
    restored_files = {
        path.relative_to(destination).as_posix()
        for path in destination.rglob("*")
        if path.is_file()
    }
    assert restored_files == {"config/config.yaml", "db/bazarr.db"}
    assert all(not path.is_symlink() for path in destination.rglob("*"))
    assert all(
        stat.S_IMODE((destination / relative).stat().st_mode) == 0o600
        for relative in restored_files
    )
    with zipfile.ZipFile(artifact) as archive:
        expected_hashes = {
            "config/config.yaml": hashlib.sha256(archive.read("config.yaml")).hexdigest(),
            "db/bazarr.db": hashlib.sha256(archive.read("bazarr.db")).hexdigest(),
        }
    assert {
        relative: _sha256(destination / relative) for relative in restored_files
    } == expected_hashes
    assert fsynced_modes.count(0o700) >= 2


def test_restore_service_stages_verified_bazarr_artifact_and_records_partial_result(
    db_session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin = _plugin_class()(name="bazarr")
    source = Target(
        name="Bazarr Source",
        slug="bazarr-source",
        plugin_name="bazarr",
        plugin_config_json=json.dumps(SOURCE_CONFIG),
    )
    parent, destination, sentinel = _prepare_restore_destination(
        tmp_path,
        name="bazarr-service-restore",
    )
    destination_target = Target(
        name="Bazarr Isolated Restore",
        slug="bazarr-isolated-restore",
        plugin_name="bazarr",
        plugin_config_json=json.dumps(
            {
                "mode": "restore_destination",
                "restore_directory": str(destination),
            }
        ),
    )
    db_session.add_all([source, destination_target])
    db_session.commit()

    artifact_directory = tmp_path / source.slug / "2026-08-16"
    artifact_directory.mkdir(parents=True)
    artifact = artifact_directory / "bazarr-native-service-test.zip"
    _write_exact_native_zip(artifact, "restore-service")
    write_backup_sidecar(
        str(artifact),
        plugin,
        BackupContext(
            job_id="bazarr-source-run",
            target_id=str(source.id),
            config=dict(SOURCE_CONFIG),
            metadata={"target_slug": source.slug},
        ),
    )
    source_digest = _sha256(artifact)
    source_bytes = artifact.stat().st_size
    source_inode = artifact.stat().st_ino

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
        artifact_bytes=source_bytes,
        sha256=source_digest,
        source_identity_json=json.dumps(
            {
                "database_backend": "sqlite",
                "database_migration": "df76a4410347",
            }
        ),
        started_at=source_run.started_at,
        finished_at=source_run.finished_at,
    )
    db_session.add(source_target_run)
    db_session.commit()

    observed: dict[str, object] = {}
    real_restore = plugin.restore

    async def observe_staged_restore(context: RestoreContext) -> dict[str, Any]:
        staged_artifact = Path(context.artifact_path)
        observed["path"] = staged_artifact
        observed["inode"] = staged_artifact.stat().st_ino
        observed["sha256"] = _sha256(staged_artifact)
        observed["metadata"] = dict(context.metadata or {})
        return cast(dict[str, Any], await real_restore(context))

    monkeypatch.setenv("BACKUP_BASE_PATH", str(tmp_path))
    monkeypatch.setenv("HOMELAB_BACKUP_ALLOW_ISOLATED_RESTORE", "1")
    monkeypatch.setattr(plugin, "restore", observe_staged_restore)
    monkeypatch.setattr("app.services.restores.get_plugin", lambda _: plugin)

    result = RestoreService(db_session).restore(
        source_target_run_id=source_target_run.id,
        destination_target_id=destination_target.id,
        triggered_by="isolated_bazarr_service_test",
    )

    assert result.status == "partial"
    assert len(result.target_runs) == 1
    restore_target_run = result.target_runs[0]
    assert restore_target_run.status == "partial"
    assert restore_target_run.operation == "restore"
    assert restore_target_run.target_id == destination_target.id
    assert restore_target_run.artifact_path == str(destination)

    staged_path = observed["path"]
    assert isinstance(staged_path, Path)
    assert staged_path != artifact
    assert observed["inode"] != source_inode
    assert observed["sha256"] == source_digest
    assert staged_path.exists() is False
    assert not list(artifact_directory.glob(".homelab-backup-restore-*"))

    metadata = observed["metadata"]
    assert isinstance(metadata, dict)
    assert metadata["source_target_run_id"] == source_target_run.id
    assert metadata["source_run_id"] == source_run.id
    assert metadata["source_target_id"] == source.id
    assert metadata["source_target_slug"] == source.slug
    assert metadata["artifact_bytes"] == source_bytes
    assert metadata["artifact_sha256"] == source_digest
    assert metadata["source_database_identity"] == {
        "database_backend": "sqlite",
        "database_migration": "df76a4410347",
    }

    assert artifact.is_file()
    assert artifact.stat().st_ino == source_inode
    assert artifact.stat().st_size == source_bytes
    assert _sha256(artifact) == source_digest
    assert Path(f"{artifact}.meta.json").is_file()
    assert sentinel.exists() is False
    assert set(parent.iterdir()) == {destination}
    assert (destination / "config" / "config.yaml").is_file()
    assert (destination / "db" / "bazarr.db").is_file()


@pytest.mark.asyncio
async def test_restore_requires_explicit_local_drill_authorization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = tmp_path / "restore-service-staged-bazarr.zip"
    _write_exact_native_zip(artifact, "restore-authorization")
    parent, destination, sentinel = _prepare_restore_destination(tmp_path)
    plugin = _plugin_class()(name="bazarr")

    monkeypatch.delenv("HOMELAB_BACKUP_ALLOW_ISOLATED_RESTORE", raising=False)
    with pytest.raises(RuntimeError, match="disabled|authorized isolated"):
        await plugin.restore(_restore_context(artifact, destination))
    assert set(parent.iterdir()) == {sentinel}

    monkeypatch.setenv("HOMELAB_BACKUP_ALLOW_ISOLATED_RESTORE", "1")
    production_parent = Path("/data/bazarr-restore-parent")
    context = _restore_context(artifact, production_parent / "destination")
    with pytest.raises((FileNotFoundError, ValueError), match="parent|isolated"):
        await plugin.restore(context)
    assert set(parent.iterdir()) == {sentinel}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "expected_exception", "message"),
    (
        ("invalid-mode", ValueError, "configuration|mode"),
        ("existing", FileExistsError, "exists"),
        ("missing-sentinel", FileNotFoundError, "sentinel"),
        ("wrong-sentinel", ValueError, "sentinel"),
        ("nonexclusive", ValueError, "only.*sentinel"),
        ("symlink-parent", ValueError, "symlink"),
        ("public-parent", RuntimeError, "private"),
        ("artifact-overlap", (ValueError, FileExistsError), "artifact|overlap|exists"),
    ),
)
async def test_restore_rejects_unsafe_or_nonexclusive_destinations_without_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    expected_exception: type[Exception] | tuple[type[Exception], ...],
    message: str,
) -> None:
    monkeypatch.setenv("HOMELAB_BACKUP_ALLOW_ISOLATED_RESTORE", "1")
    artifact = tmp_path / "restore-service-staged-bazarr.zip"
    _write_exact_native_zip(artifact, "restore-boundary")
    parent, destination, sentinel = _prepare_restore_destination(tmp_path)
    foreign: Path | None = None
    context = _restore_context(artifact, destination)

    if case == "invalid-mode":
        context.config = dict(SOURCE_CONFIG)
    elif case == "existing":
        destination.mkdir(mode=0o700)
    elif case == "missing-sentinel":
        sentinel.unlink()
    elif case == "wrong-sentinel":
        sentinel.write_text("wrong\n", encoding="utf-8")
    elif case == "nonexclusive":
        foreign = parent / "foreign-state"
        foreign.write_text("must-survive", encoding="utf-8")
    elif case == "symlink-parent":
        real_parent = parent
        linked_parent = tmp_path / "linked-parent"
        linked_parent.symlink_to(real_parent, target_is_directory=True)
        destination = linked_parent / destination.name
        context = _restore_context(artifact, destination)
    elif case == "public-parent":
        parent.chmod(0o755)
    elif case == "artifact-overlap":
        destination = artifact
        context = _restore_context(artifact, destination)

    with pytest.raises(expected_exception, match=message):
        await _plugin_class()(name="bazarr").restore(context)

    assert artifact.is_file()
    if case not in {"existing", "artifact-overlap"}:
        assert destination.exists() is False
    if foreign is not None:
        assert foreign.read_text(encoding="utf-8") == "must-survive"


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ("missing-member", "unsafe-yaml", "sqlite-migration"))
async def test_restore_revalidates_staged_artifact_before_creating_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    monkeypatch.setenv("HOMELAB_BACKUP_ALLOW_ISOLATED_RESTORE", "1")
    artifact = tmp_path / "restore-service-staged-bazarr.zip"
    _write_malformed_native_zip(artifact, case)
    parent, destination, sentinel = _prepare_restore_destination(tmp_path)

    with pytest.raises(RuntimeError):
        await _plugin_class()(name="bazarr").restore(_restore_context(artifact, destination))

    assert artifact.is_file()
    assert destination.exists() is False
    assert set(parent.iterdir()) == {sentinel}


@pytest.mark.asyncio
async def test_restore_refuses_a_valid_staged_artifact_substitution_before_worker_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOMELAB_BACKUP_ALLOW_ISOLATED_RESTORE", "1")
    artifact = tmp_path / "restore-service-staged-bazarr.zip"
    relocated = tmp_path / "restore-service-original-bazarr.zip"
    _write_exact_native_zip(artifact, "verified-staged-state")
    context = _restore_context(artifact, _prepare_restore_destination(tmp_path)[1])
    parent = Path(context.config["restore_directory"]).parent
    sentinel = parent / RESTORE_SENTINEL_NAME
    plugin_module = importlib.import_module("app.plugins.bazarr.plugin")
    real_start = plugin_module._start_restore_process

    def substitute_then_start(*args: object) -> tuple[object, object]:
        artifact.rename(relocated)
        _write_exact_native_zip(artifact, "unverified-substitute")
        return cast(tuple[object, object], real_start(*args))

    monkeypatch.setattr(plugin_module, "_start_restore_process", substitute_then_start)

    with pytest.raises(ValueError, match="verified staging identity"):
        await _plugin_class()(name="bazarr").restore(context)

    assert not Path(context.config["restore_directory"]).exists()
    assert set(parent.iterdir()) == {sentinel}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ("publication-race", "extract-write", "fsync"))
async def test_restore_failure_preserves_foreign_state_and_removes_owned_partial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    monkeypatch.setenv("HOMELAB_BACKUP_ALLOW_ISOLATED_RESTORE", "1")
    artifact = tmp_path / "restore-service-staged-bazarr.zip"
    _write_exact_native_zip(artifact, "restore-failure")
    parent, destination, sentinel = _prepare_restore_destination(tmp_path)
    raced_foreign = destination / "foreign-state"

    if case == "publication-race":
        plugin_module = importlib.import_module("app.plugins.bazarr.plugin")

        def collide_at_publication(
            parent_fd: int,
            source_name: str,
            destination_name: str,
        ) -> None:
            del parent_fd, source_name, destination_name
            destination.mkdir(mode=0o700)
            raced_foreign.write_text("must-survive", encoding="utf-8")
            raise FileExistsError("synthetic restore publication race")

        monkeypatch.setattr(plugin_module, "_rename_directory_noreplace", collide_at_publication)
    elif case == "extract-write":
        plugin_module = importlib.import_module("app.plugins.bazarr.plugin")

        def fail_restore_worker(*_args: object) -> tuple[object, object]:
            return _CompletedProcess(), _ResultConnection(
                ("runtime", "synthetic restore extraction failure", None)
            )

        monkeypatch.setattr(plugin_module, "_start_restore_process", fail_restore_worker)
    else:
        original_fsync = os.fsync

        def fail_owned_fsync(file_descriptor: int) -> None:
            opened_path = Path(f"/proc/self/fd/{file_descriptor}").resolve(strict=True)
            if opened_path.is_relative_to(parent):
                raise OSError("synthetic restore fsync failure")
            original_fsync(file_descriptor)

        monkeypatch.setattr(os, "fsync", fail_owned_fsync)

    with pytest.raises((OSError, FileExistsError, RuntimeError)):
        await _plugin_class()(name="bazarr").restore(_restore_context(artifact, destination))

    assert artifact.is_file()
    if case == "publication-race":
        assert raced_foreign.read_text(encoding="utf-8") == "must-survive"
        assert set(destination.iterdir()) == {raced_foreign}
    else:
        assert destination.exists() is False
    assert sentinel.is_file()
    assert {path.name for path in parent.iterdir() if path != destination} == {
        RESTORE_SENTINEL_NAME
    }


def test_restore_validation_and_extraction_remain_bound_to_one_artifact_inode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = tmp_path / "verified.zip"
    relocated = tmp_path / "verified-relocated.zip"
    _write_exact_native_zip(artifact, "verified-state")
    expected_bytes = artifact.stat().st_size
    expected_sha256 = _sha256(artifact)
    with zipfile.ZipFile(artifact) as archive:
        expected_config = archive.read("config.yaml")
        expected_database = archive.read("bazarr.db")

    validation = tmp_path / "validation"
    config = tmp_path / "config"
    database = tmp_path / "db"
    for directory in (validation, config, database):
        directory.mkdir(mode=0o700)
    artifact_fd = os.open(artifact, os.O_RDONLY)
    validation_fd = os.open(validation, os.O_RDONLY | os.O_DIRECTORY)
    config_fd = os.open(config, os.O_RDONLY | os.O_DIRECTORY)
    database_fd = os.open(database, os.O_RDONLY | os.O_DIRECTORY)
    plugin_module = importlib.import_module("app.plugins.bazarr.plugin")
    real_validate = plugin_module._validate_native_archive

    def validate_then_replace(path: Path, *, validation_root: Path | None = None) -> object:
        evidence = real_validate(path, validation_root=validation_root)
        artifact.rename(relocated)
        _write_exact_native_zip(artifact, "unverified-replacement")
        return evidence

    monkeypatch.setattr(plugin_module, "_validate_native_archive", validate_then_replace)
    try:
        plugin_module._materialize_bound_archive(
            artifact_fd,
            validation_fd,
            config_fd,
            database_fd,
            expected_bytes,
            expected_sha256,
        )
    finally:
        os.close(database_fd)
        os.close(config_fd)
        os.close(validation_fd)
        os.close(artifact_fd)

    assert (config / "config.yaml").read_bytes() == expected_config
    assert (database / "bazarr.db").read_bytes() == expected_database
    assert _sha256(artifact) != expected_sha256


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ("helper-fsync",))
async def test_backup_cancellation_or_helper_failure_removes_private_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    backup_directory = tmp_path / "native-backups"
    backup_directory.mkdir()
    _write_exact_native_zip(
        backup_directory / BASELINE_BACKUP_NAME,
        "baseline",
    )
    backup_root = tmp_path / "published"
    _install_read_only_mount(monkeypatch, backup_directory)
    _configure_fast_backup_poll(monkeypatch, backup_root)
    _install_single_candidate_transport(
        monkeypatch,
        backup_directory,
        lambda path: _write_exact_native_zip(path, "failed-publication"),
    )

    assert case == "helper-fsync"
    original_fsync = os.fsync

    def fail_helper_fsync(file_descriptor: int) -> None:
        opened_path = Path(f"/proc/self/fd/{file_descriptor}").resolve(strict=True)
        if opened_path.is_relative_to(backup_root):
            raise OSError("synthetic artifact helper fsync failure")
        original_fsync(file_descriptor)

    monkeypatch.setattr(os, "fsync", fail_helper_fsync)

    with pytest.raises(OSError):
        await _plugin_class()(name="bazarr").backup(_backup_context(backup_directory, backup_root))

    assert _published_files(backup_root) == []
    assert not list(backup_root.rglob("*.meta.json"))
    assert not list(backup_root.rglob("*.tmp"))


@pytest.mark.asyncio
async def test_backup_worker_timeout_reaps_process_and_removes_sensitive_temps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backup_directory = tmp_path / "native-backups"
    backup_directory.mkdir()
    _write_exact_native_zip(backup_directory / BASELINE_BACKUP_NAME, "baseline")
    backup_root = tmp_path / "published"
    _install_read_only_mount(monkeypatch, backup_directory)
    _configure_fast_backup_poll(monkeypatch, backup_root)
    _install_single_candidate_transport(
        monkeypatch,
        backup_directory,
        lambda path: _write_exact_native_zip(path, "timeout"),
    )
    plugin_module = importlib.import_module("app.plugins.bazarr.plugin")
    monkeypatch.setattr(plugin_module, "_BACKUP_WORKER_TIMEOUT_SECONDS", 0.01)
    process = _BlockingProcess()
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
        artifact_path.write_bytes(b"sensitive partial archive")
        (validation_root / "bazarr.db").write_bytes(b"sensitive database")
        validation_roots.append(validation_root)
        return process, connection

    monkeypatch.setattr(plugin_module, "_start_backup_process", start_blocked_worker)

    with pytest.raises(TimeoutError, match="timed out"):
        await _plugin_class()(name="bazarr").backup(_backup_context(backup_directory, backup_root))

    assert process.exitcode == -15
    assert not process.is_alive()
    assert connection.closed
    assert validation_roots and all(not path.exists() for path in validation_roots)
    assert _published_files(backup_root) == []
    assert not list(backup_root.rglob("*.tmp"))


@pytest.mark.asyncio
async def test_backup_worker_repeated_cancellation_waits_for_reap_and_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backup_directory = tmp_path / "native-backups"
    backup_directory.mkdir()
    _write_exact_native_zip(backup_directory / BASELINE_BACKUP_NAME, "baseline")
    backup_root = tmp_path / "published"
    _install_read_only_mount(monkeypatch, backup_directory)
    _configure_fast_backup_poll(monkeypatch, backup_root)
    _install_single_candidate_transport(
        monkeypatch,
        backup_directory,
        lambda path: _write_exact_native_zip(path, "cancelled"),
    )
    plugin_module = importlib.import_module("app.plugins.bazarr.plugin")
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
        artifact_path.write_bytes(b"sensitive partial archive")
        (validation_root / "bazarr.db").write_bytes(b"sensitive database")
        validation_roots.append(validation_root)
        return process, connection

    monkeypatch.setattr(plugin_module, "_start_backup_process", start_blocked_worker)
    task = asyncio.create_task(
        _plugin_class()(name="bazarr").backup(_backup_context(backup_directory, backup_root))
    )
    assert await asyncio.to_thread(process.join_started.wait, 2)

    task.cancel()
    assert await asyncio.to_thread(process.terminate_called.wait, 2)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    process.release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.exitcode == -15
    assert connection.closed
    assert validation_roots and all(not path.exists() for path in validation_roots)
    assert _published_files(backup_root) == []


@pytest.mark.asyncio
async def test_restore_worker_timeout_reaps_before_parent_owned_secret_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOMELAB_BACKUP_ALLOW_ISOLATED_RESTORE", "1")
    artifact = tmp_path / "bazarr-restore-timeout.zip"
    _write_exact_native_zip(artifact, "restore-timeout")
    parent, destination, sentinel = _prepare_restore_destination(tmp_path)
    plugin_module = importlib.import_module("app.plugins.bazarr.plugin")
    monkeypatch.setattr(plugin_module, "_RESTORE_WORKER_TIMEOUT_SECONDS", 0.01)
    process = _BlockingProcess()
    connection = _NoResultConnection()
    owned_paths: list[Path] = []

    def start_blocked_worker(
        _artifact: Path,
        parent_path: Path,
        _parent_identity: tuple[int, int],
        staging_name: str,
        _staging_identity: tuple[int, int],
        _config_identity: tuple[int, int],
        _database_identity: tuple[int, int],
        validation_name: str,
        _validation_identity: tuple[int, int],
        _expected_artifact_bytes: int,
        _expected_sha256: str,
    ) -> tuple[object, object]:
        staging = parent_path / staging_name
        validation = parent_path / validation_name
        (staging / "config" / "config.yaml").write_bytes(b"secret config")
        (validation / "bazarr.db").write_bytes(b"secret database")
        owned_paths.extend((staging, validation))
        return process, connection

    monkeypatch.setattr(plugin_module, "_start_restore_process", start_blocked_worker)

    with pytest.raises(TimeoutError, match="timed out"):
        await _plugin_class()(name="bazarr").restore(_restore_context(artifact, destination))

    assert process.exitcode == -15
    assert not process.is_alive()
    assert connection.closed
    assert owned_paths and all(not path.exists() for path in owned_paths)
    assert artifact.is_file()
    assert sentinel.is_file()
    assert set(parent.iterdir()) == {sentinel}


@pytest.mark.asyncio
async def test_restore_worker_repeated_cancellation_waits_for_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOMELAB_BACKUP_ALLOW_ISOLATED_RESTORE", "1")
    artifact = tmp_path / "bazarr-restore-cancel.zip"
    _write_exact_native_zip(artifact, "restore-cancel")
    parent, destination, sentinel = _prepare_restore_destination(tmp_path)
    plugin_module = importlib.import_module("app.plugins.bazarr.plugin")
    process = _BlockingProcess(hold_after_terminate=True)
    connection = _NoResultConnection()

    def start_blocked_worker(
        _artifact: Path,
        parent_path: Path,
        _parent_identity: tuple[int, int],
        staging_name: str,
        _staging_identity: tuple[int, int],
        _config_identity: tuple[int, int],
        _database_identity: tuple[int, int],
        validation_name: str,
        _validation_identity: tuple[int, int],
        _expected_artifact_bytes: int,
        _expected_sha256: str,
    ) -> tuple[object, object]:
        (parent_path / staging_name / "db" / "bazarr.db").write_bytes(b"secret db")
        (parent_path / validation_name / "bazarr.db").write_bytes(b"validation db")
        return process, connection

    monkeypatch.setattr(plugin_module, "_start_restore_process", start_blocked_worker)
    task = asyncio.create_task(
        _plugin_class()(name="bazarr").restore(_restore_context(artifact, destination))
    )
    assert await asyncio.to_thread(process.join_started.wait, 2)

    task.cancel()
    assert await asyncio.to_thread(process.terminate_called.wait, 2)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    process.release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.exitcode == -15
    assert connection.closed
    assert artifact.is_file()
    assert sentinel.is_file()
    assert set(parent.iterdir()) == {sentinel}


@pytest.mark.asyncio
async def test_worker_stop_escalates_to_kill_and_reaps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin_module = importlib.import_module("app.plugins.bazarr.plugin")
    monkeypatch.setattr(plugin_module, "_WORKER_STOP_TIMEOUT_SECONDS", 0.01)
    process = _BlockingProcess(hold_after_terminate=True)

    await plugin_module._stop_worker_process(process, operation="test")

    assert process.terminate_called.is_set()
    assert process.kill_called.is_set()
    assert process.exitcode == -15
    assert not process.is_alive()


@pytest.mark.asyncio
async def test_restore_parent_replacement_preserves_foreign_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOMELAB_BACKUP_ALLOW_ISOLATED_RESTORE", "1")
    artifact = tmp_path / "bazarr-parent-race.zip"
    _write_exact_native_zip(artifact, "parent-race")
    parent, destination, _sentinel = _prepare_restore_destination(tmp_path)
    relocated = tmp_path / "relocated-owned-parent"
    foreign = parent / "foreign-state"
    plugin_module = importlib.import_module("app.plugins.bazarr.plugin")

    def replace_parent_before_worker(*_args: object) -> tuple[object, object]:
        parent.rename(relocated)
        parent.mkdir(mode=0o700)
        foreign.write_text("must-survive", encoding="utf-8")
        return _CompletedProcess(), _ResultConnection(
            ("runtime", "synthetic parent replacement", None)
        )

    monkeypatch.setattr(plugin_module, "_start_restore_process", replace_parent_before_worker)

    with pytest.raises(RuntimeError, match="parent replacement"):
        await _plugin_class()(name="bazarr").restore(_restore_context(artifact, destination))

    assert foreign.read_text(encoding="utf-8") == "must-survive"
    assert {path.name for path in relocated.iterdir()} == {RESTORE_SENTINEL_NAME}


@pytest.mark.asyncio
async def test_restore_sentinel_replacement_is_refused_and_foreign_marker_survives(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOMELAB_BACKUP_ALLOW_ISOLATED_RESTORE", "1")
    artifact = tmp_path / "bazarr-sentinel-race.zip"
    _write_exact_native_zip(artifact, "sentinel-race")
    parent, destination, sentinel = _prepare_restore_destination(tmp_path)
    original_rename = os.rename
    replaced = False

    def replace_sentinel_before_rename(
        source: str | bytes,
        destination_name: str | bytes,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        nonlocal replaced
        if source == RESTORE_SENTINEL_NAME and not replaced:
            replaced = True
            sentinel.unlink()
            sentinel.write_text("foreign-marker\n", encoding="utf-8")
            sentinel.chmod(0o600)
        original_rename(
            source,
            destination_name,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(os, "rename", replace_sentinel_before_rename)

    with pytest.raises(ValueError, match="sentinel changed"):
        await _plugin_class()(name="bazarr").restore(_restore_context(artifact, destination))

    assert replaced is True
    assert destination.exists() is False
    assert sentinel.read_text(encoding="utf-8") == "foreign-marker\n"
