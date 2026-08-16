from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import shutil
import stat
import subprocess
import time
import traceback
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, cast

import pytest

from app.core.plugins.sidecar import read_backup_sidecar
from app.plugins.invoiceninja.plugin import InvoiceNinjaPlugin

_DOCKER_DRILL_SKIP = pytest.mark.skipif(
    os.getenv("RUN_INVOICENINJA_DOCKER_DRILL") != "1",
    reason="set RUN_INVOICENINJA_DOCKER_DRILL=1 for the isolated exact-image drill",
)

_BACKEND_ROOT = Path(__file__).resolve().parents[2]
_APP_IMAGE = (
    "invoiceninja/invoiceninja@"
    "sha256:5c051fd2a7914b05deb759556ba1a7959a86a22a8ffff488267f7cdd00713217"
)
_MYSQL_IMAGE = "mysql@" "sha256:53a71a83be1fcbb1489c0fe23d377297f55b77a6c6ce816ca8fa30225adfe2df"
_NGINX_IMAGE = "nginx@" "sha256:963cfe6e75d1c292f66589d7e190b137cf89310414c0c1c5b476dfc61a4fcd0d"
_EXACT_VERSION = "5.13.31"
_RESTORE_ENABLE_ENV = "HOMELAB_BACKUP_ALLOW_ISOLATED_RESTORE"
_RESTORE_ORIGINS_ENV = "HOMELAB_BACKUP_ISOLATED_RESTORE_ALLOWED_ORIGINS"
_LABEL = "asia.hollinger.homelab-backup.invoice-ninja-drill"
_SYNTHETIC_SECRETS: set[str] = set()
_RUNNER_CONTAINERS: set[str] = set()


@dataclass(frozen=True)
class _Triplet:
    app: str
    database: str
    web: str
    app_alias: str
    database_alias: str
    web_alias: str
    origin: str
    public_volume: str
    storage_volume: str
    database_volume: str
    token: str
    email: str
    password: str


@dataclass(frozen=True)
class _PhaseEvidence:
    phase: str
    company_name: str
    client_name: str
    client_id_number: str
    contact_email: str
    invoice_number: str
    invoice_public_notes: str
    invoice_private_notes: str
    invoice_product_key: str
    invoice_line_notes: str
    document_name: str
    document_bytes: bytes
    client_id: str
    invoice_id: str
    document_id: str


def _redact(value: str) -> str:
    result = value
    for secret in _SYNTHETIC_SECRETS:
        result = result.replace(secret, "[REDACTED]")
    return result[-8000:]


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
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(
            "Disposable Invoice Ninja Docker command failed:\n"
            f"{_redact(str(exc.stderr or ''))}\n{_redact(str(exc.stdout or ''))}"
        ) from None


def _sha256(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _secret_urlsafe() -> str:
    value = secrets.token_urlsafe(24)
    _SYNTHETIC_SECRETS.add(value)
    return value


def _app_key() -> str:
    value = "base64:" + base64.b64encode(secrets.token_bytes(32)).decode()
    _SYNTHETIC_SECRETS.add(value)
    return value


def _json_result(completed: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    for line in reversed(completed.stdout.splitlines()):
        if line.startswith("{"):
            parsed = json.loads(line)
            assert isinstance(parsed, dict)
            return cast(dict[str, Any], parsed)
    raise AssertionError(
        "Invoice Ninja runner returned no JSON result: "
        f"{_redact(completed.stderr)}\n{_redact(completed.stdout)}"
    )


def _capture_login_token(result: dict[str, Any]) -> str | None:
    body = result.get("body")
    data = body.get("data") if isinstance(body, dict) else None
    first = data[0] if isinstance(data, list) and data and isinstance(data[0], dict) else None
    token_record = first.get("token") if isinstance(first, dict) else None
    token = token_record.get("token") if isinstance(token_record, dict) else None
    if isinstance(token, str) and token:
        _SYNTHETIC_SECRETS.add(token)
        return token
    return None


def _login_diagnostic(result: dict[str, Any] | None) -> dict[str, object]:
    if result is None:
        return {"response": "missing"}
    body = result.get("body")
    data = body.get("data") if isinstance(body, dict) else None
    return {
        "status": result.get("status"),
        "version": result.get("version"),
        "body_type": type(body).__name__,
        "data_type": type(data).__name__,
        "data_count": len(data) if isinstance(data, list) else None,
    }


def _runner(
    *,
    name: str,
    image: str,
    network: str,
    script: str,
    environment: Iterable[tuple[str, str]] = (),
    mounts: Iterable[tuple[Path, str, bool]] = (),
    input_text: str | None = None,
    timeout: int = 360,
) -> subprocess.CompletedProcess[str]:
    arguments = [
        "create",
        "--name",
        name,
        "--label",
        f"{_LABEL}=runner",
        "--network",
        network,
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=256m",
        "--memory",
        "512m",
        "--memory-swap",
        "512m",
        "--pids-limit",
        "256",
        "--security-opt",
        "no-new-privileges:true",
        "--cap-drop",
        "ALL",
    ]
    for key, value in environment:
        arguments.extend(("-e", f"{key}={value}"))
    for source, destination, writable in mounts:
        arguments.extend(("-v", f"{source}:{destination}:{'rw' if writable else 'ro'}"))
    arguments.extend((image, "python", "-c", script))
    _RUNNER_CONTAINERS.add(name)
    try:
        _docker(*arguments)
        inspected = json.loads(_docker("inspect", "--format", "{{json .Mounts}}", name).stdout)
        assert all(item["Destination"] != "/var/run/docker.sock" for item in inspected)
        assert _docker(
            "inspect", "--format", "{{json .HostConfig.PortBindings}}", name
        ).stdout.strip() in {"null", "{}"}
        return _docker(
            "start", "--attach", "--interactive", name, input_text=input_text, timeout=timeout
        )
    finally:
        _docker("rm", "-f", name, check=False)


_RUNNER_GUARDS = r"""
import socket
from pathlib import Path

def assert_runner_guards():
    assert not Path('/var/run/docker.sock').exists()
    sock = socket.socket()
    sock.settimeout(2)
    try:
        assert sock.connect_ex(('1.1.1.1', 53)) != 0
    finally:
        sock.close()
"""


def _request(
    *,
    runner_image: str,
    runner_name: str,
    network: str,
    origin: str,
    method: str,
    path: str,
    token: str | None = None,
    payload: dict[str, Any] | None = None,
    expect_success: bool = True,
) -> dict[str, Any]:
    script = f"""
import json
import os
import httpx
{_RUNNER_GUARDS}

def main():
    assert_runner_guards()
    body = json.loads(os.environ['REQUEST_BODY']) if os.environ.get('REQUEST_BODY') else None
    headers = {{'Accept': 'application/json', 'X-Requested-With': 'XMLHttpRequest'}}
    if os.environ.get('API_TOKEN'):
        headers['X-API-Token'] = os.environ['API_TOKEN']
    try:
        response = httpx.request(
            os.environ['REQUEST_METHOD'],
            os.environ['REQUEST_ORIGIN'] + os.environ['REQUEST_PATH'],
            headers=headers,
            json=body,
            timeout=30,
            follow_redirects=False,
        )
    except httpx.HTTPError:
        print(json.dumps({{'status': 0, 'version': None, 'body': None}}, sort_keys=True))
        return
    try:
        response_body = response.json()
    except ValueError:
        response_body = None
    print(json.dumps({{
        'status': response.status_code,
        'version': response.headers.get('X-APP-VERSION'),
        'body': response_body,
    }}, sort_keys=True))

main()
"""
    environment = [
        ("REQUEST_METHOD", method),
        ("REQUEST_ORIGIN", origin),
        ("REQUEST_PATH", path),
    ]
    if token is not None:
        environment.append(("API_TOKEN", token))
    if payload is not None:
        environment.append(("REQUEST_BODY", json.dumps(payload, separators=(",", ":"))))
    result = _json_result(
        _runner(
            name=runner_name,
            image=runner_image,
            network=network,
            script=script,
            environment=environment,
        )
    )
    if expect_success:
        assert 200 <= result["status"] < 300, result
        assert result["version"] == _EXACT_VERSION, result
    return result


def _write_vhost(path: Path, app_alias: str) -> None:
    path.write_text(
        "server {\n"
        "    listen 80 default_server;\n"
        "    server_name _;\n"
        "    server_tokens off;\n"
        "    client_max_body_size 100M;\n"
        "    root /var/www/app/public/;\n"
        "    index index.php;\n"
        "    location / { try_files $uri $uri/ /index.php?$query_string; }\n"
        "    location = /favicon.ico { access_log off; log_not_found off; }\n"
        "    location = /robots.txt { access_log off; log_not_found off; }\n"
        "    location ~* /storage/.*\\.php$ { return 503; }\n"
        "    location ~ \\.php$ {\n"
        "        fastcgi_split_path_info ^(.+\\.php)(/.+)$;\n"
        f"        fastcgi_pass {app_alias}:9000;\n"
        "        fastcgi_index index.php;\n"
        "        include fastcgi_params;\n"
        "        fastcgi_param SCRIPT_FILENAME $document_root$fastcgi_script_name;\n"
        "        fastcgi_intercept_errors off;\n"
        "        fastcgi_buffer_size 16k;\n"
        "        fastcgi_buffers 4 16k;\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )
    path.chmod(0o644)


def _volume(name: str) -> None:
    _docker("volume", "create", "--label", f"{_LABEL}=1", name)


def _start_triplet(
    *,
    prefix: str,
    network: str,
    round_root: Path,
    runner_image: str,
    suffix: str,
) -> _Triplet:
    database = f"{prefix}-mysql"
    app = f"{prefix}-app"
    web = f"{prefix}-web"
    database_alias = database
    app_alias = app
    web_alias = web
    public_volume = f"{prefix}-public"
    storage_volume = f"{prefix}-storage"
    database_volume = f"{prefix}-database"
    for volume in (public_volume, storage_volume, database_volume):
        _volume(volume)

    database_password = _secret_urlsafe()
    root_password = _secret_urlsafe()
    owner_password = _secret_urlsafe()
    owner_email = f"drill-{suffix}@example.invalid"
    application_key = _app_key()
    _docker(
        "run",
        "-d",
        "--name",
        database,
        "--network",
        network,
        "--network-alias",
        database_alias,
        "--label",
        f"{_LABEL}=1",
        "--memory",
        "640m",
        "--memory-swap",
        "640m",
        "--pids-limit",
        "256",
        "--security-opt",
        "no-new-privileges:true",
        "-e",
        "MYSQL_DATABASE=ninja",
        "-e",
        "MYSQL_USER=ninja",
        "-e",
        f"MYSQL_PASSWORD={database_password}",
        "-e",
        f"MYSQL_ROOT_PASSWORD={root_password}",
        "-v",
        f"{database_volume}:/var/lib/mysql",
        _MYSQL_IMAGE,
    )

    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        ready = _docker(
            "exec",
            database,
            "mysqladmin",
            "ping",
            "-h",
            "127.0.0.1",
            "-uninja",
            f"-p{database_password}",
            check=False,
        )
        if ready.returncode == 0:
            break
        if _docker("inspect", "--format", "{{.State.Status}}", database).stdout.strip() == "exited":
            raise RuntimeError(
                "Disposable Invoice Ninja MySQL exited before readiness: "
                + _redact(_docker("logs", database, check=False).stdout)
            )
        time.sleep(1)
    else:
        raise RuntimeError("Disposable Invoice Ninja MySQL did not become ready")

    _docker(
        "run",
        "-d",
        "--name",
        app,
        "--network",
        network,
        "--network-alias",
        app_alias,
        "--label",
        f"{_LABEL}=1",
        "--memory",
        "768m",
        "--memory-swap",
        "768m",
        "--pids-limit",
        "384",
        "--security-opt",
        "no-new-privileges:true",
        "-e",
        f"APP_URL=http://{web_alias}",
        "-e",
        f"APP_KEY={application_key}",
        "-e",
        "APP_ENV=production",
        "-e",
        "APP_DEBUG=false",
        "-e",
        "REQUIRE_HTTPS=false",
        "-e",
        "TRUSTED_PROXIES=*",
        "-e",
        "QUEUE_CONNECTION=database",
        "-e",
        "CACHE_DRIVER=file",
        "-e",
        "SESSION_DRIVER=file",
        "-e",
        "DB_TYPE=mysql",
        "-e",
        f"DB_HOST={database_alias}",
        "-e",
        "DB_PORT=3306",
        "-e",
        "DB_DATABASE=ninja",
        "-e",
        "DB_USERNAME=ninja",
        "-e",
        f"DB_PASSWORD={database_password}",
        "-e",
        f"IN_USER_EMAIL={owner_email}",
        "-e",
        f"IN_PASSWORD={owner_password}",
        "-e",
        "MAIL_MAILER=log",
        "-e",
        "IS_DOCKER=true",
        "-v",
        f"{public_volume}:/var/www/app/public",
        "-v",
        f"{storage_volume}:/var/www/app/storage",
        _APP_IMAGE,
    )

    vhost = round_root / f"{prefix}-in-vhost.conf"
    _write_vhost(vhost, app_alias)
    _docker(
        "run",
        "-d",
        "--name",
        web,
        "--network",
        network,
        "--network-alias",
        web_alias,
        "--label",
        f"{_LABEL}=1",
        "--memory",
        "128m",
        "--memory-swap",
        "128m",
        "--pids-limit",
        "128",
        "--read-only",
        "--tmpfs",
        "/var/cache/nginx:rw,noexec,nosuid,size=32m",
        "--tmpfs",
        "/var/run:rw,noexec,nosuid,size=4m",
        "--security-opt",
        "no-new-privileges:true",
        "--cap-drop",
        "ALL",
        "--cap-add",
        "CHOWN",
        "--cap-add",
        "SETGID",
        "--cap-add",
        "SETUID",
        "--cap-add",
        "NET_BIND_SERVICE",
        "-v",
        f"{vhost}:/etc/nginx/conf.d/in-vhost.conf:ro",
        "-v",
        f"{public_volume}:/var/www/app/public:ro",
        _NGINX_IMAGE,
    )

    origin = f"http://{web_alias}"
    deadline = time.monotonic() + 300
    login_result: dict[str, Any] | None = None
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        login_result = _request(
            runner_image=runner_image,
            runner_name=f"{prefix}-login-{attempt}",
            network=network,
            origin=origin,
            method="POST",
            path="/api/v1/login",
            payload={"email": owner_email, "password": owner_password},
            expect_success=False,
        )
        token = _capture_login_token(login_result)
        if (
            login_result["status"] == 200
            and login_result["version"] == _EXACT_VERSION
            and token is not None
        ):
            break
        app_state = _docker("inspect", "--format", "{{.State.Status}}", app).stdout.strip()
        web_state = _docker("inspect", "--format", "{{.State.Status}}", web).stdout.strip()
        if "exited" in {app_state, web_state}:
            app_logs = _docker("logs", app, check=False)
            web_logs = _docker("logs", web, check=False)
            raise RuntimeError(
                "Disposable Invoice Ninja exited before readiness:\n"
                + _redact((app_logs.stdout or "") + (app_logs.stderr or ""))
                + _redact((web_logs.stdout or "") + (web_logs.stderr or ""))
            )
        time.sleep(2)
    else:
        raise RuntimeError(
            "Disposable Invoice Ninja did not become ready: " f"{_login_diagnostic(login_result)}"
        )

    assert login_result is not None
    body = login_result["body"]
    diagnostic = _login_diagnostic(login_result)
    assert isinstance(body, dict) and isinstance(body.get("data"), list), diagnostic
    rows = body["data"]
    assert len(rows) == 1 and isinstance(rows[0], dict), diagnostic
    token_record = rows[0].get("token")
    assert isinstance(token_record, dict), diagnostic
    token = token_record.get("token")
    assert isinstance(token, str) and token, diagnostic
    _SYNTHETIC_SECRETS.add(token)

    triplet = _Triplet(
        app=app,
        database=database,
        web=web,
        app_alias=app_alias,
        database_alias=database_alias,
        web_alias=web_alias,
        origin=origin,
        public_volume=public_volume,
        storage_volume=storage_volume,
        database_volume=database_volume,
        token=token,
        email=owner_email,
        password=owner_password,
    )
    _assert_exact_triplet(triplet, network)
    return triplet


def _assert_exact_triplet(triplet: _Triplet, network: str) -> None:
    expected = {
        triplet.app: _APP_IMAGE,
        triplet.database: _MYSQL_IMAGE,
        triplet.web: _NGINX_IMAGE,
    }
    for container, image in expected.items():
        assert (
            _docker("inspect", "--format", "{{.Config.Image}}", container).stdout.strip() == image
        )
        assert (
            _docker("image", "inspect", image, "--format", "{{.Architecture}}").stdout.strip()
            == "amd64"
        )
        assert _docker(
            "inspect", "--format", "{{json .HostConfig.PortBindings}}", container
        ).stdout.strip() in {"null", "{}"}
        mounts = json.loads(_docker("inspect", "--format", "{{json .Mounts}}", container).stdout)
        assert all(item["Destination"] != "/var/run/docker.sock" for item in mounts)
    assert (
        _docker("network", "inspect", "--format", "{{.Internal}}", network).stdout.strip() == "true"
    )


def _triplet_volume_identity(triplet: _Triplet) -> dict[str, tuple[tuple[str, str], ...]]:
    identity: dict[str, tuple[tuple[str, str], ...]] = {}
    for container in (triplet.database, triplet.app, triplet.web):
        mounts = json.loads(_docker("inspect", "--format", "{{json .Mounts}}", container).stdout)
        identity[container] = tuple(
            sorted(
                (item["Destination"], item["Name"]) for item in mounts if item["Type"] == "volume"
            )
        )
    return identity


def _restart_exact_triplet(
    *,
    triplet: _Triplet,
    network: str,
    runner_image: str,
    runner_name_prefix: str,
) -> None:
    volume_identity = _triplet_volume_identity(triplet)
    _docker("restart", triplet.database, triplet.app, triplet.web, timeout=300)
    deadline = time.monotonic() + 300
    attempt = 0
    last: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        attempt += 1
        last = _request(
            runner_image=runner_image,
            runner_name=f"{runner_name_prefix}-{attempt}",
            network=network,
            origin=triplet.origin,
            method="GET",
            path="/api/v1/ping",
            token=triplet.token,
            expect_success=False,
        )
        if last["status"] == 200 and last["version"] == _EXACT_VERSION:
            break
        states = {
            container: _docker("inspect", "--format", "{{.State.Status}}", container).stdout.strip()
            for container in (triplet.database, triplet.app, triplet.web)
        }
        if "exited" in states.values():
            raise RuntimeError(f"Restarted Invoice Ninja triplet exited: {states}")
        time.sleep(2)
    else:
        raise RuntimeError(f"Restarted Invoice Ninja triplet did not become ready: {last}")
    assert _triplet_volume_identity(triplet) == volume_identity
    _assert_exact_triplet(triplet, network)


def _seed_phase(
    *,
    runner_image: str,
    runner_name: str,
    network: str,
    triplet: _Triplet,
    marker: str,
    phase: str,
) -> _PhaseEvidence:
    company_name = f"Synthetic company {marker}"
    client_name = f"Synthetic client {marker}"
    client_id_number = f"CLIENT-{marker}"
    contact_email = f"phase-{phase.lower()}-{marker.lower()}@example.invalid"
    invoice_number = f"INV-{marker}"
    invoice_public_notes = f"Synthetic public note {marker}"
    invoice_private_notes = f"Synthetic private note {marker}"
    invoice_product_key = f"PRODUCT-{marker}"
    invoice_line_notes = f"Synthetic line {marker}"
    document_name = f"document-{marker}.txt"
    document_bytes = f"synthetic-document-bytes-{marker}\n".encode()
    script = f"""
import base64
import json
import os
import httpx
{_RUNNER_GUARDS}

def data(response):
    response.raise_for_status()
    payload = response.json()
    assert isinstance(payload, dict) and 'data' in payload, payload
    return payload['data']

def main():
    assert_runner_guards()
    headers = {{
        'X-API-Token': os.environ['API_TOKEN'],
        'X-Requested-With': 'XMLHttpRequest',
        'Accept': 'application/json',
    }}
    with httpx.Client(base_url=os.environ['ORIGIN'], headers=headers, timeout=30,
                      follow_redirects=False) as client:
        companies = data(client.get('/api/v1/companies'))
        assert isinstance(companies, list) and len(companies) == 1, companies
        company_id = companies[0]['id']
        company = data(client.get('/api/v1/companies/' + company_id))
        settings = dict(company['settings'])
        settings['name'] = os.environ['COMPANY_NAME']
        updated = data(client.put('/api/v1/companies/' + company_id,
                                  json={{'settings': settings}}))
        assert updated['settings']['name'] == os.environ['COMPANY_NAME']

        client_row = data(client.post('/api/v1/clients', json={{
            'name': os.environ['CLIENT_NAME'],
            'id_number': os.environ['CLIENT_ID_NUMBER'],
            'contacts': [{{
                'first_name': 'Synthetic',
                'last_name': os.environ['PHASE'],
                'email': os.environ['CONTACT_EMAIL'],
            }}],
        }}))
        invoice = data(client.post('/api/v1/invoices', json={{
            'client_id': client_row['id'],
            'number': os.environ['INVOICE_NUMBER'],
            'public_notes': os.environ['INVOICE_PUBLIC_NOTES'],
            'private_notes': os.environ['INVOICE_PRIVATE_NOTES'],
            'line_items': [{{
                'product_key': os.environ['INVOICE_PRODUCT_KEY'],
                'notes': os.environ['INVOICE_LINE_NOTES'],
                'quantity': 1,
                'cost': 1,
            }}],
        }}))
        document_bytes = base64.b64decode(os.environ['DOCUMENT_BYTES'])
        uploaded = data(client.post(
            '/api/v1/invoices/' + invoice['id'] + '/upload',
            data={{'_method': 'PUT'}},
            files={{'documents[]': (os.environ['DOCUMENT_NAME'], document_bytes, 'text/plain')}},
        ))
        documents = uploaded.get('documents')
        if not isinstance(documents, list) or not documents:
            uploaded = data(client.get('/api/v1/invoices/' + invoice['id'] +
                                       '?include=documents'))
            documents = uploaded.get('documents')
        assert isinstance(documents, list) and len(documents) == 1, uploaded
        document = documents[0]
        proved_client = data(client.get('/api/v1/clients/' + client_row['id'] +
                                        '?include=contacts'))
        assert proved_client['name'] == os.environ['CLIENT_NAME']
        assert proved_client['id_number'] == os.environ['CLIENT_ID_NUMBER']
        assert any(contact.get('email') == os.environ['CONTACT_EMAIL']
                   for contact in proved_client['contacts'])
        proved_invoice = data(client.get('/api/v1/invoices/' + invoice['id']))
        assert proved_invoice['client_id'] == client_row['id']
        assert proved_invoice['number'] == os.environ['INVOICE_NUMBER']
        assert proved_invoice['public_notes'] == os.environ['INVOICE_PUBLIC_NOTES']
        assert proved_invoice['private_notes'] == os.environ['INVOICE_PRIVATE_NOTES']
        assert any(item.get('product_key') == os.environ['INVOICE_PRODUCT_KEY'] and
                   item.get('notes') == os.environ['INVOICE_LINE_NOTES']
                   for item in proved_invoice['line_items'])
        downloaded = client.get('/api/v1/documents/' + document['id'] + '/download')
        downloaded.raise_for_status()
        assert downloaded.content == document_bytes
        ping = client.get('/api/v1/ping')
        ping.raise_for_status()
        assert ping.headers['X-APP-VERSION'] == '{_EXACT_VERSION}'
        assert ping.json()['company_name'] == os.environ['COMPANY_NAME']
        print(json.dumps({{
            'client_id': client_row['id'],
            'invoice_id': invoice['id'],
            'document_id': document['id'],
            'document_name': document.get('name'),
            'document_url': document.get('url'),
            'document_size': document.get('size'),
        }}, sort_keys=True))

main()
"""
    result = _json_result(
        _runner(
            name=runner_name,
            image=runner_image,
            network=network,
            script=script,
            environment=(
                ("ORIGIN", triplet.origin),
                ("API_TOKEN", triplet.token),
                ("PHASE", phase),
                ("COMPANY_NAME", company_name),
                ("CLIENT_NAME", client_name),
                ("CLIENT_ID_NUMBER", client_id_number),
                ("CONTACT_EMAIL", contact_email),
                ("INVOICE_NUMBER", invoice_number),
                ("INVOICE_PUBLIC_NOTES", invoice_public_notes),
                ("INVOICE_PRIVATE_NOTES", invoice_private_notes),
                ("INVOICE_PRODUCT_KEY", invoice_product_key),
                ("INVOICE_LINE_NOTES", invoice_line_notes),
                ("DOCUMENT_NAME", document_name),
                ("DOCUMENT_BYTES", base64.b64encode(document_bytes).decode()),
            ),
        )
    )
    assert result["document_size"] == len(document_bytes)
    return _PhaseEvidence(
        phase=phase,
        company_name=company_name,
        client_name=client_name,
        client_id_number=client_id_number,
        contact_email=contact_email,
        invoice_number=invoice_number,
        invoice_public_notes=invoice_public_notes,
        invoice_private_notes=invoice_private_notes,
        invoice_product_key=invoice_product_key,
        invoice_line_notes=invoice_line_notes,
        document_name=document_name,
        document_bytes=document_bytes,
        client_id=cast(str, result["client_id"]),
        invoice_id=cast(str, result["invoice_id"]),
        document_id=cast(str, result["document_id"]),
    )


def _run_backup(
    *,
    runner_image: str,
    runner_name: str,
    network: str,
    triplet: _Triplet,
    artifact_root: Path,
    target_slug: str,
    phase: str,
) -> Path:
    script = f"""
import asyncio
import json
import os
from app.core.plugins.artifacts import validate_backup_artifact
from app.core.plugins.base import BackupContext
from app.core.plugins.loader import get_plugin
{_RUNNER_GUARDS}

async def main():
    assert_runner_guards()
    plugin = get_plugin('invoiceninja')
    config = {{
        'base_url': os.environ['SOURCE_ORIGIN'],
        'token': os.environ['SOURCE_TOKEN'],
        'export_timeout_seconds': 300,
    }}
    assert await plugin.test(config) is True
    context = BackupContext(
        job_id=os.environ['JOB_ID'],
        target_id='invoice-ninja-source',
        config=config,
        metadata={{'target_slug': os.environ['TARGET_SLUG']}},
    )
    result = await plugin.backup(context)
    validated = validate_backup_artifact(result['artifact_path'], plugin, context)
    print(json.dumps({{
        'artifact_path': result['artifact_path'],
        'artifact_bytes': validated.size_bytes,
        'sha256': validated.sha256,
    }}, sort_keys=True))

asyncio.run(main())
"""
    result = _json_result(
        _runner(
            name=runner_name,
            image=runner_image,
            network=network,
            script=script,
            environment=(
                ("SOURCE_ORIGIN", triplet.origin),
                ("SOURCE_TOKEN", triplet.token),
                ("JOB_ID", f"invoiceninja-{target_slug}-{phase}"),
                ("TARGET_SLUG", target_slug),
            ),
            mounts=((artifact_root, "/backups", True),),
            timeout=900,
        )
    )
    runner_path = Path(cast(str, result["artifact_path"]))
    artifact = artifact_root / runner_path.relative_to("/backups")
    assert artifact.is_file() and not artifact.is_symlink()
    assert artifact.stat().st_size == result["artifact_bytes"]
    assert _sha256(artifact) == result["sha256"]
    assert stat.S_IMODE(artifact.stat().st_mode) == 0o600
    sidecar_path = Path(str(artifact) + ".meta.json")
    assert stat.S_IMODE(sidecar_path.stat().st_mode) == 0o600
    sidecar = read_backup_sidecar(str(artifact))
    assert sidecar is not None
    assert sidecar["plugin_name"] == "invoiceninja"
    assert sidecar["target_slug"] == target_slug
    assert sidecar["artifact_bytes"] == artifact.stat().st_size
    assert sidecar["sha256"] == _sha256(artifact)
    assert sidecar["application_version"] == _EXACT_VERSION
    assert sidecar["validation"] == "passed"
    serialized_sidecar = json.dumps(sidecar, sort_keys=True)
    for synthetic_secret in _SYNTHETIC_SECRETS:
        assert synthetic_secret not in serialized_sidecar
    return artifact


def _inspect_artifact(
    artifact: Path,
    *,
    expected: tuple[_PhaseEvidence, ...],
    forbidden: tuple[_PhaseEvidence, ...],
) -> dict[str, Any]:
    sidecar = read_backup_sidecar(str(artifact))
    assert sidecar is not None
    created_at = sidecar.get("created_at")
    assert isinstance(created_at, str) and created_at
    with zipfile.ZipFile(artifact) as archive:
        assert archive.testzip() is None
        backup = json.loads(archive.read("backup.json"))
        assert backup["app_version"] == _EXACT_VERSION
        client_rows = backup["clients"]
        client_contact_rows = backup["client_contacts"]
        invoice_rows = backup["invoices"]
        document_rows = backup["documents"]
        for phase in expected:
            client = next(row for row in client_rows if row.get("name") == phase.client_name)
            assert client["id_number"] == phase.client_id_number
            assert isinstance(client["id"], int)
            assert isinstance(client["hashed_id"], str) and client["hashed_id"]
            contact = next(
                row for row in client_contact_rows if row.get("email") == phase.contact_email
            )
            assert contact["client_id"] == client["hashed_id"]
            invoice = next(row for row in invoice_rows if row.get("number") == phase.invoice_number)
            assert invoice["client_id"] == client["hashed_id"]
            assert invoice["public_notes"] == phase.invoice_public_notes
            assert invoice["private_notes"] == phase.invoice_private_notes
            assert any(
                line.get("product_key") == phase.invoice_product_key
                and line.get("notes") == phase.invoice_line_notes
                for line in invoice["line_items"]
            )
            # The native JSON export rewrites the API's hashed document ID to
            # its migration-local integer ID. The stable supported marker is
            # the exact synthetic name plus independently proven bytes.
            document = next(row for row in document_rows if row.get("name") == phase.document_name)
            member = "documents/" + document["url"]
            assert archive.read(member) == phase.document_bytes
            assert document["size"] == len(phase.document_bytes)
        for phase in forbidden:
            assert all(row.get("name") != phase.client_name for row in client_rows)
            assert all(row.get("email") != phase.contact_email for row in client_contact_rows)
            assert all(row.get("number") != phase.invoice_number for row in invoice_rows)
            assert all(row.get("name") != phase.document_name for row in document_rows)
        return {
            "company_name": backup["company"]["settings"]["name"],
            "client_count": len(client_rows),
            "invoice_count": len(invoice_rows),
            "document_count": len(document_rows),
            "bytes": artifact.stat().st_size,
            "sha256": _sha256(artifact),
            "created_at": created_at,
        }


def _write_negative_archive(source: Path, destination: Path, case: str) -> None:
    with zipfile.ZipFile(source) as source_archive:
        backup = json.loads(source_archive.read("backup.json"))
        document_member = "documents/" + backup["documents"][0]["url"]
        with zipfile.ZipFile(destination, "w") as destination_archive:
            for member in source_archive.infolist():
                if case == "missing-document" and member.filename == document_member:
                    continue
                payload = source_archive.read(member)
                if case == "wrong-version" and member.filename == "backup.json":
                    backup["app_version"] = "5.13.30"
                    payload = json.dumps(backup, separators=(",", ":")).encode()
                destination_archive.writestr(member, payload)
    destination.chmod(0o600)


def _assert_exact_archive_negatives(artifact: Path, negative_root: Path) -> None:
    negative_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    plugin = InvoiceNinjaPlugin("invoiceninja")
    corrupt = negative_root / "corrupt.zip"
    payload = artifact.read_bytes()
    corrupt.write_bytes(payload[:-17])
    corrupt.chmod(0o600)
    wrong_version = negative_root / "wrong-version.zip"
    missing_document = negative_root / "missing-document.zip"
    _write_negative_archive(artifact, wrong_version, "wrong-version")
    _write_negative_archive(artifact, missing_document, "missing-document")

    with pytest.raises(RuntimeError, match="ZIP archive boundary|valid ZIP"):
        plugin._validate_export(corrupt)
    with pytest.raises(RuntimeError, match="wrong application version"):
        plugin._validate_export(wrong_version)
    with pytest.raises(RuntimeError, match="incomplete document data"):
        plugin._validate_export(missing_document)


def _assert_signed_url_runtime_negatives(
    *,
    runner_image: str,
    runner_name: str,
    network: str,
    artifact_root: Path,
    drill_round: int,
) -> dict[str, Any]:
    script = f"""
import asyncio
import json
import os
import httpx
from app.core.plugins.base import BackupContext
from app.plugins.invoiceninja import plugin as plugin_module
from app.plugins.invoiceninja.plugin import InvoiceNinjaPlugin
{_RUNNER_GUARDS}

async def run_case(case):
    token = os.environ['SYNTHETIC_TOKEN']
    base_url = 'http://invoice-exact.local'

    def handler(request):
        if request.url.path == '/api/v1/ping':
            assert request.headers['X-API-TOKEN'] == token
            return httpx.Response(
                200,
                headers={{'X-APP-VERSION': '{_EXACT_VERSION}'}},
                json={{'company_name': 'Synthetic company', 'user_name': 'Synthetic user'}},
            )
        if request.url.path == '/api/v1/export':
            assert request.headers['X-API-TOKEN'] == token
            origin = 'http://attacker.invalid' if case == 'cross-origin' else base_url
            return httpx.Response(200, json={{
                'message': 'Processing',
                'url': origin + '/api/v1/protected_download/'
                       '123e4567-e89b-42d3-a456-426614174000'
                       '?expires=1&signature=expired-synthetic-signature',
            }})
        assert case == 'expired' and request.url.path.startswith('/api/v1/protected_download/')
        assert 'X-API-TOKEN' not in request.headers
        return httpx.Response(403)

    transport = httpx.MockTransport(handler)
    original_client = plugin_module.httpx.AsyncClient

    def client(*args, **kwargs):
        kwargs['transport'] = transport
        return original_client(*args, **kwargs)

    plugin_module.httpx.AsyncClient = client
    plugin = InvoiceNinjaPlugin('invoiceninja')
    try:
        await plugin.backup(BackupContext(
            job_id='signed-url-negative-' + case,
            target_id='synthetic-source',
            config={{'base_url': base_url, 'token': token, 'export_timeout_seconds': 60}},
            metadata={{'target_slug': 'invoiceninja-round-{drill_round}-signed-url-negative-' + case}},
        ))
    except Exception as exc:
        return {{'case': case, 'failed': True, 'type': type(exc).__name__,
                'message': str(exc)}}
    finally:
        plugin_module.httpx.AsyncClient = original_client
    return {{'case': case, 'failed': False}}

async def main():
    assert_runner_guards()
    results = [await run_case(case) for case in ('cross-origin', 'expired')]
    assert all(item['failed'] for item in results), results
    assert 'same origin' in results[0]['message'].lower(), results
    assert 'expired' in results[1]['message'].lower(), results
    print(json.dumps({{'results': results}}, sort_keys=True))

asyncio.run(main())
"""
    token = _secret_urlsafe()
    result = _json_result(
        _runner(
            name=runner_name,
            image=runner_image,
            network=network,
            script=script,
            environment=(("SYNTHETIC_TOKEN", token),),
            mounts=((artifact_root, "/backups", True),),
        )
    )
    assert token not in json.dumps(result, sort_keys=True)
    for case in ("cross-origin", "expired"):
        negative_root = (
            artifact_root / f"invoiceninja-round-{drill_round}-signed-url-negative-{case}"
        )
        if negative_root.exists():
            assert not [path for path in negative_root.rglob("*") if path.is_file()]
    return result


def _restore_with_service(
    *,
    runner_image: str,
    runner_name: str,
    network: str,
    source: _Triplet,
    destination: _Triplet,
    artifact_root: Path,
    artifact: Path,
    source_target_slug: str,
    authorize: bool,
    expect_partial: bool,
    source_origin_override: str | None = None,
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
from app.models import Run, Target, TargetRun
from app.services.restores import RestoreService
{_RUNNER_GUARDS}

def digest(path):
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()

def main():
    assert_runner_guards()
    artifact = Path('/backups/{relative_artifact.as_posix()}')
    engine = create_engine('sqlite://', connect_args={{'check_same_thread': False}},
                           poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
    try:
        source = Target(
            name='Synthetic Invoice Ninja source',
            slug=os.environ['SOURCE_TARGET_SLUG'],
            plugin_name='invoiceninja',
            plugin_config_json=json.dumps({{
                'base_url': os.environ['SOURCE_ORIGIN'],
                'token': 'unused-source-token',
            }}),
        )
        destination = Target(
            name='Synthetic Invoice Ninja destination',
            slug='invoiceninja-restore',
            plugin_name='invoiceninja',
            plugin_config_json=json.dumps({{
                'base_url': os.environ['DESTINATION_ORIGIN'],
                'token': os.environ['DESTINATION_TOKEN'],
                'export_timeout_seconds': 300,
            }}),
        )
        session.add_all([source, destination])
        session.commit()
        backup_run = Run(
            status='success', operation='backup',
            started_at=datetime.now(timezone.utc), finished_at=datetime.now(timezone.utc),
        )
        session.add(backup_run)
        session.commit()
        source_target_run = TargetRun(
            run_id=backup_run.id,
            target_id=source.id,
            status='success',
            operation='backup',
            artifact_path=str(artifact),
            artifact_bytes=artifact.stat().st_size,
            sha256=digest(artifact),
            source_identity_json=json.dumps({{'base_url': os.environ['SOURCE_ORIGIN']}}),
            started_at=backup_run.started_at,
            finished_at=backup_run.finished_at,
        )
        session.add(source_target_run)
        session.commit()
        try:
            restored = RestoreService(session).restore(
                source_target_run_id=source_target_run.id,
                destination_target_id=destination.id,
                triggered_by='isolated_invoiceninja_exact_drill',
            )
        except Exception as exc:
            print(json.dumps({{
                'failed': True,
                'type': type(exc).__name__,
                'message': str(exc),
            }}, sort_keys=True))
            return
        target_run = restored.target_runs[0]
        print(json.dumps({{
            'failed': False,
            'status': restored.status,
            'target_status': target_run.status,
            'message': target_run.message,
            'logs': target_run.logs_text,
        }}, sort_keys=True))
    finally:
        session.close()

main()
"""
    environment = [
        ("BACKUP_BASE_PATH", "/backups"),
        ("SOURCE_TARGET_SLUG", source_target_slug),
        ("SOURCE_ORIGIN", source_origin_override or source.origin),
        ("DESTINATION_ORIGIN", destination.origin),
        ("DESTINATION_TOKEN", destination.token),
    ]
    if authorize:
        environment.extend(
            (
                (_RESTORE_ENABLE_ENV, "1"),
                (_RESTORE_ORIGINS_ENV, destination.origin),
            )
        )
    before = (
        artifact.stat().st_dev,
        artifact.stat().st_ino,
        artifact.stat().st_size,
        _sha256(artifact),
    )
    result = _json_result(
        _runner(
            name=runner_name,
            image=runner_image,
            network=network,
            script=script,
            environment=environment,
            mounts=((artifact_root, "/backups", True),),
            timeout=900,
        )
    )
    assert (
        artifact.stat().st_dev,
        artifact.stat().st_ino,
        artifact.stat().st_size,
        _sha256(artifact),
    ) == before
    assert not list(artifact.parent.glob(".homelab-backup-restore-*"))
    public_result = json.dumps(result, sort_keys=True)
    for synthetic_secret in _SYNTHETIC_SECRETS:
        assert synthetic_secret not in public_result
    if expect_partial:
        assert result["failed"] is False, result
        assert result["status"] == "partial", result
        assert result["target_status"] == "partial", result
        assert "document" in result["message"].lower(), result
        assert "terminal" in result["message"].lower(), result
    else:
        assert result["failed"] is True, result
    return result


def _inspect_destination(
    *,
    runner_image: str,
    runner_name: str,
    network: str,
    destination: _Triplet,
    expected: tuple[_PhaseEvidence, ...],
    forbidden: tuple[_PhaseEvidence, ...],
) -> dict[str, Any]:
    script = f"""
import json
import os
import time
import httpx
{_RUNNER_GUARDS}

def rows(client, resource, include=None):
    params = {{'per_page': '100'}}
    if include:
        params['include'] = include
    response = client.get('/api/v1/' + resource, params=params)
    response.raise_for_status()
    payload = response.json()
    assert isinstance(payload, dict) and isinstance(payload.get('data'), list), payload
    return payload['data']

def main():
    assert_runner_guards()
    expected = json.loads(os.environ['EXPECTED_MARKERS'])
    forbidden = json.loads(os.environ['FORBIDDEN_MARKERS'])
    headers = {{
        'X-API-Token': os.environ['API_TOKEN'],
        'X-Requested-With': 'XMLHttpRequest',
        'Accept': 'application/json',
    }}
    deadline = time.monotonic() + 300
    last = None
    with httpx.Client(base_url=os.environ['ORIGIN'], headers=headers, timeout=30,
                      follow_redirects=False) as client:
        while time.monotonic() < deadline:
            ping = client.get('/api/v1/ping')
            clients = rows(client, 'clients', 'contacts')
            invoices = rows(client, 'invoices')
            company_ok = ping.status_code == 200 and ping.json().get('company_name') == expected[-1]['company_name']
            clients_ok = all(any(row.get('name') == marker['client_name'] and
                                 row.get('id_number') == marker['client_id_number'] and
                                 any(contact.get('email') == marker['contact_email']
                                     for contact in row.get('contacts', []))
                                 for row in clients) for marker in expected)
            invoices_ok = True
            for marker in expected:
                matching_clients = [row for row in clients
                                    if row.get('name') == marker['client_name']]
                matching_invoices = [row for row in invoices
                                     if row.get('number') == marker['invoice_number']]
                if len(matching_clients) != 1 or len(matching_invoices) != 1:
                    invoices_ok = False
                    break
                restored_client = matching_clients[0]
                restored_invoice = matching_invoices[0]
                if not (
                    restored_invoice.get('client_id') == restored_client.get('id') and
                    restored_invoice.get('public_notes') == marker['invoice_public_notes'] and
                    restored_invoice.get('private_notes') == marker['invoice_private_notes'] and
                    any(item.get('product_key') == marker['invoice_product_key'] and
                        item.get('notes') == marker['invoice_line_notes']
                        for item in restored_invoice.get('line_items', []))
                ):
                    invoices_ok = False
                    break
            forbidden_ok = all(
                all(row.get('name') != marker['client_name'] and
                    all(contact.get('email') != marker['contact_email']
                        for contact in row.get('contacts', []))
                    for row in clients) and
                all(row.get('number') != marker['invoice_number'] for row in invoices)
                for marker in forbidden
            )
            last = {{'company_ok': company_ok, 'clients_ok': clients_ok,
                    'invoices_ok': invoices_ok, 'forbidden_ok': forbidden_ok}}
            if all(last.values()):
                break
            time.sleep(2)
        assert last is not None and all(last.values()), last

        documents = rows(client, 'documents')
        restored_document_names = []
        downloadable_document_names = []
        for document in documents:
            name = document.get('name')
            if any(name == marker['document_name'] for marker in expected):
                restored_document_names.append(name)
                response = client.get('/api/v1/documents/' + document['id'] + '/download')
                if response.status_code == 200:
                    downloadable_document_names.append(name)
        # Exact 5.13.31 accepts the logical import but its private-source SSRF guard
        # skips embedded document bytes. Never credit document recovery here.
        assert not downloadable_document_names, downloadable_document_names
        print(json.dumps({{
            **last,
            'document_metadata_count': len(restored_document_names),
            'document_download_count': len(downloadable_document_names),
        }}, sort_keys=True))

main()
"""
    expected_payload = [
        {
            "company_name": item.company_name,
            "client_name": item.client_name,
            "client_id_number": item.client_id_number,
            "contact_email": item.contact_email,
            "invoice_number": item.invoice_number,
            "invoice_public_notes": item.invoice_public_notes,
            "invoice_private_notes": item.invoice_private_notes,
            "invoice_product_key": item.invoice_product_key,
            "invoice_line_notes": item.invoice_line_notes,
            "document_name": item.document_name,
        }
        for item in expected
    ]
    forbidden_payload = [
        {
            "client_name": item.client_name,
            "contact_email": item.contact_email,
            "invoice_number": item.invoice_number,
        }
        for item in forbidden
    ]
    return _json_result(
        _runner(
            name=runner_name,
            image=runner_image,
            network=network,
            script=script,
            environment=(
                ("ORIGIN", destination.origin),
                ("API_TOKEN", destination.token),
                ("EXPECTED_MARKERS", json.dumps(expected_payload, separators=(",", ":"))),
                ("FORBIDDEN_MARKERS", json.dumps(forbidden_payload, separators=(",", ":"))),
            ),
            timeout=420,
        )
    )


def _stop_triplet(triplet: _Triplet) -> None:
    for container in (triplet.web, triplet.app, triplet.database):
        _docker("rm", "-f", container, check=False)
    for volume in (triplet.public_volume, triplet.storage_volume, triplet.database_volume):
        _docker("volume", "rm", "-f", volume, check=False)


def _assert_bad_credentials_refused(
    *,
    runner_image: str,
    runner_name: str,
    network: str,
    source: _Triplet,
) -> None:
    wrong_token = _secret_urlsafe()
    script = f"""
import asyncio
import json
import os
from app.core.plugins.loader import get_plugin
{_RUNNER_GUARDS}

async def main():
    assert_runner_guards()
    try:
        await get_plugin('invoiceninja').test({{
            'base_url': os.environ['SOURCE_ORIGIN'],
            'token': os.environ['WRONG_TOKEN'],
        }})
    except Exception as exc:
        print(json.dumps({{'refused': True, 'type': type(exc).__name__,
                          'message': str(exc)}}, sort_keys=True))
        return
    print(json.dumps({{'refused': False}}, sort_keys=True))

asyncio.run(main())
"""
    result = _json_result(
        _runner(
            name=runner_name,
            image=runner_image,
            network=network,
            script=script,
            environment=(("SOURCE_ORIGIN", source.origin), ("WRONG_TOKEN", wrong_token)),
        )
    )
    assert result["refused"] is True, result
    assert result["type"] == "RuntimeError", result
    assert "status" in result["message"].lower(), result
    assert wrong_token not in json.dumps(result, sort_keys=True)


def _assert_fresh_destination(
    *,
    runner_image: str,
    runner_name: str,
    network: str,
    destination: _Triplet,
) -> None:
    script = f"""
import json
import os
import httpx
{_RUNNER_GUARDS}

def main():
    assert_runner_guards()
    headers = {{'X-API-Token': os.environ['API_TOKEN'],
               'X-Requested-With': 'XMLHttpRequest', 'Accept': 'application/json'}}
    resources = ('clients', 'invoices', 'payments', 'projects', 'quotes', 'expenses',
                 'vendors', 'products', 'tasks', 'documents')
    with httpx.Client(base_url=os.environ['ORIGIN'], headers=headers, timeout=30,
                      follow_redirects=False) as client:
        counts = {{}}
        for resource in resources:
            response = client.get('/api/v1/' + resource, params={{'per_page': '1'}})
            response.raise_for_status()
            payload = response.json()
            rows = payload.get('data')
            assert isinstance(rows, list), payload
            counts[resource] = len(rows)
        assert not any(counts.values()), counts
        print(json.dumps(counts, sort_keys=True))

main()
"""
    _json_result(
        _runner(
            name=runner_name,
            image=runner_image,
            network=network,
            script=script,
            environment=(("ORIGIN", destination.origin), ("API_TOKEN", destination.token)),
        )
    )


def _planned_triplet_names(prefix: str) -> tuple[set[str], set[str]]:
    return (
        {f"{prefix}-mysql", f"{prefix}-app", f"{prefix}-web"},
        {f"{prefix}-public", f"{prefix}-storage", f"{prefix}-database"},
    )


def test_exact_invoice_ninja_drill_contract_is_immutable_and_local_only() -> None:
    assert _APP_IMAGE.endswith(
        "sha256:5c051fd2a7914b05deb759556ba1a7959a86a22a8ffff488267f7cdd00713217"
    )
    assert _MYSQL_IMAGE.endswith(
        "sha256:53a71a83be1fcbb1489c0fe23d377297f55b77a6c6ce816ca8fa30225adfe2df"
    )
    assert _NGINX_IMAGE.endswith(
        "sha256:963cfe6e75d1c292f66589d7e190b137cf89310414c0c1c5b476dfc61a4fcd0d"
    )
    assert _EXACT_VERSION == "5.13.31"
    assert "hollinger.asia" not in " ".join((_APP_IMAGE, _MYSQL_IMAGE, _NGINX_IMAGE))
    diagnostic_token = "synthetic-login-diagnostic-token"
    login_result = {
        "status": 200,
        "version": "unexpected-version",
        "body": {"data": [{"token": {"token": diagnostic_token}}]},
    }
    assert _capture_login_token(login_result) == diagnostic_token
    assert diagnostic_token not in json.dumps(_login_diagnostic(login_result))
    _SYNTHETIC_SECRETS.clear()


@pytest.mark.parametrize("failure_kind", ("called-process", "timeout"))
def test_docker_command_failures_redact_the_complete_exception_chain(
    monkeypatch: pytest.MonkeyPatch,
    failure_kind: str,
) -> None:
    secret = f"synthetic-{failure_kind}-secret-must-not-escape"
    _SYNTHETIC_SECRETS.add(secret)

    def fail(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        command = ["docker", "run", "--env", f"TOKEN={secret}"]
        if failure_kind == "called-process":
            raise subprocess.CalledProcessError(
                1,
                command,
                output=f"stdout {secret}",
                stderr=f"stderr {secret}",
            )
        raise subprocess.TimeoutExpired(
            command,
            1,
            output=f"stdout {secret}",
            stderr=f"stderr {secret}",
        )

    monkeypatch.setattr(subprocess, "run", fail)
    try:
        with pytest.raises(RuntimeError) as caught:
            _docker("run", "--env", f"TOKEN={secret}")
        rendered = "".join(
            traceback.format_exception(
                type(caught.value),
                caught.value,
                caught.value.__traceback__,
                chain=True,
            )
        )
        assert secret not in rendered
        assert "[REDACTED]" in rendered
        assert caught.value.__cause__ is None
    finally:
        _SYNTHETIC_SECRETS.clear()


@_DOCKER_DRILL_SKIP
def test_two_native_backups_restore_partially_to_fresh_exact_images_twice(
    tmp_path: Path,
) -> None:
    assert shutil.which("docker") is not None
    _SYNTHETIC_SECRETS.clear()
    _RUNNER_CONTAINERS.clear()
    suffix = uuid.uuid4().hex[:10]
    runner_image = f"codex-homelab-backup-invoiceninja-runner:{suffix}"
    containers: set[str] = set()
    volumes: set[str] = set()
    networks: set[str] = set()
    all_networks: set[str] = set()
    completed_rounds: list[dict[str, Any]] = []

    try:
        for image in (_APP_IMAGE, _MYSQL_IMAGE, _NGINX_IMAGE):
            if _docker("image", "inspect", image, check=False).returncode != 0:
                _docker("pull", "--platform", "linux/amd64", image, timeout=1200)
        _docker("build", "-t", runner_image, str(_BACKEND_ROOT), timeout=1200)

        for drill_round in (1, 2):
            round_root = tmp_path / f"round-{drill_round}"
            artifact_root = round_root / "artifacts"
            artifact_root.mkdir(mode=0o700, parents=True)
            network = f"codex-invoiceninja-round-{drill_round}-{suffix}"
            networks.add(network)
            all_networks.add(network)
            _docker("network", "create", "--internal", "--label", f"{_LABEL}=1", network)

            source_prefix = f"codex-invoiceninja-source-{drill_round}-{suffix}"
            names, named_volumes = _planned_triplet_names(source_prefix)
            containers.update(names)
            volumes.update(named_volumes)
            source = _start_triplet(
                prefix=source_prefix,
                network=network,
                round_root=round_root,
                runner_image=runner_image,
                suffix=f"source-{drill_round}-{suffix}",
            )
            _assert_bad_credentials_refused(
                runner_image=runner_image,
                runner_name=f"codex-invoiceninja-bad-auth-{drill_round}-{suffix}",
                network=network,
                source=source,
            )

            phase_a = _seed_phase(
                runner_image=runner_image,
                runner_name=f"codex-invoiceninja-seed-{drill_round}-a-{suffix}",
                network=network,
                triplet=source,
                marker=f"R{drill_round}-A-{suffix}",
                phase="A",
            )
            target_slug = f"invoiceninja-round-{drill_round}"
            artifact_a = _run_backup(
                runner_image=runner_image,
                runner_name=f"codex-invoiceninja-backup-{drill_round}-a-{suffix}",
                network=network,
                triplet=source,
                artifact_root=artifact_root,
                target_slug=target_slug,
                phase="a",
            )
            # Ensure the vendor export and sidecar clocks cannot collapse A/B
            # into one timestamp even on a very fast local engine.
            time.sleep(1.1)
            phase_b = _seed_phase(
                runner_image=runner_image,
                runner_name=f"codex-invoiceninja-seed-{drill_round}-b-{suffix}",
                network=network,
                triplet=source,
                marker=f"R{drill_round}-B-{suffix}",
                phase="B",
            )
            artifact_b = _run_backup(
                runner_image=runner_image,
                runner_name=f"codex-invoiceninja-backup-{drill_round}-b-{suffix}",
                network=network,
                triplet=source,
                artifact_root=artifact_root,
                target_slug=target_slug,
                phase="b",
            )

            evidence_a = _inspect_artifact(
                artifact_a,
                expected=(phase_a,),
                forbidden=(phase_b,),
            )
            evidence_b = _inspect_artifact(
                artifact_b,
                expected=(phase_a, phase_b),
                forbidden=(),
            )
            assert evidence_a["company_name"] == phase_a.company_name
            assert evidence_b["company_name"] == phase_b.company_name
            assert artifact_a != artifact_b
            assert evidence_a["sha256"] != evidence_b["sha256"]
            assert evidence_a["bytes"] != evidence_b["bytes"]
            assert evidence_a["created_at"] != evidence_b["created_at"]
            _assert_exact_archive_negatives(
                artifact_b,
                round_root / "negative-archives",
            )
            _assert_signed_url_runtime_negatives(
                runner_image=runner_image,
                runner_name=f"codex-invoiceninja-signed-url-negatives-{drill_round}-{suffix}",
                network=network,
                artifact_root=artifact_root,
                drill_round=drill_round,
            )

            destination_evidence: list[dict[str, Any]] = []
            for phase_index, (artifact, expected, forbidden) in enumerate(
                (
                    (artifact_a, (phase_a,), (phase_b,)),
                    (artifact_b, (phase_a, phase_b), ()),
                ),
                start=1,
            ):
                destination_prefix = (
                    f"codex-invoiceninja-destination-{drill_round}-{phase_index}-{suffix}"
                )
                names, named_volumes = _planned_triplet_names(destination_prefix)
                containers.update(names)
                volumes.update(named_volumes)
                destination = _start_triplet(
                    prefix=destination_prefix,
                    network=network,
                    round_root=round_root,
                    runner_image=runner_image,
                    suffix=f"destination-{drill_round}-{phase_index}-{suffix}",
                )
                _assert_fresh_destination(
                    runner_image=runner_image,
                    runner_name=(f"codex-invoiceninja-fresh-{drill_round}-{phase_index}-{suffix}"),
                    network=network,
                    destination=destination,
                )

                if phase_index == 1:
                    same_origin = _restore_with_service(
                        runner_image=runner_image,
                        runner_name=f"codex-invoiceninja-same-origin-{drill_round}-{suffix}",
                        network=network,
                        source=source,
                        destination=destination,
                        artifact_root=artifact_root,
                        artifact=artifact,
                        source_target_slug=target_slug,
                        authorize=True,
                        expect_partial=False,
                        source_origin_override=destination.origin,
                    )
                    assert "origins must be different" in same_origin["message"].lower()
                    _assert_fresh_destination(
                        runner_image=runner_image,
                        runner_name=(
                            f"codex-invoiceninja-same-origin-still-fresh-" f"{drill_round}-{suffix}"
                        ),
                        network=network,
                        destination=destination,
                    )
                    refusal = _restore_with_service(
                        runner_image=runner_image,
                        runner_name=f"codex-invoiceninja-unauthorized-{drill_round}-{suffix}",
                        network=network,
                        source=source,
                        destination=destination,
                        artifact_root=artifact_root,
                        artifact=artifact,
                        source_target_slug=target_slug,
                        authorize=False,
                        expect_partial=False,
                    )
                    assert "isolated local drill" in refusal["message"].lower(), refusal
                    _assert_fresh_destination(
                        runner_image=runner_image,
                        runner_name=(f"codex-invoiceninja-still-fresh-{drill_round}-{suffix}"),
                        network=network,
                        destination=destination,
                    )

                _restore_with_service(
                    runner_image=runner_image,
                    runner_name=(
                        f"codex-invoiceninja-restore-{drill_round}-{phase_index}-{suffix}"
                    ),
                    network=network,
                    source=source,
                    destination=destination,
                    artifact_root=artifact_root,
                    artifact=artifact,
                    source_target_slug=target_slug,
                    authorize=True,
                    expect_partial=True,
                )
                before_restart = _inspect_destination(
                    runner_image=runner_image,
                    runner_name=(
                        f"codex-invoiceninja-inspect-{drill_round}-{phase_index}-{suffix}"
                    ),
                    network=network,
                    destination=destination,
                    expected=expected,
                    forbidden=forbidden,
                )
                _restart_exact_triplet(
                    triplet=destination,
                    network=network,
                    runner_image=runner_image,
                    runner_name_prefix=(
                        f"codex-invoiceninja-restart-ready-" f"{drill_round}-{phase_index}-{suffix}"
                    ),
                )
                after_restart = _inspect_destination(
                    runner_image=runner_image,
                    runner_name=(
                        f"codex-invoiceninja-inspect-restarted-"
                        f"{drill_round}-{phase_index}-{suffix}"
                    ),
                    network=network,
                    destination=destination,
                    expected=expected,
                    forbidden=forbidden,
                )
                destination_evidence.append(
                    {
                        "before_restart": before_restart,
                        "after_restart": after_restart,
                    }
                )

                if phase_index == 1:
                    nonfresh = _restore_with_service(
                        runner_image=runner_image,
                        runner_name=f"codex-invoiceninja-nonfresh-{drill_round}-{suffix}",
                        network=network,
                        source=source,
                        destination=destination,
                        artifact_root=artifact_root,
                        artifact=artifact,
                        source_target_slug=target_slug,
                        authorize=True,
                        expect_partial=False,
                    )
                    assert "not fresh" in nonfresh["message"].lower(), nonfresh
                    _inspect_destination(
                        runner_image=runner_image,
                        runner_name=(
                            f"codex-invoiceninja-nonfresh-unchanged-" f"{drill_round}-{suffix}"
                        ),
                        network=network,
                        destination=destination,
                        expected=expected,
                        forbidden=forbidden,
                    )
                _stop_triplet(destination)

            completed_rounds.append(
                {
                    "round": drill_round,
                    "artifact_a": evidence_a,
                    "artifact_b": evidence_b,
                    "destinations": destination_evidence,
                }
            )
            _stop_triplet(source)
            _docker("network", "rm", network)
            networks.discard(network)

        assert len(completed_rounds) == 2
        assert {item["round"] for item in completed_rounds} == {1, 2}
        assert all(
            destination[stage]["document_download_count"] == 0
            for item in completed_rounds
            for destination in item["destinations"]
            for stage in ("before_restart", "after_restart")
        )
    finally:
        containers.update(_RUNNER_CONTAINERS)
        for container in sorted(containers):
            _docker("rm", "-f", container, check=False)
        for network in sorted(networks):
            _docker("network", "rm", network, check=False)
        for volume in sorted(volumes):
            _docker("volume", "rm", "-f", volume, check=False)
        _docker("image", "rm", runner_image, check=False)
        if tmp_path.exists():
            shutil.rmtree(tmp_path)

        # These assertions execute on success, failure, and interruption. With
        # every tracked container gone, no disposable HTTP/FPM/MySQL listener or
        # runner can survive the drill. Removing the test root also removes the
        # secret-bearing native artifacts, vhosts, and synthetic credential data.
        assert not any(
            _docker("inspect", container, check=False).returncode == 0 for container in containers
        )
        assert not any(
            _docker("network", "inspect", network, check=False).returncode == 0
            for network in all_networks
        )
        assert not any(
            _docker("volume", "inspect", volume, check=False).returncode == 0 for volume in volumes
        )
        assert _docker("image", "inspect", runner_image, check=False).returncode != 0
        assert not tmp_path.exists()
        _SYNTHETIC_SECRETS.clear()
        assert not _SYNTHETIC_SECRETS
