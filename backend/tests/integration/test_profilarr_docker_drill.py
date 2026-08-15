from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any, Iterator, cast

import pytest
import yaml  # type: ignore[import-untyped]

from app.core.plugins.sidecar import read_backup_sidecar

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_PROFILARR_DOCKER_DRILL") != "1",
    reason="set RUN_PROFILARR_DOCKER_DRILL=1 for the isolated Profilarr 1.1.5 drill",
)

_IMAGE = (
    "santiagosayshey/profilarr@"
    "sha256:4d37d6b2039697c842211d0879d4d6df19c1dcbd22a962ed67ba3de8f81dfdad"
)
_EXPECTED_IMAGE_TAG = "santiagosayshey/profilarr:v1.1.5"
_EXPECTED_IMAGE_DIGEST = "sha256:4d37d6b2039697c842211d0879d4d6df19c1dcbd22a962ed67ba3de8f81dfdad"
_EXPECTED_VERSION = "1.1.5"
_EXPECTED_SOURCE_COMMIT = "21c8eaeb93241588323672866854275ff7dbed67"
# The v1.1.5 image does not expose a version/revision endpoint or OCI revision
# label (its version label is only "beta"). Pinning this independently observed
# code-tree digest makes image/source drift visible in addition to the amd64
# manifest digest above.
_EXPECTED_APP_TREE_SHA256 = "98cf52a62dbcaeb0fd9fb662916bf79b385e9f096d4ab896766bcfa541910f58"
_EXPECTED_MIGRATIONS = [
    [1, "initial_schema"],
    [2, "format_renames"],
    [3, "language_import_score"],
    [4, "update_language_score_default"],
]
_EXPECTED_TABLES = {
    "arr_config",
    "auth",
    "backups",
    "failed_attempts",
    "format_renames",
    "language_import_config",
    "migrations",
    "scheduled_tasks",
    "settings",
    "sqlite_sequence",
}
_AUTHORITATIVE_DIRECTORIES = (
    "regex_patterns",
    "custom_formats",
    "profiles",
    "media_management",
)
_RESTORE_SENTINEL = ".profilarr-restore-destination"
_RESTORE_SENTINEL_CONTENT = "profilarr-v1.1.5-isolated-restore-v1\n"
_BACKEND_ROOT = Path(__file__).resolve().parents[2]
_USERNAME = "synthetic-profilarr-user"
_PASSWORD = "synthetic-profilarr-password"
_SOURCE_GIT_SECRET = "synthetic-source-git-secret"
_RADARR_KEY = "synthetic-radarr-key"
_SONARR_KEY = "synthetic-sonarr-key"
_SECRET_MARKERS = (
    _PASSWORD,
    _SOURCE_GIT_SECRET,
    _RADARR_KEY,
    _SONARR_KEY,
    "synthetic-profilarr-api-key",
)
_PUBLIC_FORBIDDEN = (
    *_SECRET_MARKERS,
    "http://arr-mock:8080",
    _USERNAME,
    "Synthetic Profile",
    "Synthetic Format",
    "Synthetic Regex",
    "Synthetic Radarr",
    "Synthetic Sonarr",
)


def _redact(text: str) -> str:
    redacted = text
    for secret in _PUBLIC_FORBIDDEN:
        redacted = redacted.replace(secret, "[REDACTED]")
    return redacted[-4000:]


def _assert_secret_free(text: str, *, surface: str, markers: tuple[str, ...]) -> None:
    leaked_marker_indexes = [index for index, marker in enumerate(markers) if marker in text]
    assert (
        not leaked_marker_indexes
    ), f"{surface} exposed {len(leaked_marker_indexes)} forbidden synthetic marker(s)"


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
        stderr = _redact(exc.stderr or "")
        stdout = _redact(exc.stdout or "")
        raise RuntimeError(
            f"Disposable Profilarr Docker command failed:\n{stderr}\n{stdout}"
        ) from exc


def _git(repository: Path, *arguments: str, check: bool = True) -> str:
    completed = subprocess.run(
        ("git", "-C", str(repository), *arguments),
        check=check,
        capture_output=True,
        text=True,
        timeout=60,
        env={
            **os.environ,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "LC_ALL": "C",
        },
    )
    return completed.stdout.strip()


def _sha256(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _json_result(completed: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    for line in reversed(completed.stdout.splitlines()):
        if line.startswith("{"):
            result = json.loads(line)
            assert isinstance(result, dict)
            return result
    raise AssertionError(f"Runner returned no JSON result: {_redact(completed.stderr)}")


def _request_json(
    container: str,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    *,
    authenticated: bool = True,
) -> tuple[int, Any]:
    # Read the API key in the exact application container and send request data
    # over stdin. The scrubber hashes all credential/URL-bearing response fields
    # before anything reaches pytest output.
    script = r"""
import hashlib, json, sqlite3, sys, urllib.error, urllib.request

request_data = json.load(sys.stdin)
headers = {'Content-Type': 'application/json'}
if request_data['authenticated']:
    with sqlite3.connect('/config/profilarr.db') as connection:
        row = connection.execute('SELECT api_key FROM auth').fetchone()
    assert row and row[0]
    headers['X-Api-Key'] = row[0]
body = None
if request_data['payload'] is not None:
    body = json.dumps(request_data['payload'], separators=(',', ':')).encode()
request = urllib.request.Request(
    'http://127.0.0.1:6868' + request_data['path'],
    data=body,
    headers=headers,
    method=request_data['method'],
)
try:
    response = urllib.request.urlopen(request, timeout=15)
    status = response.status
    raw = response.read()
except urllib.error.HTTPError as exc:
    status = exc.code
    raw = exc.read()
try:
    parsed = json.loads(raw or b'null')
except json.JSONDecodeError:
    parsed = {'body_sha256': hashlib.sha256(raw).hexdigest()}

private_keys = {
    'api_key', 'apikey', 'apiKey', 'password', 'password_hash', 'session_id',
    'secret_key', 'arrServer', 'gitRepo', 'url',
}
def scrub(value):
    if isinstance(value, dict):
        return {
            key: (
                {'sha256': hashlib.sha256(str(item).encode()).hexdigest()}
                if key in private_keys and item is not None
                else scrub(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [scrub(item) for item in value]
    return value

print(json.dumps({'status': status, 'body': scrub(parsed)}, sort_keys=True))
"""
    request = {
        "authenticated": authenticated,
        "method": method,
        "path": path,
        "payload": payload,
    }
    completed = _docker(
        "exec",
        "-i",
        container,
        "python",
        "-c",
        script,
        input_text=json.dumps(request, separators=(",", ":")),
    )
    result = _json_result(completed)
    return cast(int, result["status"]), result["body"]


def _wait_for_profilarr(container: str) -> None:
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        state = _docker(
            "inspect", "--format", "{{.State.Status}}", container, check=False
        ).stdout.strip()
        if state == "exited":
            logs = _redact(_docker("logs", container, check=False).stdout)
            raise RuntimeError(f"Disposable Profilarr exited before readiness: {logs}")
        probe = _docker(
            "exec",
            container,
            "python",
            "-c",
            (
                "import urllib.error,urllib.request; "
                "u='http://127.0.0.1:6868/api/auth/setup'; "
                "\ntry: r=urllib.request.urlopen(u,timeout=3); print(r.status)"
                "\nexcept urllib.error.HTTPError as e: print(e.code)"
            ),
            check=False,
        )
        if probe.returncode == 0 and probe.stdout.strip() in {"200", "400"}:
            return
        time.sleep(0.5)
    raise RuntimeError(f"Disposable Profilarr container {container} was not ready")


def _app_tree_sha256(container: str) -> str:
    script = r"""
import hashlib
from pathlib import Path
root = Path('/app/app')
files = sorted(
    path for path in root.rglob('*')
    if path.is_file() and (path.suffix == '.py' or path.name == 'index.html')
)
digest = hashlib.sha256()
for path in files:
    digest.update(path.relative_to(root).as_posix().encode() + b'\0')
    digest.update(path.read_bytes())
print(digest.hexdigest())
"""
    return _docker("exec", container, "python", "-c", script).stdout.strip()


def _assert_no_external_egress(container: str) -> None:
    script = r"""
import socket
sock = socket.socket()
sock.settimeout(2)
try:
    result = sock.connect_ex(('1.1.1.1', 53))
finally:
    sock.close()
assert result != 0, 'internal-only container unexpectedly reached a public address'
"""
    _docker("exec", container, "python", "-c", script)


def _assert_exact_container(container: str, network: str) -> None:
    configured = _docker("inspect", "--format", "{{.Config.Image}}", container).stdout.strip()
    assert configured == _IMAGE
    assert (
        _docker("image", "inspect", _IMAGE, "--format", "{{.Architecture}}").stdout.strip()
        == "amd64"
    )
    assert _app_tree_sha256(container) == _EXPECTED_APP_TREE_SHA256
    assert (
        _docker("network", "inspect", "--format", "{{.Internal}}", network).stdout.strip() == "true"
    )
    assert (
        _docker("inspect", "--format", "{{.HostConfig.NetworkMode}}", container).stdout.strip()
        == network
    )
    mounts = json.loads(_docker("inspect", "--format", "{{json .Mounts}}", container).stdout)
    assert all(mount["Destination"] != "/var/run/docker.sock" for mount in mounts)
    _assert_no_external_egress(container)


def _start_profilarr(container: str, network: str, config_root: Path) -> None:
    config_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    _docker(
        "run",
        "-d",
        "--name",
        container,
        "--network",
        network,
        "--memory",
        "256m",
        "--memory-swap",
        "256m",
        "--pids-limit",
        "128",
        "--security-opt",
        "no-new-privileges:true",
        "-e",
        "PUID=1000",
        "-e",
        "PGID=1000",
        "-e",
        "TZ=Etc/UTC",
        "-v",
        f"{config_root}:/config",
        _IMAGE,
    )
    _wait_for_profilarr(container)
    _assert_exact_container(container, network)


_ARR_MOCK = r"""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

counts = {'radarr': 0, 'sonarr': 0, 'unknown': 0}
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/__requests':
            payload = counts
        elif self.path == '/radarr/api/v3/system/status':
            counts['radarr'] += 1
            payload = {'appName': 'Radarr', 'version': '5.10.4'}
        elif self.path == '/sonarr/api/v3/system/status':
            counts['sonarr'] += 1
            payload = {'appName': 'Sonarr', 'version': '4.0.10'}
        else:
            counts['unknown'] += 1
            self.send_response(404)
            self.end_headers()
            return
        body = json.dumps(payload, sort_keys=True).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, format, *args):
        return

ThreadingHTTPServer(('0.0.0.0', 8080), Handler).serve_forever()
"""


def _start_arr_mock(container: str, network: str) -> None:
    _docker(
        "run",
        "-d",
        "--name",
        container,
        "--network",
        network,
        "--network-alias",
        "arr-mock",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=16m",
        "--security-opt",
        "no-new-privileges:true",
        "--cap-drop",
        "ALL",
        "--entrypoint",
        "python",
        _IMAGE,
        "-c",
        _ARR_MOCK,
    )
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if (
            _docker(
                "exec",
                container,
                "python",
                "-c",
                (
                    "import urllib.request; "
                    "print(urllib.request.urlopen('http://127.0.0.1:8080/__requests',"
                    "timeout=2).status)"
                ),
                check=False,
            ).stdout.strip()
            == "200"
        ):
            _assert_no_external_egress(container)
            return
        time.sleep(0.2)
    raise RuntimeError("Disposable internal Arr mock was not ready")


def _mock_counts(profilarr_container: str) -> dict[str, int]:
    completed = _docker(
        "exec",
        profilarr_container,
        "python",
        "-c",
        (
            "import urllib.request; print(urllib.request.urlopen("
            "'http://arr-mock:8080/__requests',timeout=5).read().decode())"
        ),
    )
    result = json.loads(completed.stdout)
    return cast(dict[str, int], result)


def _configure_auth(container: str) -> None:
    status_code, body = _request_json(
        container,
        "POST",
        "/api/auth/setup",
        {"username": _USERNAME, "password": _PASSWORD},
        authenticated=False,
    )
    assert status_code == 200
    assert body["authenticated"] is True
    assert body["username"] == _USERNAME
    assert body["api_key"]["sha256"]


def _yaml(path: Path, value: dict[str, Any]) -> None:
    path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")
    path.chmod(0o600)


def _write_repository_phase(repository: Path, phase: str) -> None:
    assert phase in {"a", "b"}
    if phase == "a":
        _git(repository, "init", "--initial-branch=main")
        _git(repository, "config", "user.name", "Profilarr Drill")
        _git(repository, "config", "user.email", "profilarr@homelab.invalid")
        _git(
            repository,
            "remote",
            "add",
            "origin",
            f"https://synthetic-user:{_SOURCE_GIT_SECRET}@git-origin.invalid/profiles.git",
        )
        for directory in _AUTHORITATIVE_DIRECTORIES:
            (repository / directory).mkdir(mode=0o700, exist_ok=True)
        (repository / ".gitignore").write_text("*.tmp\n", encoding="utf-8")

    label = phase.upper()
    _yaml(
        repository / "regex_patterns" / "primary.yml",
        {
            "name": f"Synthetic Regex {label}",
            "pattern": f"synthetic-{phase}",
            "description": f"Synthetic phase {label}",
            "tags": [phase],
            "tests": [{"input": f"synthetic-{phase}", "expected": True}],
        },
    )
    _yaml(
        repository / "custom_formats" / "primary.yml",
        {
            "name": f"Synthetic Format {label}",
            "description": f"Synthetic phase {label}",
            "tags": [phase],
            "conditions": [
                {
                    "name": f"Synthetic Condition {label}",
                    "type": "release_title",
                    "required": True,
                    "negate": False,
                    "pattern": f"Synthetic Regex {label}",
                }
            ],
            "tests": [{"input": f"synthetic-{phase}", "expected": True}],
        },
    )
    _yaml(
        repository / "profiles" / "primary.yml",
        {
            "name": f"Synthetic Profile {label}",
            "description": f"Synthetic phase {label}",
            "tags": [phase],
            "upgradesAllowed": True,
            "minCustomFormatScore": 0,
            "upgradeUntilScore": 100 if phase == "a" else 200,
            "minScoreIncrement": 1,
            "custom_formats": [{"name": f"Synthetic Format {label}", "score": 10}],
            "custom_formats_radarr": [],
            "custom_formats_sonarr": [],
            "qualities": ["WEB-1080p"],
            "upgrade_until": "WEB-1080p",
            "language": "Original",
        },
    )
    if phase == "b":
        _yaml(
            repository / "profiles" / "extra.yml",
            {
                "name": "Synthetic Profile Extra B",
                "description": "Synthetic phase B extra",
                "tags": ["b", "extra"],
                "upgradesAllowed": False,
                "minCustomFormatScore": 0,
                "upgradeUntilScore": 0,
                "minScoreIncrement": 1,
                "custom_formats": [],
                "custom_formats_radarr": [],
                "custom_formats_sonarr": [],
                "qualities": ["HDTV-720p"],
                "upgrade_until": "HDTV-720p",
                "language": "Original",
            },
        )
    _yaml(
        repository / "media_management" / "misc.yml",
        {
            "radarr": {"propersRepacks": "preferAndUpgrade", "enableMediaInfo": True},
            "sonarr": {"propersRepacks": "doNotPrefer", "enableMediaInfo": phase == "b"},
        },
    )
    _yaml(
        repository / "media_management" / "naming.yml",
        {
            "radarr": {"rename": True, "movieFormat": f"Synthetic Movie {label}"},
            "sonarr": {
                "rename": True,
                "standardEpisodeFormat": f"Synthetic Episode {label}",
            },
        },
    )
    _yaml(
        repository / "media_management" / "quality_definitions.yml",
        {
            "qualityDefinitions": {
                "radarr": {"WEB-1080p": {"preferred": 10 if phase == "a" else 20}},
                "sonarr": {"HDTV-720p": {"preferred": 5 if phase == "a" else 15}},
            }
        },
    )
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", f"Synthetic Profilarr phase {label}")
    head = _git(repository, "rev-parse", "HEAD")
    _git(repository, "update-ref", f"refs/heads/local-{phase}", head)
    _git(repository, "tag", f"phase-{phase}", head)
    _git(repository, "notes", "--ref=drill", "add", "-f", "-m", f"phase-{label}", head)
    assert _git(repository, "status", "--porcelain=v1") == ""
    _git(repository, "fsck", "--full")


def _seed_database_phase(database: Path, phase: str) -> None:
    # Direct stopped-state seeding is deliberate: v1.1.5 has no transactionally
    # coherent fixture/import API for its combined DB/Git state. The exact image
    # boots before and after every seed, and all restored assertions use its
    # authenticated public APIs, so the evidence is explicit rather than
    # pretending these SQL writes are a supported production workflow.
    assert phase in {"a", "b"}
    label = phase.upper()
    profile_names = [f"Synthetic Profile {label}"]
    if phase == "b":
        profile_names.append("Synthetic Profile Extra B")
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA journal_mode=DELETE").fetchone() == ("delete",)
        assert connection.execute("PRAGMA quick_check").fetchall() == [("ok",)]
        assert [
            list(row)
            for row in connection.execute(
                "SELECT version, name FROM migrations ORDER BY version"
            ).fetchall()
        ] == _EXPECTED_MIGRATIONS
        connection.execute("DELETE FROM arr_config")
        connection.execute("DELETE FROM scheduled_tasks WHERE type NOT IN ('Sync', 'Backup')")
        connection.execute(
            "UPDATE scheduled_tasks SET interval_minutes = 43200, "
            "last_run = '2099-01-01 00:00:00', status = 'pending'"
        )
        configs = (
            (
                "Synthetic Radarr",
                "radarr",
                _RADARR_KEY,
                "http://arr-mock:8080/radarr",
                "manual",
                0,
                0,
            ),
            (
                "Synthetic Sonarr",
                "sonarr",
                _SONARR_KEY,
                "http://arr-mock:8080/sonarr",
                "schedule",
                43200,
                1,
            ),
        )
        for name, arr_type, api_key, server, method, interval, unique in configs:
            data = json.dumps(
                {
                    "profiles": profile_names,
                    "customFormats": [f"Synthetic Format {label}"],
                },
                separators=(",", ":"),
            )
            cursor = connection.execute(
                "INSERT INTO scheduled_tasks"
                "(name, type, interval_minutes, last_run, status) VALUES (?, ?, ?, ?, ?)",
                (
                    f"Import {name}",
                    "Import",
                    interval or 43200,
                    "2099-01-01 00:00:00",
                    "pending",
                ),
            )
            task_id = cursor.lastrowid if method == "schedule" else None
            connection.execute(
                "INSERT INTO arr_config"
                "(name,type,tags,arr_server,api_key,data_to_sync,sync_method,"
                "sync_interval,import_as_unique,import_task_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    name,
                    arr_type,
                    json.dumps([phase]),
                    server,
                    api_key,
                    data,
                    method,
                    interval,
                    unique,
                    task_id,
                ),
            )
        settings = {
            "gitRepo": "http://arr-mock:8080/git/profiles.git",
            "auto_pull_enabled": "0" if phase == "a" else "1",
            "drill_phase": phase,
        }
        for key, value in settings.items():
            connection.execute(
                "INSERT INTO settings(key,value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                "updated_at=CURRENT_TIMESTAMP",
                (key, value),
            )
        connection.execute("DELETE FROM format_renames")
        connection.execute(
            "INSERT INTO format_renames(format_name) VALUES (?)",
            (f"Synthetic Format {label}",),
        )
        connection.execute(
            "UPDATE language_import_config SET score=?, updated_at=CURRENT_TIMESTAMP",
            (-400 if phase == "a" else -500,),
        )
        connection.commit()
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def _repository_state(repository: Path) -> dict[str, Any]:
    refs: dict[str, str] = {}
    output = _git(
        repository,
        "for-each-ref",
        "--format=%(refname) %(objectname)",
        "refs/heads",
        "refs/tags",
        "refs/notes",
    )
    for line in output.splitlines():
        name, object_id = line.split(" ", 1)
        refs[name] = object_id
    inventory: dict[str, str] = {}
    for directory in _AUTHORITATIVE_DIRECTORIES:
        for path in sorted((repository / directory).rglob("*")):
            if path.is_file():
                inventory[path.relative_to(repository).as_posix()] = _sha256(path)
    assert _git(repository, "status", "--porcelain=v1") == ""
    return {
        "branch": _git(repository, "symbolic-ref", "--short", "HEAD"),
        "head": _git(repository, "rev-parse", "HEAD"),
        "refs": refs,
        "inventory": inventory,
    }


def _assert_application_state(container: str, phase: str) -> None:
    label = phase.upper()
    status, authenticated = _request_json(
        container,
        "POST",
        "/api/auth/authenticate",
        {"username": _USERNAME, "password": _PASSWORD},
        authenticated=False,
    )
    assert status == 200 and authenticated["authenticated"] is True

    status, general = _request_json(container, "GET", "/api/settings/general")
    assert status == 200 and general["username"] == _USERNAME
    assert general["api_key"]["sha256"]

    status, regexes = _request_json(container, "GET", "/api/data/regex_pattern")
    assert status == 200 and len(regexes) == 1
    assert regexes[0]["content"]["name"] == f"Synthetic Regex {label}"
    assert regexes[0]["content"]["pattern"] == f"synthetic-{phase}"

    status, formats = _request_json(container, "GET", "/api/data/custom_format")
    assert status == 200 and len(formats) == 1
    assert formats[0]["content"]["name"] == f"Synthetic Format {label}"

    status, profiles = _request_json(container, "GET", "/api/data/profile")
    assert status == 200
    expected_profiles = {f"Synthetic Profile {label}"}
    if phase == "b":
        expected_profiles.add("Synthetic Profile Extra B")
    assert {profile["content"]["name"] for profile in profiles} == expected_profiles

    status, media = _request_json(container, "GET", "/api/media-management")
    assert status == 200
    assert media["radarr"]["naming"]["movieFormat"] == f"Synthetic Movie {label}"
    assert media["sonarr"]["naming"]["standardEpisodeFormat"] == f"Synthetic Episode {label}"

    status, arr_result = _request_json(container, "GET", "/api/arr/config")
    assert status == 200 and arr_result["success"] is True
    arrs = {item["type"]: item for item in arr_result["data"]}
    assert set(arrs) == {"radarr", "sonarr"}
    assert arrs["radarr"]["sync_method"] == "manual"
    assert arrs["sonarr"]["sync_method"] == "schedule"
    assert arrs["sonarr"]["sync_interval"] == 43200
    assert arrs["sonarr"]["import_as_unique"] is True
    assert arrs["radarr"]["apiKey"]["sha256"] == _hash_text(_RADARR_KEY)
    assert arrs["sonarr"]["apiKey"]["sha256"] == _hash_text(_SONARR_KEY)
    assert arrs["radarr"]["arrServer"]["sha256"] == _hash_text("http://arr-mock:8080/radarr")
    assert arrs["sonarr"]["arrServer"]["sha256"] == _hash_text("http://arr-mock:8080/sonarr")
    expected_names = [f"Synthetic Profile {label}"]
    if phase == "b":
        expected_names.append("Synthetic Profile Extra B")
    assert arrs["radarr"]["data_to_sync"]["profiles"] == expected_names

    status, language = _request_json(container, "GET", "/api/settings/language-import-score")
    assert status == 200 and language["score"] == (-400 if phase == "a" else -500)
    status, auto_pull = _request_json(container, "GET", "/api/git/autopull")
    assert status == 200 and auto_pull["enabled"] is (phase == "b")
    status, git_status = _request_json(container, "GET", "/api/git/status")
    assert status == 200 and git_status["success"] is True
    status, branches = _request_json(container, "GET", "/api/git/branches")
    assert status == 200 and branches["success"] is True

    for arr_type, key in (("radarr", _RADARR_KEY), ("sonarr", _SONARR_KEY)):
        status, ping = _request_json(
            container,
            "POST",
            "/api/arr/ping",
            {
                "url": f"http://arr-mock:8080/{arr_type}",
                "apiKey": key,
                "type": arr_type,
            },
        )
        assert status == 200 and ping["success"] is True


_RUNNER_GUARDS = r"""
import errno
import socket
from pathlib import Path

def assert_runner_guards(database=None, repository=None):
    assert {name for _, name in socket.if_nameindex()} == {'lo'}
    assert not Path('/var/run/docker.sock').exists()
    cap_eff = next(
        line for line in Path('/proc/self/status').read_text().splitlines()
        if line.startswith('CapEff:')
    )
    assert int(cap_eff.split()[1], 16) == 0
    for source in (database, repository):
        if source is None:
            continue
        mount_line = next(
            line for line in Path('/proc/self/mountinfo').read_text().splitlines()
            if line.split()[4] == str(source)
        )
        assert 'ro' in mount_line.split()[5].split(',')
    if database is not None:
        try:
            database.open('ab').close()
        except OSError as exc:
            assert exc.errno in (errno.EROFS, errno.EACCES)
        else:
            raise RuntimeError('Profilarr database source is writable')
    if repository is not None:
        try:
            (repository / '.write-probe').write_text('must fail', encoding='utf-8')
        except OSError as exc:
            assert exc.errno in (errno.EROFS, errno.EACCES)
        else:
            raise RuntimeError('Profilarr repository source is writable')
    try:
        Path('/rootfs-write-probe').write_text('must fail', encoding='utf-8')
    except OSError as exc:
        assert exc.errno in (errno.EROFS, errno.EACCES)
    else:
        raise RuntimeError('plugin runner root filesystem is writable')
"""


def _start_live_db_churn(container: str, phase: str) -> None:
    score = -400 if phase == "a" else -500
    script = r"""
import json, sqlite3, time, urllib.request
with sqlite3.connect('/config/profilarr.db') as connection:
    key = connection.execute('SELECT api_key FROM auth').fetchone()[0]
for _ in range(60):
    request = urllib.request.Request(
        'http://127.0.0.1:6868/api/settings/language-import-score',
        data=json.dumps({'score': SCORE}).encode(),
        headers={'Content-Type': 'application/json', 'X-Api-Key': key},
        method='PUT',
    )
    assert urllib.request.urlopen(request, timeout=5).status == 200
    time.sleep(0.05)
open('/tmp/profilarr-drill-churn.done', 'w').close()
""".replace(
        "SCORE", str(score)
    )
    _docker("exec", "-d", container, "python", "-c", script)


def _wait_for_db_churn(container: str) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if (
            _docker(
                "exec",
                container,
                "test",
                "-f",
                "/tmp/profilarr-drill-churn.done",
                check=False,
            ).returncode
            == 0
        ):
            _docker("exec", container, "rm", "/tmp/profilarr-drill-churn.done")
            return
        time.sleep(0.1)
    raise RuntimeError("Synthetic supported-API DB churn did not complete")


def _run_backup(
    runner_image: str,
    source_config: Path,
    artifact_root: Path,
    run_number: int,
    runner_name: str,
    *,
    extra_mounts: tuple[str, ...] = (),
) -> Path:
    script = f"""
import asyncio
import json
from pathlib import Path
from app.core.plugins.artifacts import validate_backup_artifact
from app.core.plugins.base import BackupContext
from app.plugins.profilarr.plugin import ProfilarrPlugin
{_RUNNER_GUARDS}

async def main():
    database = Path('/sources/profilarr/profilarr.db')
    repository = Path('/sources/profilarr/db')
    assert_runner_guards(database, repository)
    plugin = ProfilarrPlugin(name='profilarr')
    config = {{
        'mode': 'source',
        'database_path': str(database),
        'repository_path': str(repository),
    }}
    assert await plugin.test(config) is True
    context = BackupContext(
        job_id='profilarr-drill-{run_number}',
        target_id='profilarr-source',
        config=config,
        metadata={{'target_slug': 'profilarr-drill'}},
    )
    result = await plugin.backup(context)
    validated = validate_backup_artifact(result['artifact_path'], plugin, context)
    result['validated_sha256'] = validated.sha256
    result['validated_size_bytes'] = validated.size_bytes
    print(json.dumps(result, sort_keys=True))

asyncio.run(main())
"""
    arguments = [
        "run",
        "--rm",
        "--name",
        runner_name,
        "--network",
        "none",
        "--user",
        "1000:1000",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=256m",
        "--security-opt",
        "no-new-privileges:true",
        "--cap-drop",
        "ALL",
        "-v",
        f"{source_config / 'profilarr.db'}:/sources/profilarr/profilarr.db:ro",
        "-v",
        f"{source_config / 'db'}:/sources/profilarr/db:ro",
        "-v",
        f"{artifact_root}:/backups:rw",
    ]
    arguments.extend(extra_mounts)
    arguments.extend((runner_image, "python", "-c", script))
    completed = _docker(*arguments)
    assert not (source_config / "db" / ".write-probe").exists()
    result = _json_result(completed)
    container_path = Path(result["artifact_path"])
    artifact = artifact_root / container_path.relative_to("/backups")
    assert result["validated_sha256"] == _sha256(artifact)
    assert result["validated_size_bytes"] == artifact.stat().st_size
    return artifact


def _run_expected_backup_stop(
    runner_image: str,
    source_config: Path,
    artifact_root: Path,
    runner_name: str,
    *,
    extra_mounts: tuple[str, ...] = (),
) -> None:
    script = f"""
import asyncio
import json
from pathlib import Path
from app.core.plugins.base import BackupContext
from app.plugins.profilarr.plugin import ProfilarrPlugin
{_RUNNER_GUARDS}

async def main():
    database = Path('/sources/profilarr/profilarr.db')
    repository = Path('/sources/profilarr/db')
    assert_runner_guards(database, repository)
    plugin = ProfilarrPlugin(name='profilarr')
    config = {{'mode': 'source', 'database_path': str(database),
              'repository_path': str(repository)}}
    try:
        await plugin.backup(BackupContext(
            job_id='profilarr-negative', target_id='profilarr-source',
            config=config, metadata={{'target_slug': 'profilarr-negative'}},
        ))
    except Exception as exc:
        print(json.dumps({{'stopped': True, 'type': type(exc).__name__}}))
        return
    raise AssertionError('unsafe Profilarr source unexpectedly produced an artifact')

asyncio.run(main())
"""
    arguments = [
        "run",
        "--rm",
        "--name",
        runner_name,
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
        f"{source_config / 'profilarr.db'}:/sources/profilarr/profilarr.db:ro",
        "-v",
        f"{source_config / 'db'}:/sources/profilarr/db:ro",
        "-v",
        f"{artifact_root}:/backups:rw",
    ]
    arguments.extend(extra_mounts)
    arguments.extend((runner_image, "python", "-c", script))
    before = set(artifact_root.rglob("*"))
    result = _json_result(_docker(*arguments))
    assert result["stopped"] is True
    assert set(artifact_root.rglob("*")) == before


def _json_scalars(value: Any) -> Iterator[str]:
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _json_scalars(item)
    elif isinstance(value, list):
        for item in value:
            yield from _json_scalars(item)
    elif isinstance(value, (str, int, float, bool)):
        yield str(value)


def _inspect_artifact(
    artifact: Path,
    inspection_root: Path,
    phase: str,
    source_state: dict[str, Any],
) -> dict[str, Any]:
    assert artifact.is_file() and not artifact.is_symlink()
    assert stat.S_IMODE(artifact.stat().st_mode) == 0o600
    sidecar_path = Path(f"{artifact}.meta.json")
    assert sidecar_path.is_file() and not sidecar_path.is_symlink()
    assert stat.S_IMODE(sidecar_path.stat().st_mode) == 0o600
    sidecar = read_backup_sidecar(str(artifact))
    assert sidecar is not None
    assert sidecar["plugin_name"] == "profilarr"
    assert sidecar["target_slug"] == "profilarr-drill"
    assert sidecar["artifact_bytes"] == artifact.stat().st_size
    assert sidecar["sha256"] == _sha256(artifact)
    serialized_sidecar = json.dumps(sidecar, sort_keys=True)
    _assert_secret_free(
        serialized_sidecar,
        surface="Profilarr sidecar",
        markers=_PUBLIC_FORBIDDEN,
    )

    phase_root = inspection_root / phase
    phase_root.mkdir(mode=0o700, parents=True)
    with zipfile.ZipFile(artifact) as archive:
        assert archive.testzip() is None
        assert [entry.filename for entry in archive.infolist()] == [
            "profilarr.db",
            "repository.bundle",
            "manifest.json",
        ]
        assert all(
            not entry.is_dir() and not (entry.flag_bits & 0x1) for entry in archive.infolist()
        )
        database_bytes = archive.read("profilarr.db")
        bundle_bytes = archive.read("repository.bundle")
        manifest_bytes = archive.read("manifest.json")
        database = phase_root / "profilarr.db"
        bundle = phase_root / "repository.bundle"
        database.write_bytes(database_bytes)
        bundle.write_bytes(bundle_bytes)
        manifest = json.loads(manifest_bytes)
        assert isinstance(manifest, dict)
        assert (
            manifest_bytes == json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        )

    database_hash = hashlib.sha256(database_bytes).hexdigest()
    bundle_hash = hashlib.sha256(bundle_bytes).hexdigest()
    manifest_values = set(_json_scalars(manifest))
    assert _EXPECTED_VERSION in manifest_values
    assert _EXPECTED_SOURCE_COMMIT in manifest_values
    assert _EXPECTED_IMAGE_TAG in manifest_values
    assert _EXPECTED_IMAGE_DIGEST in manifest_values
    assert database_hash in manifest_values
    assert bundle_hash in manifest_values
    assert source_state["head"] in manifest_values
    for ref_name, object_id in source_state["refs"].items():
        assert ref_name in manifest_values
        assert object_id in manifest_values
    manifest_text = manifest_bytes.decode()
    _assert_secret_free(
        manifest_text,
        surface="Profilarr private manifest",
        markers=_PUBLIC_FORBIDDEN,
    )

    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
        assert connection.execute("PRAGMA quick_check").fetchall() == [("ok",)]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("delete",)
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert tables == _EXPECTED_TABLES
        migrations = [
            list(row)
            for row in connection.execute(
                "SELECT version,name FROM migrations ORDER BY version"
            ).fetchall()
        ]
        assert migrations == _EXPECTED_MIGRATIONS
        assert connection.execute("SELECT count(*) FROM auth").fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM arr_config").fetchone() == (2,)
        assert connection.execute(
            "SELECT value FROM settings WHERE key='drill_phase'"
        ).fetchone() == (phase,)
        assert connection.execute("SELECT score FROM language_import_config").fetchone() == (
            -400 if phase == "a" else -500,
        )

    validation_repo = phase_root / "validation.git"
    validation_repo.mkdir(mode=0o700)
    _git(validation_repo, "init", "--bare")
    _git(validation_repo, "bundle", "verify", str(bundle))
    listed: dict[str, str] = {}
    for line in _git(validation_repo, "bundle", "list-heads", str(bundle)).splitlines():
        object_id, ref_name = line.split(" ", 1)
        if ref_name != "HEAD":
            listed[ref_name] = object_id
    assert listed == source_state["refs"]
    for ref_name, object_id in listed.items():
        _git(validation_repo, "fetch", str(bundle), f"{object_id}:{ref_name}")
    _git(validation_repo, "fsck", "--full")
    worktree = phase_root / "worktree"
    worktree.mkdir(mode=0o700)
    _git(
        validation_repo,
        "--git-dir=.",
        f"--work-tree={worktree}",
        "checkout",
        "-f",
        source_state["head"],
    )
    recovered_inventory = {
        path.relative_to(worktree).as_posix(): _sha256(path)
        for directory in _AUTHORITATIVE_DIRECTORIES
        for path in sorted((worktree / directory).rglob("*"))
        if path.is_file()
    }
    assert recovered_inventory == source_state["inventory"]
    assert (
        bundle_bytes.find(_SOURCE_GIT_SECRET.encode()) == -1
    ), "Profilarr bundle exposed the synthetic source Git credential"
    return {
        "artifact": _sha256(artifact),
        "artifact_size": artifact.stat().st_size,
        "database": database_hash,
        "bundle": bundle_hash,
        "manifest": hashlib.sha256(manifest_bytes).hexdigest(),
    }


def _run_restore(
    runner_image: str,
    artifact_root: Path,
    artifact: Path,
    restore_parent: Path,
    runner_name: str,
    *,
    authorize: bool = True,
    expect_success: bool = True,
) -> dict[str, Any]:
    relative_artifact = artifact.relative_to(artifact_root)
    original_identity = (artifact.stat().st_dev, artifact.stat().st_ino)
    original_hash = _sha256(artifact)
    script = f"""
import hashlib
import json
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
        source = Target(name='Synthetic Profilarr Source', slug='profilarr-drill',
                        plugin_name='profilarr',
                        plugin_config_json=json.dumps({{'mode': 'source'}}))
        destination = Target(
            name='Synthetic Profilarr Restore', slug='profilarr-restore',
            plugin_name='profilarr',
            plugin_config_json=json.dumps({{
                'mode': 'restore_destination',
                'restore_directory': '/restore/destination',
            }}),
        )
        session.add_all([source, destination])
        session.commit()
        source_run = Run(status='success', operation='backup',
                         started_at=datetime.now(timezone.utc),
                         finished_at=datetime.now(timezone.utc))
        session.add(source_run)
        session.commit()
        target_run = TargetRun(
            run_id=source_run.id, target_id=source.id, status='success',
            operation='backup', artifact_path=str(artifact),
            artifact_bytes=artifact.stat().st_size, sha256=digest(artifact),
            source_identity_json=json.dumps({{
                'application_version': '{_EXPECTED_VERSION}',
                'source_commit': '{_EXPECTED_SOURCE_COMMIT}',
            }}),
            started_at=source_run.started_at, finished_at=source_run.finished_at,
        )
        session.add(target_run)
        session.commit()
        try:
            result = RestoreService(session).restore(
                source_target_run_id=target_run.id,
                destination_target_id=destination.id,
                triggered_by='isolated_profilarr_exact_drill',
            )
        except Exception as exc:
            print(json.dumps({{
                'failed': True,
                'type': type(exc).__name__,
                'message': str(exc),
            }}))
            return
        restored = result.target_runs[0]
        print(json.dumps({{
            'failed': False,
            'status': result.status,
            'target_status': restored.status,
            'restored_path': restored.artifact_path,
            'message': restored.message,
        }}, sort_keys=True))
    finally:
        session.close()

main()
"""
    arguments = [
        "run",
        "--rm",
        "--name",
        runner_name,
        "--network",
        "none",
        "-e",
        "BACKUP_BASE_PATH=/backups",
    ]
    if authorize:
        arguments.extend(("-e", "HOMELAB_BACKUP_ALLOW_ISOLATED_RESTORE=1"))
    arguments.extend(
        (
            "--user",
            "1000:1000",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=256m",
            "--security-opt",
            "no-new-privileges:true",
            "--cap-drop",
            "ALL",
            "-v",
            f"{artifact_root}:/backups:rw",
            "-v",
            f"{restore_parent}:/restore:rw",
            runner_image,
            "python",
            "-c",
            script,
        )
    )
    result = _json_result(_docker(*arguments))
    assert (artifact.stat().st_dev, artifact.stat().st_ino) == original_identity
    assert _sha256(artifact) == original_hash
    assert not list(artifact.parent.glob(".homelab-backup-restore-*"))
    assert result["failed"] is (not expect_success), result
    return result


def _assert_restored_root(
    artifact: Path,
    restore_root: Path,
    expected_state: dict[str, Any],
) -> None:
    assert {path.name for path in restore_root.iterdir()} == {"profilarr.db", "db"}
    assert stat.S_IMODE((restore_root / "profilarr.db").stat().st_mode) == 0o600
    for path in (restore_root / "db").rglob("*"):
        assert not path.is_symlink()
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode & 0o077 == 0
    hooks = restore_root / "db" / ".git" / "hooks"
    assert not hooks.exists() or not any(hooks.iterdir())
    logs = restore_root / "db" / ".git" / "logs"
    assert not logs.exists() or not any(logs.rglob("*"))
    assert not list((restore_root / "db" / ".git").rglob("*.lock"))
    _assert_secret_free(
        (restore_root / "db" / ".git" / "config").read_text(encoding="utf-8"),
        surface="reconstructed Profilarr Git config",
        markers=(_SOURCE_GIT_SECRET,),
    )
    assert _git(restore_root / "db", "status", "--porcelain=v1") == ""
    assert _git(restore_root / "db", "symbolic-ref", "--short", "HEAD") == "main"
    assert _git(restore_root / "db", "rev-parse", "HEAD") == expected_state["head"]
    _git(restore_root / "db", "fsck", "--full")
    restored_state = _repository_state(restore_root / "db")
    assert restored_state == expected_state
    with zipfile.ZipFile(artifact) as archive:
        assert (
            _sha256(restore_root / "profilarr.db")
            == hashlib.sha256(archive.read("profilarr.db")).hexdigest()
        )


def test_two_live_backups_restore_to_two_fresh_exact_profilarr_images(
    tmp_path: Path,
) -> None:
    if os.getuid() != 1000 or os.getgid() != 1000:
        pytest.skip("the exact bind-mount drill requires host UID:GID 1000:1000")
    assert shutil.which("docker") is not None
    assert shutil.which("git") is not None
    plugin_package = importlib.util.find_spec("app.plugins.profilarr")
    plugin_module = (
        importlib.util.find_spec("app.plugins.profilarr.plugin")
        if plugin_package is not None
        else None
    )
    assert plugin_module is not None, "RED: Plan 013's Profilarr plugin is not implemented"

    disposable_root = tmp_path.resolve()
    assert not str(disposable_root).startswith(("/docker-apps", "/mnt/nas"))
    suffix = uuid.uuid4().hex[:10]
    network = f"codex-profilarr-internal-{suffix}"
    source_container = f"codex-profilarr-source-{suffix}"
    mock_container = f"codex-profilarr-arr-mock-{suffix}"
    restore_containers = [f"codex-profilarr-restore-{number}-{suffix}" for number in (1, 2)]
    backup_runners = [f"codex-profilarr-backup-runner-{number}-{suffix}" for number in (1, 2)]
    restore_runners = [f"codex-profilarr-restore-runner-{number}-{suffix}" for number in (1, 2)]
    negative_runners = [
        f"codex-profilarr-negative-{name}-{suffix}"
        for name in ("dirty", "wal", "tampered", "unauthorized")
    ]
    containers = [
        source_container,
        mock_container,
        *restore_containers,
        *backup_runners,
        *restore_runners,
        *negative_runners,
    ]
    runner_image = f"codex-homelab-backup-profilarr-runner:{suffix}"
    source_config = tmp_path / "source-config"
    artifact_root = tmp_path / "artifacts"
    inspection_root = tmp_path / "inspection"
    artifact_root.mkdir(mode=0o700)
    inspection_root.mkdir(mode=0o700)

    try:
        _docker("network", "create", "--internal", network)
        _start_arr_mock(mock_container, network)
        _docker("build", "-t", runner_image, str(_BACKEND_ROOT))
        _start_profilarr(source_container, network, source_config)
        _configure_auth(source_container)

        artifacts: list[Path] = []
        signatures: list[dict[str, Any]] = []
        source_states: list[dict[str, Any]] = []
        for run_number, phase in enumerate(("a", "b"), start=1):
            _docker("stop", "--time", "30", source_container)
            _write_repository_phase(source_config / "db", phase)
            _seed_database_phase(source_config / "profilarr.db", phase)
            source_state = _repository_state(source_config / "db")
            source_states.append(source_state)
            _docker("start", source_container)
            _wait_for_profilarr(source_container)
            _assert_exact_container(source_container, network)
            _assert_application_state(source_container, phase)

            state_before = _repository_state(source_config / "db")
            assert state_before == source_state
            assert (
                _docker("inspect", "--format", "{{.State.Status}}", source_container).stdout.strip()
                == "running"
            )
            _start_live_db_churn(source_container, phase)
            artifact = _run_backup(
                runner_image,
                source_config,
                artifact_root,
                run_number,
                backup_runners[run_number - 1],
            )
            _wait_for_db_churn(source_container)
            assert _repository_state(source_config / "db") == state_before
            signature = _inspect_artifact(artifact, inspection_root, phase, source_state)
            artifacts.append(artifact)
            signatures.append(signature)

            restore_parent = tmp_path / f"restore-{run_number}"
            restore_parent.mkdir(mode=0o700)
            sentinel = restore_parent / _RESTORE_SENTINEL
            sentinel.write_text(_RESTORE_SENTINEL_CONTENT, encoding="utf-8")
            sentinel.chmod(0o600)
            result = _run_restore(
                runner_image,
                artifact_root,
                artifact,
                restore_parent,
                restore_runners[run_number - 1],
            )
            assert result["status"] == "success"
            assert result["target_status"] == "success"
            assert result["restored_path"] == "/restore/destination"
            restored_config = restore_parent / "destination"
            assert not sentinel.exists()
            assert set(restore_parent.iterdir()) == {restored_config}
            _assert_restored_root(artifact, restored_config, source_state)

            _start_profilarr(restore_containers[run_number - 1], network, restored_config)
            _assert_application_state(restore_containers[run_number - 1], phase)
            _docker("restart", restore_containers[run_number - 1], timeout=180)
            _wait_for_profilarr(restore_containers[run_number - 1])
            _assert_exact_container(restore_containers[run_number - 1], network)
            _assert_application_state(restore_containers[run_number - 1], phase)

        assert artifacts[0] != artifacts[1]
        assert signatures[0]["artifact"] != signatures[1]["artifact"]
        assert signatures[0]["database"] != signatures[1]["database"]
        assert signatures[0]["bundle"] != signatures[1]["bundle"]
        assert signatures[0]["manifest"] != signatures[1]["manifest"]
        assert artifacts[0].stat().st_size == signatures[0]["artifact_size"]
        assert _sha256(artifacts[0]) == signatures[0]["artifact"]
        immutable_a = _inspect_artifact(
            artifacts[0], inspection_root / "immutable-a", "a", source_states[0]
        )
        assert immutable_a == signatures[0]

        # Representative fail-closed STOPs: dirty Git, visible WAL companion,
        # tampered artifact attribution/hash, and missing local authorization.
        dirty = source_config / "db" / "profiles" / "dirty-stop.yml"
        dirty.write_text("name: must stop\n", encoding="utf-8")
        _run_expected_backup_stop(
            runner_image,
            source_config,
            artifact_root,
            negative_runners[0],
        )
        dirty.unlink()
        assert _git(source_config / "db", "status", "--porcelain=v1") == ""

        wal = tmp_path / "profilarr.db-wal"
        wal.write_bytes(b"synthetic-visible-wal-stop")
        _run_expected_backup_stop(
            runner_image,
            source_config,
            artifact_root,
            negative_runners[1],
            extra_mounts=(
                "-v",
                f"{wal}:/sources/profilarr/profilarr.db-wal:ro",
            ),
        )

        tampered_root = tmp_path / "tampered-artifacts"
        relative_a = artifacts[0].relative_to(artifact_root)
        tampered = tampered_root / relative_a
        tampered.parent.mkdir(mode=0o700, parents=True)
        shutil.copy2(artifacts[0], tampered)
        shutil.copy2(Path(f"{artifacts[0]}.meta.json"), Path(f"{tampered}.meta.json"))
        with tampered.open("r+b") as file_handle:
            first = file_handle.read(1)
            file_handle.seek(0)
            file_handle.write(bytes([first[0] ^ 0xFF]))
        tampered_restore = tmp_path / "tampered-restore"
        tampered_restore.mkdir(mode=0o700)
        (tampered_restore / _RESTORE_SENTINEL).write_text(
            _RESTORE_SENTINEL_CONTENT, encoding="utf-8"
        )
        _run_restore(
            runner_image,
            tampered_root,
            tampered,
            tampered_restore,
            negative_runners[2],
            expect_success=False,
        )
        assert not (tampered_restore / "destination").exists()

        unauthorized_restore = tmp_path / "unauthorized-restore"
        unauthorized_restore.mkdir(mode=0o700)
        (unauthorized_restore / _RESTORE_SENTINEL).write_text(
            _RESTORE_SENTINEL_CONTENT, encoding="utf-8"
        )
        _run_restore(
            runner_image,
            artifact_root,
            artifacts[1],
            unauthorized_restore,
            negative_runners[3],
            authorize=False,
            expect_success=False,
        )
        assert not (unauthorized_restore / "destination").exists()

        counts = _mock_counts(source_container)
        assert counts["radarr"] >= 6
        assert counts["sonarr"] >= 6
        assert counts["unknown"] == 0
        for container in [source_container, *restore_containers]:
            logs = _docker("logs", container, check=False).stdout
            _assert_secret_free(
                logs,
                surface=f"disposable Profilarr log for {container}",
                markers=_SECRET_MARKERS,
            )
    finally:
        for container in containers:
            _docker("rm", "-f", container, check=False)
        _docker("network", "rm", network, check=False)
        _docker("image", "rm", "-f", runner_image, check=False)
        assert all(
            _docker("container", "inspect", container, check=False).returncode != 0
            for container in containers
        )
        assert _docker("network", "inspect", network, check=False).returncode != 0
        assert _docker("image", "inspect", runner_image, check=False).returncode != 0
