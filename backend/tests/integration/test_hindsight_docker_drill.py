from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

from app.core.plugins.artifacts import validate_backup_artifact
from app.core.plugins.base import BackupContext, RestoreContext
from app.core.plugins.sidecar import read_backup_sidecar
from app.plugins.hindsight import plugin as hindsight_module
from app.plugins.hindsight.plugin import HindsightPlugin

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_HINDSIGHT_DOCKER_DRILL") != "1",
    reason="set RUN_HINDSIGHT_DOCKER_DRILL=1 for the isolated Hindsight 0.8.6 drill",
)

_HINDSIGHT_IMAGE = (
    "ghcr.io/vectorize-io/hindsight@"
    "sha256:47eba343fe1cc0feb30839fa9bae4d1bb592676a2e7a7c3b8c80689ac93fbf8c"
)
_POSTGRES_IMAGE = (
    "pgvector/pgvector@" "sha256:ff8da7b0714e5efa413d77f43e24d93064dd66469d418d12608c1bbc91fcf045"
)
_EXPECTED_API_VERSION = "0.8.6"
_SOURCE_DATABASE = "hindsight_source"
_UNRELATED_DATABASE = "hindsight_unrelated"
_SOURCE_OWNER = "hindsight_owner"
_SOURCE_OWNER_PASSWORD = "synthetic-local-source-owner-password"
_BACKUP_USER = "hindsight_backup"
_BACKUP_PASSWORD = "synthetic-local-read-only-password"
_RESTORE_PASSWORD = "synthetic-local-restore-owner-password"
_WEBHOOK_SECRET_A = "synthetic-local-webhook-secret-a"
_WEBHOOK_SECRET_B = "synthetic-local-webhook-secret-b"
_OAUTH_EXCLUSION_MARKER = "synthetic-oauth-file-must-not-be-mounted-or-sidecarred"


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
        # Docker resources and all credentials are synthetic, but do not echo
        # database command input or complete container configuration on failure.
        stderr = (exc.stderr or "")[-3000:]
        raise RuntimeError(f"Disposable Hindsight Docker command failed: {stderr}") from exc


def _psql(container: str, database: str, sql: str, *, check: bool = True) -> str:
    completed = _docker(
        "exec",
        "-i",
        container,
        "psql",
        "-X",
        "-U",
        "postgres",
        "--dbname",
        database,
        "--set",
        "ON_ERROR_STOP=on",
        "-tA",
        "-f",
        "-",
        check=check,
        input_text=sql,
    )
    return completed.stdout.strip()


def _request_json(
    container: str,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
) -> Any:
    # Request bodies travel over stdin, keeping webhook secrets out of argv and
    # Docker error messages.
    script = r"""
import base64, json, sys, urllib.error, urllib.request
request = json.load(sys.stdin)
data = None if request["body"] is None else json.dumps(request["body"]).encode()
headers = {} if data is None else {"Content-Type": "application/json"}
try:
    response = urllib.request.urlopen(
        urllib.request.Request(
            "http://127.0.0.1:8888" + request["path"],
            data=data,
            headers=headers,
            method=request["method"],
        ),
        timeout=15,
    )
    payload = response.read()
    status = response.status
except urllib.error.HTTPError as error:
    payload = error.read()
    status = error.code
print(json.dumps({"status": status, "body": base64.b64encode(payload).decode()}))
"""
    request = json.dumps({"method": method, "path": path, "body": body}, separators=(",", ":"))
    completed = _docker("exec", "-i", container, "python", "-c", script, input_text=request)
    response = json.loads(completed.stdout)
    payload = base64.b64decode(response["body"])
    if not 200 <= int(response["status"]) < 300:
        raise AssertionError(
            f"Hindsight {method} {path} returned {response['status']}: "
            f"{payload.decode(errors='replace')[:500]}"
        )
    return json.loads(payload)


def _wait_for_postgres(container: str) -> None:
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if _docker("exec", container, "pg_isready", "-U", "postgres", check=False).returncode == 0:
            return
        time.sleep(0.5)
    raise RuntimeError("Disposable PostgreSQL 18 did not become ready")


def _wait_for_hindsight(container: str) -> None:
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        state = _docker(
            "inspect", "--format", "{{.State.Status}}", container, check=False
        ).stdout.strip()
        if state == "exited":
            logs = _docker("logs", container, check=False).stdout[-3000:]
            raise RuntimeError(f"Disposable Hindsight exited before readiness: {logs}")
        try:
            health = _request_json(container, "GET", "/health")
            version = _request_json(container, "GET", "/version")
        except (AssertionError, json.JSONDecodeError, RuntimeError):
            time.sleep(0.5)
            continue
        if (
            health.get("status") == "healthy"
            and version.get("api_version") == _EXPECTED_API_VERSION
        ):
            return
        time.sleep(0.5)
    raise RuntimeError(f"Disposable Hindsight container {container} did not become ready")


def _hindsight_environment(
    postgres_container: str, database: str, user: str, password: str
) -> list[str]:
    return [
        "-e",
        f"HINDSIGHT_API_DATABASE_URL=postgresql://{user}:{password}@{postgres_container}:5432/{database}",
        "-e",
        "HINDSIGHT_API_LLM_PROVIDER=none",
        "-e",
        "HINDSIGHT_API_EMBEDDINGS_PROVIDER=tei",
        "-e",
        "HINDSIGHT_API_EMBEDDINGS_TEI_URL=http://hlb-hindsight-tei:8080",
        "-e",
        "HINDSIGHT_API_RERANKER_PROVIDER=tei",
        "-e",
        "HINDSIGHT_API_RERANKER_TEI_URL=http://hlb-hindsight-tei:8080",
        "-e",
        "HINDSIGHT_API_FILE_STORAGE_TYPE=native",
        "-e",
        "HINDSIGHT_API_FILE_DELETE_AFTER_RETAIN=false",
        "-e",
        "HINDSIGHT_API_WORKER_ENABLED=false",
        "-e",
        "HINDSIGHT_API_OTEL_TRACES_ENABLED=false",
    ]


def _start_fake_tei(container: str, network: str) -> None:
    # The exact image does not contain its default HuggingFace models. A tiny
    # network-local TEI protocol double keeps this recovery drill offline; the
    # exercised bank/directive/webhook reads do not invoke inference.
    script = r"""
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        payload = {"model_id": "synthetic-offline-drill"}
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
        self.wfile.write(json.dumps(payload).encode())
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(length) or b"{}")
        if self.path == "/embed":
            payload = [[0.0] * 384 for _ in request.get("inputs", [])]
        else:
            payload = []
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
        self.wfile.write(json.dumps(payload).encode())
    def log_message(self, *args):
        pass
HTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
"""
    _docker(
        "run",
        "-d",
        "--name",
        container,
        "--network",
        network,
        "--network-alias",
        "hlb-hindsight-tei",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=32m",
        "--security-opt",
        "no-new-privileges:true",
        "--cap-drop",
        "ALL",
        "--entrypoint",
        "python",
        _HINDSIGHT_IMAGE,
        "-c",
        script,
    )
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        ready = _docker(
            "exec",
            container,
            "python",
            "-c",
            "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/info').read()",
            check=False,
        )
        if ready.returncode == 0:
            return
        time.sleep(0.2)
    raise RuntimeError("Disposable local TEI protocol double did not become ready")


def _run_migrations(network: str, postgres_container: str) -> None:
    _docker(
        "run",
        "--rm",
        "--network",
        network,
        "--read-only",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=128m",
        "--security-opt",
        "no-new-privileges:true",
        "--cap-drop",
        "ALL",
        *_hindsight_environment(
            postgres_container,
            _SOURCE_DATABASE,
            _SOURCE_OWNER,
            _SOURCE_OWNER_PASSWORD,
        ),
        _HINDSIGHT_IMAGE,
        "hindsight-admin",
        "run-db-migration",
        timeout=600,
    )


def _start_hindsight(
    container: str,
    network: str,
    postgres_container: str,
    database: str,
    user: str,
    password: str,
) -> None:
    _docker(
        "run",
        "-d",
        "--name",
        container,
        "--network",
        network,
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=256m",
        "--security-opt",
        "no-new-privileges:true",
        "--cap-drop",
        "ALL",
        *_hindsight_environment(postgres_container, database, user, password),
        _HINDSIGHT_IMAGE,
    )
    configured_image = _docker("inspect", "--format", "{{.Config.Image}}", container).stdout.strip()
    mounts = json.loads(_docker("inspect", "--format", "{{json .Mounts}}", container).stdout)
    network_mode = _docker(
        "inspect", "--format", "{{.HostConfig.NetworkMode}}", container
    ).stdout.strip()
    assert configured_image == _HINDSIGHT_IMAGE
    assert mounts == []
    assert network_mode == network
    _wait_for_hindsight(container)


def _create_api_phase(container: str, phase: str, webhook_secret: str) -> dict[str, str]:
    bank_id = f"hindsight-drill-{phase}"
    bank = _request_json(
        container,
        "PUT",
        f"/v1/default/banks/{bank_id}",
        {"name": f"Hindsight drill phase {phase.upper()}"},
    )
    directive = _request_json(
        container,
        "POST",
        f"/v1/default/banks/{bank_id}/directives",
        {
            "name": f"phase-{phase}-directive",
            "content": f"Synthetic durable content for phase {phase.upper()}",
            "priority": 10,
            "tags": ["backup-drill", phase],
        },
    )
    webhook = _request_json(
        container,
        "POST",
        f"/v1/default/banks/{bank_id}/webhooks",
        {
            "url": f"https://example.invalid/hindsight/{phase}",
            "secret": webhook_secret,
            "event_types": ["consolidation.completed"],
            "enabled": False,
        },
    )
    assert bank["bank_id"] == bank_id
    assert directive["content"].endswith(phase.upper())
    assert webhook["secret"] is None
    return {"bank_id": bank_id, "directive_id": directive["id"], "webhook_id": webhook["id"]}


def _assert_api_phase(container: str, phase: dict[str, str], *, present: bool) -> None:
    banks = _request_json(container, "GET", "/v1/default/banks")["banks"]
    bank_ids = {bank["bank_id"] for bank in banks}
    assert (phase["bank_id"] in bank_ids) is present
    if not present:
        return
    directives = _request_json(
        container, "GET", f"/v1/default/banks/{phase['bank_id']}/directives"
    )["items"]
    webhooks = _request_json(container, "GET", f"/v1/default/banks/{phase['bank_id']}/webhooks")[
        "items"
    ]
    assert {item["id"] for item in directives} == {phase["directive_id"]}
    assert {item["id"] for item in webhooks} == {phase["webhook_id"]}
    assert all(item["secret"] is None for item in webhooks)


def _insert_native_file(container: str, key: str, data: bytes) -> None:
    encoded = base64.b64encode(data).decode("ascii")
    _psql(
        container,
        _SOURCE_DATABASE,
        "INSERT INTO file_storage(storage_key, data) "
        f"VALUES ('{key}', decode('{encoded}', 'base64'));\n",
    )


def _file_rows(container: str, database: str) -> dict[str, bytes]:
    value = _psql(
        container,
        database,
        "SELECT COALESCE(json_object_agg(storage_key, encode(data, 'base64')), '{}'::json) "
        "FROM file_storage;\n",
    )
    return {key: base64.b64decode(data) for key, data in json.loads(value).items()}


def _webhook_secrets(container: str, database: str) -> dict[str, str]:
    value = _psql(
        container,
        database,
        "SELECT COALESCE(json_object_agg(url, secret), '{}'::json) FROM webhooks;\n",
    )
    return dict(json.loads(value))


def _assert_backup_role(postgres_container: str) -> None:
    attributes = json.loads(
        _psql(
            postgres_container,
            "postgres",
            "SELECT row_to_json(r) FROM (SELECT rolsuper, rolcreaterole, rolcreatedb, "
            "rolreplication, rolbypassrls FROM pg_roles WHERE rolname='hindsight_backup') r;\n",
        )
    )
    assert set(attributes.values()) == {False}
    privileges = _psql(
        postgres_container,
        "postgres",
        "SELECT has_database_privilege('hindsight_backup', 'hindsight_source', 'CONNECT')::int, "
        "has_database_privilege('hindsight_backup', 'hindsight_unrelated', 'CONNECT')::int, "
        "pg_has_role('hindsight_backup', 'pg_signal_backend', 'MEMBER')::int, "
        "pg_has_role('hindsight_backup', 'pg_read_server_files', 'MEMBER')::int, "
        "pg_has_role('hindsight_backup', 'pg_execute_server_program', 'MEMBER')::int;\n",
    )
    assert privileges == "1|0|0|0|0"
    denied = _docker(
        "exec",
        "-e",
        f"PGPASSWORD={_BACKUP_PASSWORD}",
        postgres_container,
        "psql",
        "-X",
        "-h",
        "127.0.0.1",
        "-U",
        _BACKUP_USER,
        "--dbname",
        _SOURCE_DATABASE,
        "--set",
        "ON_ERROR_STOP=on",
        "-c",
        "INSERT INTO file_storage(storage_key,data) VALUES ('forbidden', ''::bytea)",
        check=False,
    )
    assert denied.returncode != 0
    assert "forbidden" not in _file_rows(postgres_container, _SOURCE_DATABASE)


def _inspect_artifact(
    plugin: HindsightPlugin,
    context: BackupContext,
    artifact: Path,
) -> str:
    assert artifact.is_file() and not artifact.is_symlink()
    assert artifact.stat().st_size > 0
    assert stat.S_IMODE(artifact.stat().st_mode) == 0o600
    assert artifact.read_bytes()[:5] == b"PGDMP"
    validated = validate_backup_artifact(str(artifact), plugin, context)
    assert validated.size_bytes == artifact.stat().st_size
    digest = hashlib.file_digest(artifact.open("rb"), "sha256").hexdigest()
    assert validated.sha256 == digest
    sidecar = read_backup_sidecar(str(artifact))
    assert sidecar is not None
    assert sidecar["plugin_name"] == "hindsight"
    assert sidecar["target_slug"] == "hindsight-drill"
    assert Path(sidecar["artifact_path"]) == artifact
    sidecar_text = Path(f"{artifact}.meta.json").read_text(encoding="utf-8")
    for excluded in (
        _SOURCE_OWNER_PASSWORD,
        _BACKUP_PASSWORD,
        _RESTORE_PASSWORD,
        _WEBHOOK_SECRET_A,
        _WEBHOOK_SECRET_B,
        _OAUTH_EXCLUSION_MARKER,
    ):
        assert excluded not in sidecar_text
    toc = subprocess.run(
        ["pg_restore", "--list", str(artifact)],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    ).stdout
    assert "Dumped from database version: 18." in toc
    assert "Dumped by pg_dump version: 18." in toc
    assert " EXTENSION - vector" in toc
    assert " EXTENSION - pg_trgm" in toc
    tables = {
        match.group(1)
        for line in toc.splitlines()
        if (match := re.search(r"\sTABLE public ([^ ]+)\s", line)) is not None
    }
    assert tables == hindsight_module.REQUIRED_TABLES
    return digest


def _create_restore_database(
    postgres_container: str,
    database: str,
    owner: str,
) -> None:
    _psql(
        postgres_container,
        "postgres",
        f"CREATE ROLE {owner} LOGIN PASSWORD '{_RESTORE_PASSWORD}';\n"
        f"CREATE DATABASE {database} OWNER {owner} TEMPLATE template0;\n"
        f"REVOKE CONNECT ON DATABASE {database} FROM PUBLIC;\n"
        f"GRANT CONNECT ON DATABASE {database} TO {owner};\n"
        f"COMMENT ON DATABASE {database} IS '{hindsight_module.RESTORE_SENTINEL}';\n",
    )
    _psql(postgres_container, database, "CREATE EXTENSION vector;\n")


def _postgres_address(container: str, network: str) -> str:
    value = _docker(
        "inspect",
        "--format",
        f'{{{{(index .NetworkSettings.Networks "{network}").IPAddress}}}}',
        container,
    ).stdout.strip()
    assert value
    return value


@pytest.mark.asyncio
async def test_exact_hindsight_two_backup_two_fresh_restore_drill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert shutil.which("docker") is not None
    for binary in ("psql", "pg_dump", "pg_restore"):
        path = shutil.which(binary)
        assert path is not None, f"PostgreSQL 18 {binary} client is required"
        version = subprocess.run(
            [path, "--version"], check=True, capture_output=True, text=True
        ).stdout
        assert " 18." in version or version.rstrip().endswith(" 18")

    suffix = uuid.uuid4().hex[:10]
    network = f"hlb-hindsight-drill-{suffix}"
    volume = f"hlb-hindsight-pg-{suffix}"
    postgres_container = f"hlb-hindsight-pg-{suffix}"
    source_app = f"hlb-hindsight-source-{suffix}"
    tei_container = f"hlb-hindsight-tei-{suffix}"
    restore_apps = [f"hlb-hindsight-restore-a-{suffix}", f"hlb-hindsight-restore-b-{suffix}"]
    restore_databases = [
        f"hlb_hindsight_restore_a_{suffix}",
        f"hlb_hindsight_restore_b_{suffix}",
    ]
    restore_owners = [f"hindsight_restore_a_{suffix}", f"hindsight_restore_b_{suffix}"]
    cleanup_containers = [source_app, *restore_apps, tei_container, postgres_container]
    cleaned = False

    def cleanup() -> None:
        nonlocal cleaned
        for container in cleanup_containers:
            _docker("rm", "-f", container, check=False)
        _docker("volume", "rm", "-f", volume, check=False)
        _docker("network", "rm", network, check=False)
        cleaned = True

    try:
        _docker("network", "create", "--internal", network)
        _docker("volume", "create", volume)
        _docker(
            "run",
            "-d",
            "--name",
            postgres_container,
            "--network",
            network,
            "-e",
            "POSTGRES_HOST_AUTH_METHOD=trust",
            "-v",
            f"{volume}:/var/lib/postgresql",
            _POSTGRES_IMAGE,
        )
        assert (
            _docker("inspect", "--format", "{{.Config.Image}}", postgres_container).stdout.strip()
            == _POSTGRES_IMAGE
        )
        _wait_for_postgres(postgres_container)
        postgres_address = _postgres_address(postgres_container, network)
        _start_fake_tei(tei_container, network)

        _psql(
            postgres_container,
            "postgres",
            f"CREATE ROLE {_SOURCE_OWNER} LOGIN PASSWORD '{_SOURCE_OWNER_PASSWORD}';\n"
            f"CREATE DATABASE {_SOURCE_DATABASE} OWNER {_SOURCE_OWNER} TEMPLATE template0;\n"
            f"CREATE DATABASE {_UNRELATED_DATABASE} TEMPLATE template0;\n"
            f"REVOKE CONNECT ON DATABASE {_UNRELATED_DATABASE} FROM PUBLIC;\n",
        )
        _psql(
            postgres_container,
            _SOURCE_DATABASE,
            "CREATE EXTENSION vector;\nCREATE EXTENSION pg_trgm;\n",
        )
        _run_migrations(network, postgres_container)
        _psql(
            postgres_container,
            _SOURCE_DATABASE,
            f"GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO {_SOURCE_OWNER};\n"
            f"GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO {_SOURCE_OWNER};\n"
            f"GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO {_SOURCE_OWNER};\n",
        )
        assert (
            _psql(
                postgres_container,
                _SOURCE_DATABASE,
                "SELECT current_setting('server_version_num'), "
                "(SELECT extversion FROM pg_extension WHERE extname='vector'), "
                "(SELECT extversion FROM pg_extension WHERE extname='pg_trgm'), "
                "(SELECT version_num FROM alembic_version);\n",
            )
            == "180006|0.8.6|1.6|c7d1e9a4b3f2"
        )

        _start_hindsight(
            source_app,
            network,
            postgres_container,
            _SOURCE_DATABASE,
            _SOURCE_OWNER,
            _SOURCE_OWNER_PASSWORD,
        )
        phase_a = _create_api_phase(source_app, "a", _WEBHOOK_SECRET_A)
        file_a = b"Hindsight native file bytes from phase A\x00\xff"
        _insert_native_file(postgres_container, "drill/phase-a.bin", file_a)

        _psql(
            postgres_container,
            "postgres",
            f"CREATE ROLE {_BACKUP_USER} LOGIN PASSWORD '{_BACKUP_PASSWORD}';\n"
            f"GRANT CONNECT ON DATABASE {_SOURCE_DATABASE} TO {_BACKUP_USER};\n",
        )
        _psql(
            postgres_container,
            _SOURCE_DATABASE,
            f"REVOKE CREATE ON SCHEMA public FROM PUBLIC;\n"
            f"GRANT USAGE ON SCHEMA public TO {_BACKUP_USER};\n"
            f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO {_BACKUP_USER};\n"
            f"GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO {_BACKUP_USER};\n",
        )
        _assert_backup_role(postgres_container)

        backup_root = tmp_path / "backups"
        monkeypatch.setattr(hindsight_module, "BACKUP_BASE_PATH", str(backup_root))
        plugin = HindsightPlugin(name="hindsight")
        source_config = {
            "mode": "source",
            "host": postgres_address,
            "port": 5432,
            "database": _SOURCE_DATABASE,
            "user": _BACKUP_USER,
            "password": _BACKUP_PASSWORD,
        }
        assert await plugin.test(source_config) is True
        backup_context_a = BackupContext(
            job_id="hindsight-drill-a",
            target_id="hindsight-source",
            config=source_config,
            metadata={"target_slug": "hindsight-drill"},
        )
        artifact_a = Path((await plugin.backup(backup_context_a))["artifact_path"])
        digest_a = _inspect_artifact(plugin, backup_context_a, artifact_a)

        phase_b = _create_api_phase(source_app, "b", _WEBHOOK_SECRET_B)
        file_b = b"Hindsight native file bytes from phase B\x10\x00"
        _insert_native_file(postgres_container, "drill/phase-b.bin", file_b)
        backup_context_b = BackupContext(
            job_id="hindsight-drill-b",
            target_id="hindsight-source",
            config=source_config,
            metadata={"target_slug": "hindsight-drill"},
        )
        artifact_b = Path((await plugin.backup(backup_context_b))["artifact_path"])
        digest_b = _inspect_artifact(plugin, backup_context_b, artifact_b)
        assert artifact_a != artifact_b
        assert digest_a != digest_b

        artifacts = [artifact_a, artifact_b]
        for database, owner, artifact in zip(
            restore_databases, restore_owners, artifacts, strict=True
        ):
            _create_restore_database(postgres_container, database, owner)
            destination_config = {
                "mode": "restore_destination",
                "host": postgres_address,
                "port": 5432,
                "database": database,
                "user": owner,
                "password": _RESTORE_PASSWORD,
            }
            assert await plugin.test(destination_config) is True
            result = await plugin.restore(
                RestoreContext(
                    job_id=f"restore-{database}",
                    source_target_id="hindsight-source",
                    destination_target_id=database,
                    config=destination_config,
                    artifact_path=str(artifact),
                    metadata={"source_target_slug": "hindsight-drill"},
                )
            )
            assert result["status"] == "success"
            assert "exact-image boot" in result["message"]

        assert _file_rows(postgres_container, restore_databases[0]) == {"drill/phase-a.bin": file_a}
        assert _file_rows(postgres_container, restore_databases[1]) == {
            "drill/phase-a.bin": file_a,
            "drill/phase-b.bin": file_b,
        }
        assert _webhook_secrets(postgres_container, restore_databases[0]) == {
            "https://example.invalid/hindsight/a": _WEBHOOK_SECRET_A
        }
        assert _webhook_secrets(postgres_container, restore_databases[1]) == {
            "https://example.invalid/hindsight/a": _WEBHOOK_SECRET_A,
            "https://example.invalid/hindsight/b": _WEBHOOK_SECRET_B,
        }

        for app, database, owner in zip(
            restore_apps, restore_databases, restore_owners, strict=True
        ):
            _start_hindsight(app, network, postgres_container, database, owner, _RESTORE_PASSWORD)
        _assert_api_phase(restore_apps[0], phase_a, present=True)
        _assert_api_phase(restore_apps[0], phase_b, present=False)
        _assert_api_phase(restore_apps[1], phase_a, present=True)
        _assert_api_phase(restore_apps[1], phase_b, present=True)

        _docker("restart", postgres_container, timeout=180)
        _wait_for_postgres(postgres_container)
        for app in restore_apps:
            _docker("restart", app, timeout=180)
            _wait_for_hindsight(app)
        _assert_api_phase(restore_apps[0], phase_a, present=True)
        _assert_api_phase(restore_apps[0], phase_b, present=False)
        _assert_api_phase(restore_apps[1], phase_a, present=True)
        _assert_api_phase(restore_apps[1], phase_b, present=True)
        assert _file_rows(postgres_container, restore_databases[0])["drill/phase-a.bin"] == file_a
        assert _file_rows(postgres_container, restore_databases[1])["drill/phase-b.bin"] == file_b

        cleanup()
        for container in cleanup_containers:
            assert _docker("inspect", container, check=False).returncode != 0
        assert _docker("network", "inspect", network, check=False).returncode != 0
        assert _docker("volume", "inspect", volume, check=False).returncode != 0
    finally:
        if not cleaned:
            cleanup()
