from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import time
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, cast

import pytest

from app.core.plugins.sidecar import read_backup_sidecar


@dataclass(frozen=True)
class _Service:
    key: str
    app_name: str
    image: str
    digest: str
    version: str
    image_version: str
    port: int
    migration: int
    database: str
    required_tables: frozenset[str]


_SERVICES = (
    _Service(
        key="readarr",
        app_name="Readarr",
        image=(
            "ghcr.io/home-operations/readarr@"
            "sha256:440dc56b904d7363468c1b19e60ccd9dd18b69bdccdb9712d5718779cc48d279"
        ),
        digest="sha256:440dc56b904d7363468c1b19e60ccd9dd18b69bdccdb9712d5718779cc48d279",
        version="0.4.18.2805",
        image_version="0.4.18.2805",
        port=8787,
        migration=158,
        database="readarr.db",
        required_tables=frozenset(
            {
                "Authors",
                "AuthorMetadata",
                "Books",
                "BookFiles",
                "Config",
                "DownloadClients",
                "Editions",
                "History",
                "Indexers",
                "MetadataProfiles",
                "Notifications",
                "QualityProfiles",
                "RootFolders",
                "Tags",
            }
        ),
    ),
    _Service(
        key="prowlarr",
        app_name="Prowlarr",
        image=(
            "ghcr.io/linuxserver/prowlarr@"
            "sha256:a82572d17330327d1efd3d2242eac03b95402607dc96f620447a8426be2f7bd1"
        ),
        digest="sha256:a82572d17330327d1efd3d2242eac03b95402607dc96f620447a8426be2f7bd1",
        version="2.4.0.5397",
        image_version="2.4.0.5397-ls265",
        port=9696,
        migration=44,
        database="prowlarr.db",
        required_tables=frozenset(
            {
                "Applications",
                "ApplicationIndexerMapping",
                "AppSyncProfiles",
                "Config",
                "DownloadClients",
                "History",
                "IndexerProxies",
                "Indexers",
                "Notifications",
                "Tags",
            }
        ),
    ),
)

_BACKEND_ROOT = Path(__file__).resolve().parents[2]
_OPT_IN_ENV = "RUN_READARR_PROWLARR_DOCKER_DRILL"
_RESTORE_ENABLE_ENV = "HOMELAB_BACKUP_ALLOW_ISOLATED_RESTORE"
_RESTORE_ORIGINS_ENV = "HOMELAB_BACKUP_ISOLATED_RESTORE_ALLOWED_ORIGINS"
_SYNTHETIC_KEYS: set[str] = set()


def _redact(text: str) -> str:
    redacted = text
    for secret in _SYNTHETIC_KEYS:
        redacted = redacted.replace(secret, "[REDACTED]")
    return redacted[-6000:]


def _docker(
    *arguments: str,
    check: bool = True,
    input_text: str | None = None,
    timeout: int = 300,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["docker", *arguments],
            check=check,
            capture_output=True,
            text=True,
            input=input_text,
            timeout=timeout,
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            "Disposable Readarr/Prowlarr Docker command failed:\n"
            f"{_redact(exc.stderr or '')}\n{_redact(exc.stdout or '')}"
        ) from exc


def _sha256(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _json_result(completed: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    for line in reversed(completed.stdout.splitlines()):
        if line.startswith("{"):
            parsed = json.loads(line)
            assert isinstance(parsed, dict)
            return parsed
    raise AssertionError(f"Runner returned no JSON result: {_redact(completed.stderr)}")


def _synthetic_key(service: _Service, suffix: str) -> str:
    key = hashlib.sha256(f"synthetic-{service.key}-{suffix}".encode()).hexdigest()
    _SYNTHETIC_KEYS.add(key)
    return key


def _write_config(config_root: Path, service: _Service, api_key: str) -> None:
    config_root.mkdir(mode=0o700, parents=True)
    config = config_root / "config.xml"
    config.write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n'
        "<Config>\n"
        "  <BindAddress>*</BindAddress>\n"
        f"  <Port>{service.port}</Port>\n"
        "  <EnableSsl>False</EnableSsl>\n"
        "  <LaunchBrowser>False</LaunchBrowser>\n"
        f"  <ApiKey>{api_key}</ApiKey>\n"
        "  <AuthenticationMethod>Forms</AuthenticationMethod>\n"
        "  <AuthenticationRequired>Enabled</AuthenticationRequired>\n"
        "  <Branch>develop</Branch>\n"
        "  <LogLevel>info</LogLevel>\n"
        "  <SslCertPath></SslCertPath>\n"
        "  <SslCertPassword></SslCertPassword>\n"
        "  <UrlBase></UrlBase>\n"
        f"  <InstanceName>{service.app_name}</InstanceName>\n"
        "  <UpdateMechanism>Docker</UpdateMechanism>\n"
        "</Config>\n",
        encoding="utf-8",
    )
    config.chmod(0o600)
    (config_root / "Backups" / "manual").mkdir(mode=0o755, parents=True)


def _api_json(
    container: str,
    service: _Service,
    api_key: str,
    method: str,
    path: str,
    payload: dict[str, object] | None = None,
    *,
    check: bool = True,
) -> Any:
    body = json.dumps(payload, separators=(",", ":")) if payload is not None else ""
    # curl does not expand an environment reference in an argv element. Use a
    # minimal shell only inside the synthetic container so the key never enters
    # a URL, response, artifact, or pytest assertion.
    shell = (
        "curl -fsS --max-time 15 "
        f'-X {method} -H "X-Api-Key: $SYNTHETIC_API_KEY" '
        "-H 'Content-Type: application/json' "
    )
    if payload is not None:
        shell += "--data-binary @- "
    shell += f"http://127.0.0.1:{service.port}{path}"
    completed = _docker(
        "exec",
        "-e",
        f"SYNTHETIC_API_KEY={api_key}",
        "-i",
        container,
        "sh",
        "-c",
        shell,
        input_text=body if payload is not None else None,
        check=check,
        timeout=30,
    )
    if completed.returncode != 0:
        return None
    return json.loads(completed.stdout or "null")


def _wait_ready(
    container: str,
    service: _Service,
    api_key: str,
    *,
    previous_start: str | None = None,
) -> dict[str, Any]:
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        state = _docker(
            "inspect",
            "--format",
            "{{.State.Status}}",
            container,
            check=False,
        ).stdout.strip()
        if state == "exited":
            logs = _redact(_docker("logs", container, check=False).stdout)
            raise RuntimeError(f"Disposable {service.app_name} exited before readiness: {logs}")
        try:
            payload = _api_json(
                container,
                service,
                api_key,
                "GET",
                "/api/v1/system/status",
                check=False,
            )
        except (json.JSONDecodeError, RuntimeError):
            payload = None
        if isinstance(payload, dict):
            start_time = payload.get("startTime")
            if previous_start is None or (
                isinstance(start_time, str) and start_time and start_time != previous_start
            ):
                return cast(dict[str, Any], payload)
        time.sleep(0.5)
    raise RuntimeError(f"Disposable {service.app_name} did not become ready")


def _start_app(
    *,
    container: str,
    alias: str,
    network: str,
    config_root: Path,
    service: _Service,
    api_key: str,
) -> dict[str, Any]:
    _write_config(config_root, service, api_key)
    arguments = [
        "run",
        "-d",
        "--pull",
        "never",
        "--name",
        container,
        "--hostname",
        alias,
        "--network",
        network,
        "--network-alias",
        alias,
        "--restart",
        "unless-stopped",
        "--memory",
        "256m",
        "--memory-swap",
        "256m",
        "--pids-limit",
        "256",
        "--security-opt",
        "no-new-privileges:true",
        "-v",
        f"{config_root}:/config",
    ]
    if service.key == "readarr":
        arguments.extend(("--user", "0:0"))
    else:
        arguments.extend(("-e", "PUID=0", "-e", "PGID=0", "-e", "TZ=Etc/UTC"))
    arguments.append(service.image)
    _docker(*arguments)
    status = _wait_ready(container, service, api_key)
    _assert_exact_app(container, network, config_root, service, status)
    return status


def _assert_exact_app(
    container: str,
    network: str,
    config_root: Path,
    service: _Service,
    status: dict[str, Any],
) -> None:
    assert _docker("inspect", "--format", "{{.Config.Image}}", container).stdout.strip() == (
        service.image
    )
    assert (
        _docker("image", "inspect", service.image, "--format", "{{.Architecture}}").stdout.strip()
        == "amd64"
    )
    labels = json.loads(
        _docker("image", "inspect", service.image, "--format", "{{json .Config.Labels}}").stdout
    )
    assert labels["org.opencontainers.image.version"] == service.image_version
    assert status["appName"] == service.app_name
    assert status["version"] == service.version
    assert str(status["databaseType"]).lower() == "sqlite"
    assert status["migrationVersion"] == service.migration
    assert status["urlBase"] == ""
    assert (
        _docker("network", "inspect", "--format", "{{.Internal}}", network).stdout.strip() == "true"
    )
    assert (
        _docker(
            "inspect", "--format", "{{.HostConfig.RestartPolicy.Name}}", container
        ).stdout.strip()
        == "unless-stopped"
    )
    assert _docker(
        "inspect", "--format", "{{json .HostConfig.PortBindings}}", container
    ).stdout.strip() in {"null", "{}"}
    mounts = json.loads(_docker("inspect", "--format", "{{json .Mounts}}", container).stdout)
    assert [(item["Source"], item["Destination"], item["RW"]) for item in mounts] == [
        (str(config_root), "/config", True)
    ]


def _create_runner(
    *,
    name: str,
    image: str,
    network: str,
    script: str,
    mounts: Iterable[tuple[Path, str, bool]],
    environment: Iterable[tuple[str, str]],
) -> subprocess.CompletedProcess[str]:
    arguments = [
        "create",
        "--name",
        name,
        "--network",
        network,
        "--user",
        "0:0",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=256m",
        "--memory",
        "384m",
        "--memory-swap",
        "384m",
        "--pids-limit",
        "128",
        "--security-opt",
        "no-new-privileges:true",
        "--cap-drop",
        "ALL",
        "--cap-add",
        "DAC_OVERRIDE",
        "--cap-add",
        "CHOWN",
    ]
    for key, value in environment:
        arguments.extend(("-e", f"{key}={value}"))
    for source, destination, writable in mounts:
        arguments.extend(("-v", f"{source}:{destination}:{'rw' if writable else 'ro'}"))
    arguments.extend((image, "python", "-c", script))
    _docker(*arguments)

    inspected_mounts = json.loads(_docker("inspect", "--format", "{{json .Mounts}}", name).stdout)
    expected = sorted((destination, writable) for _, destination, writable in mounts)
    observed = sorted((item["Destination"], item["RW"]) for item in inspected_mounts)
    assert observed == expected
    assert all(item["Destination"] != "/config" for item in inspected_mounts)
    assert all(item["Destination"] != "/var/run/docker.sock" for item in inspected_mounts)
    assert _docker(
        "inspect", "--format", "{{json .HostConfig.PortBindings}}", name
    ).stdout.strip() in {"null", "{}"}
    try:
        return _docker("start", "--attach", name, timeout=360)
    finally:
        _docker("rm", "-f", name, check=False)


_RUNNER_GUARDS = r"""
import os
import socket
from pathlib import Path

def assert_runner_guards():
    assert not Path('/config').exists()
    assert not Path('/var/run/docker.sock').exists()
    sock = socket.socket()
    sock.settimeout(2)
    try:
        assert sock.connect_ex(('1.1.1.1', 53)) != 0
    finally:
        sock.close()
"""


def _run_backup(
    *,
    runner_image: str,
    runner_name: str,
    network: str,
    service: _Service,
    source_alias: str,
    api_key: str,
    native_directory: Path,
    artifact_root: Path,
    drill_round: int,
    phase: str,
) -> Path:
    script = f"""
import asyncio
import json
import os
from pathlib import Path
from app.core.plugins.artifacts import validate_backup_artifact
from app.core.plugins.base import BackupContext
from app.core.plugins.loader import get_plugin
{_RUNNER_GUARDS}

async def main():
    assert_runner_guards()
    native = Path('/sources/{service.key}/backups')
    assert os.path.ismount(native)
    assert os.statvfs(native).f_flag & os.ST_RDONLY
    try:
        (native / '.write-probe').write_bytes(b'x')
    except OSError:
        pass
    else:
        raise AssertionError('native backup mount was writable')
    plugin = get_plugin('{service.key}')
    plugin.backup_root = '/backups'
    config = {{
        'base_url': 'http://{source_alias}:{service.port}',
        'api_key': os.environ['SYNTHETIC_API_KEY'],
        'backup_directory': str(native),
    }}
    assert await plugin.test(config) is True
    context = BackupContext(
        job_id='{service.key}-round-{drill_round}-{phase}',
        target_id='{service.key}-source',
        config=config,
        metadata={{'target_slug': '{service.key}-round-{drill_round}'}},
    )
    result = await plugin.backup(context)
    validated = validate_backup_artifact(result['artifact_path'], plugin, context)
    # The exact PUID=0 topology writes private root-owned source archives, so
    # this runner also runs as root. Hand only the already-published synthetic
    # evidence back to the invoking pytest UID while preserving mode 0600.
    os.chown(result['artifact_path'], int(os.environ['DRILL_HOST_UID']),
             int(os.environ['DRILL_HOST_GID']))
    os.chown(result['artifact_path'] + '.meta.json', int(os.environ['DRILL_HOST_UID']),
             int(os.environ['DRILL_HOST_GID']))
    print(json.dumps({{
        'artifact_path': result['artifact_path'],
        'artifact_bytes': validated.size_bytes,
        'sha256': validated.sha256,
    }}, sort_keys=True))

asyncio.run(main())
"""
    completed = _create_runner(
        name=runner_name,
        image=runner_image,
        network=network,
        script=script,
        mounts=(
            (native_directory, f"/sources/{service.key}/backups", False),
            (artifact_root, "/backups", True),
        ),
        environment=(
            ("SYNTHETIC_API_KEY", api_key),
            ("DRILL_HOST_UID", str(os.getuid())),
            ("DRILL_HOST_GID", str(os.getgid())),
        ),
    )
    result = _json_result(completed)
    runner_path = Path(cast(str, result["artifact_path"]))
    artifact = artifact_root / runner_path.relative_to("/backups")
    assert artifact.is_file() and not artifact.is_symlink()
    assert artifact.stat().st_size == result["artifact_bytes"]
    assert _sha256(artifact) == result["sha256"]
    return artifact


def _inspect_artifact(
    artifact: Path,
    service: _Service,
    expected_markers: set[str],
    forbidden_markers: set[str],
) -> dict[str, Any]:
    assert stat.S_IMODE(artifact.stat().st_mode) == 0o600
    sidecar_path = Path(f"{artifact}.meta.json")
    assert sidecar_path.is_file() and not sidecar_path.is_symlink()
    assert stat.S_IMODE(sidecar_path.stat().st_mode) == 0o600
    sidecar = read_backup_sidecar(str(artifact))
    assert sidecar is not None
    assert sidecar["plugin_name"] == service.key
    assert sidecar["artifact_bytes"] == artifact.stat().st_size
    assert sidecar["sha256"] == _sha256(artifact)
    assert sidecar["target_slug"].startswith(f"{service.key}-round-")
    assert isinstance(sidecar["created_at"], str) and sidecar["created_at"]
    # The exact drill must retain enough structural evidence to prove that two
    # native commands and two attributed native identities were distinct.
    for evidence_key in (
        "application",
        "application_version",
        "command_id",
        "database_backend",
        "database_migration",
        "source_backup_id",
        "source_backup_time",
        "source_backup_type",
        "validation",
    ):
        assert evidence_key in sidecar, f"RED: sidecar lacks {evidence_key} evidence"
    assert sidecar["application"] == service.app_name
    assert sidecar["application_version"] == service.version
    assert sidecar["database_backend"] == "sqlite"
    assert sidecar["database_migration"] == service.migration
    assert sidecar["source_backup_type"] == "manual"
    assert sidecar["validation"] == "strict-native-v1"
    serialized_sidecar = json.dumps(sidecar, sort_keys=True)
    assert not any(secret in serialized_sidecar for secret in _SYNTHETIC_KEYS)
    assert f"http://" not in serialized_sidecar and f"https://" not in serialized_sidecar

    with zipfile.ZipFile(artifact) as archive:
        assert archive.testzip() is None
        assert [item.filename for item in archive.infolist()] == [
            "config.xml",
            service.database,
            "INFO",
        ] or {item.filename for item in archive.infolist()} == {
            "config.xml",
            service.database,
            "INFO",
        }
        assert all(not item.is_dir() and not (item.flag_bits & 0x1) for item in archive.infolist())
        info = archive.read("INFO").decode("utf-8").splitlines()
        assert len(info) == 2 and info[0] == f"v{service.version}"
        database_bytes = archive.read(service.database)
    with tempfile.TemporaryDirectory(prefix=f"{service.key}-drill-inspect-") as directory:
        database_path = Path(directory) / service.database
        database_path.write_bytes(database_bytes)
        with sqlite3.connect(f"file:{database_path}?mode=ro&immutable=1", uri=True) as connection:
            assert connection.execute("PRAGMA quick_check").fetchall() == [("ok",)]
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            assert service.required_tables <= tables
            assert connection.execute('SELECT MAX("Version") FROM "VersionInfo"').fetchone() == (
                service.migration,
            )
            markers = {
                row[0] for row in connection.execute('SELECT "Label" FROM "Tags"').fetchall()
            }
    assert expected_markers <= markers
    assert not (forbidden_markers & markers)
    return {
        "sha256": _sha256(artifact),
        "bytes": artifact.stat().st_size,
        "created_at": sidecar["created_at"],
        "command_id": sidecar["command_id"],
        "source_backup_id": sidecar["source_backup_id"],
        "source_backup_time": sidecar["source_backup_time"],
        "source_backup_type": sidecar["source_backup_type"],
        "application": sidecar["application"],
        "application_version": sidecar["application_version"],
        "database_backend": sidecar["database_backend"],
        "database_migration": sidecar["database_migration"],
        "markers": sorted(markers),
    }


def _run_exact_status_negatives(
    *,
    runner_image: str,
    runner_name: str,
    network: str,
    service: _Service,
    source_alias: str,
    api_key: str,
    native_directory: Path,
) -> dict[str, Any]:
    script = f"""
import asyncio
import json
import os
from app.core.plugins.loader import get_plugin
{_RUNNER_GUARDS}

async def main():
    assert_runner_guards()
    config = {{
        'base_url': 'http://{source_alias}:{service.port}',
        'api_key': os.environ['SYNTHETIC_API_KEY'],
        'backup_directory': '/sources/{service.key}/backups',
    }}
    cases = (
        ('wrong_version', 'expected_version', '0.0.0-exact-negative'),
        ('wrong_database', 'expected_database_type', 'postgresql'),
        ('wrong_migration', 'expected_migration', {service.migration + 1}),
    )
    results = {{}}
    for label, attribute, replacement in cases:
        plugin = get_plugin('{service.key}')
        original = getattr(plugin, attribute)
        setattr(plugin, attribute, replacement)
        try:
            await plugin.test(config)
        except Exception as exc:
            results[label] = {{'failed': True, 'type': type(exc).__name__,
                              'message': str(exc)}}
        else:
            results[label] = {{'failed': False}}
        finally:
            setattr(plugin, attribute, original)
    print(json.dumps(results, sort_keys=True))

asyncio.run(main())
"""
    result = _json_result(
        _create_runner(
            name=runner_name,
            image=runner_image,
            network=network,
            script=script,
            mounts=((native_directory, f"/sources/{service.key}/backups", False),),
            environment=(("SYNTHETIC_API_KEY", api_key),),
        )
    )
    for case in ("wrong_version", "wrong_database", "wrong_migration"):
        assert result[case]["failed"] is True, result
        assert _redact(result[case]["message"]) == result[case]["message"]
    return result


def _run_mount_negative(
    *,
    case: str,
    runner_image: str,
    runner_name: str,
    network: str,
    service: _Service,
    source_alias: str,
    api_key: str,
    native_directory: Path,
    artifact_root: Path,
    swapped_directory: Path,
) -> dict[str, Any]:
    assert case in {"missing", "writable", "swapped"}
    operation = "backup" if case == "swapped" else "test"
    script = f"""
import asyncio
import json
import os
from app.core.plugins.base import BackupContext
from app.core.plugins.loader import get_plugin
{_RUNNER_GUARDS}

async def main():
    assert_runner_guards()
    plugin = get_plugin('{service.key}')
    plugin.backup_root = '/backups'
    config = {{
        'base_url': 'http://{source_alias}:{service.port}',
        'api_key': os.environ['SYNTHETIC_API_KEY'],
        'backup_directory': '/sources/{service.key}/backups',
    }}
    try:
        if '{operation}' == 'test':
            await plugin.test(config)
        else:
            await plugin.backup(BackupContext(
                job_id='{service.key}-{case}-mount-negative',
                target_id='{service.key}-negative-source',
                config=config,
                metadata={{'target_slug': '{service.key}-mount-negative'}},
            ))
    except Exception as exc:
        print(json.dumps({{'failed': True, 'type': type(exc).__name__,
                          'message': str(exc)}}, sort_keys=True))
        return
    print(json.dumps({{'failed': False}}))

asyncio.run(main())
"""
    mounts: list[tuple[Path, str, bool]] = [(artifact_root, "/backups", True)]
    if case == "writable":
        mounts.append((native_directory, f"/sources/{service.key}/backups", True))
    elif case == "swapped":
        mounts.append((swapped_directory, f"/sources/{service.key}/backups", False))
    before = {path for path in artifact_root.rglob("*") if path.is_file()}
    result = _json_result(
        _create_runner(
            name=runner_name,
            image=runner_image,
            network=network,
            script=script,
            mounts=mounts,
            environment=(("SYNTHETIC_API_KEY", api_key),),
        )
    )
    assert result["failed"] is True, result
    assert {path for path in artifact_root.rglob("*") if path.is_file()} == before
    return result


def _run_command_negative(
    *,
    case: str,
    runner_image: str,
    runner_name: str,
    network: str,
    service: _Service,
    source_alias: str,
    api_key: str,
    native_directory: Path,
    artifact_root: Path,
) -> dict[str, Any]:
    assert case in {"failed", "ambiguous"}
    script = f"""
import asyncio
import json
import os
import urllib.error
import urllib.request
import httpx
from app.core.plugins.base import BackupContext
from app.core.plugins.loader import get_plugin
{_RUNNER_GUARDS}

upstream = 'http://{source_alias}:{service.port}'
api_key = os.environ['SYNTHETIC_API_KEY']
real_client = httpx.AsyncClient
list_calls = 0

def forward(request):
    headers = {{'X-Api-Key': api_key, 'Content-Type': 'application/json'}}
    body = bytes(request.content) or None
    upstream_request = urllib.request.Request(
        upstream + request.url.raw_path.decode(), data=body,
        headers=headers, method=request.method,
    )
    try:
        with urllib.request.urlopen(upstream_request, timeout=30) as response:
            return httpx.Response(response.status, content=response.read(),
                                  headers={{'Content-Type': response.headers.get_content_type()}})
    except urllib.error.HTTPError as exc:
        return httpx.Response(exc.code, content=exc.read())

def handler(request):
    global list_calls
    path = request.url.path
    if '{case}' == 'failed':
        if request.method == 'POST' and path == '/api/v1/command':
            return httpx.Response(201, json={{'id': 9001}})
        if request.method == 'GET' and path == '/api/v1/command/9001':
            return httpx.Response(200, json={{'id': 9001, 'status': 'failed'}})
    response = forward(request)
    if '{case}' == 'ambiguous' and request.method == 'GET' and path == '/api/v1/system/backup':
        list_calls += 1
        payload = response.json()
        if list_calls > 1 and payload:
            duplicate = dict(payload[0])
            duplicate['id'] = int(duplicate['id']) + 1
            return httpx.Response(200, json=[payload[0], duplicate])
    return response

transport = httpx.MockTransport(handler)
def client(*args, **kwargs):
    kwargs['transport'] = transport
    return real_client(*args, **kwargs)
httpx.AsyncClient = client

async def main():
    assert_runner_guards()
    plugin = get_plugin('{service.key}')
    plugin.backup_root = '/backups'
    plugin.backup_deadline_seconds = 30.0
    plugin.poll_interval_seconds = 0.2
    config = {{
        'base_url': upstream,
        'api_key': api_key,
        'backup_directory': '/sources/{service.key}/backups',
    }}
    try:
        await plugin.backup(BackupContext(
            job_id='{service.key}-{case}-command-negative',
            target_id='{service.key}-negative-source',
            config=config,
            metadata={{'target_slug': '{service.key}-command-negative'}},
        ))
    except Exception as exc:
        print(json.dumps({{'failed': True, 'type': type(exc).__name__,
                          'message': str(exc)}}, sort_keys=True))
        return
    print(json.dumps({{'failed': False}}))

asyncio.run(main())
"""
    before = {path for path in artifact_root.rglob("*") if path.is_file()}
    result = _json_result(
        _create_runner(
            name=runner_name,
            image=runner_image,
            network=network,
            script=script,
            mounts=(
                (native_directory, f"/sources/{service.key}/backups", False),
                (artifact_root, "/backups", True),
            ),
            environment=(("SYNTHETIC_API_KEY", api_key),),
        )
    )
    assert result["failed"] is True, result
    assert {path for path in artifact_root.rglob("*") if path.is_file()} == before
    return result


def _run_restore(
    *,
    runner_image: str,
    runner_name: str,
    network: str,
    service: _Service,
    destination_alias: str,
    destination_key: str,
    artifact_root: Path,
    artifact: Path,
    target_slug: str,
    authorize: bool,
    expect_success: bool,
    corrupt: bool = False,
) -> dict[str, Any]:
    relative_artifact = artifact.relative_to(artifact_root)
    script = f"""
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
import app.models
from app.core.db import Base
from app.core.plugins.base import BackupContext
from app.core.plugins.loader import get_plugin
from app.core.plugins.sidecar import write_backup_sidecar
from app.models import Run, Target, TargetRun
from app.services.restores import RestoreService
{_RUNNER_GUARDS}

def digest(path):
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()

def main():
    assert_runner_guards()
    artifact = Path('/backups/{relative_artifact.as_posix()}')
    corrupt_artifact = None
    if {corrupt!r}:
        original = artifact
        corrupt_artifact = original.with_name('corrupt-' + original.name)
        payload = original.read_bytes()
        corrupt_artifact.write_bytes(payload[:-17])
        corrupt_artifact.chmod(0o600)
        plugin = get_plugin('{service.key}')
        write_backup_sidecar(
            str(corrupt_artifact), plugin,
            BackupContext(
                job_id='{service.key}-corrupt-negative',
                target_id='{service.key}-source', config={{}},
                metadata={{'target_slug': '{target_slug}'}},
            ),
        )
        artifact = corrupt_artifact
    engine = create_engine('sqlite://', connect_args={{'check_same_thread': False}},
                           poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
    try:
        source = Target(name='Synthetic {service.app_name} Source', slug='{target_slug}',
                        plugin_name='{service.key}', plugin_config_json='{{}}')
        destination = Target(
            name='Synthetic {service.app_name} Destination',
            slug='{service.key}-restore',
            plugin_name='{service.key}',
            plugin_config_json=json.dumps({{
                'base_url': 'http://{destination_alias}:{service.port}',
                'api_key': os.environ['SYNTHETIC_DESTINATION_KEY'],
                'backup_directory': '/sources/{service.key}/backups',
            }}),
        )
        session.add_all([source, destination])
        session.commit()
        backup_run = Run(status='success', operation='backup',
                         started_at=datetime.now(timezone.utc),
                         finished_at=datetime.now(timezone.utc))
        session.add(backup_run)
        session.commit()
        source_run = TargetRun(
            run_id=backup_run.id, target_id=source.id, status='success',
            operation='backup', artifact_path=str(artifact),
            artifact_bytes=artifact.stat().st_size, sha256=digest(artifact),
            started_at=backup_run.started_at, finished_at=backup_run.finished_at,
        )
        session.add(source_run)
        session.commit()
        try:
            restored = RestoreService(session).restore(
                source_target_run_id=source_run.id,
                destination_target_id=destination.id,
                triggered_by='isolated_readarr_prowlarr_exact_drill',
            )
        except Exception as exc:
            print(json.dumps({{'failed': True, 'type': type(exc).__name__,
                              'message': str(exc)}}))
            return
        target_run = restored.target_runs[0]
        public = json.dumps({{
            'failed': False,
            'status': restored.status,
            'target_status': target_run.status,
            'message': target_run.message,
            'logs': target_run.logs_text,
        }}, sort_keys=True)
        for secret in (os.environ['SYNTHETIC_DESTINATION_KEY'],):
            assert secret not in public
        print(public)
    finally:
        session.close()
        if corrupt_artifact is not None:
            Path(str(corrupt_artifact) + '.meta.json').unlink(missing_ok=True)
            corrupt_artifact.unlink(missing_ok=True)

main()
"""
    environment = [
        ("BACKUP_BASE_PATH", "/backups"),
        ("SYNTHETIC_DESTINATION_KEY", destination_key),
    ]
    if authorize:
        environment.extend(
            (
                (_RESTORE_ENABLE_ENV, "1"),
                (
                    _RESTORE_ORIGINS_ENV,
                    f"http://{destination_alias}:{service.port}",
                ),
            )
        )
    before_identity = (artifact.stat().st_dev, artifact.stat().st_ino, _sha256(artifact))
    result = _json_result(
        _create_runner(
            name=runner_name,
            image=runner_image,
            network=network,
            script=script,
            mounts=((artifact_root, "/backups", True),),
            environment=environment,
        )
    )
    assert (artifact.stat().st_dev, artifact.stat().st_ino, _sha256(artifact)) == before_identity
    assert not list(artifact.parent.glob(".homelab-backup-restore-*"))
    assert result["failed"] is (not expect_success), result
    return result


def _assert_no_manual_backups(container: str, service: _Service, api_key: str) -> None:
    backups = _api_json(
        container,
        service,
        api_key,
        "GET",
        "/api/v1/system/backup",
    )
    assert backups == []


def _cleanup_manual_backups(container: str, service: _Service, api_key: str) -> None:
    backups = _api_json(
        container,
        service,
        api_key,
        "GET",
        "/api/v1/system/backup",
    )
    assert isinstance(backups, list)
    for backup in backups:
        assert backup["type"] == "manual"
        _api_json(
            container,
            service,
            api_key,
            "DELETE",
            f"/api/v1/system/backup/{backup['id']}",
        )
    _assert_no_manual_backups(container, service, api_key)


def _fresh_restore_paths(service: _Service) -> tuple[str, ...]:
    if service.key == "readarr":
        return ("tag", "rootfolder", "indexer", "downloadclient", "notification")
    return ("tag", "indexer", "downloadclient", "applications", "notification")


def _assert_fresh_restore_resources(
    container: str,
    service: _Service,
    api_key: str,
) -> None:
    for path in _fresh_restore_paths(service):
        payload = _api_json(
            container,
            service,
            api_key,
            "GET",
            f"/api/v1/{path}",
        )
        assert payload == [], f"fresh exact {service.key} destination has {path} state"


def _remove_path_with_runner(runner_image: str, path: Path) -> None:
    if not path.exists():
        return
    try:
        shutil.rmtree(path)
        return
    except PermissionError:
        pass
    parent = path.parent
    _docker(
        "run",
        "--rm",
        "--network",
        "none",
        "--user",
        "0:0",
        "--read-only",
        "-v",
        f"{parent}:/cleanup:rw",
        runner_image,
        "python",
        "-c",
        f"import shutil; shutil.rmtree('/cleanup/{path.name}')",
    )
    assert not path.exists()


def test_exact_drill_contract_is_immutable_and_has_no_production_endpoint() -> None:
    assert all("@sha256:" in service.image for service in _SERVICES)
    assert all(service.image.endswith(service.digest) for service in _SERVICES)
    assert {service.migration for service in _SERVICES} == {44, 158}
    assert {service.database for service in _SERVICES} == {"prowlarr.db", "readarr.db"}
    assert {service.port for service in _SERVICES} == {8787, 9696}


@pytest.mark.skipif(
    os.getenv(_OPT_IN_ENV) != "1",
    reason=f"set {_OPT_IN_ENV}=1 for the disposable exact-image drill",
)
def test_two_online_backups_restore_to_fresh_exact_images_twice(tmp_path: Path) -> None:
    assert shutil.which("docker") is not None
    suffix = uuid.uuid4().hex[:10]
    runner_image = f"codex-homelab-backup-readarr-prowlarr-runner:{suffix}"
    resource_containers: set[str] = set()
    resource_networks: set[str] = set()
    all_containers: set[str] = set()
    all_networks: set[str] = set()
    pulled_service_images: set[str] = set()
    completed_evidence: list[dict[str, Any]] = []

    try:
        for service in _SERVICES:
            if _docker("image", "inspect", service.image, check=False).returncode != 0:
                _docker("pull", service.image, timeout=900)
                pulled_service_images.add(service.image)
        _docker("build", "-t", runner_image, str(_BACKEND_ROOT), timeout=900)
        for service in _SERVICES:
            for drill_round in (1, 2):
                round_root = tmp_path / f"{service.key}-round-{drill_round}"
                source_config = round_root / "source-config"
                artifact_root = round_root / "artifacts"
                artifact_root.mkdir(mode=0o700, parents=True)
                source_network = f"codex-{service.key}-source-{drill_round}-{suffix}"
                source_container = f"codex-{service.key}-source-{drill_round}-{suffix}"
                source_alias = f"{service.key}-source-{drill_round}"
                source_key = _synthetic_key(service, f"source-{drill_round}-{suffix}")
                resource_networks.add(source_network)
                resource_containers.add(source_container)
                all_networks.add(source_network)
                all_containers.add(source_container)
                _docker("network", "create", "--internal", source_network)
                _start_app(
                    container=source_container,
                    alias=source_alias,
                    network=source_network,
                    config_root=source_config,
                    service=service,
                    api_key=source_key,
                )
                native_directory = source_config / "Backups" / "manual"
                _assert_no_manual_backups(source_container, service, source_key)

                if drill_round == 1:
                    status_runner = f"codex-{service.key}-status-negatives-{suffix}"
                    resource_containers.add(status_runner)
                    all_containers.add(status_runner)
                    status_negatives = _run_exact_status_negatives(
                        runner_image=runner_image,
                        runner_name=status_runner,
                        network=source_network,
                        service=service,
                        source_alias=source_alias,
                        api_key=source_key,
                        native_directory=native_directory,
                    )
                    resource_containers.discard(status_runner)
                    assert "version" in status_negatives["wrong_version"]["message"].lower()
                    assert "database" in status_negatives["wrong_database"]["message"].lower()
                    assert "migration" in status_negatives["wrong_migration"]["message"].lower()

                    swapped_directory = round_root / "swapped-native-backups"
                    swapped_directory.mkdir(mode=0o700)
                    for mount_case in ("missing", "writable"):
                        mount_runner = f"codex-{service.key}-{mount_case}-mount-negative-{suffix}"
                        resource_containers.add(mount_runner)
                        all_containers.add(mount_runner)
                        mount_result = _run_mount_negative(
                            case=mount_case,
                            runner_image=runner_image,
                            runner_name=mount_runner,
                            network=source_network,
                            service=service,
                            source_alias=source_alias,
                            api_key=source_key,
                            native_directory=native_directory,
                            artifact_root=artifact_root,
                            swapped_directory=swapped_directory,
                        )
                        resource_containers.discard(mount_runner)
                        assert "mount" in mount_result["message"].lower() or (
                            "directory" in mount_result["message"].lower()
                        )

                    for command_case in ("failed", "ambiguous"):
                        command_runner = (
                            f"codex-{service.key}-{command_case}-command-negative-{suffix}"
                        )
                        resource_containers.add(command_runner)
                        all_containers.add(command_runner)
                        command_result = _run_command_negative(
                            case=command_case,
                            runner_image=runner_image,
                            runner_name=command_runner,
                            network=source_network,
                            service=service,
                            source_alias=source_alias,
                            api_key=source_key,
                            native_directory=native_directory,
                            artifact_root=artifact_root,
                        )
                        resource_containers.discard(command_runner)
                        assert command_case in command_result["message"].lower()
                        if command_case == "ambiguous":
                            _cleanup_manual_backups(source_container, service, source_key)

                    swapped_runner = f"codex-{service.key}-swapped-mount-negative-{suffix}"
                    resource_containers.add(swapped_runner)
                    all_containers.add(swapped_runner)
                    swapped_result = _run_mount_negative(
                        case="swapped",
                        runner_image=runner_image,
                        runner_name=swapped_runner,
                        network=source_network,
                        service=service,
                        source_alias=source_alias,
                        api_key=source_key,
                        native_directory=native_directory,
                        artifact_root=artifact_root,
                        swapped_directory=swapped_directory,
                    )
                    resource_containers.discard(swapped_runner)
                    assert "missing" in swapped_result["message"].lower()
                    _cleanup_manual_backups(source_container, service, source_key)

                markers = {
                    "a": f"codex-{service.key}-round-{drill_round}-marker-a",
                    "b": f"codex-{service.key}-round-{drill_round}-marker-b",
                }
                artifacts: list[Path] = []
                evidence: list[dict[str, Any]] = []
                for phase in ("a", "b"):
                    marker = markers[phase]
                    created = _api_json(
                        source_container,
                        service,
                        source_key,
                        "POST",
                        "/api/v1/tag",
                        {"label": marker},
                    )
                    assert created["label"] == marker
                    runner_name = f"codex-{service.key}-backup-{drill_round}-{phase}-{suffix}"
                    resource_containers.add(runner_name)
                    all_containers.add(runner_name)
                    artifact = _run_backup(
                        runner_image=runner_image,
                        runner_name=runner_name,
                        network=source_network,
                        service=service,
                        source_alias=source_alias,
                        api_key=source_key,
                        native_directory=native_directory,
                        artifact_root=artifact_root,
                        drill_round=drill_round,
                        phase=phase,
                    )
                    resource_containers.discard(runner_name)
                    artifacts.append(artifact)
                    expected = {markers["a"]}
                    forbidden = {markers["b"]}
                    if phase == "b":
                        expected.add(markers["b"])
                        forbidden.clear()
                    evidence.append(_inspect_artifact(artifact, service, expected, forbidden))
                    _assert_no_manual_backups(source_container, service, source_key)
                    if phase == "a":
                        # The exact APIs serialize native backup time to whole
                        # seconds. Cross the next vendor clock tick so A/B time
                        # provenance is deterministically distinct.
                        time.sleep(1.1)

                assert artifacts[0] != artifacts[1]
                assert evidence[0]["sha256"] != evidence[1]["sha256"]
                assert evidence[0]["command_id"] != evidence[1]["command_id"]
                assert evidence[0]["source_backup_id"] != evidence[1]["source_backup_id"]
                assert evidence[0]["source_backup_time"] != evidence[1]["source_backup_time"]
                assert evidence[0]["created_at"] != evidence[1]["created_at"]
                provenance_identities = {
                    (
                        item["command_id"],
                        item["source_backup_id"],
                        item["source_backup_time"],
                        item["sha256"],
                        item["bytes"],
                        item["created_at"],
                    )
                    for item in evidence
                }
                assert len(provenance_identities) == 2
                for item in evidence:
                    assert item["source_backup_type"] == "manual"
                    assert item["application"] == service.app_name
                    assert item["application_version"] == service.version
                    assert item["database_backend"] == "sqlite"
                    assert item["database_migration"] == service.migration

                for index, artifact in enumerate(artifacts, start=1):
                    destination_network = (
                        f"codex-{service.key}-restore-{drill_round}-{index}-{suffix}"
                    )
                    destination_container = destination_network
                    destination_alias = f"{service.key}-restore-{drill_round}-{index}"
                    destination_config = round_root / f"destination-{index}-config"
                    destination_key = _synthetic_key(
                        service,
                        f"destination-{drill_round}-{index}-{suffix}",
                    )
                    resource_networks.add(destination_network)
                    resource_containers.add(destination_container)
                    all_networks.add(destination_network)
                    all_containers.add(destination_container)
                    _docker("network", "create", "--internal", destination_network)
                    before = _start_app(
                        container=destination_container,
                        alias=destination_alias,
                        network=destination_network,
                        config_root=destination_config,
                        service=service,
                        api_key=destination_key,
                    )
                    assert (
                        _api_json(
                            destination_container,
                            service,
                            destination_key,
                            "GET",
                            "/api/v1/tag",
                        )
                        == []
                    )

                    unauthorized_runner = (
                        f"codex-{service.key}-unauthorized-{drill_round}-{index}-{suffix}"
                    )
                    resource_containers.add(unauthorized_runner)
                    all_containers.add(unauthorized_runner)
                    unauthorized = _run_restore(
                        runner_image=runner_image,
                        runner_name=unauthorized_runner,
                        network=destination_network,
                        service=service,
                        destination_alias=destination_alias,
                        destination_key=destination_key,
                        artifact_root=artifact_root,
                        artifact=artifact,
                        target_slug=f"{service.key}-round-{drill_round}",
                        authorize=False,
                        expect_success=False,
                    )
                    resource_containers.discard(unauthorized_runner)
                    assert "isolated" in unauthorized["message"].lower()
                    unchanged = _wait_ready(destination_container, service, destination_key)
                    assert unchanged["startTime"] == before["startTime"]
                    assert (
                        _api_json(
                            destination_container,
                            service,
                            destination_key,
                            "GET",
                            "/api/v1/tag",
                        )
                        == []
                    )

                    if drill_round == 1 and index == 1:
                        corrupt_runner = f"codex-{service.key}-corrupt-restore-negative-{suffix}"
                        resource_containers.add(corrupt_runner)
                        all_containers.add(corrupt_runner)
                        corrupt_result = _run_restore(
                            runner_image=runner_image,
                            runner_name=corrupt_runner,
                            network=destination_network,
                            service=service,
                            destination_alias=destination_alias,
                            destination_key=destination_key,
                            artifact_root=artifact_root,
                            artifact=artifact,
                            target_slug=f"{service.key}-round-{drill_round}",
                            authorize=True,
                            expect_success=False,
                            corrupt=True,
                        )
                        resource_containers.discard(corrupt_runner)
                        assert any(
                            word in corrupt_result["message"].lower()
                            for word in ("archive", "zip", "artifact")
                        )
                        corrupt_unchanged = _wait_ready(
                            destination_container,
                            service,
                            destination_key,
                        )
                        assert corrupt_unchanged["startTime"] == before["startTime"]
                        _assert_fresh_restore_resources(
                            destination_container,
                            service,
                            destination_key,
                        )

                        stale_label = f"codex-{service.key}-stale-destination"
                        stale_tag = _api_json(
                            destination_container,
                            service,
                            destination_key,
                            "POST",
                            "/api/v1/tag",
                            {"label": stale_label},
                        )
                        stale_runner = f"codex-{service.key}-stale-restore-negative-{suffix}"
                        resource_containers.add(stale_runner)
                        all_containers.add(stale_runner)
                        stale_result = _run_restore(
                            runner_image=runner_image,
                            runner_name=stale_runner,
                            network=destination_network,
                            service=service,
                            destination_alias=destination_alias,
                            destination_key=destination_key,
                            artifact_root=artifact_root,
                            artifact=artifact,
                            target_slug=f"{service.key}-round-{drill_round}",
                            authorize=True,
                            expect_success=False,
                        )
                        resource_containers.discard(stale_runner)
                        assert "not fresh" in stale_result["message"].lower()
                        stale_unchanged = _wait_ready(
                            destination_container,
                            service,
                            destination_key,
                        )
                        assert stale_unchanged["startTime"] == before["startTime"]
                        assert [
                            item["label"]
                            for item in _api_json(
                                destination_container,
                                service,
                                destination_key,
                                "GET",
                                "/api/v1/tag",
                            )
                        ] == [stale_label]
                        _api_json(
                            destination_container,
                            service,
                            destination_key,
                            "DELETE",
                            f"/api/v1/tag/{stale_tag['id']}",
                        )

                    _assert_fresh_restore_resources(
                        destination_container,
                        service,
                        destination_key,
                    )

                    restore_runner = (
                        f"codex-{service.key}-restore-runner-{drill_round}-{index}-{suffix}"
                    )
                    resource_containers.add(restore_runner)
                    all_containers.add(restore_runner)
                    restored = _run_restore(
                        runner_image=runner_image,
                        runner_name=restore_runner,
                        network=destination_network,
                        service=service,
                        destination_alias=destination_alias,
                        destination_key=destination_key,
                        artifact_root=artifact_root,
                        artifact=artifact,
                        target_slug=f"{service.key}-round-{drill_round}",
                        authorize=True,
                        expect_success=True,
                    )
                    resource_containers.discard(restore_runner)
                    assert restored["status"] == "success"
                    assert restored["target_status"] == "success"
                    after = _wait_ready(
                        destination_container,
                        service,
                        source_key,
                        previous_start=cast(str, before["startTime"]),
                    )
                    assert after["version"] == service.version
                    restored_tags = _api_json(
                        destination_container,
                        service,
                        source_key,
                        "GET",
                        "/api/v1/tag",
                    )
                    restored_labels = {item["label"] for item in restored_tags}
                    assert markers["a"] in restored_labels
                    assert (markers["b"] in restored_labels) is (index == 2)

                    _docker("restart", destination_container)
                    restarted = _wait_ready(
                        destination_container,
                        service,
                        source_key,
                        previous_start=cast(str, after["startTime"]),
                    )
                    assert restarted["version"] == service.version
                    assert {
                        item["label"]
                        for item in _api_json(
                            destination_container,
                            service,
                            source_key,
                            "GET",
                            "/api/v1/tag",
                        )
                    } == restored_labels
                    _docker("rm", "-f", destination_container)
                    resource_containers.discard(destination_container)
                    _docker("network", "rm", destination_network)
                    resource_networks.discard(destination_network)

                completed_evidence.append(
                    {
                        "service": service.key,
                        "round": drill_round,
                        "artifact_hashes": [item["sha256"] for item in evidence],
                        "artifact_sizes": [item["bytes"] for item in evidence],
                        "command_ids": [item["command_id"] for item in evidence],
                        "source_backup_ids": [item["source_backup_id"] for item in evidence],
                    }
                )
                _docker("rm", "-f", source_container)
                resource_containers.discard(source_container)
                _docker("network", "rm", source_network)
                resource_networks.discard(source_network)
                _remove_path_with_runner(runner_image, round_root)

        assert len(completed_evidence) == 4
        assert {(item["service"], item["round"]) for item in completed_evidence} == {
            ("readarr", 1),
            ("readarr", 2),
            ("prowlarr", 1),
            ("prowlarr", 2),
        }
    finally:
        for container in sorted(resource_containers):
            _docker("rm", "-f", container, check=False)
        for network in sorted(resource_networks):
            _docker("network", "rm", network, check=False)
        for path in sorted(tmp_path.glob("*-round-*")):
            if runner_image:
                _remove_path_with_runner(runner_image, path)
        _docker("image", "rm", runner_image, check=False)
        for image in sorted(pulled_service_images):
            _docker("image", "rm", image, check=False)

    for container in all_containers:
        assert _docker("inspect", container, check=False).returncode != 0
    for network in all_networks:
        assert _docker("network", "inspect", network, check=False).returncode != 0
    assert _docker("image", "inspect", runner_image, check=False).returncode != 0
    for image in pulled_service_images:
        assert _docker("image", "inspect", image, check=False).returncode != 0
    assert not list(tmp_path.iterdir())
