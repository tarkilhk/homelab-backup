import asyncio
import os

import httpx
import pytest

from app.core.plugins.base import BackupContext, RestoreContext
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
async def test_test_does_not_follow_redirect_with_api_key(monkeypatch):
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(302, headers={"location": "https://attacker.invalid/collect"})

    transport = httpx.MockTransport(handler)
    original_client = httpx.AsyncClient

    def client(*args, **kwargs):
        kwargs["transport"] = transport
        return original_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)

    with pytest.raises(RuntimeError, match="status 302"):
        await SonarrPlugin("sonarr").test({"base_url": "http://example.local", "api_key": "secret"})

    assert [request.url.host for request in requests] == ["example.local"]


@pytest.mark.asyncio
async def test_backup_writes_artifact(tmp_path, monkeypatch, make_servarr_zip):
    list_calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal list_calls
        if request.method == "GET" and request.url.path == "/api/v3/system/backup":
            list_calls += 1
            if list_calls == 1:
                return httpx.Response(200, json=[])
            return httpx.Response(
                200,
                json=[{"id": 3, "type": "manual", "path": "/backup/manual/sonarr.zip"}],
            )
        if request.method == "POST" and request.url.path == "/api/v3/command":
            return httpx.Response(201, json={"id": 5})
        if request.method == "GET" and request.url.path == "/api/v3/command/5":
            return httpx.Response(200, json={"status": "completed", "result": "successful"})
        if request.method == "GET" and request.url.path == "/backup/manual/sonarr.zip":
            return httpx.Response(200, content=make_servarr_zip("sonarr.db"))
        return httpx.Response(404)

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


@pytest.mark.asyncio
async def test_restore_is_serialized_across_plugin_instances(monkeypatch) -> None:
    active = 0
    maximum_active = 0

    async def restore_without_lock(self, context, base_url, headers):  # type: ignore[no-untyped-def]
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        return {"status": "success"}

    monkeypatch.setattr(SonarrPlugin, "_restore_without_lock", restore_without_lock)
    config = {"base_url": "http://sonarr.local", "api_key": "key"}
    context = RestoreContext(
        job_id="job",
        source_target_id="source",
        destination_target_id="destination",
        config=config,
        artifact_path="unused-by-test",
    )

    await asyncio.gather(
        SonarrPlugin("sonarr").restore(context),
        SonarrPlugin("sonarr").restore(context),
    )

    assert maximum_active == 1
