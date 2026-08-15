import io
import json
import os
import sqlite3
import tarfile
from pathlib import Path
from typing import Dict

import httpx
import pytest

from app.core.plugins.base import BackupContext, RestoreContext
from app.plugins.vaultwarden import VaultWardenPlugin


def make_tar_bytes(files: Dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, content in files.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def make_client(handler: httpx.MockTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=handler, base_url="http://docker")


def make_sqlite_bytes(tmp_path: Path) -> bytes:
    database = tmp_path / "source.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE secrets (id INTEGER PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO secrets (value) VALUES ('encrypted')")
    return database.read_bytes()


@pytest.mark.asyncio
async def test_validate_config() -> None:
    plugin = VaultWardenPlugin(name="vaultwarden")
    assert await plugin.validate_config({"container_name": "vaultwarden", "data_path": "/data"})
    assert await plugin.validate_config({"container_name": "vw"}) is True
    assert await plugin.validate_config({"container_name": ""}) is False
    assert await plugin.validate_config({"container_name": 123}) is False  # type: ignore[arg-type]
    assert await plugin.validate_config({"container_name": "vw", "data_path": ""}) is False
    assert await plugin.validate_config({"container_name": "vw", "data_path": "/"}) is False
    assert (
        await plugin.validate_config({"container_name": "vw", "data_path": "/backups/vaultwarden"})
        is False
    )
    assert await plugin.validate_config({"container_name": "vw", "data_path": "data"}) is False


@pytest.mark.asyncio
async def test_test_success(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/containers/vw/json":
            return httpx.Response(200, json={"Id": "abc"})
        if request.url.path == "/containers/vw/archive" and request.method == "HEAD":
            path = request.url.params.get("path", "")
            if path.endswith("db.sqlite3"):
                return httpx.Response(200)
            if path.endswith("config.json"):
                return httpx.Response(200)
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        VaultWardenPlugin, "_docker_client", lambda self: make_client(transport)  # type: ignore[misc]
    )

    plugin = VaultWardenPlugin(name="vaultwarden")
    ok = await plugin.test({"container_name": "vw"})
    assert ok is True


@pytest.mark.asyncio
async def test_test_missing_db(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/containers/vw/json":
            return httpx.Response(200, json={"Id": "abc"})
        if request.url.path == "/containers/vw/archive" and request.method == "HEAD":
            path = request.url.params.get("path", "")
            if path.endswith("db.sqlite3"):
                return httpx.Response(404)
            if path.endswith("config.json"):
                return httpx.Response(200)
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        VaultWardenPlugin, "_docker_client", lambda self: make_client(transport)  # type: ignore[misc]
    )

    plugin = VaultWardenPlugin(name="vaultwarden")
    with pytest.raises(FileNotFoundError, match="db.sqlite3 not found"):
        await plugin.test({"container_name": "vw"})


@pytest.mark.asyncio
async def test_test_missing_config_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/containers/vw/json":
            return httpx.Response(200, json={"Id": "abc"})
        if request.url.path == "/containers/vw/archive" and request.method == "HEAD":
            path = request.url.params.get("path", "")
            if path.endswith("db.sqlite3"):
                return httpx.Response(200)
            if path.endswith("config.json"):
                return httpx.Response(404)
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        VaultWardenPlugin, "_docker_client", lambda self: make_client(transport)  # type: ignore[misc]
    )

    plugin = VaultWardenPlugin(name="vaultwarden")
    ok = await plugin.test({"container_name": "vw"})
    assert ok is True


@pytest.mark.asyncio
async def test_backup_writes_artifact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sqlite_backup = make_sqlite_bytes(tmp_path)
    commands: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/containers/vw/json":
            return httpx.Response(200, json={"Id": "abc"})
        if request.url.path == "/containers/vw/exec" and request.method == "POST":
            command = request.read()
            import json

            cmd = json.loads(command)["Cmd"]
            commands.append(cmd)
            return httpx.Response(201, json={"Id": f"exec-{len(commands)}"})
        if request.url.path.startswith("/exec/") and request.url.path.endswith("/start"):
            return httpx.Response(200, content=b"")
        if request.url.path.startswith("/exec/") and request.url.path.endswith("/json"):
            return httpx.Response(200, json={"ExitCode": 0, "Running": False})
        if request.url.path == "/containers/vw/archive":
            assert request.url.params.get("path") == "/data"
            return httpx.Response(
                200,
                content=make_tar_bytes(
                    {
                        "data/db.sqlite3": b"unsafe live database",
                        "data/db_20260814_120000.sqlite3": sqlite_backup,
                        "data/config.json": b"{}",
                        "data/rsa_key.pem": b"private-key",
                        "data/rsa_key.pub.pem": b"public-key",
                        "data/sends/send.bin": b"send",
                        "data/attachments/a/file.bin": b"attachment",
                    }
                ),
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        VaultWardenPlugin, "_docker_client", lambda self: make_client(transport)  # type: ignore[misc]
    )
    monkeypatch.setattr("app.plugins.vaultwarden.plugin.BACKUP_BASE_PATH", str(tmp_path))

    plugin = VaultWardenPlugin(name="vaultwarden")
    ctx = BackupContext(
        job_id="job-1",
        target_id="target-1",
        config={"container_name": "vw"},
        metadata={"target_slug": "vw-slug"},
    )
    result = await plugin.backup(ctx)
    artifact_path = result.get("artifact_path")
    assert artifact_path and os.path.isabs(artifact_path)
    assert Path(artifact_path).exists()
    with tarfile.open(artifact_path, "r:gz") as tar:
        names = tar.getnames()
        assert "db.sqlite3" in names
        assert "config.json" in names
        assert "rsa_key.pem" in names
        assert "rsa_key.pub.pem" in names
        assert "sends/send.bin" in names
        assert "attachments/a/file.bin" in names
        assert "backup-manifest.json" in names
        restored_db = tar.extractfile("db.sqlite3")
        assert restored_db is not None
        assert restored_db.read() == sqlite_backup
    assert commands[0] == [
        "/usr/bin/timeout",
        "--signal=KILL",
        "120",
        "/vaultwarden",
        "backup",
    ]
    assert commands[1] == [
        "/usr/bin/timeout",
        "--signal=KILL",
        "120",
        "rm",
        "-f",
        "/data/db_20260814_120000.sqlite3",
    ]


@pytest.mark.asyncio
async def test_restore_puts_archive(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    sqlite_backup = make_sqlite_bytes(tmp_path)
    artifact = tmp_path / "vaultwarden-backup.tar.gz"
    with tarfile.open(artifact, "w:gz") as tar:
        db_file = tmp_path / "db.sqlite3"
        db_file.write_bytes(sqlite_backup)
        tar.add(db_file, arcname="db.sqlite3")
        manifest = json.dumps({"format_version": 1, "components": ["db.sqlite3"]}).encode()
        info = tarfile.TarInfo("backup-manifest.json")
        info.size = len(manifest)
        tar.addfile(info, io.BytesIO(manifest))

    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.method == "GET" and request.url.path == "/containers/vw/json":
            return httpx.Response(
                200,
                json={
                    "Id": "target-id",
                    "Config": {"Image": "vaultwarden/server:1.37.1"},
                    "Mounts": [{"Destination": "/data", "RW": True}],
                    "State": {"Running": True, "Health": {"Status": "healthy"}},
                },
            )
        if request.method == "POST" and request.url.path == "/containers/vw/stop":
            return httpx.Response(204)
        if request.method == "POST" and request.url.path == "/containers/create":
            create_config = json.loads(request.content)
            assert create_config["Entrypoint"] == ["/usr/bin/sleep"]
            assert create_config["Cmd"] == ["infinity"]
            return httpx.Response(201, json={"Id": "helper-id"})
        if request.method == "POST" and request.url.path in {
            "/containers/helper-id/start",
            "/containers/vw/start",
        }:
            return httpx.Response(204)
        if request.method == "PUT" and request.url.path == "/containers/helper-id/archive":
            assert request.url.params["path"] == "/tmp"
            assert len(request.content) > 0
            return httpx.Response(200)
        if request.method == "POST" and request.url.path == "/containers/helper-id/exec":
            return httpx.Response(201, json={"Id": f"exec-{len(requests)}"})
        if request.method == "POST" and request.url.path.startswith("/exec/"):
            return httpx.Response(200)
        if request.method == "GET" and request.url.path.startswith("/exec/"):
            return httpx.Response(200, json={"ExitCode": 0})
        if request.method == "DELETE" and request.url.path == "/containers/helper-id":
            return httpx.Response(204)
        if request.method == "GET" and request.url.path == "/containers/vw/archive":
            path = request.url.params.get("path", "")
            if path.endswith("db.sqlite3"):
                return httpx.Response(200, content=make_tar_bytes({"db.sqlite3": sqlite_backup}))
            return httpx.Response(404)
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        VaultWardenPlugin, "_docker_client", lambda self: make_client(transport)  # type: ignore[misc]
    )

    plugin = VaultWardenPlugin(name="vaultwarden")
    ctx = RestoreContext(
        job_id="job-1",
        source_target_id="src",
        destination_target_id="dest",
        config={"container_name": "vw"},
        artifact_path=str(artifact),
    )
    result = await plugin.restore(ctx)

    assert result["status"] == "success"
    assert ("POST", "/containers/vw/stop") in requests
    assert ("PUT", "/containers/helper-id/archive") in requests
    assert ("DELETE", "/containers/helper-id") in requests
    assert ("POST", "/containers/vw/start") in requests


@pytest.mark.asyncio
async def test_restore_rejects_data_path_outside_writable_mount(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sqlite_backup = make_sqlite_bytes(tmp_path)
    artifact = tmp_path / "vaultwarden-backup.tar.gz"
    with tarfile.open(artifact, "w:gz") as tar:
        database = tmp_path / "db.sqlite3"
        database.write_bytes(sqlite_backup)
        tar.add(database, arcname="db.sqlite3")
        manifest = json.dumps({"format_version": 1, "components": ["db.sqlite3"]}).encode()
        info = tarfile.TarInfo("backup-manifest.json")
        info.size = len(manifest)
        tar.addfile(info, io.BytesIO(manifest))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/containers/vw/json":
            return httpx.Response(
                200,
                json={
                    "Config": {"Image": "vaultwarden/server:1.37.1"},
                    "Mounts": [{"Destination": "/vault-data", "RW": True}],
                    "State": {"Running": True},
                },
            )
        raise AssertionError(f"unexpected Docker mutation: {request.method} {request.url.path}")

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        VaultWardenPlugin, "_docker_client", lambda self: make_client(transport)  # type: ignore[misc]
    )

    with pytest.raises(RuntimeError, match="writable Docker mount"):
        await VaultWardenPlugin("vaultwarden").restore(
            RestoreContext(
                job_id="job-1",
                source_target_id="src",
                destination_target_id="dest",
                config={"container_name": "vw", "data_path": "/data"},
                artifact_path=str(artifact),
            )
        )


def test_restore_mount_requires_exact_destination() -> None:
    details = {"Mounts": [{"Destination": "/data", "RW": True}]}

    with pytest.raises(RuntimeError, match="exactly match"):
        VaultWardenPlugin("vaultwarden")._validate_restore_mount(details, "/data/link")


@pytest.mark.asyncio
async def test_restore_rolls_back_when_destination_is_unhealthy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sqlite_backup = make_sqlite_bytes(tmp_path)
    artifact = tmp_path / "vaultwarden-backup.tar.gz"
    with tarfile.open(artifact, "w:gz") as tar:
        database = tmp_path / "db.sqlite3"
        database.write_bytes(sqlite_backup)
        tar.add(database, arcname="db.sqlite3")
        manifest = json.dumps({"format_version": 1, "components": ["db.sqlite3"]}).encode()
        info = tarfile.TarInfo("backup-manifest.json")
        info.size = len(manifest)
        tar.addfile(info, io.BytesIO(manifest))

    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.method == "GET" and request.url.path == "/containers/vw/json":
            return httpx.Response(
                200,
                json={
                    "Config": {"Image": "vaultwarden/server:1.37.1"},
                    "Mounts": [{"Destination": "/data", "RW": True}],
                    "State": {"Running": True},
                },
            )
        if request.method == "POST" and request.url.path in {
            "/containers/vw/stop",
            "/containers/vw/start",
            "/containers/helper-id/start",
        }:
            return httpx.Response(204)
        if request.method == "POST" and request.url.path == "/containers/create":
            return httpx.Response(201, json={"Id": "helper-id"})
        if request.method == "DELETE" and request.url.path == "/containers/helper-id":
            return httpx.Response(204)
        return httpx.Response(404)

    uploads: list[str] = []
    apply_count = 0
    readiness_count = 0

    async def capture(self, client, container, data_path, staging_dir):  # type: ignore[no-untyped-def]
        rollback = staging_dir / "rollback.tar.gz"
        rollback.write_bytes(b"rollback")
        return rollback

    async def upload(self, client, helper_id, artifact_path):  # type: ignore[no-untyped-def]
        uploads.append(Path(artifact_path).name)

    async def apply(self, client, helper_id, data_path):  # type: ignore[no-untyped-def]
        nonlocal apply_count
        apply_count += 1

    async def verify(self, client, container, data_path):  # type: ignore[no-untyped-def]
        return None

    async def readiness(  # type: ignore[no-untyped-def]
        self, client, container, configured_health_url=None
    ):
        nonlocal readiness_count
        readiness_count += 1
        if readiness_count == 1:
            raise RuntimeError("destination became unhealthy")
        return True

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        VaultWardenPlugin, "_docker_client", lambda self: make_client(transport)  # type: ignore[misc]
    )
    monkeypatch.setattr(VaultWardenPlugin, "_capture_container_artifact", capture)
    monkeypatch.setattr(VaultWardenPlugin, "_upload_restore_archive", upload)
    monkeypatch.setattr(VaultWardenPlugin, "_apply_restore_archive", apply)
    monkeypatch.setattr(VaultWardenPlugin, "_verify_container_database", verify)
    monkeypatch.setattr(VaultWardenPlugin, "_wait_for_container_readiness", readiness)

    with pytest.raises(RuntimeError, match="previous data was restored"):
        await VaultWardenPlugin("vaultwarden").restore(
            RestoreContext(
                job_id="job-1",
                source_target_id="src",
                destination_target_id="dest",
                config={"container_name": "vw"},
                artifact_path=str(artifact),
            )
        )

    assert uploads == [artifact.name, "rollback.tar.gz"]
    assert apply_count == 2
    assert readiness_count == 2
    assert requests.count(("POST", "/containers/vw/stop")) == 2
    assert requests.count(("POST", "/containers/vw/start")) == 2


@pytest.mark.asyncio
async def test_container_readiness_uses_application_probe_without_healthcheck(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "Config": {"Env": ["ROCKET_PORT=80"]},
                "NetworkSettings": {"Networks": {"private": {"IPAddress": "172.30.0.5"}}},
                "State": {"Running": True},
            },
        )

    probed: list[str] = []

    async def probe(self, urls):  # type: ignore[no-untyped-def]
        probed.extend(urls)
        return True

    monkeypatch.setattr(VaultWardenPlugin, "_probe_vaultwarden_http", probe)

    async with make_client(httpx.MockTransport(handler)) as client:
        verified = await VaultWardenPlugin("vaultwarden")._wait_for_container_readiness(
            client, "vw"
        )

    assert verified is True
    assert probed == ["http://172.30.0.5:80/alive"]


def test_verify_artifact_rejects_non_object_manifest(tmp_path: Path) -> None:
    artifact = tmp_path / "invalid-manifest.tar.gz"
    database = tmp_path / "db.sqlite3"
    database.write_bytes(make_sqlite_bytes(tmp_path))
    with tarfile.open(artifact, "w:gz") as archive:
        archive.add(database, arcname="db.sqlite3")
        manifest = b"[]"
        info = tarfile.TarInfo("backup-manifest.json")
        info.size = len(manifest)
        archive.addfile(info, io.BytesIO(manifest))

    with pytest.raises(RuntimeError, match="manifest is invalid"):
        VaultWardenPlugin("vaultwarden")._verify_artifact(str(artifact))


@pytest.mark.asyncio
async def test_restore_rejects_unsafe_archive_before_docker_access(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    artifact = tmp_path / "unsafe.tar.gz"
    database = tmp_path / "db.sqlite3"
    database.write_bytes(make_sqlite_bytes(tmp_path))
    with tarfile.open(artifact, "w:gz") as archive:
        archive.add(database, arcname="db.sqlite3")
        manifest = json.dumps({"format_version": 1, "components": ["db.sqlite3"]}).encode()
        info = tarfile.TarInfo("backup-manifest.json")
        info.size = len(manifest)
        archive.addfile(info, io.BytesIO(manifest))
        unsafe = tarfile.TarInfo("attachments/link")
        unsafe.type = tarfile.SYMTYPE
        unsafe.linkname = "/etc/passwd"
        archive.addfile(unsafe)

    def docker_client_should_not_run(self: VaultWardenPlugin) -> httpx.AsyncClient:
        raise AssertionError("Docker must not be accessed for an unsafe artifact")

    monkeypatch.setattr(VaultWardenPlugin, "_docker_client", docker_client_should_not_run)

    with pytest.raises(RuntimeError, match="unsafe path"):
        await VaultWardenPlugin("vaultwarden").restore(
            RestoreContext(
                job_id="job-1",
                source_target_id="src",
                destination_target_id="dest",
                config={"container_name": "vw"},
                artifact_path=str(artifact),
            )
        )
