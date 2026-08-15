import asyncio
import io
import os
import zipfile

import httpx
import pytest

from app.core.plugins.base import BackupContext, RestoreContext
from app.plugins.invoiceninja.plugin import InvoiceNinjaPlugin


def _company_export_zip() -> bytes:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("backup.json", '{"company":{"name":"Restore proof"}}')
    return payload.getvalue()


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
async def test_test_does_not_follow_cross_origin_redirect_with_api_token(monkeypatch):
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
        await InvoiceNinjaPlugin("invoiceninja").test(
            {"base_url": "http://example.local", "token": "t"}
        )

    assert [request.url.host for request in requests] == ["example.local"]


@pytest.mark.asyncio
async def test_backup_writes_artifact(tmp_path, monkeypatch):
    attempts = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/api/v1/export"):
            return httpx.Response(
                200, json={"message": "Processing", "url": "http://example.local/dl/export.zip"}
            )
        if request.method == "GET" and request.url.path == "/dl/export.zip":
            attempts["n"] += 1
            if attempts["n"] < 2:
                return httpx.Response(404)
            return httpx.Response(
                200,
                headers={
                    "content-type": "application/zip",
                    "content-disposition": "attachment; filename=export.zip",
                },
                content=_company_export_zip(),
            )
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


@pytest.mark.asyncio
async def test_backup_rejects_html_page(tmp_path, monkeypatch):
    # Always return 200 HTML page to emulate Invoice Ninja error template
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/api/v1/export"):
            return httpx.Response(
                200, json={"message": "Processing", "url": "http://example.local/dl/export.zip"}
            )
        if request.method == "GET" and request.url.path == "/dl/export.zip":
            html = (
                b"<!DOCTYPE html>\n<html><head><title>Error</title></head><body>404</body></html>"
            )
            return httpx.Response(
                200, headers={"content-type": "text/html; charset=utf-8"}, content=html
            )
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

    with pytest.raises(RuntimeError):
        await plugin.backup(ctx)


@pytest.mark.asyncio
async def test_backup_rejects_corrupt_zip_response(tmp_path, monkeypatch):
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"url": "http://example.local/export.zip"})
        return httpx.Response(
            200,
            headers={"content-type": "application/zip"},
            content=b"PK\x03\x04truncated",
        )

    transport = httpx.MockTransport(handler)
    original_client = httpx.AsyncClient

    def client(*args, **kwargs):
        kwargs["transport"] = transport
        return original_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    plugin = InvoiceNinjaPlugin(name="invoiceninja")
    monkeypatch.setattr(plugin, "_base_dir", lambda: str(tmp_path))

    with pytest.raises(RuntimeError, match="valid ZIP archive"):
        await plugin.backup(
            BackupContext(
                job_id="1",
                target_id="1",
                config={"base_url": "http://example.local", "token": "t"},
                metadata={"target_slug": "slug"},
            )
        )


@pytest.mark.asyncio
async def test_backup_rejects_cross_origin_signed_download(tmp_path, monkeypatch):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"url": "https://attacker.invalid/export.zip"})

    transport = httpx.MockTransport(handler)
    original_client = httpx.AsyncClient

    def client(*args, **kwargs):
        kwargs["transport"] = transport
        return original_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    plugin = InvoiceNinjaPlugin(name="invoiceninja")
    monkeypatch.setattr(plugin, "_base_dir", lambda: str(tmp_path))

    with pytest.raises(RuntimeError, match="same origin"):
        await plugin.backup(
            BackupContext(
                job_id="1",
                target_id="1",
                config={"base_url": "http://example.local", "token": "t"},
                metadata={"target_slug": "slug"},
            )
        )


@pytest.mark.asyncio
async def test_restore_submits_official_company_import(tmp_path, monkeypatch):
    artifact = tmp_path / "invoiceninja-export.zip"
    artifact.write_bytes(_company_export_zip())

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/import_json"
        assert request.headers["X-API-Token"] == "t"
        assert request.headers["X-Requested-With"] == "XMLHttpRequest"
        assert b"invoiceninja-export.zip" in request.content
        assert b"import_data" in request.content
        assert b"import_settings" in request.content
        return httpx.Response(200, json={"message": "Processing"})

    transport = httpx.MockTransport(handler)
    original_client = httpx.AsyncClient

    def client(*args, **kwargs):
        kwargs["transport"] = transport
        return original_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)

    result = await InvoiceNinjaPlugin(name="invoiceninja").restore(
        RestoreContext(
            job_id="2",
            source_target_id="1",
            destination_target_id="2",
            config={"base_url": "http://example.local", "token": "t"},
            artifact_path=str(artifact),
        )
    )

    assert result["status"] == "partial"
    assert "queued" in result["message"].lower()
