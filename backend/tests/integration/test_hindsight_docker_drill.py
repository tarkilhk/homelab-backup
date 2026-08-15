from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy.orm import Session

from app.core.plugins.artifacts import validate_backup_artifact
from app.core.plugins.base import BackupContext
from app.core.plugins.sidecar import read_backup_sidecar
from app.models import Run, Target, TargetRun
from app.plugins.hindsight import plugin as hindsight_module
from app.plugins.hindsight.plugin import HindsightPlugin
from app.services.restores import RestoreService

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


def _request_result(
    container: str,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
) -> tuple[int, bytes]:
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
    return int(response["status"]), payload


def _request_json(
    container: str,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
) -> Any:
    status, payload = _request_result(container, method, path, body)
    if not 200 <= status < 300:
        raise AssertionError(
            f"Hindsight {method} {path} returned {status}: "
            f"{payload.decode(errors='replace')[:500]}"
        )
    return json.loads(payload)


def _request_file_retain(
    container: str,
    bank_id: str,
    filename: str,
    data: bytes,
    request_body: dict[str, Any],
) -> Any:
    script = r"""
import base64, json, sys, urllib.error, urllib.request
request = json.load(sys.stdin)
boundary = "hindsight-backup-drill-boundary"
parts = []
def field(name, value, filename=None, content_type=None):
    disposition = f'form-data; name="{name}"'
    if filename is not None:
        disposition += f'; filename="{filename}"'
    headers = [f"Content-Disposition: {disposition}"]
    if content_type is not None:
        headers.append(f"Content-Type: {content_type}")
    parts.append(("\r\n".join(headers) + "\r\n\r\n").encode() + value)
field("request", json.dumps(request["request"]).encode())
field("files", base64.b64decode(request["data"]), request["filename"], "text/plain")
body = b""
for part in parts:
    body += b"--" + boundary.encode() + b"\r\n" + part + b"\r\n"
body += b"--" + boundary.encode() + b"--\r\n"
try:
    response = urllib.request.urlopen(
        urllib.request.Request(
            "http://127.0.0.1:8888" + request["path"],
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        ),
        timeout=30,
    )
    payload = response.read(); status = response.status
except urllib.error.HTTPError as error:
    payload = error.read(); status = error.code
print(json.dumps({"status": status, "body": base64.b64encode(payload).decode()}))
"""
    request = json.dumps(
        {
            "path": f"/v1/default/banks/{bank_id}/files/retain",
            "filename": filename,
            "data": base64.b64encode(data).decode("ascii"),
            "request": request_body,
        },
        separators=(",", ":"),
    )
    completed = _docker("exec", "-i", container, "python", "-c", script, input_text=request)
    response = json.loads(completed.stdout)
    payload = base64.b64decode(response["body"])
    assert 200 <= int(response["status"]) < 300, payload.decode(errors="replace")[:500]
    return json.loads(payload)


def _wait_for_operation(container: str, bank_id: str, operation_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        operation = _request_json(
            container,
            "GET",
            f"/v1/default/banks/{bank_id}/operations/{operation_id}",
        )
        if operation["status"] == "completed":
            return cast(dict[str, Any], operation)
        if operation["status"] in {"failed", "cancelled", "not_found"}:
            raise AssertionError(
                f"Native file retain operation ended as {operation['status']}: "
                f"{operation.get('error_message', '')[:300]}"
            )
        time.sleep(0.25)
    raise AssertionError("Native file retain operation did not complete within 180 seconds")


def _wait_for_document(container: str, bank_id: str, document_id: str) -> dict[str, Any]:
    # In 0.8.6 the file-conversion operation is marked completed immediately
    # before it queues a distinct retain operation. The supported document GET,
    # rather than that parent status alone, is therefore the durable boundary.
    deadline = time.monotonic() + 180
    path = f"/v1/default/banks/{bank_id}/documents/{document_id}"
    while time.monotonic() < deadline:
        status, payload = _request_result(container, "GET", path)
        if status == 200:
            return cast(dict[str, Any], json.loads(payload))
        assert status == 404, payload.decode(errors="replace")[:500]
        time.sleep(0.25)
    raise AssertionError("Native file retain document did not become visible within 180 seconds")


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
        "HINDSIGHT_API_WORKER_ENABLED=true",
        "-e",
        "HINDSIGHT_API_OTEL_TRACES_ENABLED=false",
    ]


def _start_fake_tei(container: str, network: str) -> None:
    # The exact image does not contain its default HuggingFace models. A tiny
    # network-local TEI protocol double keeps this recovery drill offline while
    # real retain and file-retain calls still exercise the vendor's persistence.
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


def _create_api_phase(container: str, phase: str, webhook_secret: str) -> dict[str, Any]:
    bank_id = f"hindsight-drill-{phase}"
    document_id = f"phase-{phase}-document"
    deleted_document_id = f"phase-{phase}-deleted-document"
    original_memory_text = f"Synthetic memory content for phase {phase.upper()}"
    updated_memory_text = f"Curated durable memory content for phase {phase.upper()}"
    document_tags = ["backup-drill", phase, "mutated"]
    bank = _request_json(
        container,
        "PUT",
        f"/v1/default/banks/{bank_id}",
        {"name": f"Hindsight drill phase {phase.upper()}"},
    )
    retained = _request_json(
        container,
        "POST",
        f"/v1/default/banks/{bank_id}/memories",
        {
            "items": [
                {
                    "content": original_memory_text,
                    "context": f"phase-{phase}-context",
                    "document_id": document_id,
                    "tags": ["backup-drill", phase],
                    "timestamp": "unset",
                }
            ],
            "async": False,
        },
    )
    assert retained["success"] is True and retained["items_count"] >= 1
    recalled = _request_json(
        container,
        "GET",
        f"/v1/default/banks/{bank_id}/memories/list?document_id={document_id}",
    )
    assert recalled["total"] >= 1
    memory = next(item for item in recalled["items"] if item["text"] == original_memory_text)
    updated_memory = _request_json(
        container,
        "PATCH",
        f"/v1/default/banks/{bank_id}/memories/{memory['id']}",
        {"text": updated_memory_text, "context": f"phase-{phase}-curated-context"},
    )
    assert updated_memory["text"] == updated_memory_text
    document_update = _request_json(
        container,
        "PATCH",
        f"/v1/default/banks/{bank_id}/documents/{document_id}",
        {"tags": document_tags},
    )
    assert document_update["success"] is True
    document = _request_json(
        container,
        "GET",
        f"/v1/default/banks/{bank_id}/documents/{document_id}",
    )
    assert document["original_text"] == original_memory_text
    assert document["tags"] == document_tags

    deleted_retain = _request_json(
        container,
        "POST",
        f"/v1/default/banks/{bank_id}/memories",
        {
            "items": [
                {
                    "content": f"Disposable memory for phase {phase.upper()}",
                    "document_id": deleted_document_id,
                    "tags": ["backup-drill", phase, "delete-me"],
                }
            ],
            "async": False,
        },
    )
    assert deleted_retain["success"] is True
    deleted = _request_json(
        container,
        "DELETE",
        f"/v1/default/banks/{bank_id}/documents/{deleted_document_id}",
    )
    assert deleted["success"] is True and deleted["memory_units_deleted"] >= 1
    deleted_status, _ = _request_result(
        container,
        "GET",
        f"/v1/default/banks/{bank_id}/documents/{deleted_document_id}",
    )
    assert deleted_status == 404
    active_document_id = f"phase-{phase}-active-backup-write"
    active_retain = _request_json(
        container,
        "POST",
        f"/v1/default/banks/{bank_id}/memories",
        {
            "items": [
                {
                    "content": f"Concurrent backup marker for phase {phase.upper()}",
                    "document_id": active_document_id,
                    "tags": ["backup-drill", "active-0"],
                }
            ],
            "async": False,
        },
    )
    assert active_retain["success"] is True
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
    return {
        "bank_id": bank_id,
        "document_id": document_id,
        "memory_id": memory["id"],
        "memory_text": updated_memory_text,
        "document_tags": document_tags,
        "deleted_document_id": deleted_document_id,
        "active_document_id": active_document_id,
        "directive_id": directive["id"],
        "webhook_id": webhook["id"],
    }


def _assert_api_phase(container: str, phase: dict[str, Any], *, present: bool) -> None:
    banks = _request_json(container, "GET", "/v1/default/banks")["banks"]
    bank_ids = {bank["bank_id"] for bank in banks}
    assert (phase["bank_id"] in bank_ids) is present
    if not present:
        return
    recalled = _request_json(
        container,
        "GET",
        f"/v1/default/banks/{phase['bank_id']}/memories/list?document_id={phase['document_id']}",
    )
    assert {item["id"] for item in recalled["items"]} == {phase["memory_id"]}
    assert recalled["items"][0]["text"] == phase["memory_text"]
    memory = _request_json(
        container,
        "GET",
        f"/v1/default/banks/{phase['bank_id']}/memories/{phase['memory_id']}",
    )
    assert memory["text"] == phase["memory_text"]
    document = _request_json(
        container,
        "GET",
        f"/v1/default/banks/{phase['bank_id']}/documents/{phase['document_id']}",
    )
    assert document["tags"] == phase["document_tags"]
    deleted_status, _ = _request_result(
        container,
        "GET",
        f"/v1/default/banks/{phase['bank_id']}/documents/{phase['deleted_document_id']}",
    )
    assert deleted_status == 404
    active_document = _request_json(
        container,
        "GET",
        f"/v1/default/banks/{phase['bank_id']}/documents/{phase['active_document_id']}",
    )
    assert len(active_document["tags"]) == 2
    assert "backup-drill" in active_document["tags"]
    assert set(active_document["tags"]) & {"active-0", "active-1"}
    directives = _request_json(
        container, "GET", f"/v1/default/banks/{phase['bank_id']}/directives"
    )["items"]
    webhooks = _request_json(container, "GET", f"/v1/default/banks/{phase['bank_id']}/webhooks")[
        "items"
    ]
    assert {item["id"] for item in directives} == {phase["directive_id"]}
    assert {item["id"] for item in webhooks} == {phase["webhook_id"]}
    assert all(item["secret"] is None for item in webhooks)


def _upload_native_file(
    app_container: str,
    postgres_container: str,
    phase: dict[str, Any],
    filename: str,
    data: bytes,
) -> str:
    before = _file_rows(postgres_container, _SOURCE_DATABASE)
    document_id = f"{phase['bank_id']}-native-file"
    response = _request_file_retain(
        app_container,
        phase["bank_id"],
        filename,
        data,
        {
            "parser": "markitdown",
            "files_metadata": [
                {
                    "document_id": document_id,
                    "context": f"Native upload for {phase['bank_id']}",
                    "tags": ["backup-drill", "native-file"],
                }
            ],
        },
    )
    assert len(response["operation_ids"]) == 1
    _wait_for_operation(app_container, phase["bank_id"], response["operation_ids"][0])
    document = _wait_for_document(app_container, phase["bank_id"], document_id)
    assert document["original_text"].strip() == data.decode().strip()
    after = _file_rows(postgres_container, _SOURCE_DATABASE)
    new_keys = set(after) - set(before)
    assert len(new_keys) == 1
    storage_key = new_keys.pop()
    assert after[storage_key] == data
    phase["file_document_id"] = document_id
    phase["file_storage_key"] = storage_key
    phase["file_text"] = data.decode()
    return storage_key


def _assert_native_file_api(container: str, phase: dict[str, Any]) -> None:
    document = _request_json(
        container,
        "GET",
        f"/v1/default/banks/{phase['bank_id']}/documents/{phase['file_document_id']}",
    )
    assert document["original_text"].strip() == phase["file_text"].strip()
    openapi = _request_json(container, "GET", "/openapi.json")
    assert not any("files/download" in path for path in openapi["paths"])
    # Hindsight 0.8.6 creates native download URLs in its storage backend but
    # exposes no HTTP route for them. This 404 is a documented drill STOP: do
    # not mistake a direct PostgreSQL byte read for a supported file download.
    status, _ = _request_result(
        container,
        "GET",
        f"/v1/default/files/download/{phase['file_storage_key']}",
    )
    assert status == 404


async def _backup_during_supported_writes(
    plugin: HindsightPlugin,
    context: BackupContext,
    app_container: str,
    phase: dict[str, Any],
) -> Path:
    stop = threading.Event()
    started = threading.Event()
    errors: list[BaseException] = []
    writes = 0

    def write_loop() -> None:
        nonlocal writes
        try:
            while not stop.is_set():
                response = _request_json(
                    app_container,
                    "PATCH",
                    f"/v1/default/banks/{phase['bank_id']}/documents/"
                    f"{phase['active_document_id']}",
                    {"tags": ["backup-drill", f"active-{writes % 2}"]},
                )
                assert response["success"] is True
                writes += 1
                started.set()
        except BaseException as exc:  # pragma: no cover - surfaced in the main thread
            errors.append(exc)
            started.set()

    writer = threading.Thread(target=write_loop, name="hindsight-supported-writer")
    writer.start()
    try:
        assert started.wait(timeout=30), "supported API write did not start"
        assert not errors
        result = await plugin.backup(context)
    finally:
        stop.set()
        writer.join(timeout=30)
    assert not writer.is_alive()
    assert not errors
    assert writes >= 1
    return Path(result["artifact_path"])


def _published_backup_files(backup_root: Path) -> set[Path]:
    return {path for path in backup_root.rglob("*") if path.is_file()}


def _destination_inventory(postgres_container: str, database: str) -> str:
    return _psql(
        postgres_container,
        database,
        "SELECT json_build_object("
        "'comment', shobj_description(d.oid, 'pg_database'), "
        "'extensions', (SELECT COALESCE(json_agg(extname ORDER BY extname), '[]'::json) "
        "FROM pg_extension WHERE extname <> 'plpgsql'), "
        "'relations', (SELECT COALESCE(json_agg(n.nspname || '.' || c.relname || ':' || "
        "c.relkind::text ORDER BY n.nspname, c.relname), '[]'::json) FROM pg_class c "
        "JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname NOT IN "
        "('pg_catalog','information_schema') AND n.nspname !~ '^pg_toast')) "
        "FROM pg_database d WHERE d.datname=current_database();\n",
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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    db_session: Session,
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
        file_a = b"Hindsight native file content from phase A\n"
        file_key_a = _upload_native_file(
            source_app,
            postgres_container,
            phase_a,
            "phase-a.txt",
            file_a,
        )
        _assert_native_file_api(source_app, phase_a)

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
        monkeypatch.setenv("BACKUP_BASE_PATH", str(backup_root))
        monkeypatch.setenv("HOMELAB_BACKUP_ALLOW_ISOLATED_RESTORE", "1")
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

        # The source contract must fail closed before creating any public
        # artifact when SELECT is missing or an expected table enables RLS.
        unpublished_before = _published_backup_files(backup_root)
        underprivileged_user = f"hindsight_underpriv_{suffix}"
        underprivileged_password = "synthetic-local-underprivileged-password"
        _psql(
            postgres_container,
            "postgres",
            f"CREATE ROLE {underprivileged_user} LOGIN PASSWORD "
            f"'{underprivileged_password}';\n"
            f"GRANT CONNECT ON DATABASE {_SOURCE_DATABASE} TO {underprivileged_user};\n",
        )
        _psql(
            postgres_container,
            _SOURCE_DATABASE,
            f"GRANT USAGE ON SCHEMA public TO {underprivileged_user};\n",
        )
        underprivileged_config = {
            **source_config,
            "user": underprivileged_user,
            "password": underprivileged_password,
        }
        with pytest.raises((ConnectionError, RuntimeError)):
            await plugin.backup(
                BackupContext(
                    job_id="hindsight-underprivileged-source",
                    target_id="hindsight-underprivileged-source",
                    config=underprivileged_config,
                    metadata={"target_slug": "hindsight-underprivileged-source"},
                )
            )
        assert _published_backup_files(backup_root) == unpublished_before

        _psql(
            postgres_container,
            _SOURCE_DATABASE,
            "ALTER TABLE public.banks ENABLE ROW LEVEL SECURITY;\n",
        )
        try:
            with pytest.raises(RuntimeError, match="RLS"):
                await plugin.backup(
                    BackupContext(
                        job_id="hindsight-rls-source",
                        target_id="hindsight-rls-source",
                        config=source_config,
                        metadata={"target_slug": "hindsight-rls-source"},
                    )
                )
        finally:
            _psql(
                postgres_container,
                _SOURCE_DATABASE,
                "ALTER TABLE public.banks DISABLE ROW LEVEL SECURITY;\n",
            )
        assert _published_backup_files(backup_root) == unpublished_before

        backup_context_a = BackupContext(
            job_id="hindsight-drill-a",
            target_id="hindsight-source",
            config=source_config,
            metadata={"target_slug": "hindsight-drill"},
        )
        artifact_a = await _backup_during_supported_writes(
            plugin,
            backup_context_a,
            source_app,
            phase_a,
        )
        digest_a = _inspect_artifact(plugin, backup_context_a, artifact_a)

        phase_b = _create_api_phase(source_app, "b", _WEBHOOK_SECRET_B)
        file_b = b"Hindsight native file content from phase B\n"
        file_key_b = _upload_native_file(
            source_app,
            postgres_container,
            phase_b,
            "phase-b.txt",
            file_b,
        )
        _assert_native_file_api(source_app, phase_b)
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
        source_target = Target(
            name="Hindsight drill source",
            slug="hindsight-drill",
            plugin_name="hindsight",
            plugin_config_json=json.dumps(source_config),
        )
        db_session.add(source_target)
        db_session.commit()
        db_session.refresh(source_target)
        source_runs: list[TargetRun] = []
        for artifact, digest in zip(artifacts, (digest_a, digest_b), strict=True):
            source_run = Run(status="success", operation="backup")
            db_session.add(source_run)
            db_session.commit()
            db_session.refresh(source_run)
            source_target_run = TargetRun(
                run_id=source_run.id,
                target_id=source_target.id,
                status="success",
                operation="backup",
                artifact_path=str(artifact),
                artifact_bytes=artifact.stat().st_size,
                sha256=digest,
                source_identity_json=json.dumps(
                    {
                        "host": source_config["host"],
                        "port": source_config["port"],
                        "database": source_config["database"],
                        "user": source_config["user"],
                    },
                    sort_keys=True,
                ),
            )
            db_session.add(source_target_run)
            db_session.commit()
            db_session.refresh(source_target_run)
            source_runs.append(source_target_run)

        def add_destination_target(database: str, owner: str) -> tuple[Target, dict[str, Any]]:
            destination_config = {
                "mode": "restore_destination",
                "host": postgres_address,
                "port": 5432,
                "database": database,
                "user": owner,
                "password": _RESTORE_PASSWORD,
            }
            destination_target = Target(
                name=f"Hindsight drill destination {database}",
                slug=f"hindsight-drill-destination-{database}",
                plugin_name="hindsight",
                plugin_config_json=json.dumps(destination_config),
            )
            db_session.add(destination_target)
            db_session.commit()
            db_session.refresh(destination_target)
            return destination_target, destination_config

        # Corrupt provenance must be rejected before touching an otherwise valid
        # disposable destination.
        corrupt_database = f"hlb_hindsight_restore_corrupt_{suffix}"
        corrupt_owner = f"hindsight_corrupt_{suffix}"
        _create_restore_database(postgres_container, corrupt_database, corrupt_owner)
        corrupt_target, corrupt_config = add_destination_target(corrupt_database, corrupt_owner)
        assert await plugin.test(corrupt_config) is True
        corrupt_run = Run(status="success", operation="backup")
        db_session.add(corrupt_run)
        db_session.commit()
        db_session.refresh(corrupt_run)
        corrupt_source_run = TargetRun(
            run_id=corrupt_run.id,
            target_id=source_target.id,
            status="success",
            operation="backup",
            artifact_path=str(artifact_a),
            artifact_bytes=artifact_a.stat().st_size,
            sha256=digest_a,
            source_identity_json='{"database":',
        )
        db_session.add(corrupt_source_run)
        db_session.commit()
        db_session.refresh(corrupt_source_run)
        corrupt_before = _destination_inventory(postgres_container, corrupt_database)
        with pytest.raises(ValueError, match="verified source target metadata"):
            await asyncio.to_thread(
                RestoreService(db_session).restore,
                source_target_run_id=corrupt_source_run.id,
                destination_target_id=corrupt_target.id,
                triggered_by="isolated_hindsight_corrupt_provenance_drill",
            )
        assert _destination_inventory(postgres_container, corrupt_database) == corrupt_before
        assert await plugin.test(corrupt_config) is True

        # A destination containing even one user object is rejected and remains
        # byte-for-byte semantically unchanged (including its sentinel row).
        nonempty_database = f"hlb_hindsight_restore_nonempty_{suffix}"
        nonempty_owner = f"hindsight_nonempty_{suffix}"
        _create_restore_database(postgres_container, nonempty_database, nonempty_owner)
        nonempty_target, _ = add_destination_target(nonempty_database, nonempty_owner)
        _psql(
            postgres_container,
            nonempty_database,
            "CREATE TABLE public.preexisting_marker(payload text NOT NULL);\n"
            "INSERT INTO public.preexisting_marker VALUES ('must-survive');\n",
        )
        nonempty_before = _destination_inventory(postgres_container, nonempty_database)
        marker_before = _psql(
            postgres_container,
            nonempty_database,
            "SELECT payload FROM public.preexisting_marker;\n",
        )
        with pytest.raises(RuntimeError, match="destination must"):
            await asyncio.to_thread(
                RestoreService(db_session).restore,
                source_target_run_id=source_runs[0].id,
                destination_target_id=nonempty_target.id,
                triggered_by="isolated_hindsight_nonempty_destination_drill",
            )
        assert _destination_inventory(postgres_container, nonempty_database) == nonempty_before
        assert (
            _psql(
                postgres_container,
                nonempty_database,
                "SELECT payload FROM public.preexisting_marker;\n",
            )
            == marker_before
            == "must-survive"
        )

        # Force a real error at the tail of the rendered vendor SQL. psql still
        # executes the complete restore, but --single-transaction must roll all
        # of it back and leave the fresh sentinel database unchanged.
        rollback_database = f"hlb_hindsight_restore_rollback_{suffix}"
        rollback_owner = f"hindsight_rollback_{suffix}"
        _create_restore_database(postgres_container, rollback_database, rollback_owner)
        rollback_target, rollback_config = add_destination_target(rollback_database, rollback_owner)
        assert await plugin.test(rollback_config) is True
        rollback_before = _destination_inventory(postgres_container, rollback_database)
        original_render_restore_sql = HindsightPlugin._render_restore_sql

        async def render_sql_with_tail_failure(
            self: HindsightPlugin, artifact: Path, allowlist: Path
        ) -> Path:
            restore_sql = await original_render_restore_sql(self, artifact, allowlist)
            with restore_sql.open("ab") as sql_file:
                sql_file.write(
                    b"\nDO $$ BEGIN RAISE EXCEPTION "
                    b"'synthetic isolated rollback proof'; END $$;\n"
                )
            return restore_sql

        with monkeypatch.context() as rollback_patch:
            rollback_patch.setattr(
                HindsightPlugin,
                "_render_restore_sql",
                render_sql_with_tail_failure,
            )
            with pytest.raises(RuntimeError, match="transactional restore failed"):
                await asyncio.to_thread(
                    RestoreService(db_session).restore,
                    source_target_run_id=source_runs[0].id,
                    destination_target_id=rollback_target.id,
                    triggered_by="isolated_hindsight_transaction_rollback_drill",
                )
        assert _destination_inventory(postgres_container, rollback_database) == rollback_before
        assert await plugin.test(rollback_config) is True

        for database, owner, artifact, source_target_run in zip(
            restore_databases, restore_owners, artifacts, source_runs, strict=True
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
            destination_target = Target(
                name=f"Hindsight drill destination {database}",
                slug=f"hindsight-drill-destination-{database}",
                plugin_name="hindsight",
                plugin_config_json=json.dumps(destination_config),
            )
            db_session.add(destination_target)
            db_session.commit()
            db_session.refresh(destination_target)
            result = await asyncio.to_thread(
                RestoreService(db_session).restore,
                source_target_run_id=source_target_run.id,
                destination_target_id=destination_target.id,
                triggered_by="isolated_hindsight_drill",
            )
            assert result.status == "success"
            assert result.target_runs[0].artifact_path == str(artifact)
            assert "exact-image boot" in str(result.message)

        assert _file_rows(postgres_container, restore_databases[0]) == {file_key_a: file_a}
        assert _file_rows(postgres_container, restore_databases[1]) == {
            file_key_a: file_a,
            file_key_b: file_b,
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
        _assert_native_file_api(restore_apps[0], phase_a)
        _assert_native_file_api(restore_apps[1], phase_a)
        _assert_native_file_api(restore_apps[1], phase_b)

        _docker("restart", postgres_container, timeout=180)
        _wait_for_postgres(postgres_container)
        for app in restore_apps:
            _docker("restart", app, timeout=180)
            _wait_for_hindsight(app)
        _assert_api_phase(restore_apps[0], phase_a, present=True)
        _assert_api_phase(restore_apps[0], phase_b, present=False)
        _assert_api_phase(restore_apps[1], phase_a, present=True)
        _assert_api_phase(restore_apps[1], phase_b, present=True)
        assert _file_rows(postgres_container, restore_databases[0])[file_key_a] == file_a
        assert _file_rows(postgres_container, restore_databases[1])[file_key_b] == file_b

        cleanup()
        for container in cleanup_containers:
            assert _docker("inspect", container, check=False).returncode != 0
        assert _docker("network", "inspect", network, check=False).returncode != 0
        assert _docker("volume", "inspect", volume, check=False).returncode != 0
    finally:
        if not cleaned:
            cleanup()
