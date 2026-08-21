from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import secrets
import select
import shutil
import socket
import socketserver
import ssl
import stat
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import Session

from app.core.plugins.base import BackupContext
from app.core.plugins.sidecar import read_backup_sidecar
from app.models import Run, Target, TargetRun
from app.plugins.vaultwarden import VaultWardenPlugin
from app.services.restores import RestoreService

IMAGE = (
    "vaultwarden/server@sha256:" "e9efdf001bf0d68c21f2cbfb8e1d9b5961a7ca9c85e0a7e58bf51a13b997d744"
)
VERSION = "1.37.1"
CLI_VERSION = "2025.12.0"
DRILL_LABEL = "asia.hollinger.homelab-backup.vaultwarden-drill"
RESTORE_LABEL = "asia.hollinger.homelab-backup.restore-destination"
HELPER = Path(__file__).with_name("vaultwarden_webvault_helper.js")


def _run(
    command: list[str],
    *,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
    timeout: float = 120.0,
) -> str:
    try:
        completed = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            input=input_text,
            env=env,
            timeout=timeout,
        )
    except subprocess.CalledProcessError as exc:
        diagnostic = (exc.stderr or "").lower()
        category = next(
            (
                name
                for name in (
                    "premium",
                    "limit",
                    "forbidden",
                    "unauthorized",
                    "not found",
                    "invalid",
                    "unknown option",
                    "(login-load)",
                    "(login-email)",
                    "(login-continue)",
                    "(login-password)",
                    "(login-submit)",
                    "(login-ready)",
                    "(search)",
                    "(open-item)",
                    "(edit-item)",
                    "(open-attachments)",
                    "(choose-file)",
                    "(upload)",
                    "(upload-confirmation)",
                    "(download-login)",
                    "(download-open-item)",
                    "(download-item-name)",
                    "(download-note)",
                    "(download-edit-item)",
                    "(download-open-attachments)",
                    "(download-file)",
                    "attachment",
                )
                if name in diagnostic
            ),
            "unclassified",
        )
        raise RuntimeError(
            f"disposable command failed: {Path(command[0]).name} ({category})"
        ) from None
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"disposable command timed out: {Path(command[0]).name}") from None
    return completed.stdout.strip()


def _docker(*arguments: str, timeout: float = 120.0) -> str:
    return _run(["docker", *arguments], timeout=timeout)


class _ProxyHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        server = self.server
        upstream = getattr(server, "upstream", None)
        if not isinstance(upstream, str):
            return
        remote = socket.create_connection((upstream, 80), timeout=10)
        sockets = [self.request, remote]
        try:
            while True:
                readable, _, _ = select.select(sockets, [], [], 30)
                if not readable:
                    return
                for source in readable:
                    data = source.recv(64 * 1024)
                    if not data:
                        return
                    destination = remote if source is self.request else self.request
                    destination.sendall(data)
        finally:
            remote.close()


class _TlsServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, cert: Path, key: Path) -> None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        self._ssl_context = context
        self.upstream: str | None = None
        super().__init__(("127.0.0.1", 0), _ProxyHandler)

    def get_request(self) -> tuple[socket.socket, Any]:
        connection, address = super().get_request()
        return self._ssl_context.wrap_socket(connection, server_side=True), address


@dataclass
class _Phase:
    item_id: str
    item_name: str
    note: str
    attachment_name: str
    attachment_sha256: str


class _Drill:
    def __init__(
        self,
        root: Path,
        round_number: int,
        cli: Path,
        playwright_module: str,
    ) -> None:
        suffix = f"{round_number}-{uuid.uuid4().hex[:8]}"
        self.root = root
        self.prefix = f"codex-vaultwarden-{suffix}"
        self.network = f"{self.prefix}-network"
        self.source = f"{self.prefix}-source"
        self.destinations = [f"{self.prefix}-restore-a", f"{self.prefix}-restore-b"]
        self.volumes = [f"{self.prefix}-source-data"] + [
            f"{name}-data" for name in self.destinations
        ]
        self.cli = cli
        self.playwright_module = playwright_module
        self.cli_roots: list[Path] = []
        self.cert = root / "tls-cert.pem"
        self.key = root / "tls-key.pem"
        _run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-days",
                "1",
                "-subj",
                "/CN=localhost",
                "-addext",
                "subjectAltName=DNS:localhost",
                "-keyout",
                str(self.key),
                "-out",
                str(self.cert),
            ]
        )
        os.chmod(self.key, 0o600)
        self.server = _TlsServer(self.cert, self.key)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.origin = f"https://localhost:{self.server.server_address[1]}"

    def create_resources(self) -> None:
        _docker("network", "create", "--internal", "--label", f"{DRILL_LABEL}=1", self.network)
        for volume in self.volumes:
            _docker("volume", "create", "--label", f"{DRILL_LABEL}=1", volume)
        self._start_container(self.source, self.volumes[0], restore=False)

    def _start_container(self, name: str, volume: str, *, restore: bool) -> None:
        command = [
            "run",
            "-d",
            "--pull",
            "never",
            "--name",
            name,
            "--network",
            self.network,
            "--label",
            f"{DRILL_LABEL}=1",
        ]
        if restore:
            command.extend(["--label", f"{RESTORE_LABEL}=true"])
        command.extend(
            [
                "-e",
                f"DOMAIN={self.origin}",
                "-e",
                "SIGNUPS_ALLOWED=true",
                "-v",
                f"{volume}:/data",
                IMAGE,
            ]
        )
        _docker(*command)
        self._wait_ready(name)

    def create_destination(self, index: int) -> str:
        name = self.destinations[index]
        self._start_container(name, self.volumes[index + 1], restore=True)
        return name

    def _wait_ready(self, container: str) -> None:
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            state = json.loads(_docker("inspect", container))[0]["State"]
            if state.get("Health", {}).get("Status") == "healthy":
                version = _docker("exec", container, "curl", "-fsS", "http://127.0.0.1/api/version")
                if json.loads(version) == VERSION:
                    return
            time.sleep(0.25)
        raise RuntimeError("exact Vaultwarden container did not become ready")

    def address(self, container: str) -> str:
        details = json.loads(_docker("inspect", container))[0]
        address = details["NetworkSettings"]["Networks"][self.network]["IPAddress"]
        if not isinstance(address, str) or not address:
            raise RuntimeError("disposable container has no internal address")
        return address

    def point_proxy(self, container: str) -> str:
        address = self.address(container)
        self.server.upstream = address
        return address

    def web(self, mode: str, container: str, credential: dict[str, Any]) -> None:
        address = self.address(container)
        credential_path = self.root / f"web-{uuid.uuid4().hex}.json"
        credential_path.write_text(json.dumps(credential), encoding="utf-8")
        os.chmod(credential_path, 0o600)
        environment = os.environ.copy()
        environment.update(
            {
                "VAULTWARDEN_PLAYWRIGHT_MODULE": self.playwright_module,
                "VAULTWARDEN_CREDENTIAL_FILE": str(credential_path),
                "VAULTWARDEN_WEB_ORIGIN": self.origin,
                "VAULTWARDEN_UPSTREAM": f"http://{address}",
                "VAULTWARDEN_WEB_MODE": mode,
            }
        )
        output = _run(["node", str(HELPER)], env=environment, timeout=90)
        if json.loads(output) != {"ok": True}:
            raise RuntimeError("Web Vault helper returned invalid evidence")
        credential_path.unlink()

    def login(self, container: str, email: str, password_file: Path) -> tuple[Path, str]:
        self.point_proxy(container)
        cli_root = self.root / f"cli-{container}-{uuid.uuid4().hex}"
        cli_root.mkdir(mode=0o700)
        self.cli_roots.append(cli_root)
        environment = self._cli_env(cli_root)
        _run([str(self.cli), "config", "server", self.origin], env=environment)
        session = _run(
            [
                str(self.cli),
                "login",
                email,
                "--passwordfile",
                str(password_file),
                "--raw",
                "--nointeraction",
            ],
            env=environment,
        )
        if not session or any(character.isspace() for character in session):
            raise RuntimeError("official CLI did not return a private session")
        _run(
            [str(self.cli), "sync", "--nointeraction"],
            env=self._cli_env(cli_root, session),
        )
        return cli_root, session

    def _cli_env(self, cli_root: Path, session: str | None = None) -> dict[str, str]:
        environment = os.environ.copy()
        environment.update(
            {
                "BITWARDENCLI_APPDATA_DIR": str(cli_root),
                "NODE_EXTRA_CA_CERTS": str(self.cert),
            }
        )
        if session is not None:
            environment["BW_SESSION"] = session
        return environment

    def bw_json(
        self, cli_root: Path, session: str, arguments: list[str], input_text: str | None = None
    ) -> dict[str, Any]:
        output = _run(
            [str(self.cli), *arguments, "--nointeraction"],
            env=self._cli_env(cli_root, session),
            input_text=input_text,
        )
        value = json.loads(output)
        if not isinstance(value, dict):
            raise RuntimeError("official CLI returned non-object evidence")
        return value

    def assert_no_sends(self, cli_root: Path, session: str) -> None:
        value = json.loads(
            _run(
                [str(self.cli), "send", "list", "--nointeraction"],
                env=self._cli_env(cli_root, session),
            )
        )
        if value != []:
            raise RuntimeError("file Sends are outside this drill's recovery scope")

    def create_phase(
        self,
        cli_root: Path,
        session: str,
        phase_name: str,
        email: str,
        password: str,
    ) -> _Phase:
        item_name = f"vaultwarden-{phase_name}-note"
        note = f"vaultwarden-{phase_name}-body"
        item_payload = {
            "type": 2,
            "name": item_name,
            "notes": note,
            "favorite": False,
            "secureNote": {"type": 0},
        }
        encoded = base64.b64encode(
            json.dumps(item_payload, separators=(",", ":")).encode()
        ).decode()
        item = self.bw_json(cli_root, session, ["create", "item"], encoded + "\n")
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id:
            raise RuntimeError("official CLI did not create the synthetic note")

        attachment_bytes = secrets.token_bytes(257)
        attachment_path = self.root / f"attachment-{phase_name}.bin"
        attachment_path.write_bytes(attachment_bytes)
        os.chmod(attachment_path, 0o600)
        self.web(
            "attach",
            self.source,
            {
                "email": email,
                "password": password,
                "item_name": item_name,
                "item_id": item_id,
                "file_path": str(attachment_path),
            },
        )
        _run(
            [str(self.cli), "sync", "--nointeraction"],
            env=self._cli_env(cli_root, session),
        )
        updated_item = self.bw_json(cli_root, session, ["get", "item", item_id])
        attachments = updated_item.get("attachments")
        if not isinstance(attachments, list) or len(attachments) != 1:
            raise RuntimeError("official CLI did not attach the synthetic file")
        attachment_name = attachments[0].get("fileName")
        if attachment_name != attachment_path.name:
            raise RuntimeError("attachment filename is missing")

        self.assert_no_sends(cli_root, session)
        source_download = self.root / f"source-download-{phase_name}.bin"
        self.web(
            "download-attachment",
            self.source,
            {
                "email": email,
                "password": password,
                "item_id": item_id,
                "item_name": item_name,
                "note": note,
                "attachment_name": attachment_name,
                "output_path": str(source_download),
            },
        )
        if (
            hashlib.sha256(source_download.read_bytes()).digest()
            != hashlib.sha256(attachment_bytes).digest()
        ):
            raise RuntimeError("source attachment plaintext does not match")

        return _Phase(
            item_id=item_id,
            item_name=item_name,
            note=note,
            attachment_name=attachment_name,
            attachment_sha256=hashlib.sha256(attachment_bytes).hexdigest(),
        )

    def verify_phase(
        self,
        container: str,
        phase: _Phase,
        suffix: str,
        email: str,
        password: str,
    ) -> None:
        attachment = self.root / f"download-{suffix}-{phase.item_id}.bin"
        self.web(
            "download-attachment",
            container,
            {
                "email": email,
                "password": password,
                "item_id": phase.item_id,
                "item_name": phase.item_name,
                "note": phase.note,
                "attachment_name": phase.attachment_name,
                "output_path": str(attachment),
            },
        )
        assert hashlib.sha256(attachment.read_bytes()).hexdigest() == phase.attachment_sha256

    def cleanup(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        for container in [self.source, *self.destinations]:
            subprocess.run(
                ["docker", "rm", "-f", container], capture_output=True, text=True, check=False
            )
        for volume in self.volumes:
            subprocess.run(
                ["docker", "volume", "rm", "-f", volume],
                capture_output=True,
                text=True,
                check=False,
            )
        subprocess.run(
            ["docker", "network", "rm", self.network],
            capture_output=True,
            text=True,
            check=False,
        )

    def audit_clean(self) -> None:
        containers = _docker("ps", "-a", "-q", "--filter", f"label={DRILL_LABEL}=1")
        volumes = _docker("volume", "ls", "-q", "--filter", f"label={DRILL_LABEL}=1")
        networks = _docker("network", "ls", "-q", "--filter", f"label={DRILL_LABEL}=1")
        assert containers == ""
        assert volumes == ""
        assert networks == ""
        with socket.socket() as probe:
            probe.settimeout(0.2)
            assert probe.connect_ex(self.server.server_address) != 0


def _record_backup(
    db: Session,
    source: Target,
    artifact: Path,
    container_name: str,
) -> TargetRun:
    now = datetime.now(timezone.utc)
    run = Run(
        status="success",
        operation="backup",
        started_at=now,
        finished_at=now,
    )
    db.add(run)
    db.commit()
    target_run = TargetRun(
        run_id=run.id,
        target_id=source.id,
        status="success",
        operation="backup",
        artifact_path=str(artifact),
        artifact_bytes=artifact.stat().st_size,
        sha256=hashlib.sha256(artifact.read_bytes()).hexdigest(),
        source_identity_json=json.dumps({"container_name": container_name}),
        started_at=now,
        finished_at=now,
    )
    db.add(target_run)
    db.commit()
    return target_run


@pytest.mark.skipif(
    os.environ.get("RUN_VAULTWARDEN_DOCKER_DRILL") != "1",
    reason="set RUN_VAULTWARDEN_DOCKER_DRILL=1 for the exact Docker drill",
)
@pytest.mark.parametrize("round_number", [1, 2])
def test_exact_vaultwarden_two_backup_two_restore_round(
    round_number: int,
    db_session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli_value = os.environ.get("VAULTWARDEN_BW_CLI")
    playwright_module = os.environ.get("VAULTWARDEN_PLAYWRIGHT_MODULE")
    if not cli_value or not playwright_module:
        pytest.fail("exact CLI and Playwright module paths are required")
    cli = Path(cli_value)
    if _run([str(cli), "--version"]) != CLI_VERSION:
        pytest.fail("official Bitwarden CLI version is not exact")
    root = tmp_path / f"round-{round_number}"
    root.mkdir(mode=0o700)
    drill = _Drill(root, round_number, cli, playwright_module)
    plugin = VaultWardenPlugin("vaultwarden")
    monkeypatch.setenv("BACKUP_BASE_PATH", str(tmp_path))
    monkeypatch.setattr("app.plugins.vaultwarden.plugin.BACKUP_BASE_PATH", str(tmp_path))
    monkeypatch.setattr("app.services.restores.get_plugin", lambda _: plugin)
    password_file = root / "password"
    password_file.write_text(f"Vw-{secrets.token_urlsafe(30)}!\n", encoding="utf-8")
    os.chmod(password_file, 0o600)
    email = f"vaultwarden-{round_number}-{uuid.uuid4().hex}@example.invalid"

    try:
        drill.create_resources()
        drill.web(
            "signup",
            drill.source,
            {"email": email, "password": password_file.read_text().strip()},
        )
        source_cli, source_session = drill.login(drill.source, email, password_file)
        phase_a = drill.create_phase(
            source_cli,
            source_session,
            f"r{round_number}-a",
            email,
            password_file.read_text().strip(),
        )

        source_target = Target(
            name="Vaultwarden Source",
            slug=f"vaultwarden-source-r{round_number}",
            plugin_name="vaultwarden",
            plugin_config_json=json.dumps(
                {"container_name": drill.source, "allow_service_stop": True}
            ),
        )
        db_session.add(source_target)
        db_session.commit()
        backup_context = BackupContext(
            job_id=f"vaultwarden-r{round_number}",
            target_id=str(source_target.id),
            config={"container_name": drill.source, "allow_service_stop": True},
            metadata={"target_slug": source_target.slug},
        )
        artifact_a = Path(asyncio.run(plugin.backup(backup_context))["artifact_path"])
        phase_b = drill.create_phase(
            source_cli,
            source_session,
            f"r{round_number}-b",
            email,
            password_file.read_text().strip(),
        )
        artifact_b = Path(asyncio.run(plugin.backup(backup_context))["artifact_path"])
        assert artifact_a != artifact_b
        assert (
            hashlib.sha256(artifact_a.read_bytes()).digest()
            != hashlib.sha256(artifact_b.read_bytes()).digest()
        )
        for artifact in (artifact_a, artifact_b):
            assert stat.S_IMODE(artifact.stat().st_mode) == 0o600
            sidecar = read_backup_sidecar(str(artifact))
            assert sidecar is not None
            assert sidecar["application_version"] == VERSION
            assert sidecar["source_container_id"]
            assert sidecar["file_send_count"] == 0

        source_runs = [
            _record_backup(db_session, source_target, artifact_a, drill.source),
            _record_backup(db_session, source_target, artifact_b, drill.source),
        ]
        monkeypatch.setenv("HOMELAB_BACKUP_ALLOW_ISOLATED_RESTORE", "1")
        monkeypatch.setenv(
            "HOMELAB_BACKUP_ISOLATED_RESTORE_ALLOWED_CONTAINERS",
            ",".join(drill.destinations),
        )
        for index, (artifact_run, expected_phases) in enumerate(
            zip(source_runs, [(phase_a,), (phase_a, phase_b)], strict=True)
        ):
            destination_name = drill.create_destination(index)
            destination_target = Target(
                name=f"Vaultwarden Restore {index}",
                slug=f"vaultwarden-restore-r{round_number}-{index}",
                plugin_name="vaultwarden",
                plugin_config_json=json.dumps(
                    {"container_name": destination_name, "allow_service_stop": True}
                ),
            )
            db_session.add(destination_target)
            db_session.commit()
            result = RestoreService(db_session).restore(
                source_target_run_id=artifact_run.id,
                destination_target_id=destination_target.id,
                triggered_by="vaultwarden-exact-local-drill",
            )
            assert result.status == "success"
            for phase in expected_phases:
                drill.verify_phase(
                    destination_name,
                    phase,
                    f"before-restart-{index}",
                    email,
                    password_file.read_text().strip(),
                )
            _docker("restart", destination_name)
            drill._wait_ready(destination_name)
            for phase in expected_phases:
                drill.verify_phase(
                    destination_name,
                    phase,
                    f"after-restart-{index}",
                    email,
                    password_file.read_text().strip(),
                )
    finally:
        drill.cleanup()
        drill.audit_clean()
