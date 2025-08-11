import os
from datetime import datetime, timezone
from typing import Any

import httpx
import pytest

from app.core.plugins.base import BackupContext
from app.plugins.lidarr import LidarrPlugin


@pytest.mark.asyncio
async def test_lidarr_validate_and_test(monkeypatch):
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/api/v1/system/status"):
            return httpx.Response(200, json={"version": "1"})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    orig_client = httpx.AsyncClient

    def _client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return orig_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _client)

    plugin = LidarrPlugin(name="lidarr")
    cfg = {"base_url": "http://example.local", "api_key": "abc"}
    assert await plugin.validate_config(cfg) is True
    assert await plugin.test(cfg) is True


@pytest.mark.asyncio
async def test_lidarr_backup_writes_artifact(monkeypatch):
    backups = [
        {"id": 1, "time": "2024-01-01T00:00:00Z"},
        {"id": 2, "time": "2024-01-02T00:00:00Z"},
    ]

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/api/v1/command") and request.method == "POST":
            return httpx.Response(200, json={"id": 1})
        if request.url.path.endswith("/api/v1/system/backup") and request.method == "GET":
            return httpx.Response(200, json=backups)
        if request.url.path.endswith("/api/v1/system/backup/2") and request.method == "GET":
            return httpx.Response(200, content=b"zipdata")
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    orig_client = httpx.AsyncClient

    def _client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return orig_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _client)

    plugin = LidarrPlugin(name="lidarr")
    ctx = BackupContext(
        job_id="1",
        target_id="1",
        config={"base_url": "http://example.local", "api_key": "abc"},
        metadata={"target_slug": "lidarr"},
    )
    result = await plugin.backup(ctx)
    artifact_path = result.get("artifact_path")
    assert artifact_path and os.path.exists(artifact_path)
    assert artifact_path.endswith('.zip')
