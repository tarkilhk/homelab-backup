import os

import httpx
import pytest

from app.core.plugins.base import BackupContext
from .plugin import RadarrPlugin


@pytest.mark.asyncio
async def test_validate_and_test(monkeypatch):
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/system/status"):
            return httpx.Response(200, json={"version": "4.0.0"})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    orig_client = httpx.AsyncClient

    def _client(*args, **kwargs):
        kwargs["transport"] = transport
        return orig_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _client)

    plugin = RadarrPlugin(name="radarr")
    cfg = {"base_url": "http://example.local", "api_key": "token"}
    assert await plugin.validate_config(cfg) is True
    assert await plugin.test(cfg) is True


@pytest.mark.asyncio
async def test_backup_writes_artifact(tmp_path, monkeypatch):
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/system/backup"):
            return httpx.Response(200, content=b"data")
        return httpx.Response(200, json={"version": "4"})

    transport = httpx.MockTransport(handler)
    orig_client = httpx.AsyncClient

    def _client(*args, **kwargs):
        kwargs["transport"] = transport
        return orig_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _client)

    plugin = RadarrPlugin(name="radarr")
    ctx = BackupContext(
        job_id="1",
        target_id="1",
        config={"base_url": "http://example.local", "api_key": "token"},
        metadata={"target_slug": "target-slug"},
    )

    result = await plugin.backup(ctx)
    artifact_path = result.get("artifact_path")
    assert artifact_path and os.path.isabs(artifact_path)
    assert os.path.exists(artifact_path)
