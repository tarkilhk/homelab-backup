from __future__ import annotations

import io
import zipfile
from pathlib import Path

import httpx
import pytest

from app.core.plugins.base import BackupContext, RestoreContext
from app.plugins.pihole import PiHolePlugin


def _teleporter_zip() -> bytes:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("etc/pihole/pihole.toml", "dns.upstreams = ['1.1.1.1']")
    return payload.getvalue()


@pytest.mark.asyncio
async def test_backup_uses_documented_sid_header(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST" and request.url.path == "/api/auth":
            return httpx.Response(
                200,
                json={
                    "session": {
                        "valid": True,
                        "sid": "session-id",
                    }
                },
            )
        if request.method == "GET" and request.url.path == "/api/teleporter":
            assert request.headers["X-FTL-SID"] == "session-id"
            assert "X-FTL-CSRF" not in request.headers
            assert "X-CSRF-TOKEN" not in request.headers
            return httpx.Response(200, content=_teleporter_zip())
        if request.method == "DELETE" and request.url.path == "/api/auth":
            assert request.headers["X-FTL-SID"] == "session-id"
            return httpx.Response(204)
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        "app.plugins.pihole.plugin._http_client",
        lambda **kwargs: httpx.AsyncClient(
            transport=transport,
            follow_redirects=True,
            **kwargs,
        ),
    )
    monkeypatch.setattr("app.plugins.pihole.plugin.BACKUP_BASE_PATH", str(tmp_path))
    plugin = PiHolePlugin("pihole")

    result = await plugin.backup(
        BackupContext(
            job_id="1",
            target_id="2",
            config={"base_url": "http://pi.hole", "password": "not-logged"},
            metadata={"target_slug": "pihole"},
        )
    )

    assert Path(result["artifact_path"]).read_bytes() == _teleporter_zip()
    assert any(request.method == "DELETE" for request in requests)


@pytest.mark.asyncio
async def test_connection_test_rejects_session_without_sid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={"session": {"valid": True, "csrf": "csrf-token"}},
        )
    )
    monkeypatch.setattr(
        "app.plugins.pihole.plugin._http_client",
        lambda **kwargs: httpx.AsyncClient(transport=transport, **kwargs),
    )

    with pytest.raises(ValueError, match="invalid session response"):
        await PiHolePlugin("pihole").test({"base_url": "http://pi.hole", "password": "not-logged"})


@pytest.mark.asyncio
async def test_backup_reports_teleporter_unauthorized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "session": {
                        "valid": True,
                        "sid": "session-id",
                        "csrf": "csrf-token",
                    }
                },
            )
        if request.method == "GET":
            return httpx.Response(401, json={"error": {"key": "unauthorized"}})
        return httpx.Response(204)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        "app.plugins.pihole.plugin._http_client",
        lambda **kwargs: httpx.AsyncClient(transport=transport, **kwargs),
    )
    monkeypatch.setattr("app.plugins.pihole.plugin.BACKUP_BASE_PATH", str(tmp_path))

    with pytest.raises(RuntimeError, match="Teleporter export failed with status 401"):
        await PiHolePlugin("pihole").backup(
            BackupContext(
                job_id="1",
                target_id="2",
                config={"base_url": "http://pi.hole", "password": "not-logged"},
                metadata={"target_slug": "pihole"},
            )
        )


@pytest.mark.asyncio
async def test_connection_test_rejects_invalid_teleporter_archive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/api/auth":
            return httpx.Response(
                200,
                json={
                    "session": {
                        "valid": True,
                        "sid": "session-id",
                        "csrf": "csrf-token",
                    }
                },
            )
        if request.method == "GET" and request.url.path == "/api/teleporter":
            return httpx.Response(200, headers={"content-type": "text/html"}, text="error")
        if request.method == "DELETE" and request.url.path == "/api/auth":
            return httpx.Response(204)
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        "app.plugins.pihole.plugin._http_client",
        lambda **kwargs: httpx.AsyncClient(transport=transport, **kwargs),
    )

    with pytest.raises(RuntimeError, match="valid ZIP archive"):
        await PiHolePlugin("pihole").test({"base_url": "http://pi.hole", "password": "not-logged"})


@pytest.mark.asyncio
async def test_connection_test_rejects_zip_without_pihole_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("unrelated.txt", "not a Teleporter export")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"session": {"valid": True, "sid": "sid"}})
        if request.method == "GET":
            return httpx.Response(200, content=payload.getvalue())
        return httpx.Response(204)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        "app.plugins.pihole.plugin._http_client",
        lambda **kwargs: httpx.AsyncClient(transport=transport, **kwargs),
    )

    with pytest.raises(RuntimeError, match="pihole.toml"):
        await PiHolePlugin("pihole").test({"base_url": "http://pi.hole", "password": "not-logged"})


@pytest.mark.asyncio
async def test_restore_imports_archive_and_proves_teleporter_after_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = tmp_path / "pihole-teleporter.zip"
    artifact.write_bytes(_teleporter_zip())
    imported = False

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal imported
        if request.method == "POST" and request.url.path == "/api/auth":
            return httpx.Response(
                200,
                json={
                    "session": {
                        "valid": True,
                        "sid": "session-id",
                        "csrf": "csrf-token",
                    }
                },
            )
        if request.method == "POST" and request.url.path == "/api/teleporter":
            assert request.headers["X-FTL-SID"] == "session-id"
            assert b"pihole-teleporter.zip" in request.content
            imported = True
            return httpx.Response(200, json={"processed": True})
        if request.method == "GET" and request.url.path == "/api/teleporter":
            assert imported is True
            return httpx.Response(200, content=_teleporter_zip())
        if request.method == "DELETE" and request.url.path == "/api/auth":
            return httpx.Response(204)
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        "app.plugins.pihole.plugin._http_client",
        lambda **kwargs: httpx.AsyncClient(transport=transport, **kwargs),
    )

    result = await PiHolePlugin("pihole").restore(
        RestoreContext(
            job_id="2",
            source_target_id="1",
            destination_target_id="2",
            config={"base_url": "http://pi.hole", "password": "not-logged"},
            artifact_path=str(artifact),
        )
    )

    assert result["status"] == "success"
    assert imported is True
