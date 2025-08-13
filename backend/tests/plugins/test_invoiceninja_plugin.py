import os
import asyncio
import httpx
import pytest

from app.core.plugins.base import BackupContext
from app.plugins.invoiceninja.plugin import InvoiceNinjaPlugin


@pytest.mark.asyncio
async def test_test_returns_true(monkeypatch):
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/api/v1/ping"):
            return httpx.Response(200, json={"company_name": "Acme", "user_name": "User"})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    orig_client = httpx.AsyncClient

    def _client(*args, **kwargs):
        kwargs["transport"] = transport
        return orig_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _client)

    plugin = InvoiceNinjaPlugin(name="invoiceninja")
    ok = await plugin.test({"base_url": "http://example.local", "token": "t"})
    assert ok is True


@pytest.mark.asyncio
async def test_backup_writes_artifact(tmp_path, monkeypatch):
    attempts = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/api/v1/export"):
            return httpx.Response(200, json={"message": "Processing", "url": "http://example.local/dl/export.zip"})
        if request.method == "GET" and request.url.path == "/dl/export.zip":
            attempts["n"] += 1
            if attempts["n"] < 2:
                return httpx.Response(404)
            return httpx.Response(200, content=b"data")
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    orig_client = httpx.AsyncClient

    def _client(*args, **kwargs):
        kwargs["transport"] = transport
        return orig_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _client)

    plugin = InvoiceNinjaPlugin(name="invoiceninja")
    monkeypatch.setattr(plugin, "_base_dir", lambda: str(tmp_path))

    async def fake_sleep(seconds: float):
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    ctx = BackupContext(
        job_id="1",
        target_id="1",
        config={"base_url": "http://example.local", "token": "t"},
        metadata={"target_slug": "slug"},
    )
    result = await plugin.backup(ctx)
    artifact_path = result.get("artifact_path")
    assert artifact_path and os.path.isabs(artifact_path)
    assert os.path.exists(artifact_path)