from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import stat
import zipfile
from pathlib import Path

import httpx
import pytest

from app.core.plugins.base import BackupContext, RestoreContext
from app.core.plugins.loader import get_plugin, get_plugin_schema_path, list_plugins
from app.core.plugins.sidecar import read_backup_sidecar
from app.main import app

VERSION = "2.36.0"
CONFIG_SENTINEL = ".audiobookshelf-config-restore-destination"
METADATA_SENTINEL = ".audiobookshelf-metadata-restore-destination"
SENTINEL_CONTENT = "audiobookshelf-v2.36.0-isolated-restore-v1\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"fixture-image"


@pytest.fixture
def anyio_backend() -> tuple[str, dict[str, bool]]:
    return ("asyncio", {"use_uvloop": True})


def _create_database(path: Path, *, version: str = VERSION, root: bool = True) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            PRAGMA foreign_keys=ON;
            CREATE TABLE migrationsMeta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE users (
              id TEXT PRIMARY KEY, username TEXT, email TEXT, pash TEXT, type TEXT,
              token TEXT, isActive INTEGER, isLocked INTEGER, lastSeen INTEGER,
              permissions TEXT, bookmarks TEXT, extraData TEXT,
              createdAt INTEGER, updatedAt INTEGER
            );
            CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT, createdAt INTEGER, updatedAt INTEGER);
            CREATE TABLE libraries (id TEXT PRIMARY KEY, name TEXT, mediaType TEXT, provider TEXT,
              icon TEXT, settings TEXT, lastScan INTEGER, lastScanVersion TEXT, displayOrder INTEGER,
              extraData TEXT, createdAt INTEGER, updatedAt INTEGER);
            CREATE TABLE libraryFolders (id TEXT PRIMARY KEY, libraryId TEXT REFERENCES libraries(id),
              path TEXT, createdAt INTEGER, updatedAt INTEGER);
            CREATE TABLE libraryItems (id TEXT PRIMARY KEY, libraryId TEXT REFERENCES libraries(id),
              libraryFolderId TEXT REFERENCES libraryFolders(id), mediaId TEXT, mediaType TEXT,
              path TEXT, relPath TEXT, libraryFiles TEXT, title TEXT, authorNamesFirstLast TEXT,
              authorNamesLastFirst TEXT, titleIgnorePrefix TEXT, isFile INTEGER, mtime INTEGER,
              ctime INTEGER, birthtime INTEGER, ino INTEGER, size INTEGER, isMissing INTEGER,
              isInvalid INTEGER, lastScan INTEGER, lastScanVersion TEXT, extraData TEXT,
              createdAt INTEGER, updatedAt INTEGER);
            CREATE TABLE books (id TEXT PRIMARY KEY, coverPath TEXT, title TEXT);
            CREATE TABLE podcasts (id TEXT PRIMARY KEY, coverPath TEXT, title TEXT);
            CREATE TABLE authors (id TEXT PRIMARY KEY, imagePath TEXT, name TEXT);
            CREATE TABLE feeds (id TEXT PRIMARY KEY, coverPath TEXT);
            CREATE TABLE playbackSessions (id TEXT PRIMARY KEY, coverPath TEXT);
            CREATE TABLE collections (id TEXT PRIMARY KEY, name TEXT);
            CREATE TABLE playlists (id TEXT PRIMARY KEY, name TEXT);
            CREATE TABLE mediaProgresses (id TEXT PRIMARY KEY, currentTime REAL);
            """
        )
        connection.executemany(
            "INSERT INTO migrationsMeta(key, value) VALUES (?, ?)",
            (("version", version), ("maxVersion", version)),
        )
        if root:
            connection.execute(
                "INSERT INTO users(id, username, pash, type, token, isActive) VALUES (?, ?, ?, ?, ?, ?)",
                ("root-id", "fixture-root", "secret-hash", "root", "secret-token", 1),
            )
        connection.execute(
            "INSERT INTO books(id, coverPath, title) VALUES (?, ?, ?)",
            ("book-1", "/metadata/items/book-1/cover.png", "Synthetic Book"),
        )
        connection.execute(
            "INSERT INTO authors(id, imagePath, name) VALUES (?, ?, ?)",
            ("author-1", "/metadata/authors/author-1.png", "Synthetic Author"),
        )
        connection.execute(
            "INSERT INTO podcasts(id, coverPath, title) VALUES (?, ?, ?)",
            ("podcast-1", "/audiobooks/podcast/cover.png", "External Cover"),
        )


def _source(tmp_path: Path, *, version: str = VERSION, root: bool = True) -> tuple[Path, Path]:
    config = tmp_path / "source-config"
    metadata = tmp_path / "source-metadata"
    (metadata / "items" / "book-1").mkdir(parents=True)
    (metadata / "authors").mkdir(parents=True)
    config.mkdir()
    _create_database(config / "absdatabase.sqlite", version=version, root=root)
    (metadata / "items" / "book-1" / "cover.png").write_bytes(PNG)
    (metadata / "items" / "book-1" / "metadata.json").write_text(
        json.dumps({"id": "book-1", "title": "Synthetic Book"}), encoding="utf-8"
    )
    (metadata / "authors" / "author-1.png").write_bytes(PNG)
    return config, metadata


def _config(config: Path, metadata: Path) -> dict[str, str]:
    return {"config_path": str(config), "metadata_path": str(metadata)}


def _backup_context(config: Path, metadata: Path, backup_root: Path) -> BackupContext:
    return BackupContext(
        job_id="job-1",
        target_id="target-1",
        config=_config(config, metadata),
        metadata={"target_slug": "audiobookshelf-fixture", "backup_root": str(backup_root)},
    )


def _restore_context(artifact: Path, config: Path, metadata: Path) -> RestoreContext:
    return RestoreContext(
        job_id="restore-1",
        source_target_id="source",
        destination_target_id="destination",
        config=_config(config, metadata),
        artifact_path=str(artifact),
    )


def _restore_destinations(tmp_path: Path) -> tuple[Path, Path]:
    config = tmp_path / "restore-config"
    metadata = tmp_path / "restore-metadata"
    config.mkdir()
    metadata.mkdir()
    (config / CONFIG_SENTINEL).write_text(SENTINEL_CONTENT, encoding="utf-8")
    (metadata / METADATA_SENTINEL).write_text(SENTINEL_CONTENT, encoding="utf-8")
    return config, metadata


@pytest.mark.anyio
async def test_public_contract_and_schema() -> None:
    plugin = get_plugin("audiobookshelf")
    assert plugin.restore_capability == "partial"
    listed = {item["key"]: item for item in list_plugins()}
    assert listed["audiobookshelf"]["restore_capability"] == "partial"
    schema_path = get_plugin_schema_path("audiobookshelf")
    assert schema_path is not None
    schema = json.loads(Path(schema_path).read_text(encoding="utf-8"))
    assert schema["required"] == ["config_path", "metadata_path"]
    assert set(schema["properties"]) == {"config_path", "metadata_path"}


@pytest.mark.anyio
async def test_public_api_exposes_schema_and_test_result(tmp_path: Path) -> None:
    config, metadata = _source(tmp_path)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        plugins_response = await client.get("/api/v1/plugins/")
        schema_response = await client.get("/api/v1/plugins/audiobookshelf/schema")
        test_response = await client.post(
            "/api/v1/plugins/audiobookshelf/test", json=_config(config, metadata)
        )
        invalid_response = await client.post(
            "/api/v1/plugins/audiobookshelf/test", json={"config_path": "missing"}
        )
    assert plugins_response.status_code == 200
    assert any(item["key"] == "audiobookshelf" for item in plugins_response.json())
    assert schema_response.status_code == 200
    assert schema_response.json()["additionalProperties"] is False
    assert test_response.json() == {"ok": True}
    assert invalid_response.json()["ok"] is False
    assert "Invalid configuration" in invalid_response.json()["error"]


@pytest.mark.anyio
async def test_test_accepts_exact_read_only_source(tmp_path: Path) -> None:
    config, metadata = _source(tmp_path)
    assert await get_plugin("audiobookshelf").test(_config(config, metadata)) is True


@pytest.mark.anyio
@pytest.mark.parametrize("missing", ["config_path", "metadata_path"])
async def test_invalid_configuration_raises(tmp_path: Path, missing: str) -> None:
    config, metadata = _source(tmp_path)
    values = _config(config, metadata)
    values.pop(missing)
    with pytest.raises(ValueError, match="Invalid configuration"):
        await get_plugin("audiobookshelf").test(values)


@pytest.mark.anyio
async def test_source_symlink_is_refused(tmp_path: Path) -> None:
    config, metadata = _source(tmp_path)
    link = tmp_path / "metadata-link"
    link.symlink_to(metadata, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        await get_plugin("audiobookshelf").test(_config(config, link))


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("version", "root", "message"),
    [("2.35.0", True, "2.36.0"), (VERSION, False, "root user")],
)
async def test_wrong_database_contract_is_refused(
    tmp_path: Path, version: str, root: bool, message: str
) -> None:
    config, metadata = _source(tmp_path, version=version, root=root)
    with pytest.raises(ValueError, match=message):
        await get_plugin("audiobookshelf").test(_config(config, metadata))


@pytest.mark.anyio
async def test_backup_is_private_native_shape_and_has_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, metadata = _source(tmp_path)
    backup_root = tmp_path / "backups"
    monkeypatch.setattr("app.plugins.audiobookshelf.plugin.BACKUP_BASE_PATH", str(backup_root))
    plugin = get_plugin("audiobookshelf")
    result = await plugin.backup(_backup_context(config, metadata, backup_root))
    artifact = Path(result["artifact_path"])
    assert artifact.suffix == ".audiobookshelf"
    assert stat.S_IMODE(artifact.stat().st_mode) == 0o600
    assert not any(path.name.endswith(".tmp") for path in backup_root.rglob("*"))
    with zipfile.ZipFile(artifact) as archive:
        assert set(archive.namelist()) == {
            "absdatabase.sqlite",
            "details",
            "metadata-items/book-1/cover.png",
            "metadata-items/book-1/metadata.json",
            "metadata-authors/author-1.png",
        }
        details = json.loads(archive.read("details"))
        assert details["serverVersion"] == VERSION
        assert archive.testzip() is None
    sidecar = read_backup_sidecar(str(artifact))
    assert sidecar is not None
    assert sidecar["plugin_name"] == "audiobookshelf"
    assert sidecar["artifact_path"] == str(artifact)
    assert len(hashlib.sha256(artifact.read_bytes()).hexdigest()) == 64


@pytest.mark.anyio
async def test_backup_rejects_missing_reference_and_bad_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, metadata = _source(tmp_path)
    monkeypatch.setattr(
        "app.plugins.audiobookshelf.plugin.BACKUP_BASE_PATH", str(tmp_path / "backups")
    )
    (metadata / "items" / "book-1" / "cover.png").unlink()
    with pytest.raises(ValueError, match="referenced metadata file"):
        await get_plugin("audiobookshelf").backup(
            _backup_context(config, metadata, tmp_path / "backups")
        )
    (metadata / "items" / "book-1" / "cover.png").write_bytes(PNG)
    (metadata / "items" / "book-1" / "metadata.json").write_text("{", encoding="utf-8")
    with pytest.raises(ValueError, match="metadata.json"):
        await get_plugin("audiobookshelf").backup(
            _backup_context(config, metadata, tmp_path / "backups")
        )


@pytest.mark.anyio
async def test_two_backups_are_unique(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config, metadata = _source(tmp_path)
    backup_root = tmp_path / "backups"
    monkeypatch.setattr("app.plugins.audiobookshelf.plugin.BACKUP_BASE_PATH", str(backup_root))
    plugin = get_plugin("audiobookshelf")
    first = Path(
        (await plugin.backup(_backup_context(config, metadata, backup_root)))["artifact_path"]
    )
    second = Path(
        (await plugin.backup(_backup_context(config, metadata, backup_root)))["artifact_path"]
    )
    assert first != second
    assert first.exists() and second.exists()


@pytest.mark.anyio
async def test_restore_is_create_only_and_revalidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_config, source_metadata = _source(tmp_path)
    backup_root = tmp_path / "backups"
    monkeypatch.setattr("app.plugins.audiobookshelf.plugin.BACKUP_BASE_PATH", str(backup_root))
    plugin = get_plugin("audiobookshelf")
    artifact = Path(
        (await plugin.backup(_backup_context(source_config, source_metadata, backup_root)))[
            "artifact_path"
        ]
    )
    config, metadata = _restore_destinations(tmp_path)
    result = await plugin.restore(_restore_context(artifact, config, metadata))
    assert result["status"] == "partial"
    assert (config / "absdatabase.sqlite").is_file()
    assert (metadata / "items" / "book-1" / "cover.png").read_bytes() == PNG
    assert (metadata / "authors" / "author-1.png").read_bytes() == PNG
    assert await plugin.test(_config(config, metadata)) is True


@pytest.mark.anyio
async def test_restore_refuses_wrong_sentinel_collision_and_overlap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_config, source_metadata = _source(tmp_path)
    backup_root = tmp_path / "backups"
    monkeypatch.setattr("app.plugins.audiobookshelf.plugin.BACKUP_BASE_PATH", str(backup_root))
    plugin = get_plugin("audiobookshelf")
    artifact = Path(
        (await plugin.backup(_backup_context(source_config, source_metadata, backup_root)))[
            "artifact_path"
        ]
    )
    config, metadata = _restore_destinations(tmp_path)
    (config / CONFIG_SENTINEL).write_text("wrong\n", encoding="utf-8")
    with pytest.raises(ValueError, match="sentinel"):
        await plugin.restore(_restore_context(artifact, config, metadata))
    (config / CONFIG_SENTINEL).write_text(SENTINEL_CONTENT, encoding="utf-8")
    (config / "foreign").write_text("preserve", encoding="utf-8")
    with pytest.raises(ValueError, match="empty"):
        await plugin.restore(_restore_context(artifact, config, metadata))
    assert (config / "foreign").read_text(encoding="utf-8") == "preserve"
    with pytest.raises(ValueError, match="sentinel"):
        await plugin.restore(_restore_context(artifact, source_config, source_metadata))


@pytest.mark.anyio
async def test_restore_rejects_duplicate_and_corrupt_archives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_config, source_metadata = _source(tmp_path)
    backup_root = tmp_path / "backups"
    monkeypatch.setattr("app.plugins.audiobookshelf.plugin.BACKUP_BASE_PATH", str(backup_root))
    plugin = get_plugin("audiobookshelf")
    artifact = Path(
        (await plugin.backup(_backup_context(source_config, source_metadata, backup_root)))[
            "artifact_path"
        ]
    )
    duplicate = tmp_path / "duplicate.audiobookshelf"
    with zipfile.ZipFile(artifact) as source, zipfile.ZipFile(duplicate, "w") as target:
        for member in source.infolist():
            target.writestr(member.filename, source.read(member))
        target.writestr("details", b"{}")
    config, metadata = _restore_destinations(tmp_path)
    with pytest.raises(ValueError, match="duplicate"):
        await plugin.restore(_restore_context(duplicate, config, metadata))
    corrupt = tmp_path / "corrupt.audiobookshelf"
    corrupt.write_bytes(b"not a zip")
    with pytest.raises(ValueError, match="archive"):
        await plugin.restore(_restore_context(corrupt, config, metadata))


@pytest.mark.anyio
async def test_cancelled_backup_cleans_partial_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, metadata = _source(tmp_path)
    backup_root = tmp_path / "backups"
    monkeypatch.setattr("app.plugins.audiobookshelf.plugin.BACKUP_BASE_PATH", str(backup_root))
    monkeypatch.setattr("app.plugins.audiobookshelf.plugin.BACKUP_TIMEOUT_SECONDS", 30.0)
    monkeypatch.setattr("app.plugins.audiobookshelf.plugin._WORKER_TEST_DELAY_SECONDS", 30.0)
    task = asyncio.create_task(
        get_plugin("audiobookshelf").backup(_backup_context(config, metadata, backup_root))
    )
    await asyncio.sleep(0.25)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not any(path.is_file() for path in backup_root.rglob("*"))


@pytest.mark.anyio
async def test_validation_timeout_stops_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, metadata = _source(tmp_path)
    monkeypatch.setattr("app.plugins.audiobookshelf.plugin.VALIDATION_TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr("app.plugins.audiobookshelf.plugin._WORKER_TEST_DELAY_SECONDS", 30.0)
    with pytest.raises(TimeoutError, match="validation timed out"):
        await get_plugin("audiobookshelf").test(_config(config, metadata))


@pytest.mark.anyio
async def test_cancelled_restore_preserves_fresh_destinations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_config, source_metadata = _source(tmp_path)
    backup_root = tmp_path / "backups"
    monkeypatch.setattr("app.plugins.audiobookshelf.plugin.BACKUP_BASE_PATH", str(backup_root))
    plugin = get_plugin("audiobookshelf")
    artifact = Path(
        (await plugin.backup(_backup_context(source_config, source_metadata, backup_root)))[
            "artifact_path"
        ]
    )
    config, metadata = _restore_destinations(tmp_path)
    monkeypatch.setattr("app.plugins.audiobookshelf.plugin._WORKER_TEST_DELAY_SECONDS", 30.0)
    task = asyncio.create_task(plugin.restore(_restore_context(artifact, config, metadata)))
    await asyncio.sleep(0.25)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert {entry.name for entry in config.iterdir()} == {CONFIG_SENTINEL}
    assert {entry.name for entry in metadata.iterdir()} == {METADATA_SENTINEL}


@pytest.mark.anyio
async def test_status_is_honest(tmp_path: Path) -> None:
    config, metadata = _source(tmp_path)
    plugin = get_plugin("audiobookshelf")
    assert (await plugin.get_status(_backup_context(config, metadata, tmp_path / "backups")))[
        "status"
    ] == "ok"
    (config / "absdatabase.sqlite").unlink()
    assert (await plugin.get_status(_backup_context(config, metadata, tmp_path / "backups")))[
        "status"
    ] == "unknown"
