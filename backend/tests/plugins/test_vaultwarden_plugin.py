import asyncio
import hashlib
import io
import json
import os
import sqlite3
import stat
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy.orm import Session

from app.core.plugins.base import BackupContext, RestoreContext
from app.core.plugins.loader import get_plugin, get_plugin_schema_path, list_plugins
from app.core.plugins.sidecar import read_backup_sidecar, write_backup_sidecar
from app.main import app
from app.models import Run, Target, TargetRun
from app.plugins.vaultwarden import VaultWardenPlugin
from app.services.restores import RestoreService

VAULTWARDEN_DIGEST = "sha256:e9efdf001bf0d68c21f2cbfb8e1d9b5961a7ca9c85e0a7e58bf51a13b997d744"
VAULTWARDEN_IMAGE = f"vaultwarden/server@{VAULTWARDEN_DIGEST}"
VAULTWARDEN_REVISION = "2629bcbe1380c894e3a7f52cafcac3988edb8fbb"


def exact_container_details() -> dict[str, object]:
    return {
        "Id": "source-id",
        "Config": {
            "Image": VAULTWARDEN_IMAGE,
            "Env": ["ROCKET_PORT=80", "SIGNUPS_ALLOWED=false"],
            "Healthcheck": {"Test": ["CMD", "/healthcheck.sh"]},
            "Labels": {
                "org.opencontainers.image.version": "1.37.1",
                "org.opencontainers.image.revision": VAULTWARDEN_REVISION,
            },
        },
        "Image": "sha256:local-image-id",
        "Mounts": [
            {
                "Destination": "/data",
                "RW": True,
                "Type": "volume",
                "Name": "vaultwarden-data",
            }
        ],
        "State": {"Running": True, "Health": {"Status": "healthy"}},
    }


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


def make_exact_sqlite_bytes(
    tmp_path: Path,
    *,
    attachment: bytes = b"attachment-phase-a",
    send: bytes = b"send-phase-a",
    include_state: bool = True,
) -> bytes:
    database = tmp_path / f"vaultwarden-{len(list(tmp_path.iterdir()))}.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE __diesel_schema_migrations (
                version VARCHAR(50) PRIMARY KEY NOT NULL,
                run_on TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE users (uuid TEXT PRIMARY KEY NOT NULL);
            CREATE TABLE ciphers (uuid TEXT PRIMARY KEY NOT NULL);
            CREATE TABLE organizations (uuid TEXT PRIMARY KEY NOT NULL);
            CREATE TABLE devices (uuid TEXT PRIMARY KEY NOT NULL);
            CREATE TABLE attachments (
                id TEXT PRIMARY KEY NOT NULL,
                cipher_uuid TEXT NOT NULL,
                file_name TEXT NOT NULL,
                file_size INTEGER NOT NULL,
                akey TEXT
            );
            CREATE TABLE sends (
                uuid TEXT PRIMARY KEY NOT NULL,
                user_uuid TEXT,
                organization_uuid TEXT,
                name TEXT NOT NULL,
                notes TEXT,
                atype INTEGER NOT NULL,
                data TEXT NOT NULL,
                akey TEXT NOT NULL,
                deletion_date DATETIME NOT NULL,
                disabled BOOLEAN NOT NULL
            );
            """
        )
        connection.execute(
            "INSERT INTO __diesel_schema_migrations(version) VALUES (?)",
            ("20260505120000",),
        )
        if include_state:
            connection.execute("INSERT INTO users(uuid) VALUES ('user-1')")
            connection.execute("INSERT INTO ciphers(uuid) VALUES ('cipher-1')")
            connection.execute(
                "INSERT INTO attachments(id, cipher_uuid, file_name, file_size) "
                "VALUES (?, ?, ?, ?)",
                ("attachment-1", "cipher-1", "encrypted.bin", len(attachment)),
            )
            connection.execute(
                "INSERT INTO sends(uuid, name, atype, data, akey, deletion_date, disabled) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    "send-1",
                    "encrypted-name",
                    1,
                    json.dumps(
                        {
                            "id": "file-1",
                            "fileName": "encrypted-send.bin",
                            "size": len(send),
                        }
                    ),
                    "encrypted-key",
                    "2099-01-01T00:00:00Z",
                    False,
                ),
            )
    return database.read_bytes()


def make_rsa_private_key() -> bytes:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def make_exact_artifact(tmp_path: Path, *, phase: str = "a") -> Path:
    attachment = f"attachment-{phase}".encode()
    send = f"send-{phase}".encode()
    staging = tmp_path / f"staging-{phase}"
    (staging / "attachments" / "cipher-1").mkdir(parents=True)
    (staging / "sends" / "send-1").mkdir(parents=True)
    (staging / "db.sqlite3").write_bytes(
        make_exact_sqlite_bytes(tmp_path, attachment=attachment, send=send)
    )
    (staging / "attachments" / "cipher-1" / "attachment-1").write_bytes(attachment)
    (staging / "sends" / "send-1" / "file-1").write_bytes(send)
    (staging / "config.json").write_text(json.dumps({"phase": phase}), encoding="utf-8")
    (staging / "rsa_key.pem").write_bytes(make_rsa_private_key())
    artifact = tmp_path / f"vaultwarden-{phase}.tar.gz"
    VaultWardenPlugin("vaultwarden")._write_exact_artifact(staging, artifact)
    return artifact


def test_exact_artifact_is_private_under_a_normal_process_umask(tmp_path: Path) -> None:
    previous_umask = os.umask(0o022)
    try:
        artifact = make_exact_artifact(tmp_path)
    finally:
        os.umask(previous_umask)

    assert stat.S_IMODE(artifact.stat().st_mode) == 0o600


@pytest.mark.asyncio
async def test_discovery_schema_and_exact_configuration_contract() -> None:
    plugin = get_plugin("vaultwarden")
    assert isinstance(plugin, VaultWardenPlugin)
    assert plugin.restore_capability == "automatic"
    assert any(
        item["key"] == "vaultwarden" and item["restore_capability"] == "automatic"
        for item in list_plugins()
    )

    schema_path = get_plugin_schema_path("vaultwarden")
    assert schema_path is not None
    schema = json.loads(Path(schema_path).read_text(encoding="utf-8"))
    assert schema == {
        "type": "object",
        "additionalProperties": False,
        "required": ["container_name", "allow_service_stop"],
        "properties": {
            "container_name": {
                "type": "string",
                "title": "Container Name",
                "default": "vaultwarden",
                "pattern": r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$",
            },
            "allow_service_stop": {
                "type": "boolean",
                "title": "Allow Vaultwarden Stop During Backup",
                "const": True,
                "default": True,
            },
        },
    }

    exact = {"container_name": "vaultwarden", "allow_service_stop": True}
    assert await plugin.validate_config(exact) is True
    invalid: tuple[dict[str, object], ...] = (
        {},
        {"container_name": "vaultwarden"},
        {"allow_service_stop": True},
        {"container_name": "vaultwarden", "allow_service_stop": False},
        {"container_name": "vaultwarden", "allow_service_stop": 1},
        {"container_name": "", "allow_service_stop": True},
        {"container_name": "vault warden", "allow_service_stop": True},
        {"container_name": "vaultwarden", "allow_service_stop": True, "data_path": "/data"},
        {
            "container_name": "vaultwarden",
            "allow_service_stop": True,
            "health_url": "http://vaultwarden/alive",
        },
    )
    for config in invalid:
        assert await plugin.validate_config(config) is False


@pytest.mark.asyncio
async def test_test_and_status_prove_exact_image_layout_and_readiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/containers/vaultwarden/json":
            return httpx.Response(200, json=exact_container_details())
        if request.method == "HEAD" and request.url.path == "/containers/vaultwarden/archive":
            assert request.url.params["path"] == "/data/db.sqlite3"
            return httpx.Response(200)
        if request.method == "POST" and request.url.path == "/containers/vaultwarden/exec":
            command = json.loads(request.content)["Cmd"]
            commands.append(command)
            return httpx.Response(201, json={"Id": f"exec-{len(commands)}"})
        if request.method == "POST" and request.url.path.startswith("/exec/"):
            return httpx.Response(200)
        if request.method == "GET" and request.url.path.startswith("/exec/"):
            return httpx.Response(200, json={"Running": False, "ExitCode": 0})
        raise AssertionError(f"unexpected Docker request: {request.method} {request.url}")

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        VaultWardenPlugin,
        "_docker_client",
        lambda self: make_client(transport),  # type: ignore[misc]
    )
    plugin = VaultWardenPlugin("vaultwarden")
    config = {"container_name": "vaultwarden", "allow_service_stop": True}

    assert await plugin.test(config) is True
    status = await plugin.get_status(
        BackupContext(
            job_id="status",
            target_id="target",
            config=config,
            metadata={"target_slug": "vaultwarden"},
        )
    )

    assert status == {
        "status": "ok",
        "version": "1.37.1",
        "image_digest": VAULTWARDEN_DIGEST,
    }
    health_command = [
        "/usr/bin/timeout",
        "--signal=KILL",
        "120",
        "/healthcheck.sh",
    ]
    version_command = [
        "/usr/bin/timeout",
        "--signal=KILL",
        "120",
        "/bin/sh",
        "-c",
        'test "$(curl -fsS http://127.0.0.1:${ROCKET_PORT:-80}/api/version)" = \'"1.37.1"\'',
    ]
    assert commands == [health_command, version_command, health_command, version_command]


@pytest.mark.asyncio
async def test_restart_readiness_waits_for_health_starting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin = VaultWardenPlugin("vaultwarden")
    attempts = 0

    async def preflight(client: object, container: str) -> dict[str, object]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("Vaultwarden container is not healthy")
        return exact_container_details()

    monkeypatch.setattr(plugin, "_exact_preflight", preflight)

    result = await plugin._wait_for_exact_preflight(object(), "vaultwarden")  # type: ignore[arg-type]

    assert result == exact_container_details()
    assert attempts == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutator", "message"),
    [
        (lambda value: value["Config"].update({"Image": "vaultwarden/server:latest"}), "digest"),
        (
            lambda value: value["Config"]["Env"].append("DATA_FOLDER=/elsewhere"),
            "storage layout",
        ),
        (lambda value: value.update({"Mounts": []}), "writable /data"),
        (lambda value: value["State"].update({"Running": False}), "not running"),
        (
            lambda value: value["State"]["Health"].update({"Status": "unhealthy"}),
            "not healthy",
        ),
    ],
)
async def test_test_rejects_inexact_runtime_before_exec(
    monkeypatch: pytest.MonkeyPatch,
    mutator: Callable[[dict[str, Any]], None],
    message: str,
) -> None:
    details = exact_container_details()
    mutator(details)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/containers/vaultwarden/json":
            return httpx.Response(200, json=details)
        raise AssertionError(f"unexpected request after failed preflight: {request.method}")

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        VaultWardenPlugin, "_docker_client", lambda self: make_client(transport)  # type: ignore[misc]
    )
    with pytest.raises(RuntimeError, match=message):
        await VaultWardenPlugin("vaultwarden").test(
            {"container_name": "vaultwarden", "allow_service_stop": True}
        )


@pytest.mark.asyncio
async def test_public_api_exposes_schema_and_secret_safe_connectivity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exec_counter = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal exec_counter
        if request.method == "GET" and request.url.path == "/containers/vaultwarden/json":
            return httpx.Response(200, json=exact_container_details())
        if request.method == "HEAD" and request.url.path == "/containers/vaultwarden/archive":
            return httpx.Response(200)
        if request.method == "POST" and request.url.path == "/containers/vaultwarden/exec":
            exec_counter += 1
            return httpx.Response(201, json={"Id": f"api-exec-{exec_counter}"})
        if request.method == "POST" and request.url.path.startswith("/exec/api-exec-"):
            return httpx.Response(200)
        if request.method == "GET" and request.url.path.startswith("/exec/api-exec-"):
            return httpx.Response(200, json={"Running": False, "ExitCode": 0})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        VaultWardenPlugin, "_docker_client", lambda self: make_client(transport)  # type: ignore[misc]
    )
    secret = "must-not-appear-in-response"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        discovery = await client.get("/api/v1/plugins/")
        schema = await client.get("/api/v1/plugins/vaultwarden/schema")
        success = await client.post(
            "/api/v1/plugins/vaultwarden/test",
            json={"container_name": "vaultwarden", "allow_service_stop": True},
        )
        failure = await client.post(
            "/api/v1/plugins/vaultwarden/test",
            json={
                "container_name": "vaultwarden",
                "allow_service_stop": False,
                "token": secret,
            },
        )
    assert discovery.status_code == 200
    assert any(
        item["key"] == "vaultwarden" and item["restore_capability"] == "automatic"
        for item in discovery.json()
    )
    assert schema.json()["required"] == ["container_name", "allow_service_stop"]
    assert success.json() == {"ok": True}
    assert failure.json()["ok"] is False
    assert secret not in failure.text


@pytest.mark.asyncio
async def test_backup_stops_source_captures_complete_state_and_restarts_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attachment = b"attachment-phase-a"
    send = b"send-phase-a"
    sqlite_backup = make_exact_sqlite_bytes(tmp_path, attachment=attachment, send=send)
    rsa_key = make_rsa_private_key()
    running = True
    requests: list[tuple[str, str]] = []
    helper_config: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal running
        requests.append((request.method, request.url.path))
        if request.method == "GET" and request.url.path == "/containers/vaultwarden/json":
            details = exact_container_details()
            details["State"] = {
                "Running": running,
                "Health": {"Status": "healthy" if running else "none"},
            }
            return httpx.Response(200, json=details)
        if request.method == "HEAD" and request.url.path == "/containers/vaultwarden/archive":
            return httpx.Response(200 if request.url.params["path"] == "/data/db.sqlite3" else 404)
        if request.method == "POST" and request.url.path == "/containers/vaultwarden/exec":
            return httpx.Response(201, json={"Id": f"health-{len(requests)}"})
        if request.method == "POST" and request.url.path.startswith("/exec/health-"):
            return httpx.Response(200)
        if request.method == "GET" and request.url.path.startswith("/exec/health-"):
            return httpx.Response(200, json={"Running": False, "ExitCode": 0})
        if request.method == "POST" and request.url.path == "/containers/vaultwarden/stop":
            running = False
            return httpx.Response(204)
        if request.method == "POST" and request.url.path == "/containers/create":
            helper_config.update(json.loads(request.content))
            return httpx.Response(201, json={"Id": "backup-helper"})
        if request.method == "POST" and request.url.path == "/containers/backup-helper/start":
            return httpx.Response(204)
        if request.method == "POST" and request.url.path == "/containers/backup-helper/wait":
            return httpx.Response(200, json={"StatusCode": 0})
        if request.method == "GET" and request.url.path == "/containers/backup-helper/archive":
            component = request.url.params["path"]
            archives = {
                "/tmp/db.sqlite3": {"db.sqlite3": sqlite_backup},
                "/tmp/generated-name": {"generated-name": b"db_20260821_120000.sqlite3\n"},
                "/data/attachments": {"attachments/cipher-1/attachment-1": attachment},
                "/data/sends": {"sends/send-1/file-1": send},
                "/data/config.json": {"config.json": b'{"admin_token":"encrypted"}'},
                "/data/rsa_key.pem": {"rsa_key.pem": rsa_key},
            }
            files = archives.get(component)
            return (
                httpx.Response(200, content=make_tar_bytes(files))
                if files is not None
                else httpx.Response(404)
            )
        if request.method == "DELETE" and request.url.path == "/containers/backup-helper":
            return httpx.Response(204)
        if request.method == "POST" and request.url.path == "/containers/vaultwarden/start":
            running = True
            return httpx.Response(204)
        raise AssertionError(f"unexpected Docker request: {request.method} {request.url}")

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        VaultWardenPlugin, "_docker_client", lambda self: make_client(transport)  # type: ignore[misc]
    )
    monkeypatch.setattr("app.plugins.vaultwarden.plugin.BACKUP_BASE_PATH", str(tmp_path))

    plugin = VaultWardenPlugin(name="vaultwarden")
    ctx = BackupContext(
        job_id="job-1",
        target_id="target-1",
        config={"container_name": "vaultwarden", "allow_service_stop": True},
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
        assert "rsa_key.pub.pem" not in names
        assert "sends/send-1/file-1" in names
        assert "attachments/cipher-1/attachment-1" in names
        assert "backup-manifest.json" in names
        restored_db = tar.extractfile("db.sqlite3")
        assert restored_db is not None
        assert restored_db.read() == sqlite_backup
        manifest_member = tar.extractfile("backup-manifest.json")
        assert manifest_member is not None
        manifest = json.loads(manifest_member.read())
    assert manifest["format_version"] == 2
    assert manifest["application"] == "vaultwarden"
    assert manifest["application_version"] == "1.37.1"
    assert manifest["image_digest"] == VAULTWARDEN_DIGEST
    assert manifest["source_revision"] == VAULTWARDEN_REVISION
    assert manifest["database_migration"] == "20260505120000"
    inventory = {entry["path"]: entry for entry in manifest["files"]}
    assert inventory["attachments/cipher-1/attachment-1"] == {
        "path": "attachments/cipher-1/attachment-1",
        "bytes": len(attachment),
        "sha256": hashlib.sha256(attachment).hexdigest(),
    }
    assert inventory["sends/send-1/file-1"]["sha256"] == hashlib.sha256(send).hexdigest()
    assert helper_config["Image"] == VAULTWARDEN_IMAGE
    assert helper_config["WorkingDir"] == "/"
    assert helper_config["NetworkDisabled"] is True
    assert helper_config["Volumes"] == {"/tmp": {}}
    assert helper_config["HostConfig"] == {
        "VolumesFrom": ["vaultwarden:rw"],
        "NetworkMode": "none",
        "ReadonlyRootfs": True,
        "CapDrop": ["ALL"],
        "SecurityOpt": ["no-new-privileges:true"],
        "PidsLimit": 128,
    }
    assert requests.index(("POST", "/containers/vaultwarden/stop")) < requests.index(
        ("POST", "/containers/create")
    )
    assert requests.index(("DELETE", "/containers/backup-helper")) < requests.index(
        ("POST", "/containers/vaultwarden/start")
    )
    assert running is True
    assert stat.S_IMODE(Path(artifact_path).stat().st_mode) == 0o600
    sidecar = read_backup_sidecar(str(artifact_path))
    assert sidecar is not None
    assert stat.S_IMODE(Path(f"{artifact_path}.meta.json").stat().st_mode) == 0o600
    assert sidecar["artifact_bytes"] == Path(artifact_path).stat().st_size
    assert sidecar["sha256"] == hashlib.sha256(Path(artifact_path).read_bytes()).hexdigest()
    assert sidecar["application_version"] == "1.37.1"
    assert sidecar["source_container_id"] == "source-id"
    assert sidecar["attachment_count"] == 1
    assert sidecar["file_send_count"] == 1


@pytest.mark.asyncio
async def test_restore_requires_explicit_local_authorization_before_docker_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = make_exact_artifact(tmp_path)
    metadata = {
        "artifact_bytes": artifact.stat().st_size,
        "artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
        "artifact_sidecar": {"source_container_id": "source-id"},
        "source_database_identity": {"container_name": "vaultwarden-source"},
    }
    config = {"container_name": "vaultwarden-restore", "allow_service_stop": True}

    def forbidden_client(self: VaultWardenPlugin) -> httpx.AsyncClient:
        raise AssertionError("Docker must not be contacted before restore authorization")

    monkeypatch.setattr(VaultWardenPlugin, "_docker_client", forbidden_client)
    monkeypatch.delenv("HOMELAB_BACKUP_ALLOW_ISOLATED_RESTORE", raising=False)
    monkeypatch.delenv("HOMELAB_BACKUP_ISOLATED_RESTORE_ALLOWED_CONTAINERS", raising=False)

    with pytest.raises(ValueError, match="isolated local restore"):
        await VaultWardenPlugin("vaultwarden").restore(
            RestoreContext(
                job_id="restore-1",
                source_target_id="source-target",
                destination_target_id="destination-target",
                config=config,
                artifact_path=str(artifact),
                metadata=metadata,
            )
        )


@pytest.mark.asyncio
async def test_restore_refuses_nonfresh_destination_before_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = make_exact_artifact(tmp_path)
    destination_database = make_exact_sqlite_bytes(tmp_path, include_state=True)
    details = exact_container_details()
    details["Id"] = "destination-id"
    config = details["Config"]
    assert isinstance(config, dict)
    labels = config["Labels"]
    assert isinstance(labels, dict)
    labels["asia.hollinger.homelab-backup.restore-destination"] = "true"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/containers/vaultwarden-restore/json":
            return httpx.Response(200, json=details)
        if (
            request.method == "HEAD"
            and request.url.path == "/containers/vaultwarden-restore/archive"
        ):
            return httpx.Response(200)
        if request.method == "POST" and request.url.path == "/containers/vaultwarden-restore/exec":
            return httpx.Response(201, json={"Id": "freshness-health"})
        if request.method == "POST" and request.url.path == "/exec/freshness-health/start":
            return httpx.Response(200)
        if request.method == "GET" and request.url.path == "/exec/freshness-health/json":
            return httpx.Response(200, json={"Running": False, "ExitCode": 0})
        if (
            request.method == "GET"
            and request.url.path == "/containers/vaultwarden-restore/archive"
        ):
            assert request.url.params["path"] == "/data/db.sqlite3"
            return httpx.Response(200, content=make_tar_bytes({"db.sqlite3": destination_database}))
        raise AssertionError(f"unexpected Docker mutation: {request.method} {request.url.path}")

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        VaultWardenPlugin, "_docker_client", lambda self: make_client(transport)  # type: ignore[misc]
    )
    monkeypatch.setenv("HOMELAB_BACKUP_ALLOW_ISOLATED_RESTORE", "1")
    monkeypatch.setenv(
        "HOMELAB_BACKUP_ISOLATED_RESTORE_ALLOWED_CONTAINERS",
        "vaultwarden-restore",
    )
    metadata = {
        "artifact_bytes": artifact.stat().st_size,
        "artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
        "artifact_sidecar": {"source_container_id": "source-id"},
        "source_database_identity": {"container_name": "vaultwarden-source"},
    }
    with pytest.raises(ValueError, match="fresh"):
        await VaultWardenPlugin("vaultwarden").restore(
            RestoreContext(
                job_id="restore-1",
                source_target_id="source-target",
                destination_target_id="destination-target",
                config={
                    "container_name": "vaultwarden-restore",
                    "allow_service_stop": True,
                },
                artifact_path=str(artifact),
                metadata=metadata,
            )
        )


@pytest.mark.asyncio
async def test_restore_applies_verified_artifact_to_fresh_exact_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = make_exact_artifact(tmp_path, phase="restore")
    fresh_rsa = make_rsa_private_key()
    files: dict[str, bytes] = {
        "db.sqlite3": make_exact_sqlite_bytes(tmp_path, include_state=False),
        "config.json": b"{}",
        "rsa_key.pem": fresh_rsa,
    }
    restored_files: dict[str, bytes] = {}
    running = True
    started_at = "2026-08-21T00:00:00.000000000Z"
    exec_counter = 0
    helper_config: dict[str, object] = {}

    details = exact_container_details()
    details["Id"] = "destination-id"
    config_details = details["Config"]
    assert isinstance(config_details, dict)
    labels = config_details["Labels"]
    assert isinstance(labels, dict)
    labels["asia.hollinger.homelab-backup.restore-destination"] = "true"

    def container_details() -> dict[str, object]:
        current: dict[str, object] = json.loads(json.dumps(details))
        current["State"] = {
            "Running": running,
            "Health": {"Status": "healthy" if running else "none"},
            "StartedAt": started_at,
        }
        return current

    def component_archive(path: str) -> httpx.Response:
        relative = path.removeprefix("/data/")
        if relative in files:
            return httpx.Response(200, content=make_tar_bytes({relative: files[relative]}))
        prefix = f"{relative}/"
        selected = {name: value for name, value in files.items() if name.startswith(prefix)}
        if selected:
            return httpx.Response(200, content=make_tar_bytes(selected))
        return httpx.Response(404)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal running, started_at, exec_counter, files, restored_files
        path = request.url.path
        if request.method == "GET" and path == "/containers/vaultwarden-restore/json":
            return httpx.Response(200, json=container_details())
        if request.method == "HEAD" and path == "/containers/vaultwarden-restore/archive":
            return httpx.Response(200 if request.url.params["path"] == "/data/db.sqlite3" else 404)
        if request.method == "GET" and path == "/containers/vaultwarden-restore/archive":
            return component_archive(request.url.params["path"])
        if request.method == "POST" and path == "/containers/vaultwarden-restore/stop":
            running = False
            return httpx.Response(204)
        if request.method == "POST" and path == "/containers/vaultwarden-restore/start":
            running = True
            started_at = "2026-08-21T00:01:00.000000000Z"
            return httpx.Response(204)
        if request.method == "POST" and path == "/containers/create":
            helper_config.update(json.loads(request.content))
            return httpx.Response(201, json={"Id": "restore-helper"})
        if request.method == "POST" and path == "/containers/restore-helper/start":
            return httpx.Response(204)
        if request.method == "PUT" and path == "/containers/restore-helper/archive":
            with tarfile.open(fileobj=io.BytesIO(request.content), mode="r:") as outer:
                wrapped = outer.extractfile("restore.tar.gz")
                assert wrapped is not None
                with tarfile.open(fileobj=io.BytesIO(wrapped.read()), mode="r:gz") as inner:
                    restored_files = {
                        member.name: inner.extractfile(member).read()  # type: ignore[union-attr]
                        for member in inner.getmembers()
                        if member.isfile() and member.name != "backup-manifest.json"
                    }
            return httpx.Response(200)
        if request.method == "POST" and path.endswith("/exec"):
            exec_counter += 1
            command = json.loads(request.content)["Cmd"]
            if path == "/containers/restore-helper/exec" and "tar" in command:
                files = dict(restored_files)
            return httpx.Response(201, json={"Id": f"exec-{exec_counter}"})
        if request.method == "POST" and path.startswith("/exec/"):
            return httpx.Response(200)
        if request.method == "GET" and path.startswith("/exec/"):
            return httpx.Response(200, json={"Running": False, "ExitCode": 0})
        if request.method == "DELETE" and path == "/containers/restore-helper":
            return httpx.Response(204)
        raise AssertionError(f"unexpected Docker request: {request.method} {request.url}")

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        VaultWardenPlugin, "_docker_client", lambda self: make_client(transport)  # type: ignore[misc]
    )
    monkeypatch.setenv("HOMELAB_BACKUP_ALLOW_ISOLATED_RESTORE", "1")
    monkeypatch.setenv(
        "HOMELAB_BACKUP_ISOLATED_RESTORE_ALLOWED_CONTAINERS",
        "vaultwarden-restore,another-local-target",
    )
    metadata = {
        "artifact_bytes": artifact.stat().st_size,
        "artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
        "artifact_sidecar": {"source_container_id": "source-id"},
        "source_database_identity": {"container_name": "vaultwarden-source"},
    }
    result = await VaultWardenPlugin("vaultwarden").restore(
        RestoreContext(
            job_id="restore-1",
            source_target_id="source-target",
            destination_target_id="destination-target",
            config={
                "container_name": "vaultwarden-restore",
                "allow_service_stop": True,
            },
            artifact_path=str(artifact),
            metadata=metadata,
        )
    )

    assert result["status"] == "success"
    assert result["artifact_path"] == str(artifact)
    assert files["attachments/cipher-1/attachment-1"] == b"attachment-restore"
    assert files["sends/send-1/file-1"] == b"send-restore"
    assert helper_config["Image"] == VAULTWARDEN_IMAGE
    assert helper_config["NetworkDisabled"] is True
    assert helper_config["HostConfig"] == {
        "VolumesFrom": ["vaultwarden-restore:rw"],
        "NetworkMode": "none",
        "ReadonlyRootfs": True,
        "CapDrop": ["ALL"],
        "SecurityOpt": ["no-new-privileges:true"],
        "PidsLimit": 128,
    }


@pytest.mark.parametrize("mutation", ["payload", "manifest", "symlink"])
def test_strict_artifact_validation_rejects_tampering(tmp_path: Path, mutation: str) -> None:
    artifact = make_exact_artifact(tmp_path)
    tampered = tmp_path / f"tampered-{mutation}.tar.gz"
    with tarfile.open(artifact, "r:gz") as source, tarfile.open(tampered, "w:gz") as target:
        for member in source.getmembers():
            extracted = source.extractfile(member)
            assert extracted is not None
            content = extracted.read()
            if mutation == "payload" and member.name == "sends/send-1/file-1":
                content = b"same-size-wrong"
                member.size = len(content)
            if mutation == "manifest" and member.name == "backup-manifest.json":
                manifest = json.loads(content)
                manifest["application_version"] = "1.37.0"
                content = json.dumps(manifest).encode()
                member.size = len(content)
            target.addfile(member, io.BytesIO(content))
        if mutation == "symlink":
            link = tarfile.TarInfo("attachments/unsafe")
            link.type = tarfile.SYMTYPE
            link.linkname = "/etc/passwd"
            target.addfile(link)

    with pytest.raises(RuntimeError):
        VaultWardenPlugin("vaultwarden")._verify_artifact(str(tampered))


@pytest.mark.asyncio
async def test_backup_helper_failure_restarts_source_and_publishes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []

    class Client:
        async def __aenter__(self) -> "Client":
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

    plugin = VaultWardenPlugin("vaultwarden")

    async def preflight(client: object, container: str) -> dict[str, object]:
        events.append("preflight")
        return exact_container_details()

    async def stop(client: object, container: str) -> None:
        events.append("stop")

    async def confirm(client: object, container: str, identity: str) -> None:
        events.append("stopped")

    async def create(client: object, container: str) -> str:
        events.append("create")
        return "helper"

    async def start(client: object, container: str) -> None:
        events.append(f"start:{container}")

    async def wait(client: object, helper: str) -> None:
        raise RuntimeError("synthetic helper failure")

    async def remove(client: object, helper: str, *, strict: bool) -> None:
        events.append("remove")

    monkeypatch.setattr(plugin, "_docker_client", lambda: Client())
    monkeypatch.setattr(plugin, "_exact_preflight", preflight)
    monkeypatch.setattr(plugin, "_stop_container", stop)
    monkeypatch.setattr(plugin, "_confirm_stopped", confirm)
    monkeypatch.setattr(plugin, "_create_backup_helper", create)
    monkeypatch.setattr(plugin, "_start_container", start)
    monkeypatch.setattr(plugin, "_wait_for_backup_helper", wait)
    monkeypatch.setattr(plugin, "_remove_helper", remove)
    monkeypatch.setattr("app.plugins.vaultwarden.plugin.BACKUP_BASE_PATH", str(tmp_path))

    with pytest.raises(RuntimeError, match="synthetic helper failure"):
        await plugin.backup(
            BackupContext(
                job_id="failure",
                target_id="target",
                config={"container_name": "vaultwarden", "allow_service_stop": True},
                metadata={"target_slug": "vaultwarden"},
            )
        )
    assert events[-3:] == ["remove", "start:vaultwarden", "preflight"]
    assert not list(tmp_path.rglob("*.tar.gz"))


@pytest.mark.asyncio
async def test_restore_rejects_staged_digest_mismatch_before_docker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = make_exact_artifact(tmp_path)
    monkeypatch.setenv("HOMELAB_BACKUP_ALLOW_ISOLATED_RESTORE", "1")
    monkeypatch.setenv("HOMELAB_BACKUP_ISOLATED_RESTORE_ALLOWED_CONTAINERS", "vaultwarden-restore")
    monkeypatch.setattr(
        VaultWardenPlugin,
        "_docker_client",
        lambda self: (_ for _ in ()).throw(  # type: ignore[misc]
            AssertionError("Docker must not be contacted")
        ),
    )
    with pytest.raises(ValueError, match="identity does not match"):
        await VaultWardenPlugin("vaultwarden").restore(
            RestoreContext(
                job_id="restore",
                source_target_id="source",
                destination_target_id="destination",
                config={
                    "container_name": "vaultwarden-restore",
                    "allow_service_stop": True,
                },
                artifact_path=str(artifact),
                metadata={
                    "artifact_bytes": artifact.stat().st_size,
                    "artifact_sha256": "0" * 64,
                    "artifact_sidecar": {"source_container_id": "source-id"},
                    "source_database_identity": {"container_name": "vaultwarden-source"},
                },
            )
        )


@pytest.mark.asyncio
async def test_post_mutation_restore_failure_rolls_back_and_restarts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin = VaultWardenPlugin("vaultwarden")
    artifact = make_exact_artifact(tmp_path)
    expected = plugin._verify_artifact(str(artifact))
    artifact_fd = os.open(artifact, os.O_RDONLY)
    events: list[str] = []
    rollback = tmp_path / "rollback.tar.gz"
    rollback.write_bytes(b"rollback")

    async def stop(client: object, container: str) -> None:
        events.append("stop")

    async def inspect(client: object, container: str) -> dict[str, object]:
        details = exact_container_details()
        details["Id"] = "destination-id"
        details["State"] = {"Running": False}
        return details

    async def capture(*args: object) -> Path:
        events.append("capture")
        return rollback

    async def create(client: object, container: str) -> str:
        events.append("create")
        return "helper"

    async def start(client: object, container: str) -> None:
        events.append(f"start:{container}")

    async def upload(client: object, helper: str, source: Path | int) -> None:
        events.append("upload:artifact" if isinstance(source, int) else "upload:rollback")

    async def apply(client: object, helper: str, data_path: str) -> None:
        events.append("apply")

    async def verify_state(client: object, container: str) -> object:
        raise RuntimeError("synthetic restored-state mismatch")

    async def verify_database(client: object, container: str, data_path: str) -> None:
        events.append("verify:rollback")

    async def remove(client: object, helper: str, *, strict: bool) -> None:
        events.append("remove")

    async def preflight(client: object, container: str) -> dict[str, object]:
        events.append("preflight")
        return exact_container_details()

    async def fresh(client: object, container: str) -> None:
        events.append("fresh")

    monkeypatch.setattr(plugin, "_stop_container", stop)
    monkeypatch.setattr(plugin, "_inspect_container", inspect)
    monkeypatch.setattr(plugin, "_capture_container_artifact", capture)
    monkeypatch.setattr(plugin, "_create_restore_helper", create)
    monkeypatch.setattr(plugin, "_start_container", start)
    monkeypatch.setattr(plugin, "_upload_restore_archive", upload)
    monkeypatch.setattr(plugin, "_apply_restore_archive", apply)
    monkeypatch.setattr(plugin, "_verify_container_state", verify_state)
    monkeypatch.setattr(plugin, "_verify_container_database", verify_database)
    monkeypatch.setattr(plugin, "_remove_helper", remove)
    monkeypatch.setattr(plugin, "_exact_preflight", preflight)
    monkeypatch.setattr(plugin, "_assert_fresh_destination", fresh)

    try:
        with pytest.raises(RuntimeError, match="fresh preimage"):
            await plugin._perform_restore_with_rollback(
                object(),  # type: ignore[arg-type]
                "vaultwarden-restore",
                artifact_fd,
                expected,
                tmp_path / "workspace",
                "2026-08-21T00:00:00Z",
            )
    finally:
        os.close(artifact_fd)
    assert events == [
        "stop",
        "capture",
        "create",
        "start:helper",
        "upload:artifact",
        "apply",
        "upload:rollback",
        "apply",
        "verify:rollback",
        "remove",
        "start:vaultwarden-restore",
        "preflight",
        "fresh",
    ]


def test_container_identity_lock_serializes_operations() -> None:
    plugin = VaultWardenPlugin("vaultwarden")
    lock = plugin._acquire_container_lock("vaultwarden-identity-lock-test")
    try:
        with pytest.raises(RuntimeError, match="already has"):
            plugin._acquire_container_lock("vaultwarden-identity-lock-test")
    finally:
        lock.release()
    second = plugin._acquire_container_lock("vaultwarden-identity-lock-test")
    second.release()


@pytest.mark.asyncio
async def test_worker_cleanup_survives_repeated_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin = VaultWardenPlugin("vaultwarden")
    release = asyncio.Event()
    finished = asyncio.Event()

    async def stop(process: object) -> None:
        await release.wait()
        finished.set()

    monkeypatch.setattr(plugin, "_stop_artifact_worker", stop)
    task = asyncio.create_task(plugin._stop_artifact_worker_before_return(object()))  # type: ignore[arg-type]
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()


def test_restore_service_stages_private_artifact_and_records_automatic_success(
    db_session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin = VaultWardenPlugin("vaultwarden")
    source = Target(
        name="Vaultwarden Source",
        slug="vaultwarden-source",
        plugin_name="vaultwarden",
        plugin_config_json=json.dumps(
            {"container_name": "vaultwarden-source", "allow_service_stop": True}
        ),
    )
    destination = Target(
        name="Vaultwarden Restore",
        slug="vaultwarden-restore",
        plugin_name="vaultwarden",
        plugin_config_json=json.dumps(
            {"container_name": "vaultwarden-restore", "allow_service_stop": True}
        ),
    )
    db_session.add_all([source, destination])
    db_session.commit()
    artifact_directory = tmp_path / source.slug / "2026-08-21"
    artifact_directory.mkdir(parents=True)
    built = make_exact_artifact(tmp_path, phase="service")
    artifact = artifact_directory / "vaultwarden-service.tar.gz"
    artifact.write_bytes(built.read_bytes())
    write_backup_sidecar(
        str(artifact),
        plugin,
        BackupContext(
            job_id="source-run",
            target_id=str(source.id),
            config={"container_name": "vaultwarden-source", "allow_service_stop": True},
            metadata={"target_slug": source.slug},
        ),
    )
    artifact_size = artifact.stat().st_size
    artifact_sha = hashlib.sha256(artifact.read_bytes()).hexdigest()
    source_run = Run(
        status="success",
        operation="backup",
        started_at=datetime.now(timezone.utc),
        finished_at=datetime.now(timezone.utc),
    )
    db_session.add(source_run)
    db_session.commit()
    source_target_run = TargetRun(
        run_id=source_run.id,
        target_id=source.id,
        status="success",
        operation="backup",
        artifact_path=str(artifact),
        artifact_bytes=artifact_size,
        sha256=artifact_sha,
        source_identity_json=json.dumps({"container_name": "vaultwarden-source"}),
        started_at=source_run.started_at,
        finished_at=source_run.finished_at,
    )
    db_session.add(source_target_run)
    db_session.commit()
    observed: dict[str, object] = {}

    async def observe(context: RestoreContext) -> dict[str, Any]:
        staged = Path(context.artifact_path)
        observed["inode"] = staged.stat().st_ino
        observed["mode"] = stat.S_IMODE(staged.stat().st_mode)
        observed["metadata"] = dict(context.metadata or {})
        assert hashlib.sha256(staged.read_bytes()).hexdigest() == artifact_sha
        return {
            "status": "success",
            "artifact_path": str(staged),
            "artifact_bytes": staged.stat().st_size,
            "message": "isolated Vaultwarden restore verified",
        }

    monkeypatch.setattr(plugin, "restore", observe)
    monkeypatch.setattr("app.services.restores.get_plugin", lambda _: plugin)
    monkeypatch.setenv("BACKUP_BASE_PATH", str(tmp_path))
    result = RestoreService(db_session).restore(
        source_target_run_id=source_target_run.id,
        destination_target_id=destination.id,
        triggered_by="vaultwarden-local-drill",
    )

    assert observed["inode"] != artifact.stat().st_ino
    assert observed["mode"] == 0o600
    metadata = observed["metadata"]
    assert isinstance(metadata, dict)
    assert metadata["artifact_bytes"] == artifact_size
    assert metadata["artifact_sha256"] == artifact_sha
    assert metadata["source_database_identity"] == {"container_name": "vaultwarden-source"}
    assert result.status == "success"
    assert result.target_runs[0].status == "success"
    assert result.target_runs[0].message == "isolated Vaultwarden restore verified"
