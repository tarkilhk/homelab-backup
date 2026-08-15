import asyncio
import io
import json
import os
import zipfile
from pathlib import Path

import httpx
import pytest

import app.plugins.jellyfin.plugin as jellyfin_module
from app.core.plugins.base import BackupContext
from app.plugins.jellyfin import JellyfinPlugin


def _backup_zip() -> bytes:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr(
            "manifest.json",
            json.dumps({"ServerVersion": "10.11.11", "BackupEngineVersion": 1}),
        )
        archive.writestr("Database/jellyfin_UserData.json", "[]")
        archive.writestr("Config/system.xml", "<ServerConfiguration />")
    return payload.getvalue()


@pytest.mark.asyncio
async def test_test_returns_true(monkeypatch):
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/System/Info"):
            assert request.headers["Authorization"].endswith('Token="k"')
            return httpx.Response(200, json={"Version": "10.8.0"})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    orig_client = httpx.AsyncClient

    def _client(*args, **kwargs):
        kwargs["transport"] = transport
        return orig_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _client)

    plugin = JellyfinPlugin(name="jellyfin")
    ok = await plugin.test(
        {"base_url": "http://example.local", "api_key": "k", "backup_path": "/tmp"}
    )
    assert ok is True


@pytest.mark.asyncio
async def test_backup_writes_artifact(tmp_path, monkeypatch):
    server_backup_dir = tmp_path / "server-backups"
    server_backup_dir.mkdir()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/Backup/Create"):
            (server_backup_dir / "jellyfin-backup-20260815010000.zip").write_bytes(_backup_zip())
            return httpx.Response(
                200,
                json={
                    "ServerVersion": "10.11.11",
                    "BackupEngineVersion": 1,
                    "Path": "/config/data/backups/jellyfin-backup-20260815010000.zip",
                },
            )
        return httpx.Response(200, json={"Version": "10.8.0"})

    transport = httpx.MockTransport(handler)
    orig_client = httpx.AsyncClient

    def _client(*args, **kwargs):
        kwargs["transport"] = transport
        return orig_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _client)
    monkeypatch.setattr(jellyfin_module, "BACKUP_BASE", str(tmp_path))

    plugin = JellyfinPlugin(name="jellyfin")
    ctx = BackupContext(
        job_id="1",
        target_id="1",
        config={
            "base_url": "http://example.local",
            "api_key": "k",
            "backup_path": str(server_backup_dir),
        },
        metadata={"target_slug": "test"},
    )
    result = await plugin.backup(ctx)
    artifact_path = result.get("artifact_path")
    assert artifact_path and os.path.isabs(artifact_path) and os.path.exists(artifact_path)
    assert not (server_backup_dir / "jellyfin-backup-20260815010000.zip").exists()


@pytest.mark.asyncio
async def test_backup_rejects_non_zip_response(tmp_path, monkeypatch):
    server_backup_dir = tmp_path / "server-backups"
    server_backup_dir.mkdir()

    def handler(request: httpx.Request) -> httpx.Response:
        (server_backup_dir / "jellyfin-backup-bad.zip").write_text("<html>error</html>")
        return httpx.Response(
            200,
            json={"Path": "/config/data/backups/jellyfin-backup-bad.zip"},
        )

    transport = httpx.MockTransport(handler)
    original_client = httpx.AsyncClient

    def client(*args, **kwargs):
        kwargs["transport"] = transport
        return original_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    monkeypatch.setattr(jellyfin_module, "BACKUP_BASE", str(tmp_path))

    with pytest.raises(RuntimeError, match="valid ZIP archive"):
        await JellyfinPlugin(name="jellyfin").backup(
            BackupContext(
                job_id="1",
                target_id="1",
                config={
                    "base_url": "http://example.local",
                    "api_key": "k",
                    "backup_path": str(server_backup_dir),
                },
                metadata={"target_slug": "test"},
            )
        )


@pytest.mark.asyncio
async def test_restore_stages_archive_and_calls_official_endpoint(tmp_path, monkeypatch):
    server_backup_dir = tmp_path / "server-backups"
    server_backup_dir.mkdir()
    artifact = tmp_path / "jellyfin-backup-restore.zip"
    artifact.write_bytes(_backup_zip())
    calls = 0
    readiness_calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls, readiness_calls
        if request.method == "POST" and request.url.path == "/Backup/Restore":
            assert request.headers["Authorization"].endswith('Token="k"')
            assert json.loads(request.content)["ArchiveFileName"] == artifact.name
            calls += 1
            return httpx.Response(204)
        if request.method == "GET" and request.url.path == "/System/Info/Public":
            assert "Authorization" not in request.headers
            readiness_calls += 1
            if readiness_calls == 1:
                return httpx.Response(503)
            return httpx.Response(200, json={"Version": "10.11.11"})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    original_client = httpx.AsyncClient

    def client(*args, **kwargs):
        kwargs["transport"] = transport
        return original_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)

    from app.core.plugins.base import RestoreContext

    result = await JellyfinPlugin(name="jellyfin").restore(
        RestoreContext(
            job_id="2",
            source_target_id="1",
            destination_target_id="2",
            config={
                "base_url": "http://example.local",
                "api_key": "k",
                "backup_path": str(server_backup_dir),
            },
            artifact_path=str(artifact),
        )
    )

    assert result["status"] == "success"
    assert not (server_backup_dir / artifact.name).exists()
    assert calls == 1
    assert readiness_calls == 2


@pytest.mark.asyncio
async def test_restore_returns_partial_when_fast_restart_is_not_observable(tmp_path, monkeypatch):
    server_backup_dir = tmp_path / "server-backups"
    server_backup_dir.mkdir()
    artifact = tmp_path / "jellyfin-backup-restore.zip"
    artifact.write_bytes(_backup_zip())

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/Backup/Restore":
            return httpx.Response(204)
        if request.method == "GET" and request.url.path == "/System/Info/Public":
            return httpx.Response(200, json={"Version": "10.11.11"})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    original_client = httpx.AsyncClient

    def client(*args, **kwargs):
        kwargs["transport"] = transport
        return original_client(*args, **kwargs)

    async def no_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(httpx, "AsyncClient", client)
    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    monkeypatch.setattr(jellyfin_module, "RESTORE_MAX_POLLS", 2)

    from app.core.plugins.base import RestoreContext

    result = await JellyfinPlugin(name="jellyfin").restore(
        RestoreContext(
            job_id="2",
            source_target_id="1",
            destination_target_id="2",
            config={
                "base_url": "http://example.local",
                "api_key": "k",
                "backup_path": str(server_backup_dir),
            },
            artifact_path=str(artifact),
        )
    )

    assert result["status"] == "partial"
    assert "restart transition" in result["message"]
