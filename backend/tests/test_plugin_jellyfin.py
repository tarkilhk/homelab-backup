import os
import httpx
import pytest

from app.core.plugins.base import BackupContext
from app.plugins.jellyfin import JellyfinPlugin
import app.plugins.jellyfin.plugin as jellyfin_module


@pytest.mark.asyncio
async def test_test_returns_true(monkeypatch):
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/System/Info"):
            return httpx.Response(200, json={"Version": "10.8.0"})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    orig_client = httpx.AsyncClient

    def _client(*args, **kwargs):
        kwargs["transport"] = transport
        return orig_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _client)

    plugin = JellyfinPlugin(name="jellyfin")
    ok = await plugin.test({"base_url": "http://example.local", "api_key": "k"})
    assert ok is True


@pytest.mark.asyncio
async def test_backup_writes_artifact(tmp_path, monkeypatch):
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/Backup/Archive"):
            return httpx.Response(200, content=b"data")
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
        config={"base_url": "http://example.local", "api_key": "k"},
        metadata={"target_slug": "test"},
    )
    result = await plugin.backup(ctx)
    artifact_path = result.get("artifact_path")
    assert artifact_path and os.path.isabs(artifact_path) and os.path.exists(artifact_path)
