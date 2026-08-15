from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import sqlite3
import stat
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

from app.core.plugins.sidecar import read_backup_sidecar
from app.plugins.sftpgo.plugin import (
    RESTORE_SENTINEL_CONTENT,
    RESTORE_SENTINEL_NAME,
)

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_SFTPGO_DOCKER_DRILL") != "1",
    reason="set RUN_SFTPGO_DOCKER_DRILL=1 for the disposable local drill",
)

_IMAGE = "drakkan/sftpgo@" "sha256:d1e2877600aba270ac395bf76fc7c8a2a0bb4ac83c3e6c180a0540f5d4c3efb2"
_EXPECTED_VERSION = "2.7.5-9888a3d1-2026-07-17T17:02:02Z"
_BACKEND_ROOT = Path(__file__).resolve().parents[2]
_ADMIN_USERNAME = "drill-admin"
_ADMIN_PASSWORD = "synthetic-admin-password"
_TRANSIENT_TABLES = (
    "active_transfers",
    "shared_sessions",
    "tasks",
    "defender_events",
    "defender_hosts",
)


def _docker(
    *arguments: str,
    check: bool = True,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *arguments],
        check=check,
        capture_output=True,
        text=True,
        input=input_text,
        timeout=180,
    )


def _basic_auth(username: str, password: str) -> str:
    value = base64.b64encode(f"{username}:{password}".encode()).decode()
    return f"Basic {value}"


def _request_json(
    container: str,
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    authorization: str | None = None,
    api_key: str | None = None,
) -> Any:
    command = ["exec", container, "wget", "-q", "-O", "-"]
    if authorization is not None:
        command.extend(["--header", f"Authorization: {authorization}"])
    if api_key is not None:
        command.extend(["--header", f"X-SFTPGO-API-KEY: {api_key}"])
    if method == "POST":
        command.extend(
            [
                "--header",
                "Content-Type: application/json",
                "--post-data",
                json.dumps(body or {}),
            ]
        )
    elif method != "GET":
        raise ValueError(f"Unsupported drill request method: {method}")
    command.append(f"http://127.0.0.1:8080/api/v2{path}")
    response = _docker(*command).stdout
    return json.loads(response)


def _token(container: str, username: str, password: str, *, user: bool = False) -> str:
    path = "/user/token" if user else "/token"
    response = _request_json(
        container,
        "GET",
        path,
        authorization=_basic_auth(username, password),
    )
    token = response.get("access_token")
    assert isinstance(token, str) and token
    return token


def _api(
    container: str,
    token: str,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
) -> Any:
    return _request_json(
        container,
        method,
        path,
        body=body,
        authorization=f"Bearer {token}",
    )


def _wait_for_ready(container: str) -> str:
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        health = _docker(
            "exec",
            container,
            "wget",
            "-q",
            "-O",
            "-",
            "http://127.0.0.1:8080/healthz",
            check=False,
        )
        if health.returncode == 0 and health.stdout.strip() == "ok":
            try:
                token = _token(container, _ADMIN_USERNAME, _ADMIN_PASSWORD)
            except (subprocess.CalledProcessError, AssertionError, json.JSONDecodeError):
                time.sleep(0.5)
                continue
            status_response = _api(container, token, "GET", "/status")
            assert status_response["data_provider"] == {
                "is_active": True,
                "driver": "sqlite",
                "error": "",
            }
            assert status_response["ssh"]["is_active"] is False
            assert status_response["ftp"]["is_active"] is False
            assert status_response["webdav"]["is_active"] is False
            return token
        state = _docker(
            "inspect", "--format", "{{.State.Status}}", container, check=False
        ).stdout.strip()
        if state == "exited":
            logs = _docker("logs", container, check=False).stdout[-2000:]
            raise RuntimeError(f"Disposable SFTPGo container exited: {logs}")
        time.sleep(0.5)
    raise RuntimeError(f"Disposable SFTPGo container {container} did not become ready")


def _start_sftpgo(container: str, network: str, config_dir: Path, data_dir: Path) -> str:
    config_dir.mkdir(mode=0o750, exist_ok=True)
    data_dir.mkdir(mode=0o750, exist_ok=True)
    _docker(
        "run",
        "-d",
        "--name",
        container,
        "--network",
        network,
        "--user",
        "1000:1000",
        "--security-opt",
        "no-new-privileges:true",
        "--cap-drop",
        "ALL",
        "-e",
        "SFTPGO_DATA_PROVIDER__CREATE_DEFAULT_ADMIN=1",
        "-e",
        f"SFTPGO_DEFAULT_ADMIN_USERNAME={_ADMIN_USERNAME}",
        "-e",
        f"SFTPGO_DEFAULT_ADMIN_PASSWORD={_ADMIN_PASSWORD}",
        "-e",
        "SFTPGO_SFTPD__BINDINGS__0__PORT=0",
        "-e",
        "SFTPGO_FTPD__BINDINGS__0__PORT=0",
        "-e",
        "SFTPGO_WEBDAVD__BINDINGS__0__PORT=0",
        "-e",
        "SFTPGO_HTTPD__BINDINGS__0__PORT=8080",
        "-e",
        "SFTPGO_HTTPD__BINDINGS__0__ENABLE_REST_API=true",
        "-v",
        f"{config_dir}:/var/lib/sftpgo",
        "-v",
        f"{data_dir}:/srv/sftpgo",
        _IMAGE,
    )
    configured_image = _docker("inspect", "--format", "{{.Config.Image}}", container).stdout.strip()
    assert configured_image == _IMAGE
    version = _docker("exec", container, "sftpgo", "--version").stdout
    assert _EXPECTED_VERSION in version
    return _wait_for_ready(container)


def _public_key(tmp_path: Path, phase: str) -> str:
    key_path = tmp_path / f"{phase}-key"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key_path)],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return key_path.with_suffix(".pub").read_text(encoding="utf-8").strip()


def _seed_phase(
    container: str,
    admin_token: str,
    tmp_path: Path,
    phase: str,
) -> dict[str, str]:
    password = f"synthetic-{phase}-password"
    admin_password = f"synthetic-{phase}-admin-password"
    public_key = _public_key(tmp_path, phase)

    _api(
        container,
        admin_token,
        "POST",
        "/folders",
        {
            "name": f"{phase}-folder",
            "mapped_path": f"/srv/sftpgo/{phase}-folder",
            "description": f"{phase} virtual folder",
        },
    )
    _api(
        container,
        admin_token,
        "POST",
        "/groups",
        {
            "name": f"{phase}-group",
            "description": f"{phase} group",
            "user_settings": {"permissions": {"/": ["list", "download"]}},
            "virtual_folders": [
                {
                    "name": f"{phase}-folder",
                    "virtual_path": "/shared",
                    "quota_size": 0,
                    "quota_files": 0,
                }
            ],
        },
    )
    _api(
        container,
        admin_token,
        "POST",
        "/roles",
        {"name": f"{phase}-role", "description": f"{phase} role"},
    )
    _api(
        container,
        admin_token,
        "POST",
        "/users",
        {
            "status": 1,
            "username": f"{phase}-user",
            "password": password,
            "public_keys": [public_key],
            "home_dir": f"/srv/sftpgo/{phase}-user",
            "permissions": {"/": ["list", "download"]},
            "groups": [{"name": f"{phase}-group", "type": 1}],
            "role": f"{phase}-role",
            "description": f"{phase} user",
        },
    )
    _api(
        container,
        admin_token,
        "POST",
        "/admins",
        {
            "status": 1,
            "username": f"{phase}-admin",
            "password": admin_password,
            "permissions": ["*"],
            "filters": {"allow_api_key_auth": True},
            "description": f"{phase} administrator",
        },
    )
    api_key_response = _api(
        container,
        admin_token,
        "POST",
        "/apikeys",
        {
            "name": f"{phase}-api-key",
            "scope": 1,
            "admin": f"{phase}-admin",
            "description": f"{phase} API key",
        },
    )
    api_key = api_key_response["key"]
    assert isinstance(api_key, str) and api_key

    user_token = _token(container, f"{phase}-user", password, user=True)
    _api(
        container,
        user_token,
        "POST",
        "/user/shares",
        {
            "name": f"{phase}-share",
            "scope": 1,
            "paths": ["/shared"],
            "password": f"synthetic-{phase}-share-password",
            "description": f"{phase} share",
        },
    )
    _api(
        container,
        admin_token,
        "POST",
        "/eventactions",
        {
            "name": f"{phase}-action",
            "description": f"{phase} disabled backup action",
            "type": 4,
            "options": {},
        },
    )
    _api(
        container,
        admin_token,
        "POST",
        "/eventrules",
        {
            "name": f"{phase}-rule",
            "status": 0,
            "description": f"{phase} disabled provider rule",
            "trigger": 2,
            "conditions": {
                "provider_events": ["add"],
                "options": {"provider_objects": ["user"]},
            },
            "actions": [{"name": f"{phase}-action", "order": 1}],
        },
    )
    return {
        "username": f"{phase}-user",
        "password": password,
        "admin_username": f"{phase}-admin",
        "admin_password": admin_password,
        "api_key": api_key,
        "public_key": public_key,
    }


def _seed_transient_rows(database: Path) -> None:
    future = 4_000_000_000_000
    with sqlite3.connect(database, timeout=30) as connection:
        connection.execute(
            "INSERT INTO active_transfers "
            "(id, connection_id, transfer_id, transfer_type, username, folder_name, "
            "ip, truncated_size, current_ul_size, current_dl_size, created_at, updated_at) "
            "VALUES (900001, 'drill', 1, 0, 'phase-one-user', NULL, "
            "'127.0.0.1', 0, 0, 0, ?, ?)",
            (future, future),
        )
        connection.execute(
            "INSERT INTO shared_sessions (key, type, data, timestamp) "
            "VALUES ('drill-session', 1, '{}', ?)",
            (future,),
        )
        connection.execute(
            "INSERT INTO tasks (id, name, updated_at, version) "
            "VALUES (900001, 'drill-task', ?, 1)",
            (future,),
        )
        connection.execute(
            "INSERT INTO defender_hosts (id, ip, ban_time, updated_at) "
            "VALUES (900001, '192.0.2.1', ?, ?)",
            (future, future),
        )
        connection.execute(
            "INSERT INTO defender_events (id, date_time, score, host_id) "
            "VALUES (900001, ?, 1, 900001)",
            (future,),
        )


def _database_names(database: Path, table: str, column: str = "name") -> set[str]:
    uri = f"file:{database}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        return {row[0] for row in connection.execute(f'SELECT "{column}" FROM "{table}"')}


def _assert_database(
    database: Path,
    *,
    expected_phases: tuple[str, ...],
    transients_empty: bool,
) -> None:
    uri = f"file:{database}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        assert connection.execute("SELECT version FROM schema_version").fetchone() == (33,)
        assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        for table in _TRANSIENT_TABLES:
            count = connection.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
            assert (count == 0) is transients_empty

    for phase in expected_phases:
        assert f"{phase}-user" in _database_names(database, "users", "username")
        assert f"{phase}-admin" in _database_names(database, "admins", "username")
        assert f"{phase}-group" in _database_names(database, "groups")
        assert f"{phase}-folder" in _database_names(database, "folders")
        assert f"{phase}-share" in _database_names(database, "shares")
        assert f"{phase}-api-key" in _database_names(database, "api_keys")
        assert f"{phase}-role" in _database_names(database, "roles")
        assert f"{phase}-action" in _database_names(database, "events_actions")
        assert f"{phase}-rule" in _database_names(database, "events_rules")


def _names(records: list[dict[str, Any]], key: str) -> set[str]:
    return {record[key] for record in records}


def _assert_restored_state(
    container: str,
    database: Path,
    phase_states: dict[str, dict[str, str]],
    expected_phases: tuple[str, ...],
) -> None:
    token = _wait_for_ready(container)
    expected_names = {f"{phase}-user" for phase in expected_phases}
    users = _api(container, token, "GET", "/users")
    assert _names(users, "username") == expected_names
    users_by_name = {user["username"]: user for user in users}
    admins = _api(container, token, "GET", "/admins")
    assert _names(admins, "username") == {
        _ADMIN_USERNAME,
        *(f"{phase}-admin" for phase in expected_phases),
    }
    admins_by_name = {admin["username"]: admin for admin in admins}
    for phase in expected_phases:
        state = phase_states[phase]
        user = users_by_name[f"{phase}-user"]
        assert user["public_keys"] == [state["public_key"]]
        assert user["role"] == f"{phase}-role"
        assert user["groups"] == [{"name": f"{phase}-group", "type": 1}]
        assert admins_by_name[f"{phase}-admin"]["description"] == (f"{phase} administrator")

        restored_admin_token = _token(container, state["admin_username"], state["admin_password"])
        assert _api(container, restored_admin_token, "GET", "/status")["data_provider"]["is_active"]
        api_key_users = _request_json(
            container,
            "GET",
            "/users",
            api_key=state["api_key"],
        )
        assert f"{phase}-user" in _names(api_key_users, "username")
        user_token = _token(container, state["username"], state["password"], user=True)
        shares = _api(container, user_token, "GET", "/user/shares")
        assert _names(shares, "name") == {f"{phase}-share"}
        assert shares[0]["scope"] == 1
        assert shares[0]["paths"] == ["/shared"]

    groups = _api(container, token, "GET", "/groups")
    assert _names(groups, "name") == {f"{phase}-group" for phase in expected_phases}
    for group in groups:
        phase = group["name"].removesuffix("-group")
        assert group["virtual_folders"][0]["name"] == f"{phase}-folder"
        assert group["virtual_folders"][0]["virtual_path"] == "/shared"

    folders = _api(container, token, "GET", "/folders")
    assert _names(folders, "name") == {f"{phase}-folder" for phase in expected_phases}
    for folder in folders:
        phase = folder["name"].removesuffix("-folder")
        assert folder["mapped_path"] == f"/srv/sftpgo/{phase}-folder"

    roles = _api(container, token, "GET", "/roles")
    assert _names(roles, "name") == {f"{phase}-role" for phase in expected_phases}
    assert {role["description"] for role in roles} == {f"{phase} role" for phase in expected_phases}

    api_keys = _api(container, token, "GET", "/apikeys")
    assert _names(api_keys, "name") == {f"{phase}-api-key" for phase in expected_phases}
    assert {api_key["admin"] for api_key in api_keys} == {
        f"{phase}-admin" for phase in expected_phases
    }

    actions = _api(container, token, "GET", "/eventactions")
    assert _names(actions, "name") == {f"{phase}-action" for phase in expected_phases}
    assert {action["type"] for action in actions} == {4}

    rules = _api(container, token, "GET", "/eventrules")
    assert _names(rules, "name") == {f"{phase}-rule" for phase in expected_phases}
    for rule in rules:
        phase = rule["name"].removesuffix("-rule")
        assert rule["status"] == 0
        assert rule["trigger"] == 2
        assert rule["conditions"]["provider_events"] == ["add"]
        assert rule["actions"][0]["name"] == f"{phase}-action"
    _assert_database(database, expected_phases=expected_phases, transients_empty=True)


def _sha256(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _json_result(completed: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    for line in reversed(completed.stdout.splitlines()):
        if line.startswith("{"):
            result = json.loads(line)
            assert isinstance(result, dict)
            return result
    raise AssertionError(f"Plugin runner returned no JSON result: {completed.stderr}")


def _run_backup_through_read_only_mount(
    runner_image: str,
    source_config: Path,
    artifact_root: Path,
) -> Path:
    script = """
import asyncio
import json
from pathlib import Path
from app.core.plugins.base import BackupContext
from app.plugins.sftpgo.plugin import SFTPGoPlugin

async def main():
    source = Path('/sources/sftpgo/config/sftpgo.db')
    probe = source.parent / '.write-probe'
    try:
        probe.write_text('must fail', encoding='utf-8')
    except OSError:
        pass
    else:
        probe.unlink(missing_ok=True)
        raise RuntimeError('SFTPGo source mount is writable')
    plugin = SFTPGoPlugin(name='sftpgo')
    config = {'database_path': str(source)}
    assert await plugin.test(config) is True
    result = await plugin.backup(BackupContext(
        job_id='sftpgo-drill',
        target_id='sftpgo-source',
        config=config,
        metadata={'target_slug': 'sftpgo-drill'},
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
        "/tmp:rw,noexec,nosuid,size=64m",
        "--security-opt",
        "no-new-privileges:true",
        "--cap-drop",
        "ALL",
        "-v",
        f"{source_config}:/sources/sftpgo/config:ro",
        "-v",
        f"{artifact_root}:/backups:rw",
        runner_image,
        "python",
        "-c",
        script,
    )
    container_path = Path(_json_result(completed)["artifact_path"])
    return artifact_root / container_path.relative_to("/backups")


def _run_restore_through_isolated_mounts(
    runner_image: str,
    artifact_root: Path,
    artifact: Path,
    restore_config: Path,
) -> dict[str, Any]:
    relative_artifact = artifact.relative_to(artifact_root)
    script = f"""
import asyncio
import json
from app.core.plugins.base import RestoreContext
from app.plugins.sftpgo.plugin import SFTPGoPlugin

async def main():
    plugin = SFTPGoPlugin(name='sftpgo')
    config = {{'database_path': '/restore/sftpgo.db'}}
    assert await plugin.test(config) is True
    result = await plugin.restore(RestoreContext(
        job_id='sftpgo-restore',
        source_target_id='sftpgo-source',
        destination_target_id='sftpgo-restore',
        config=config,
        artifact_path='/backups/{relative_artifact.as_posix()}',
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
        "/tmp:rw,noexec,nosuid,size=64m",
        "--security-opt",
        "no-new-privileges:true",
        "--cap-drop",
        "ALL",
        "-v",
        f"{artifact_root}:/backups:ro",
        "-v",
        f"{restore_config}:/restore:rw",
        runner_image,
        "python",
        "-c",
        script,
    )
    return _json_result(completed)


@pytest.mark.asyncio
async def test_two_live_backups_restore_to_fresh_exact_sftpgo_images(
    tmp_path: Path,
) -> None:
    if os.getuid() != 1000 or os.getgid() != 1000:
        pytest.skip("the exact SFTPGo bind-mount drill requires host UID:GID 1000:1000")

    suffix = uuid.uuid4().hex[:10]
    network = f"codex-sftpgo-drill-{suffix}"
    source_container = f"codex-sftpgo-source-{suffix}"
    runner_image = f"codex-homelab-backup-sftpgo-runner:{suffix}"
    destination_containers = [f"codex-sftpgo-restore-{number}-{suffix}" for number in (1, 2)]
    containers = [source_container, *destination_containers]
    source_config = tmp_path / "source-config"
    source_data = tmp_path / "source-data"
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir(mode=0o700)
    wal_reader: sqlite3.Connection | None = None

    try:
        _docker("build", "-t", runner_image, str(_BACKEND_ROOT))
        _docker("network", "create", "--internal", network)
        assert (
            _docker("network", "inspect", "--format", "{{.Internal}}", network)
            .stdout.strip()
            .lower()
            == "true"
        )
        admin_token = _start_sftpgo(
            source_container,
            network,
            source_config,
            source_data,
        )
        source_database = source_config / "sftpgo.db"
        wal_reader = sqlite3.connect(source_database, timeout=30)
        assert wal_reader.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        wal_reader.execute("BEGIN")
        wal_reader.execute("SELECT count(*) FROM admins").fetchone()

        phase_states: dict[str, dict[str, str]] = {}
        phase_states["phase-one"] = _seed_phase(
            source_container,
            admin_token,
            tmp_path,
            "phase-one",
        )
        _seed_transient_rows(source_database)
        assert source_database.with_name("sftpgo.db-wal").is_file()

        artifacts: list[Path] = []
        expected_by_run = [("phase-one",), ("phase-one", "phase-two")]

        for run_number, expected_phases in enumerate(expected_by_run, start=1):
            if run_number == 2:
                phase_states["phase-two"] = _seed_phase(
                    source_container,
                    admin_token,
                    tmp_path,
                    "phase-two",
                )

            artifact = await asyncio.to_thread(
                _run_backup_through_read_only_mount,
                runner_image,
                source_config,
                artifact_root,
            )
            assert artifact.stat().st_size > 0
            assert stat.S_IMODE(artifact.stat().st_mode) == 0o600
            assert not artifact.with_name(f"{artifact.name}-wal").exists()
            assert not artifact.with_name(f"{artifact.name}-shm").exists()
            sidecar = read_backup_sidecar(str(artifact))
            assert sidecar is not None
            assert sidecar["plugin_name"] == "sftpgo"
            assert sidecar["target_slug"] == "sftpgo-drill"
            assert sidecar["artifact_path"] == str(
                Path("/backups") / artifact.relative_to(artifact_root)
            )
            _assert_database(
                artifact,
                expected_phases=expected_phases,
                transients_empty=True,
            )
            _assert_database(
                source_database,
                expected_phases=expected_phases,
                transients_empty=False,
            )
            artifacts.append(artifact)

            restore_config = tmp_path / f"restore-{run_number}-config"
            restore_data = tmp_path / f"restore-{run_number}-data"
            restore_config.mkdir(mode=0o750)
            (restore_config / RESTORE_SENTINEL_NAME).write_text(
                RESTORE_SENTINEL_CONTENT,
                encoding="utf-8",
            )
            destination_database = restore_config / "sftpgo.db"
            restore_result = await asyncio.to_thread(
                _run_restore_through_isolated_mounts,
                runner_image,
                artifact_root,
                artifact,
                restore_config,
            )
            assert restore_result["status"] == "partial"
            assert _sha256(destination_database) == _sha256(artifact)
            assert stat.S_IMODE(destination_database.stat().st_mode) == 0o600

            _start_sftpgo(
                destination_containers[run_number - 1],
                network,
                restore_config,
                restore_data,
            )
            _assert_restored_state(
                destination_containers[run_number - 1],
                destination_database,
                phase_states,
                expected_phases,
            )

        assert artifacts[0] != artifacts[1]
        assert _sha256(artifacts[0]) != _sha256(artifacts[1])
        assert "phase-two-user" not in _database_names(artifacts[0], "users", "username")
        assert "phase-two-user" in _database_names(artifacts[1], "users", "username")
    finally:
        if wal_reader is not None:
            wal_reader.close()
        for container in containers:
            _docker("rm", "-f", container, check=False)
        _docker("network", "rm", network, check=False)
        _docker("image", "rm", "-f", runner_image, check=False)
