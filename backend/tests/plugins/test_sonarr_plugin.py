import os
import os
import httpx
import pytest

from app.core.plugins.base import BackupContext
from app.plugins.sonarr.plugin import SonarrPlugin


@pytest.mark.asyncio
async def test_validate_config():
    plugin = SonarrPlugin(name="sonarr")
    assert await plugin.validate_config({"base_url": "http://example.local", "api_key": "k"})
    assert not await plugin.validate_config({"base_url": "", "api_key": ""})


@pytest.mark.asyncio
async def test_test(monkeypatch):
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/system/status"):
            return httpx.Response(200, json={"version": "3.0"})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    orig_client = httpx.AsyncClient

    def _client(*args, **kwargs):
        kwargs["transport"] = transport
        return orig_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _client)

    plugin = SonarrPlugin(name="sonarr")
    ok = await plugin.test({"base_url": "http://example.local", "api_key": "k"})
    assert ok is True


@pytest.mark.asyncio
async def test_backup_writes_artifact(tmp_path, monkeypatch):
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/system/backup"):
            return httpx.Response(200, content=b"zipdata")
        return httpx.Response(200, json={"version": "3.0"})

    transport = httpx.MockTransport(handler)
    orig_client = httpx.AsyncClient

    def _client(*args, **kwargs):
        kwargs["transport"] = transport
        return orig_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _client)

    plugin = SonarrPlugin(name="sonarr")
    plugin.backup_root = str(tmp_path)
    ctx = BackupContext(
        job_id="1",
        target_id="1",
        config={"base_url": "http://example.local", "api_key": "k"},
        metadata={"target_slug": "sonarr"},
    )
    result = await plugin.backup(ctx)
    artifact_path = result.get("artifact_path")
    assert artifact_path and os.path.isabs(artifact_path)
    assert os.path.exists(artifact_path)
