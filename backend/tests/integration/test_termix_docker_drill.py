from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any, cast

import pytest

from app.core.plugins.sidecar import read_backup_sidecar
from app.plugins.termix.plugin import (
    RESTORE_SENTINEL_CONTENT,
    RESTORE_SENTINEL_NAME,
)

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_TERMIX_DOCKER_DRILL") != "1",
    reason="set RUN_TERMIX_DOCKER_DRILL=1 for the disposable local drill",
)

_IMAGE = (
    "ghcr.io/lukegus/termix@"
    "sha256:06a27a3dc22ae426cf0681fcdbdb58732f2aab56d8ce9e95f4deea18306e5c2f"
)
_EXPECTED_VERSION = "2.3.2"
_EXPECTED_REVISION = "c3282b5dca081d52513e94329bbc71084338217d"
_BACKEND_ROOT = Path(__file__).resolve().parents[2]
_USERNAME = "drill-admin"
_PASSWORD = "synthetic-local-password"
_COOKIE_PATH = "/tmp/termix-drill-cookies"


def _docker(
    *arguments: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *arguments],
        check=check,
        capture_output=True,
        text=True,
        timeout=300,
    )


def _request_json(
    container: str,
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    authenticated: bool = False,
    save_cookie: bool = False,
) -> Any:
    command = ["exec", container, "wget", "-q", "-O", "-", "--timeout=15"]
    if authenticated:
        command.extend(["--load-cookies", _COOKIE_PATH])
    if save_cookie:
        command.extend(["--save-cookies", _COOKIE_PATH, "--keep-session-cookies"])
    if method == "POST":
        command.extend(
            [
                "--header",
                "Content-Type: application/json",
                "--post-data",
                json.dumps(body or {}, separators=(",", ":")),
            ]
        )
    elif method != "GET":
        raise ValueError(f"Unsupported Termix drill request method: {method}")
    command.append(f"http://127.0.0.1:8080{path}")
    return json.loads(_docker(*command).stdout)


def _wait_for_ready(container: str) -> None:
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        response = _docker(
            "exec",
            container,
            "wget",
            "-q",
            "-O",
            "-",
            "--timeout=5",
            "http://127.0.0.1:8080/health",
            check=False,
        )
        if response.returncode == 0 and response.stdout.strip() == '{"status":"ok"}':
            health = _docker(
                "inspect",
                "--format",
                "{{if .State.Health}}{{.State.Health.Status}}{{end}}",
                container,
            ).stdout.strip()
            if health == "healthy":
                return
        state = _docker(
            "inspect",
            "--format",
            "{{.State.Status}}",
            container,
            check=False,
        ).stdout.strip()
        if state == "exited":
            logs = _docker("logs", container, check=False).stdout[-3000:]
            raise RuntimeError(f"Disposable Termix container exited: {logs}")
        time.sleep(0.5)
    raise RuntimeError(f"Disposable Termix container {container} did not become ready")


def _assert_exact_image(container: str) -> None:
    configured_image = _docker("inspect", "--format", "{{.Config.Image}}", container).stdout.strip()
    assert configured_image == _IMAGE
    revision = _docker(
        "inspect",
        "--format",
        '{{index .Config.Labels "org.opencontainers.image.revision"}}',
        container,
    ).stdout.strip()
    assert revision == _EXPECTED_REVISION
    version = _docker(
        "exec",
        container,
        "node",
        "-p",
        "require('/app/package.json').version",
    ).stdout.strip()
    assert version == _EXPECTED_VERSION
    network_mode = _docker(
        "inspect", "--format", "{{.HostConfig.NetworkMode}}", container
    ).stdout.strip()
    assert network_mode == "none"


def _start_termix(container: str, data_path: Path) -> None:
    data_path.mkdir(mode=0o700, exist_ok=True)
    _docker(
        "run",
        "-d",
        "--name",
        container,
        "--network",
        "none",
        "--user",
        "1000:1000",
        "--memory",
        "256m",
        "--memory-swap",
        "256m",
        "--security-opt",
        "no-new-privileges:true",
        "--cap-drop",
        "ALL",
        "-e",
        "PORT=8080",
        "-e",
        "ENABLE_SSL=false",
        "-v",
        f"{data_path}:/app/data",
        _IMAGE,
    )
    _assert_exact_image(container)
    _wait_for_ready(container)


def _login(container: str) -> None:
    response = _request_json(
        container,
        "POST",
        "/users/login",
        body={"username": _USERNAME, "password": _PASSWORD, "rememberMe": False},
        save_cookie=True,
    )
    assert response == {
        "success": True,
        "is_admin": True,
        "username": _USERNAME,
    }


def _create_user(container: str) -> None:
    response = _request_json(
        container,
        "POST",
        "/users/create",
        body={"username": _USERNAME, "password": _PASSWORD},
    )
    assert response["message"] == "User created"
    assert response["is_admin"] is True
    _login(container)


def _sha256(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _file_signature(path: Path) -> tuple[int, int, str]:
    with path.open("rb") as source:
        before = os.fstat(source.fileno())
        digest = hashlib.file_digest(source, "sha256").hexdigest()
        after = os.fstat(source.fileno())
    assert (before.st_ino, before.st_size, before.st_mtime_ns) == (
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    )
    return after.st_size, after.st_mtime_ns, digest


def _wait_for_persisted_change(database: Path, previous_digest: str) -> str:
    deadline = time.monotonic() + 20
    last_signature: tuple[int, int, str] | None = None
    stable_since: float | None = None
    while time.monotonic() < deadline:
        try:
            current = _file_signature(database)
        except (FileNotFoundError, AssertionError, OSError):
            last_signature = None
            stable_since = None
            time.sleep(0.1)
            continue
        now = time.monotonic()
        if current[2] != previous_digest and current == last_signature:
            if stable_since is not None and now - stable_since >= 3.0:
                return current[2]
        else:
            stable_since = now if current[2] != previous_digest else None
        last_signature = current
        time.sleep(0.1)
    raise RuntimeError("Termix encrypted database did not change and stabilize")


def _seed_phase(
    container: str,
    database: Path,
    phase: str,
    address_suffix: int,
) -> None:
    previous_digest = _sha256(database)

    # In 2.3.2 the snippet route does not mark the in-memory database dirty.
    # Create it before the host, whose two-second save trigger persists both records.
    snippet = _request_json(
        container,
        "POST",
        "/snippets/",
        authenticated=True,
        body={
            "name": f"{phase}-snippet",
            "content": f"printf {phase}-marker",
            "description": "disposable restore proof",
            "folder": "drill",
        },
    )
    assert snippet["name"] == f"{phase}-snippet"
    assert snippet["content"] == f"printf {phase}-marker"

    host = _request_json(
        container,
        "POST",
        "/host/db/host",
        authenticated=True,
        body={
            "connectionType": "ssh",
            "name": f"{phase}-host",
            "ip": f"192.0.2.{address_suffix}",
            "port": 22,
            "username": "drill",
            "authType": "none",
            "notes": f"{phase}-marker",
            "enableTerminal": True,
        },
    )
    assert host["name"] == f"{phase}-host"
    assert host["notes"] == f"{phase}-marker"
    _wait_for_persisted_change(database, previous_digest)


def _expected_names(phases: tuple[str, ...], suffix: str) -> set[str]:
    return {f"{phase}-{suffix}" for phase in phases}


def _assert_application_state(container: str, phases: tuple[str, ...]) -> None:
    _login(container)
    user = _request_json(container, "GET", "/users/me", authenticated=True)
    assert user["username"] == _USERNAME
    assert user["is_admin"] is True
    assert user["data_unlocked"] is True

    hosts = _request_json(container, "GET", "/host/db/host", authenticated=True)
    assert {host["name"] for host in hosts} == _expected_names(phases, "host")
    for host in hosts:
        phase = host["name"].removesuffix("-host")
        assert host["notes"] == f"{phase}-marker"
        assert host["hasPassword"] is False
        assert host["hasKey"] is False

    snippets = _request_json(container, "GET", "/snippets/", authenticated=True)
    assert {snippet["name"] for snippet in snippets} == _expected_names(phases, "snippet")
    for snippet in snippets:
        phase = snippet["name"].removesuffix("-snippet")
        assert snippet["content"] == f"printf {phase}-marker"
        assert snippet["description"] == "disposable restore proof"


def _json_result(completed: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    for line in reversed(completed.stdout.splitlines()):
        if line.startswith("{"):
            result = json.loads(line)
            assert isinstance(result, dict)
            return result
    raise AssertionError(f"Plugin runner returned no JSON result: {completed.stderr}")


def _run_backup_through_read_only_mount(
    runner_image: str,
    source_data: Path,
    artifact_root: Path,
) -> Path:
    script = """
import asyncio
import errno
import json
from pathlib import Path
from app.core.plugins.base import BackupContext
from app.plugins.termix.plugin import TermixPlugin

async def main():
    source = Path('/sources/termix/data')
    mount_line = next(
        line for line in Path('/proc/self/mountinfo').read_text().splitlines()
        if line.split()[4] == str(source)
    )
    if 'ro' not in mount_line.split()[5].split(','):
        raise RuntimeError('Termix source mount is not marked read-only')
    probe = source / '.write-probe'
    try:
        probe.write_text('must fail', encoding='utf-8')
    except OSError as exc:
        if exc.errno not in (errno.EROFS, errno.EACCES):
            raise
    else:
        probe.unlink(missing_ok=True)
        raise RuntimeError('Termix source mount is writable')
    plugin = TermixPlugin(name='termix')
    config = {'data_path': str(source)}
    assert await plugin.test(config) is True
    result = await plugin.backup(BackupContext(
        job_id='termix-drill',
        target_id='termix-source',
        config=config,
        metadata={'target_slug': 'termix-drill'},
    ))
    print(json.dumps(result, sort_keys=True))

asyncio.run(main())
"""
    completed = _docker(
        "run",
        "--rm",
        "--network",
        "none",
        "--user",
        "1000:1000",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=128m",
        "--security-opt",
        "no-new-privileges:true",
        "--cap-drop",
        "ALL",
        "-v",
        f"{source_data}:/sources/termix/data:ro",
        "-v",
        f"{artifact_root}:/backups:rw",
        runner_image,
        "python",
        "-c",
        script,
    )
    assert not (source_data / ".write-probe").exists()
    container_path = Path(_json_result(completed)["artifact_path"])
    return artifact_root / container_path.relative_to("/backups")


def _run_restore_through_isolated_mounts(
    runner_image: str,
    artifact_root: Path,
    artifact: Path,
    restore_parent: Path,
) -> dict[str, Any]:
    relative_artifact = artifact.relative_to(artifact_root)
    script = f"""
import asyncio
import json
from app.core.plugins.base import RestoreContext
from app.plugins.termix.plugin import TermixPlugin

async def main():
    plugin = TermixPlugin(name='termix')
    config = {{'data_path': '/restore/data'}}
    result = await plugin.restore(RestoreContext(
        job_id='termix-restore',
        source_target_id='termix-source',
        destination_target_id='termix-restore',
        config=config,
        artifact_path='/backups/{relative_artifact.as_posix()}',
    ))
    assert await plugin.test(config) is True
    print(json.dumps(result, sort_keys=True))

asyncio.run(main())
"""
    completed = _docker(
        "run",
        "--rm",
        "--network",
        "none",
        "--user",
        "1000:1000",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=128m",
        "--security-opt",
        "no-new-privileges:true",
        "--cap-drop",
        "ALL",
        "-v",
        f"{artifact_root}:/backups:ro",
        "-v",
        f"{restore_parent}:/restore:rw",
        runner_image,
        "python",
        "-c",
        script,
    )
    return _json_result(completed)


def _inspect_artifact(artifact: Path) -> dict[str, Any]:
    assert artifact.is_file()
    assert artifact.stat().st_size > 0
    assert stat.S_IMODE(artifact.stat().st_mode) == 0o600
    with zipfile.ZipFile(artifact) as archive:
        assert archive.testzip() is None
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["plugin"] == "termix"
        assert manifest["termix_version"] == _EXPECTED_VERSION
        assert manifest["termix_commit"] == _EXPECTED_REVISION
        assert set(manifest["files"]) == {".env", "db.sqlite.encrypted"}
        for name, evidence in manifest["files"].items():
            payload = archive.read(name)
            assert len(payload) == evidence["size_bytes"]
            assert hashlib.sha256(payload).hexdigest() == evidence["sha256"]
            assert evidence["mode"] == 0o600
    return cast(dict[str, Any], manifest)


def _assert_restored_payloads(
    destination: Path,
    manifest: dict[str, Any],
) -> None:
    assert destination.is_dir()
    assert stat.S_IMODE(destination.stat().st_mode) == 0o700
    assert not (destination / ".termix-restore-owner").exists()
    for name, evidence in manifest["files"].items():
        restored = destination / name
        assert restored.is_file()
        assert not restored.is_symlink()
        assert stat.S_IMODE(restored.stat().st_mode) == 0o600
        assert restored.stat().st_size == evidence["size_bytes"]
        assert _sha256(restored) == evidence["sha256"]


def test_two_live_backups_restore_to_fresh_exact_termix_images(
    tmp_path: Path,
) -> None:
    if os.getuid() != 1000 or os.getgid() != 1000:
        pytest.skip("the exact Termix bind-mount drill requires host UID:GID 1000:1000")

    suffix = uuid.uuid4().hex[:10]
    source_container = f"codex-termix-source-{suffix}"
    destination_containers = [f"codex-termix-restore-{number}-{suffix}" for number in (1, 2)]
    containers = [source_container, *destination_containers]
    runner_image = f"codex-homelab-backup-termix-runner:{suffix}"
    source_data = tmp_path / "source-data"
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir(mode=0o700)

    try:
        _docker("build", "-t", runner_image, str(_BACKEND_ROOT))
        _start_termix(source_container, source_data)
        _create_user(source_container)
        source_database = source_data / "db.sqlite.encrypted"
        assert source_database.is_file()

        artifacts: list[Path] = []
        manifests: list[dict[str, Any]] = []
        expected_by_run = [("phase-one",), ("phase-one", "phase-two")]

        for run_number, expected_phases in enumerate(expected_by_run, start=1):
            phase = expected_phases[-1]
            _seed_phase(
                source_container,
                source_database,
                phase,
                address_suffix=10 + run_number,
            )
            _assert_application_state(source_container, expected_phases)

            artifact = _run_backup_through_read_only_mount(
                runner_image,
                source_data,
                artifact_root,
            )
            manifest = _inspect_artifact(artifact)
            sidecar = read_backup_sidecar(str(artifact))
            assert sidecar is not None
            assert sidecar["plugin_name"] == "termix"
            assert sidecar["target_slug"] == "termix-drill"
            assert sidecar["artifact_path"] == str(
                Path("/backups") / artifact.relative_to(artifact_root)
            )
            assert sidecar["created_at"]
            artifacts.append(artifact)
            manifests.append(manifest)

            restore_parent = tmp_path / f"restore-{run_number}"
            restore_parent.mkdir(mode=0o700)
            (restore_parent / RESTORE_SENTINEL_NAME).write_text(
                RESTORE_SENTINEL_CONTENT,
                encoding="utf-8",
            )
            restore_result = _run_restore_through_isolated_mounts(
                runner_image,
                artifact_root,
                artifact,
                restore_parent,
            )
            destination_data = restore_parent / "data"
            assert restore_result["status"] == "partial"
            assert restore_result["restored_path"] == "/restore/data"
            _assert_restored_payloads(destination_data, manifest)

            _start_termix(
                destination_containers[run_number - 1],
                destination_data,
            )
            _assert_application_state(destination_containers[run_number - 1], expected_phases)

        assert artifacts[0] != artifacts[1]
        assert _sha256(artifacts[0]) != _sha256(artifacts[1])
        assert (
            manifests[0]["files"]["db.sqlite.encrypted"]["sha256"]
            != manifests[1]["files"]["db.sqlite.encrypted"]["sha256"]
        )
    finally:
        for container in containers:
            _docker("rm", "-f", container, check=False)
        _docker("image", "rm", "-f", runner_image, check=False)
