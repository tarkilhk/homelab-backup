import io
import os
import sqlite3
import tarfile
from pathlib import Path
from typing import Dict

import httpx
import pytest

from app.core.plugins.base import BackupContext, RestoreContext
from app.plugins.vaultwarden import VaultWardenPlugin


def make_tar_bytes(files: Dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, content in files.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def make_client(handler: httpx.MockTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=handler, base_url="http://docker")


def make_sqlite_bytes(tmp_path: Path) -> bytes:
    database = tmp_path / "source.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE secrets (id INTEGER PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO secrets (value) VALUES ('encrypted')")
    return database.read_bytes()


@pytest.mark.asyncio
async def test_validate_config() -> None:
    plugin = VaultWardenPlugin(name="vaultwarden")
    assert await plugin.validate_config({"container_name": "vaultwarden", "data_path": "/data"})
    assert await plugin.validate_config({"container_name": "vw"}) is True
    assert await plugin.validate_config({"container_name": ""}) is False
    assert await plugin.validate_config({"container_name": 123}) is False  # type: ignore[arg-type]
    assert await plugin.validate_config({"container_name": "vw", "data_path": ""}) is False


@pytest.mark.asyncio
async def test_test_success(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/containers/vw/json":
            return httpx.Response(200, json={"Id": "abc"})
        if request.url.path == "/containers/vw/archive":
            path = request.url.params.get("path", "")
            if path.endswith("db.sqlite3"):
                return httpx.Response(200, content=make_tar_bytes({"db.sqlite3": b"db"}))
            if path.endswith("config.json"):
                return httpx.Response(200, content=make_tar_bytes({"config.json": b"cfg"}))
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        VaultWardenPlugin, "_docker_client", lambda self: make_client(transport)  # type: ignore[misc]
    )

    plugin = VaultWardenPlugin(name="vaultwarden")
    ok = await plugin.test({"container_name": "vw"})
    assert ok is True


@pytest.mark.asyncio
async def test_test_missing_db(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/containers/vw/json":
            return httpx.Response(200, json={"Id": "abc"})
        if request.url.path == "/containers/vw/archive":
            path = request.url.params.get("path", "")
            if path.endswith("db.sqlite3"):
                return httpx.Response(404)
            if path.endswith("config.json"):
                return httpx.Response(200, content=make_tar_bytes({"config.json": b"cfg"}))
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        VaultWardenPlugin, "_docker_client", lambda self: make_client(transport)  # type: ignore[misc]
    )

    plugin = VaultWardenPlugin(name="vaultwarden")
    with pytest.raises(FileNotFoundError, match="db.sqlite3 not found"):
        await plugin.test({"container_name": "vw"})


@pytest.mark.asyncio
async def test_test_missing_config_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/containers/vw/json":
            return httpx.Response(200, json={"Id": "abc"})
        if request.url.path == "/containers/vw/archive":
            path = request.url.params.get("path", "")
            if path.endswith("db.sqlite3"):
                return httpx.Response(200, content=make_tar_bytes({"db.sqlite3": b"db"}))
            if path.endswith("config.json"):
                return httpx.Response(404)
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        VaultWardenPlugin, "_docker_client", lambda self: make_client(transport)  # type: ignore[misc]
    )

    plugin = VaultWardenPlugin(name="vaultwarden")
    ok = await plugin.test({"container_name": "vw"})
    assert ok is True


@pytest.mark.asyncio
async def test_backup_writes_artifact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sqlite_backup = make_sqlite_bytes(tmp_path)
    commands: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/containers/vw/json":
            return httpx.Response(200, json={"Id": "abc"})
        if request.url.path == "/containers/vw/exec" and request.method == "POST":
            command = request.read()
            import json

            cmd = json.loads(command)["Cmd"]
            commands.append(cmd)
            return httpx.Response(201, json={"Id": f"exec-{len(commands)}"})
        if request.url.path.startswith("/exec/") and request.url.path.endswith("/start"):
            return httpx.Response(200, content=b"")
        if request.url.path.startswith("/exec/") and request.url.path.endswith("/json"):
            return httpx.Response(200, json={"ExitCode": 0, "Running": False})
        if request.url.path == "/containers/vw/archive":
            assert request.url.params.get("path") == "/data"
            return httpx.Response(
                200,
                content=make_tar_bytes(
                    {
                        "data/db.sqlite3": b"unsafe live database",
                        "data/db_20260814_120000.sqlite3": sqlite_backup,
                        "data/config.json": b"{}",
                    }
                ),
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        VaultWardenPlugin, "_docker_client", lambda self: make_client(transport)  # type: ignore[misc]
    )
    monkeypatch.setattr("app.plugins.vaultwarden.plugin.BACKUP_BASE_PATH", str(tmp_path))

    plugin = VaultWardenPlugin(name="vaultwarden")
    ctx = BackupContext(
        job_id="job-1",
        target_id="target-1",
        config={"container_name": "vw"},
        metadata={"target_slug": "vw-slug"},
    )
    result = await plugin.backup(ctx)
    artifact_path = result.get("artifact_path")
    assert artifact_path and os.path.isabs(artifact_path)
    assert Path(artifact_path).exists()
    with tarfile.open(artifact_path, "r:gz") as tar:
        names = tar.getnames()
        assert "db.sqlite3" in names
        assert "config.json" in names
        restored_db = tar.extractfile("db.sqlite3")
        assert restored_db is not None
        assert restored_db.read() == sqlite_backup
    assert commands[0] == ["/vaultwarden", "backup"]
    assert commands[1] == ["rm", "-f", "/data/db_20260814_120000.sqlite3"]


@pytest.mark.asyncio
async def test_restore_puts_archive(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    artifact = tmp_path / "vaultwarden-backup.tar.gz"
    with tarfile.open(artifact, "w:gz") as tar:
        db_file = tmp_path / "db.sqlite3"
        db_file.write_bytes(b"db")
        tar.add(db_file, arcname="db.sqlite3")

    plugin = VaultWardenPlugin(name="vaultwarden")
    ctx = RestoreContext(
        job_id="job-1",
        source_target_id="src",
        destination_target_id="dest",
        config={"container_name": "vw"},
        artifact_path=str(artifact),
    )
    with pytest.raises(NotImplementedError, match="stopped container"):
        await plugin.restore(ctx)
