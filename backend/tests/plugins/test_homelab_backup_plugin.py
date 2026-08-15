from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import stat
import threading
import time
import warnings
import zipfile
from importlib.metadata import version as installed_package_version
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import create_engine

from app.core.db import Base
from app.core.plugins import artifacts as artifacts_module
from app.core.plugins.base import BackupContext, RestoreContext
from app.core.plugins.loader import get_plugin, get_plugin_schema_path, list_plugins
from app.core.plugins.sidecar import read_backup_sidecar
from app.main import app
from app.plugins.homelab_backup import plugin as homelab_backup_module
from app.plugins.homelab_backup.plugin import (
    RESTORE_SENTINEL_CONTENT,
    RESTORE_SENTINEL_NAME,
    HomelabBackupPlugin,
)


def _create_app_database(path: Path) -> None:
    import app.models  # noqa: F401

    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    engine.dispose()


def _insert_target(path: Path, *, name: str, secret: str) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            INSERT INTO targets (
                name, slug, plugin_name, plugin_config_json,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (name, name.lower(), "pihole", json.dumps({"password": secret})),
        )


@pytest.fixture
def anyio_backend() -> tuple[str, dict[str, bool]]:
    """Use uvloop on this dev VM because its default loop misses thread wakeups."""

    return ("asyncio", {"use_uvloop": True})


@pytest.mark.anyio
async def test_homelab_backup_discovery_schema_and_configuration_contract() -> None:
    plugin = get_plugin("homelab_backup")

    assert isinstance(plugin, HomelabBackupPlugin)
    assert plugin.restore_capability == "partial"
    assert any(item["key"] == "homelab_backup" for item in list_plugins())

    schema_path = get_plugin_schema_path("homelab_backup")
    assert schema_path is not None
    schema = json.loads(Path(schema_path).read_text(encoding="utf-8"))
    assert schema["required"] == ["database_path"]
    assert set(schema["properties"]) == {"database_path"}
    assert schema["properties"]["database_path"]["default"] == ("/app/db/homelab_backup.db")

    assert await plugin.validate_config({"database_path": "/safe/isolated/homelab_backup.db"})
    for invalid in (
        {},
        {"database_path": None},
        {"database_path": "homelab_backup.db"},
        {"database_path": "/safe/isolated/other.db"},
        {"database_path": "/safe/../isolated/homelab_backup.db"},
        {"database_path": "/app/db/homelab_backup.db", "extra": True},
    ):
        assert not await plugin.validate_config(invalid)


@pytest.mark.anyio
async def test_connectivity_validates_database_and_reports_observed_status(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "homelab_backup.db"
    _create_app_database(database_path)
    plugin = HomelabBackupPlugin(name="homelab_backup")
    config = {"database_path": str(database_path)}

    assert await plugin.test(config) is True
    assert await plugin.get_status(BackupContext(job_id="1", target_id="1", config=config)) == {
        "status": "ok"
    }


@pytest.mark.anyio
async def test_connectivity_accepts_only_a_sentinel_marked_fresh_destination(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "restore"
    destination.mkdir()
    database_path = destination / "homelab_backup.db"
    plugin = HomelabBackupPlugin(name="homelab_backup")
    config = {"database_path": str(database_path)}

    with pytest.raises(FileNotFoundError, match="sentinel"):
        await plugin.test(config)

    sentinel = destination / RESTORE_SENTINEL_NAME
    sentinel.write_text(RESTORE_SENTINEL_CONTENT, encoding="utf-8")
    assert await plugin.test(config) is True
    assert await plugin.get_status(
        BackupContext(job_id="1", target_id="restore", config=config)
    ) == {"status": "unknown"}

    (destination / "unexpected").write_text("not empty", encoding="utf-8")
    with pytest.raises(ValueError, match="otherwise empty"):
        await plugin.test(config)


@pytest.mark.anyio
async def test_connectivity_refuses_sentinel_destination_under_forbidden_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forbidden_root = tmp_path / "forbidden"
    forbidden_root.mkdir()
    (forbidden_root / RESTORE_SENTINEL_NAME).write_text(
        RESTORE_SENTINEL_CONTENT,
        encoding="utf-8",
    )
    monkeypatch.setattr(
        homelab_backup_module,
        "_FORBIDDEN_RESTORE_ROOTS",
        (forbidden_root,),
    )
    plugin = HomelabBackupPlugin(name="homelab_backup")

    with pytest.raises(ValueError, match="forbidden live or backup path"):
        await plugin.test({"database_path": str(forbidden_root / "homelab_backup.db")})


@pytest.mark.anyio
async def test_connectivity_rejects_invalid_schema_and_foreign_keys(tmp_path: Path) -> None:
    plugin = HomelabBackupPlugin(name="homelab_backup")
    incomplete_path = tmp_path / "homelab_backup.db"
    with sqlite3.connect(incomplete_path) as connection:
        connection.execute("CREATE TABLE targets (id INTEGER PRIMARY KEY)")

    with pytest.raises(RuntimeError, match="required tables"):
        await plugin.test({"database_path": str(incomplete_path)})

    incomplete_path.unlink()
    _create_app_database(incomplete_path)
    with sqlite3.connect(incomplete_path) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            """
            INSERT INTO targets (
                name, slug, plugin_name, plugin_config_json, group_id,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            ("broken", "broken", "pihole", "secret-marker", 999),
        )

    with pytest.raises(RuntimeError, match="foreign-key") as error:
        await plugin.test({"database_path": str(incomplete_path)})
    assert "secret-marker" not in str(error.value)


@pytest.mark.anyio
async def test_connectivity_rejects_invalid_configuration_and_symlinks(tmp_path: Path) -> None:
    plugin = HomelabBackupPlugin(name="homelab_backup")
    with pytest.raises(ValueError, match="Invalid configuration"):
        await plugin.test({})

    actual_path = tmp_path / "actual" / "homelab_backup.db"
    actual_path.parent.mkdir()
    _create_app_database(actual_path)
    linked_dir = tmp_path / "linked"
    linked_dir.symlink_to(actual_path.parent, target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic link"):
        await plugin.test({"database_path": str(linked_dir / "homelab_backup.db")})


@pytest.mark.anyio
async def test_backup_publishes_private_validated_snapshot_and_sidecar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "source" / "homelab_backup.db"
    database_path.parent.mkdir()
    _create_app_database(database_path)
    secret = "synthetic-secret-marker"
    _insert_target(database_path, name="Proof", secret=secret)
    backup_root = tmp_path / "backups"
    monkeypatch.setattr(homelab_backup_module, "BACKUP_BASE_PATH", str(backup_root))
    plugin = HomelabBackupPlugin(name="homelab_backup")
    context = BackupContext(
        job_id="11",
        target_id="22",
        config={"database_path": str(database_path)},
        metadata={"target_slug": "self-primary"},
    )

    result = await plugin.backup(context)

    artifact_path = Path(result["artifact_path"])
    assert artifact_path.is_file()
    assert stat.S_IMODE(artifact_path.stat().st_mode) == 0o600
    sidecar = read_backup_sidecar(str(artifact_path))
    assert sidecar is not None
    assert sidecar["plugin_name"] == "homelab_backup"
    with zipfile.ZipFile(artifact_path) as archive:
        assert set(archive.namelist()) == {"manifest.json", "homelab_backup.db"}
        manifest = json.loads(archive.read("manifest.json"))
        database_bytes = archive.read("homelab_backup.db")
    assert manifest["format_version"] == 1
    assert manifest["application_version"] == installed_package_version("homelab-backup")
    assert manifest["database"]["size_bytes"] == len(database_bytes)
    assert manifest["database"]["sha256"] == hashlib.sha256(database_bytes).hexdigest()
    assert manifest["row_counts"]["targets"] == 1
    assert manifest["required_tables"] == sorted(homelab_backup_module.REQUIRED_SCHEMA)
    assert len(manifest["schema_sha256"]) == 64

    restored_snapshot = tmp_path / "inspected.db"
    restored_snapshot.write_bytes(database_bytes)
    with sqlite3.connect(restored_snapshot) as connection:
        stored_config = connection.execute(
            "SELECT plugin_config_json FROM targets WHERE name = 'Proof'"
        ).fetchone()[0]
    assert json.loads(stored_config)["password"] == secret


@pytest.mark.anyio
async def test_two_backups_are_unique_and_capture_later_committed_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "source" / "homelab_backup.db"
    database_path.parent.mkdir()
    _create_app_database(database_path)
    backup_root = tmp_path / "backups"
    monkeypatch.setattr(homelab_backup_module, "BACKUP_BASE_PATH", str(backup_root))
    plugin = HomelabBackupPlugin(name="homelab_backup")
    context = BackupContext(
        job_id="11",
        target_id="22",
        config={"database_path": str(database_path)},
        metadata={"target_slug": "self-primary"},
    )

    first = Path((await plugin.backup(context))["artifact_path"])
    _insert_target(database_path, name="Later", secret="second-marker")
    second = Path((await plugin.backup(context))["artifact_path"])

    assert first != second
    with zipfile.ZipFile(first) as archive:
        first_manifest = json.loads(archive.read("manifest.json"))
    with zipfile.ZipFile(second) as archive:
        second_manifest = json.loads(archive.read("manifest.json"))
    assert first_manifest["row_counts"]["targets"] == 0
    assert second_manifest["row_counts"]["targets"] == 1


@pytest.mark.anyio
async def test_backup_snapshot_is_private_before_archive_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "source" / "homelab_backup.db"
    database_path.parent.mkdir()
    _create_app_database(database_path)
    backup_root = tmp_path / "backups"
    original_writer = homelab_backup_module._write_backup_archive

    def assert_private_snapshot(snapshot_path: Path, archive_path: Path) -> None:
        assert stat.S_IMODE(snapshot_path.stat().st_mode) == 0o600
        original_writer(snapshot_path, archive_path)

    monkeypatch.setattr(homelab_backup_module, "BACKUP_BASE_PATH", str(backup_root))
    monkeypatch.setattr(
        homelab_backup_module,
        "_write_backup_archive",
        assert_private_snapshot,
    )
    plugin = HomelabBackupPlugin(name="homelab_backup")

    await plugin.backup(
        BackupContext(
            job_id="private",
            target_id="private",
            config={"database_path": str(database_path)},
            metadata={"target_slug": "private"},
        )
    )


@pytest.mark.anyio
async def test_backup_revalidates_written_zip_before_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "source" / "homelab_backup.db"
    database_path.parent.mkdir()
    _create_app_database(database_path)
    backup_root = tmp_path / "backups"
    original_writer = homelab_backup_module._write_backup_archive

    def write_tampered_archive(snapshot_path: Path, archive_path: Path) -> None:
        original_writer(snapshot_path, archive_path)
        replacement = archive_path.with_suffix(".tampered.zip")
        _tamper_archive(archive_path, replacement, "digest")
        replacement.replace(archive_path)

    monkeypatch.setattr(homelab_backup_module, "BACKUP_BASE_PATH", str(backup_root))
    monkeypatch.setattr(
        homelab_backup_module,
        "_write_backup_archive",
        write_tampered_archive,
    )
    plugin = HomelabBackupPlugin(name="homelab_backup")

    with pytest.raises(ValueError, match="digest"):
        await plugin.backup(
            BackupContext(
                job_id="tampered",
                target_id="tampered",
                config={"database_path": str(database_path)},
                metadata={"target_slug": "tampered"},
            )
        )
    assert not list(backup_root.rglob("*.zip"))
    assert not list(backup_root.rglob("*.meta.json"))


@pytest.mark.anyio
async def test_backup_cleans_artifact_when_private_mode_or_sidecar_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "source" / "homelab_backup.db"
    database_path.parent.mkdir()
    _create_app_database(database_path)
    backup_root = tmp_path / "backups"
    plugin = HomelabBackupPlugin(name="homelab_backup")
    context = BackupContext(
        job_id="failure",
        target_id="failure",
        config={"database_path": str(database_path)},
        metadata={"target_slug": "failure"},
    )
    original_chmod = homelab_backup_module.os.chmod
    original_writer = homelab_backup_module._write_backup_archive
    original_sidecar = artifacts_module.write_backup_sidecar
    sidecar_called = False

    def write_public_archive(snapshot_path: Path, archive_path: Path) -> None:
        original_writer(snapshot_path, archive_path)
        original_chmod(archive_path, 0o644)

    def track_sidecar(*args: Any, **kwargs: Any) -> None:
        nonlocal sidecar_called
        sidecar_called = True
        original_sidecar(*args, **kwargs)

    monkeypatch.setattr(homelab_backup_module, "BACKUP_BASE_PATH", str(backup_root))
    monkeypatch.setattr(
        homelab_backup_module,
        "_write_backup_archive",
        write_public_archive,
    )
    monkeypatch.setattr(artifacts_module, "write_backup_sidecar", track_sidecar)
    with pytest.raises(PermissionError, match="not private"):
        await plugin.backup(context)
    assert sidecar_called is False
    assert not list(backup_root.rglob("*.zip"))

    monkeypatch.setattr(
        homelab_backup_module,
        "_write_backup_archive",
        original_writer,
    )

    def fail_sidecar(*_args: object, **_kwargs: object) -> None:
        raise OSError("injected sidecar failure")

    monkeypatch.setattr(artifacts_module, "write_backup_sidecar", fail_sidecar)
    with pytest.raises(OSError, match="sidecar failure"):
        await plugin.backup(context)
    assert not list(backup_root.rglob("*.zip"))
    assert not list(backup_root.rglob("*.meta.json"))


async def _create_backup_for_restore(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[HomelabBackupPlugin, Path, str]:
    source_path = tmp_path / "source" / "homelab_backup.db"
    source_path.parent.mkdir()
    _create_app_database(source_path)
    secret = "restore-only-synthetic-marker"
    _insert_target(source_path, name="RestoreProof", secret=secret)
    backup_root = tmp_path / "backups"
    monkeypatch.setattr(homelab_backup_module, "BACKUP_BASE_PATH", str(backup_root))
    plugin = HomelabBackupPlugin(name="homelab_backup")
    artifact_path = Path(
        (
            await plugin.backup(
                BackupContext(
                    job_id="1",
                    target_id="2",
                    config={"database_path": str(source_path)},
                    metadata={"target_slug": "self-source"},
                )
            )
        )["artifact_path"]
    )
    return plugin, artifact_path, secret


def _fresh_restore_path(tmp_path: Path, name: str = "restore") -> Path:
    parent = tmp_path / name
    parent.mkdir()
    (parent / RESTORE_SENTINEL_NAME).write_text(
        RESTORE_SENTINEL_CONTENT,
        encoding="utf-8",
    )
    return parent / "homelab_backup.db"


def _tamper_archive(source: Path, destination: Path, variant: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(source) as archive:
        manifest_bytes = archive.read("manifest.json")
        database_bytes = archive.read("homelab_backup.db")
    manifest = json.loads(manifest_bytes)
    if variant == "application-version":
        manifest["application_version"] = "999.0.0"
    elif variant == "digest":
        manifest["database"]["sha256"] = "0" * 64
    elif variant == "row-count":
        manifest["row_counts"]["targets"] += 1
    elif variant == "invalid-database":
        database_bytes = b"not a SQLite database"
        manifest["database"]["size_bytes"] = len(database_bytes)
        manifest["database"]["sha256"] = hashlib.sha256(database_bytes).hexdigest()

    with zipfile.ZipFile(destination, mode="w") as archive:
        if variant == "invalid-json":
            archive.writestr("manifest.json", b"{")
        else:
            archive.writestr("manifest.json", json.dumps(manifest))
        if variant == "symlink":
            member = zipfile.ZipInfo("homelab_backup.db")
            member.create_system = 3
            member.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(member, b"manifest.json")
        elif variant != "missing-database":
            archive.writestr("homelab_backup.db", database_bytes)
        if variant == "extra-member":
            archive.writestr("../escape", b"unsafe")
        if variant == "duplicate-manifest":
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                archive.writestr("manifest.json", json.dumps(manifest))


@pytest.mark.anyio
async def test_restore_creates_and_revalidates_fresh_offline_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin, artifact_path, secret = await _create_backup_for_restore(tmp_path, monkeypatch)
    destination_path = _fresh_restore_path(tmp_path)
    context = RestoreContext(
        job_id="restore-1",
        source_target_id="2",
        destination_target_id="3",
        config={"database_path": str(destination_path)},
        artifact_path=str(artifact_path),
    )

    result = await plugin.restore(context)

    assert result["status"] == "partial"
    assert result["restored_path"] == str(destination_path)
    assert "isolated backend" in result["message"]
    assert stat.S_IMODE(destination_path.stat().st_mode) == 0o600
    assert not list(destination_path.parent.glob(".*.restore.tmp"))
    with sqlite3.connect(destination_path) as connection:
        stored_config = connection.execute(
            "SELECT plugin_config_json FROM targets WHERE name = 'RestoreProof'"
        ).fetchone()[0]
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    assert json.loads(stored_config)["password"] == secret


@pytest.mark.anyio
@pytest.mark.parametrize("unsafe_root", ["/app/db", "/backups/isolated"])
async def test_restore_refuses_live_and_backup_roots_before_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unsafe_root: str,
) -> None:
    plugin, artifact_path, _secret = await _create_backup_for_restore(tmp_path, monkeypatch)
    destination_path = Path(unsafe_root) / "homelab_backup.db"

    with pytest.raises(ValueError, match="forbidden live or backup path"):
        await plugin.restore(
            RestoreContext(
                job_id="restore-unsafe",
                source_target_id="2",
                destination_target_id="3",
                config={"database_path": str(destination_path)},
                artifact_path=str(artifact_path),
            )
        )


@pytest.mark.anyio
async def test_restore_refuses_existing_or_nonempty_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin, artifact_path, _secret = await _create_backup_for_restore(tmp_path, monkeypatch)
    destination_path = _fresh_restore_path(tmp_path)
    destination_path.write_bytes(b"existing-state")
    before = destination_path.read_bytes()

    with pytest.raises(ValueError, match="already exists"):
        await plugin.restore(
            RestoreContext(
                job_id="restore-existing",
                source_target_id="2",
                destination_target_id="3",
                config={"database_path": str(destination_path)},
                artifact_path=str(artifact_path),
            )
        )
    assert destination_path.read_bytes() == before

    destination_path.unlink()
    (destination_path.parent / "unexpected").write_text("keep", encoding="utf-8")
    with pytest.raises(ValueError, match="otherwise empty"):
        await plugin.restore(
            RestoreContext(
                job_id="restore-nonempty",
                source_target_id="2",
                destination_target_id="3",
                config={"database_path": str(destination_path)},
                artifact_path=str(artifact_path),
            )
        )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "variant",
    [
        "application-version",
        "digest",
        "row-count",
        "invalid-database",
        "invalid-json",
        "symlink",
        "missing-database",
        "extra-member",
        "duplicate-manifest",
    ],
)
async def test_restore_rejects_unusable_or_unsafe_archives_before_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    variant: str,
) -> None:
    plugin, artifact_path, _secret = await _create_backup_for_restore(tmp_path, monkeypatch)
    tampered_path = tmp_path / "tampered-store" / f"{variant}.zip"
    _tamper_archive(artifact_path, tampered_path, variant)
    destination_path = _fresh_restore_path(tmp_path)

    with pytest.raises((ValueError, RuntimeError)):
        await plugin.restore(
            RestoreContext(
                job_id="restore-tampered",
                source_target_id="2",
                destination_target_id="3",
                config={"database_path": str(destination_path)},
                artifact_path=str(tampered_path),
            )
        )
    assert not destination_path.exists()
    assert {item.name for item in destination_path.parent.iterdir()} == {RESTORE_SENTINEL_NAME}


@pytest.mark.anyio
@pytest.mark.parametrize("companion", ["-wal", "-shm"])
async def test_restore_refuses_sqlite_companion_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    companion: str,
) -> None:
    plugin, artifact_path, _secret = await _create_backup_for_restore(tmp_path, monkeypatch)
    destination_path = _fresh_restore_path(tmp_path)
    companion_path = Path(f"{destination_path}{companion}")
    companion_path.write_bytes(b"existing-companion")

    with pytest.raises(ValueError, match="companion files"):
        await plugin.restore(
            RestoreContext(
                job_id="restore-companion",
                source_target_id="2",
                destination_target_id="3",
                config={"database_path": str(destination_path)},
                artifact_path=str(artifact_path),
            )
        )
    assert companion_path.read_bytes() == b"existing-companion"


@pytest.mark.anyio
async def test_restore_removes_new_database_after_post_publication_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin, artifact_path, _secret = await _create_backup_for_restore(tmp_path, monkeypatch)
    destination_path = _fresh_restore_path(tmp_path)
    original_assertion = homelab_backup_module._assert_database_matches_manifest

    def fail_after_publish(path: Path, manifest: dict[str, object]) -> None:
        if path == destination_path:
            raise RuntimeError("injected post-publication validation failure")
        original_assertion(path, manifest)

    monkeypatch.setattr(
        homelab_backup_module,
        "_assert_database_matches_manifest",
        fail_after_publish,
    )

    with pytest.raises(RuntimeError, match="injected post-publication"):
        await plugin.restore(
            RestoreContext(
                job_id="restore-failure",
                source_target_id="2",
                destination_target_id="3",
                config={"database_path": str(destination_path)},
                artifact_path=str(artifact_path),
            )
        )
    assert not destination_path.exists()
    assert not list(destination_path.parent.glob(".*.restore.tmp"))


@pytest.mark.anyio
async def test_restore_cleans_staging_when_atomic_create_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin, artifact_path, _secret = await _create_backup_for_restore(tmp_path, monkeypatch)
    destination_path = _fresh_restore_path(tmp_path)

    def fail_link(_source: Path, _destination: Path) -> None:
        raise OSError("injected link failure")

    monkeypatch.setattr(homelab_backup_module.os, "link", fail_link)
    with pytest.raises(OSError, match="link failure"):
        await plugin.restore(
            RestoreContext(
                job_id="restore-link-failure",
                source_target_id="2",
                destination_target_id="3",
                config={"database_path": str(destination_path)},
                artifact_path=str(artifact_path),
            )
        )
    assert not destination_path.exists()
    assert not list(destination_path.parent.glob(".*.restore.tmp"))


@pytest.mark.anyio
async def test_concurrent_backups_of_one_database_publish_distinct_valid_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "source" / "homelab_backup.db"
    database_path.parent.mkdir()
    _create_app_database(database_path)
    _insert_target(database_path, name="Concurrent", secret="bounded-marker")
    backup_root = tmp_path / "backups"
    monkeypatch.setattr(homelab_backup_module, "BACKUP_BASE_PATH", str(backup_root))
    plugin = HomelabBackupPlugin(name="homelab_backup")

    results = await asyncio.gather(
        *(
            plugin.backup(
                BackupContext(
                    job_id=str(index),
                    target_id=str(index),
                    config={"database_path": str(database_path)},
                    metadata={"target_slug": f"self-{index}"},
                )
            )
            for index in range(2)
        )
    )

    paths = [Path(result["artifact_path"]) for result in results]
    assert paths[0] != paths[1]
    for path in paths:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        with zipfile.ZipFile(path) as archive:
            manifest = json.loads(archive.read("manifest.json"))
        assert manifest["row_counts"]["targets"] == 1


@pytest.mark.anyio
async def test_backup_timeout_stops_snapshot_worker_and_cleans_partial_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "source" / "homelab_backup.db"
    database_path.parent.mkdir()
    _create_app_database(database_path)
    backup_root = tmp_path / "backups"
    stopped = threading.Event()

    def blocked_snapshot(
        _source_path: Path,
        _snapshot_path: Path,
        stop_event: threading.Event,
        _deadline: float,
    ) -> None:
        while not stop_event.wait(0.001):
            pass
        stopped.set()
        raise RuntimeError("worker stopped")

    monkeypatch.setattr(homelab_backup_module, "BACKUP_BASE_PATH", str(backup_root))
    monkeypatch.setattr(homelab_backup_module, "_SNAPSHOT_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(homelab_backup_module, "_snapshot_database", blocked_snapshot)
    plugin = HomelabBackupPlugin(name="homelab_backup")

    with pytest.raises(TimeoutError, match="timed out"):
        await plugin.backup(
            BackupContext(
                job_id="timeout",
                target_id="1",
                config={"database_path": str(database_path)},
                metadata={"target_slug": "self-timeout"},
            )
        )
    assert stopped.wait(1.0)
    assert not list(backup_root.rglob("*.zip"))
    assert not list(backup_root.rglob("*.tmp"))


@pytest.mark.anyio
async def test_backup_cancellation_stops_snapshot_worker_and_cleans_partial_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "source" / "homelab_backup.db"
    database_path.parent.mkdir()
    _create_app_database(database_path)
    backup_root = tmp_path / "backups"
    started = threading.Event()
    stopped = threading.Event()

    def blocked_snapshot(
        _source_path: Path,
        _snapshot_path: Path,
        stop_event: threading.Event,
        _deadline: float,
    ) -> None:
        started.set()
        while not stop_event.wait(0.001):
            time.sleep(0.001)
        stopped.set()

    monkeypatch.setattr(homelab_backup_module, "BACKUP_BASE_PATH", str(backup_root))
    monkeypatch.setattr(homelab_backup_module, "_snapshot_database", blocked_snapshot)
    plugin = HomelabBackupPlugin(name="homelab_backup")
    task = asyncio.create_task(
        plugin.backup(
            BackupContext(
                job_id="cancel",
                target_id="1",
                config={"database_path": str(database_path)},
                metadata={"target_slug": "self-cancel"},
            )
        )
    )
    assert await asyncio.to_thread(started.wait, 2.0)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert stopped.wait(1.0)
    assert not list(backup_root.rglob("*.zip"))
    assert not list(backup_root.rglob("*.tmp"))


@pytest.mark.anyio
async def test_plugin_api_exposes_schema_and_real_read_only_connectivity(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "homelab_backup.db"
    _create_app_database(database_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        plugins_response = await client.get("/api/v1/plugins/")
        schema_response = await client.get("/api/v1/plugins/homelab_backup/schema")
        test_response = await client.post(
            "/api/v1/plugins/homelab_backup/test",
            json={"database_path": str(database_path)},
        )
        invalid_response = await client.post(
            "/api/v1/plugins/homelab_backup/test",
            json={"database_path": "relative.db"},
        )

    assert plugins_response.status_code == 200
    assert any(
        item["key"] == "homelab_backup" and item["restore_capability"] == "partial"
        for item in plugins_response.json()
    )
    assert schema_response.status_code == 200
    assert set(schema_response.json()["properties"]) == {"database_path"}
    assert test_response.json() == {"ok": True}
    assert invalid_response.json()["ok"] is False
    assert "Invalid configuration" in invalid_response.json()["error"]
