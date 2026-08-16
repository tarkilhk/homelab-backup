import os
from typing import Any

import httpx
import pytest

from app.core.plugins.base import BackupContext, RestoreContext
from app.plugins.radarr import RadarrPlugin


@pytest.mark.asyncio
async def test_validate_and_test(monkeypatch):
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/api/v3/system/status"):
            return httpx.Response(200, json={"version": "4"})
        if request.url.path.endswith("/api/v3/system/backup"):
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    orig_client = httpx.AsyncClient

    def _client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return orig_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _client)

    plugin = RadarrPlugin(name="radarr")
    cfg = {"base_url": "http://example.local", "api_key": "token"}
    assert await plugin.validate_config(cfg) is True
    assert await plugin.test(cfg) is True


@pytest.mark.asyncio
async def test_backup_waits_for_its_command_and_writes_verified_artifact(
    monkeypatch, tmp_path, make_servarr_zip
):
    requests: list[tuple[str, str]] = []
    backup_lists = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal backup_lists
        requests.append((request.method, request.url.path))
        assert request.url.params.get("apikey") is None
        assert request.headers["X-Api-Key"] == "token"
        if request.method == "GET" and request.url.path == "/api/v3/system/backup":
            backup_lists += 1
            if backup_lists == 1:
                return httpx.Response(200, json=[])
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 12,
                        "name": "radarr-test.zip",
                        "type": "manual",
                        "path": "/backup/manual/radarr-test.zip",
                        "size": 1024,
                        "time": "2099-01-01T00:00:00Z",
                    }
                ],
            )
        if request.method == "POST" and request.url.path == "/api/v3/command":
            return httpx.Response(201, json={"id": 44, "status": "queued"})
        if request.method == "GET" and request.url.path == "/api/v3/command/44":
            return httpx.Response(
                200, json={"id": 44, "status": "completed", "result": "successful"}
            )
        if request.method == "GET" and request.url.path == "/backup/manual/radarr-test.zip":
            return httpx.Response(200, content=make_servarr_zip("radarr.db"))
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    orig_client = httpx.AsyncClient

    def _client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return orig_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _client)

    plugin = RadarrPlugin(name="radarr")
    plugin.backup_root = str(tmp_path)
    ctx = BackupContext(
        job_id="1",
        target_id="1",
        config={"base_url": "http://example.local", "api_key": "token"},
        metadata={"target_slug": "radarr"},
    )
    result = await plugin.backup(ctx)
    artifact_path = result.get("artifact_path")
    assert artifact_path and os.path.exists(artifact_path)
    assert artifact_path.endswith(".zip")
    assert ("GET", "/api/v3/command/44") in requests


@pytest.mark.asyncio
async def test_restore_uploads_restarts_and_waits_for_new_process(
    monkeypatch, tmp_path, make_servarr_zip
):
    artifact = tmp_path / "radarr-backup.zip"
    artifact.write_bytes(make_servarr_zip("radarr.db"))
    status_calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal status_calls
        if request.method == "GET" and request.url.path == "/api/v3/system/status":
            status_calls += 1
            if status_calls == 1:
                assert request.headers["X-Api-Key"] == "destination-key"
                return httpx.Response(200, json={"version": "6.3.0", "startTime": "old"})
            assert request.headers["X-Api-Key"] == "test-key"
            return httpx.Response(200, json={"version": "6.3.0", "startTime": "new"})
        if request.method == "POST" and request.url.path == "/api/v3/system/backup/restore/upload":
            assert request.headers["X-Api-Key"] == "destination-key"
            assert b"radarr-backup.zip" in request.content
            return httpx.Response(200, json={"restartRequired": True})
        if request.method == "POST" and request.url.path == "/api/v3/system/restart":
            assert request.headers["X-Api-Key"] == "destination-key"
            return httpx.Response(200, json={"restarting": True})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    original_client = httpx.AsyncClient

    def client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return original_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)

    result = await RadarrPlugin(name="radarr").restore(
        RestoreContext(
            job_id="2",
            source_target_id="1",
            destination_target_id="2",
            config={"base_url": "http://example.local", "api_key": "destination-key"},
            artifact_path=str(artifact),
        )
    )

    assert result["status"] == "success"
    assert result["artifact_bytes"] == artifact.stat().st_size
    assert status_calls >= 2
