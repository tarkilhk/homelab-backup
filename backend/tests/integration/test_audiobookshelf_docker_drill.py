from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import threading
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any, cast

import pytest

from app.core.plugins.sidecar import read_backup_sidecar

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_AUDIOBOOKSHELF_DOCKER_DRILL") != "1",
    reason=(
        "set RUN_AUDIOBOOKSHELF_DOCKER_DRILL=1 for the disposable exact-version "
        "Audiobookshelf drill"
    ),
)

_IMAGE = (
    "ghcr.io/advplyr/audiobookshelf@"
    "sha256:180acad33d69c99ed208676465d8edcb268fa46967735579a7810859885b1a8e"
)
_EXPECTED_VERSION = "2.36.0"
_EXPECTED_REVISION = "96d4021a3cd45f67bf374b65abafbe5d73e926b5"
_BACKEND_ROOT = Path(__file__).resolve().parents[2]
_ROOT_USERNAME = "drill-root"
_ROOT_PASSWORD = "synthetic-local-root-password"
_EXTRA_USERNAME = "drill-reader"
_EXTRA_PASSWORD = "synthetic-local-reader-password"
_CONFIG_SENTINEL_NAME = ".audiobookshelf-config-restore-destination"
_METADATA_SENTINEL_NAME = ".audiobookshelf-metadata-restore-destination"
_RESTORE_SENTINEL_CONTENT = "audiobookshelf-v2.36.0-isolated-restore-v1\n"
_MEDIA_EXTENSIONS = {
    ".aac",
    ".aiff",
    ".alac",
    ".epub",
    ".flac",
    ".m4a",
    ".m4b",
    ".mka",
    ".mp3",
    ".ogg",
    ".opus",
    ".pdf",
    ".wav",
    ".wma",
}


def _docker(
    *arguments: str,
    check: bool = True,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["docker", *arguments],
            check=check,
            capture_output=True,
            text=True,
            input=input_text,
            timeout=300,
        )
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "")[-4000:]
        stdout = (exc.stdout or "")[-4000:]
        raise RuntimeError(f"Disposable Docker command failed:\n{stderr}\n{stdout}") from exc


def _sha256(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _request(
    container: str,
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    token: str | None = None,
) -> tuple[int, bytes]:
    # Keep synthetic credentials and JWTs off the docker command line so a failed
    # subprocess cannot echo them in its exception or process listing.
    script = r"""
const chunks = []
process.stdin.on('data', chunk => chunks.push(chunk))
process.stdin.on('end', async () => {
  const request = JSON.parse(Buffer.concat(chunks).toString('utf8'))
  const headers = {}
  if (request.token) headers.Authorization = `Bearer ${request.token}`
  let body
  if (request.body !== null) {
    headers['Content-Type'] = 'application/json'
    body = JSON.stringify(request.body)
  }
  const response = await fetch(`http://127.0.0.1:80${request.path}`, {
    method: request.method,
    headers,
    body
  })
  const payload = Buffer.from(await response.arrayBuffer())
  process.stdout.write(JSON.stringify({
    status: response.status,
    body: payload.toString('base64')
  }))
})
"""
    request = json.dumps(
        {"method": method, "path": path, "body": body, "token": token},
        separators=(",", ":"),
    )
    completed = _docker("exec", "-i", container, "node", "-e", script, input_text=request)
    response = json.loads(completed.stdout)
    payload = base64.b64decode(response["body"])
    status = int(response["status"])
    if not 200 <= status < 300:
        message = payload.decode("utf-8", errors="replace")[:500]
        raise AssertionError(f"Audiobookshelf {method} {path} returned {status}: {message}")
    return status, payload


def _request_json(
    container: str,
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    token: str | None = None,
) -> Any:
    _, payload = _request(container, method, path, body=body, token=token)
    return json.loads(payload)


def _wait_for_status(container: str, *, initialized: bool) -> dict[str, Any]:
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        try:
            status = _request_json(container, "GET", "/status")
        except (
            subprocess.CalledProcessError,
            json.JSONDecodeError,
            AssertionError,
            RuntimeError,
        ):
            state = _docker(
                "inspect", "--format", "{{.State.Status}}", container, check=False
            ).stdout.strip()
            if state == "exited":
                logs = _docker("logs", container, check=False).stdout[-3000:]
                raise RuntimeError(
                    f"Disposable Audiobookshelf container exited before readiness: {logs}"
                )
            time.sleep(0.5)
            continue
        if (
            status.get("app") == "audiobookshelf"
            and status.get("serverVersion") == _EXPECTED_VERSION
            and status.get("isInit") is initialized
        ):
            return cast(dict[str, Any], status)
        time.sleep(0.5)
    raise RuntimeError(f"Disposable Audiobookshelf container {container} did not become ready")


def _assert_exact_image(container: str) -> None:
    configured = _docker("inspect", "--format", "{{.Config.Image}}", container).stdout.strip()
    assert configured == _IMAGE
    revision = _docker(
        "inspect",
        "--format",
        '{{index .Config.Labels "org.opencontainers.image.revision"}}',
        container,
    ).stdout.strip()
    assert revision == _EXPECTED_REVISION
    network_mode = _docker(
        "inspect", "--format", "{{.HostConfig.NetworkMode}}", container
    ).stdout.strip()
    assert network_mode == "none"


def _start_audiobookshelf(
    container: str,
    config_path: Path,
    metadata_path: Path,
    media_path: Path,
    *,
    initialized: bool,
) -> None:
    config_path.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata_path.mkdir(mode=0o700, parents=True, exist_ok=True)
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
        "768m",
        "--memory-swap",
        "768m",
        "--security-opt",
        "no-new-privileges:true",
        "--cap-drop",
        "ALL",
        "-e",
        "TZ=UTC",
        "-v",
        f"{config_path}:/config",
        "-v",
        f"{metadata_path}:/metadata",
        "-v",
        f"{media_path}:/audiobooks:ro",
        _IMAGE,
    )
    _assert_exact_image(container)
    _wait_for_status(container, initialized=initialized)


def _generate_synthetic_files(media_path: Path, fixture_path: Path) -> None:
    book_path = media_path / "Synthetic Author" / "Control Plane Book"
    book_path.mkdir(mode=0o700, parents=True)
    fixture_path.mkdir(mode=0o700)

    def run_ffmpeg(*arguments: str) -> None:
        _docker(
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
            f"{media_path}:/audiobooks:rw",
            "-v",
            f"{fixture_path}:/fixtures:rw",
            "--entrypoint",
            "ffmpeg",
            _IMAGE,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            *arguments,
        )

    run_ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:duration=4",
        "-metadata",
        "title=Control Plane Book",
        "-metadata",
        "artist=Synthetic Author",
        "/audiobooks/Synthetic Author/Control Plane Book/control-plane.mp3",
    )
    for phase, colour in (("phase-a", "red"), ("phase-b", "blue")):
        run_ffmpeg(
            "-f",
            "lavfi",
            "-i",
            f"color=c={colour}:s=48x48",
            "-frames:v",
            "1",
            f"/fixtures/{phase}.png",
        )


def _login(container: str, username: str, password: str) -> dict[str, Any]:
    response = _request_json(
        container,
        "POST",
        "/login",
        body={"username": username, "password": password},
    )
    user = response["user"]
    assert user["username"] == username
    assert isinstance(user["accessToken"], str) and user["accessToken"]
    return cast(dict[str, Any], user)


def _wait_for_library_item(container: str, token: str, library_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        response = _request_json(
            container,
            "GET",
            f"/api/libraries/{library_id}/items?limit=10&page=0",
            token=token,
        )
        if response["total"] == 1 and len(response["results"]) == 1:
            return cast(dict[str, Any], response["results"][0])
        time.sleep(0.5)
    raise RuntimeError("Audiobookshelf did not scan the disposable synthetic audiobook")


def _initialize_source(
    container: str,
) -> dict[str, str]:
    _request(
        container,
        "POST",
        "/init",
        body={"newRoot": {"username": _ROOT_USERNAME, "password": _ROOT_PASSWORD}},
    )
    _wait_for_status(container, initialized=True)
    root = _login(container, _ROOT_USERNAME, _ROOT_PASSWORD)
    token = root["accessToken"]

    extra_user = _request_json(
        container,
        "POST",
        "/api/users",
        token=token,
        body={
            "username": _EXTRA_USERNAME,
            "password": _EXTRA_PASSWORD,
            "type": "user",
            "isActive": True,
        },
    )["user"]
    library = _request_json(
        container,
        "POST",
        "/api/libraries",
        token=token,
        body={
            "name": "Synthetic Library",
            "mediaType": "book",
            "provider": "google",
            "folders": [{"fullPath": "/audiobooks"}],
            "settings": {"disableWatcher": True},
        },
    )
    library_id = library["id"]
    _request(container, "POST", f"/api/libraries/{library_id}/scan", token=token)
    item = _wait_for_library_item(container, token, library_id)
    item_id = item["id"]

    updated = _request_json(
        container,
        "PATCH",
        f"/api/items/{item_id}/media",
        token=token,
        body={
            "metadata": {
                "title": "phase-a-title",
                "description": "phase-a-description",
                "authors": [{"name": "Synthetic Author"}],
            }
        },
    )["libraryItem"]
    author_id = updated["media"]["metadata"]["authors"][0]["id"]
    _request_json(
        container,
        "PATCH",
        f"/api/items/{item_id}/cover",
        token=token,
        body={"cover": "/metadata/cache/phase-a.png"},
    )
    collection = _request_json(
        container,
        "POST",
        "/api/collections",
        token=token,
        body={
            "name": "phase-a-collection",
            "description": "phase-a-description",
            "libraryId": library_id,
            "books": [item_id],
        },
    )
    playlist = _request_json(
        container,
        "POST",
        "/api/playlists",
        token=token,
        body={
            "name": "phase-a-playlist",
            "description": "phase-a-description",
            "libraryId": library_id,
            "items": [{"libraryItemId": item_id}],
        },
    )
    _request(
        container,
        "PATCH",
        f"/api/me/progress/{item_id}",
        token=token,
        body={"duration": 4, "currentTime": 1, "progress": 0.25, "isFinished": False},
    )
    _request_json(
        container,
        "POST",
        f"/api/me/item/{item_id}/bookmark",
        token=token,
        body={"time": 1, "title": "phase-a-bookmark"},
    )
    api_key = _request_json(
        container,
        "POST",
        "/api/api-keys",
        token=token,
        body={"name": "disposable-drill-key", "userId": extra_user["id"], "isActive": True},
    )["apiKey"]
    return {
        "library_id": library_id,
        "item_id": item_id,
        "author_id": author_id,
        "collection_id": collection["id"],
        "playlist_id": playlist["id"],
        "api_key_id": api_key["id"],
    }


def _mutate_to_phase_b(container: str, ids: dict[str, str]) -> None:
    root = _login(container, _ROOT_USERNAME, _ROOT_PASSWORD)
    token = root["accessToken"]
    item_id = ids["item_id"]
    _request_json(
        container,
        "PATCH",
        f"/api/items/{item_id}/media",
        token=token,
        body={
            "metadata": {
                "title": "phase-b-title",
                "description": "phase-b-description",
                "authors": [{"name": "Synthetic Author"}],
            }
        },
    )
    _request_json(
        container,
        "PATCH",
        f"/api/items/{item_id}/cover",
        token=token,
        body={"cover": "/metadata/cache/phase-b.png"},
    )
    _request_json(
        container,
        "PATCH",
        f"/api/authors/{ids['author_id']}",
        token=token,
        body={"description": "phase-b-author-description"},
    )
    _request_json(
        container,
        "PATCH",
        f"/api/collections/{ids['collection_id']}",
        token=token,
        body={"name": "phase-b-collection", "description": "phase-b-description"},
    )
    _request_json(
        container,
        "PATCH",
        f"/api/playlists/{ids['playlist_id']}",
        token=token,
        body={"name": "phase-b-playlist", "description": "phase-b-description"},
    )
    _request(
        container,
        "DELETE",
        f"/api/collections/{ids['collection_id']}/book/{item_id}",
        token=token,
    )
    _request_json(
        container,
        "POST",
        f"/api/collections/{ids['collection_id']}/book",
        token=token,
        body={"id": item_id},
    )
    second_playlist = _request_json(
        container,
        "POST",
        "/api/playlists",
        token=token,
        body={
            "name": "phase-b-membership-playlist",
            "description": "phase-b-membership-change",
            "libraryId": ids["library_id"],
            "items": [{"libraryItemId": item_id}],
        },
    )
    ids["phase_b_playlist_id"] = second_playlist["id"]
    _request(
        container,
        "PATCH",
        f"/api/me/progress/{item_id}",
        token=token,
        body={"duration": 4, "currentTime": 4, "progress": 1, "isFinished": True},
    )
    _request_json(
        container,
        "PATCH",
        f"/api/me/item/{item_id}/bookmark",
        token=token,
        body={"time": 1, "title": "phase-b-bookmark"},
    )
    _request_json(
        container,
        "PATCH",
        f"/api/api-keys/{ids['api_key_id']}",
        token=token,
        body={"isActive": False},
    )


def _replace_disposable_author_image(
    container: str,
    source_config: Path,
    source_metadata: Path,
    fixture: Path,
    author_id: str,
) -> None:
    # v2.36.0 correctly blocks loopback/private URLs in its author-image API.
    # The drill has no network by design, so install this one synthetic reference
    # only while the disposable source is stopped, then boot the exact image again.
    _docker("stop", "--time", "30", container)
    authors = source_metadata / "authors"
    authors.mkdir(mode=0o700, exist_ok=True)
    destination = authors / f"{author_id}.png"
    destination.unlink(missing_ok=True)
    shutil.copyfile(fixture, destination)
    with sqlite3.connect(source_config / "absdatabase.sqlite") as connection:
        updated = connection.execute(
            "UPDATE authors SET imagePath = ? WHERE id = ?",
            (f"/metadata/authors/{author_id}.png", author_id),
        ).rowcount
    assert updated == 1
    _docker("start", container)
    _wait_for_status(container, initialized=True)


def _create_completed_session(container: str, item_id: str) -> str:
    token = _login(container, _ROOT_USERNAME, _ROOT_PASSWORD)["accessToken"]
    session = _request_json(
        container,
        "POST",
        f"/api/items/{item_id}/play",
        token=token,
        body={
            "forceDirectPlay": True,
            "mediaPlayer": "disposable-drill",
            "deviceInfo": {
                "deviceId": "disposable-drill-device",
                "clientName": "disposable-drill",
                "clientVersion": "1",
            },
        },
    )
    _request(
        container,
        "POST",
        f"/api/session/{session['id']}/close",
        token=token,
        body={"currentTime": 3, "timeListened": 60, "duration": 4},
    )
    return cast(str, session["id"])


def _json_result(completed: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    for line in reversed(completed.stdout.splitlines()):
        if line.startswith("{"):
            result = json.loads(line)
            assert isinstance(result, dict)
            return result
    raise AssertionError(f"Plugin runner returned no JSON result: {completed.stderr}")


_RUNNER_GUARDS = r"""
import errno
import os
import socket
from pathlib import Path

def assert_runner_guards(read_only_sources):
    assert {name for _, name in socket.if_nameindex()} == {'lo'}
    assert not Path('/var/run/docker.sock').exists()
    assert not Path('/audiobooks').exists()
    assert not any(key.startswith('AUDIOBOOKSHELF_') or key.startswith('ABS_') for key in os.environ)
    cap_eff = next(line for line in Path('/proc/self/status').read_text().splitlines() if line.startswith('CapEff:'))
    assert int(cap_eff.split()[1], 16) == 0
    for source in read_only_sources:
        mount_line = next(
            line for line in Path('/proc/self/mountinfo').read_text().splitlines()
            if line.split()[4] == str(source)
        )
        assert 'ro' in mount_line.split()[5].split(',')
        try:
            (source / '.write-probe').write_text('must fail', encoding='utf-8')
        except OSError as exc:
            assert exc.errno in (errno.EROFS, errno.EACCES)
        else:
            raise RuntimeError(f'Audiobookshelf source mount is writable: {source}')
    try:
        Path('/rootfs-write-probe').write_text('must fail', encoding='utf-8')
    except OSError as exc:
        assert exc.errno in (errno.EROFS, errno.EACCES)
    else:
        raise RuntimeError('plugin runner root filesystem is writable')
"""


def _run_backup(
    runner_image: str,
    source_config: Path,
    source_metadata: Path,
    artifact_root: Path,
) -> Path:
    script = f"""
import asyncio
import json
from pathlib import Path
from app.core.plugins.artifacts import validate_backup_artifact
from app.core.plugins.base import BackupContext
from app.plugins.audiobookshelf.plugin import AudiobookshelfPlugin
{_RUNNER_GUARDS}

async def main():
    config_path = Path('/sources/audiobookshelf/config')
    metadata_path = Path('/sources/audiobookshelf/metadata')
    assert_runner_guards((config_path, metadata_path))
    plugin = AudiobookshelfPlugin(name='audiobookshelf')
    config = {{'config_path': str(config_path), 'metadata_path': str(metadata_path)}}
    assert await plugin.test(config) is True
    context = BackupContext(
        job_id='audiobookshelf-drill',
        target_id='audiobookshelf-source',
        config=config,
        metadata={{'target_slug': 'audiobookshelf-drill'}},
    )
    result = await plugin.backup(context)
    validated = validate_backup_artifact(result['artifact_path'], plugin, context)
    result['validated_sha256'] = validated.sha256
    result['validated_size_bytes'] = validated.size_bytes
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
        "/tmp:rw,noexec,nosuid,size=256m",
        "--security-opt",
        "no-new-privileges:true",
        "--cap-drop",
        "ALL",
        "-v",
        f"{source_config}:/sources/audiobookshelf/config:ro",
        "-v",
        f"{source_metadata}:/sources/audiobookshelf/metadata:ro",
        "-v",
        f"{artifact_root}:/backups:rw",
        runner_image,
        "python",
        "-c",
        script,
    )
    assert not (source_config / ".write-probe").exists()
    assert not (source_metadata / ".write-probe").exists()
    result = _json_result(completed)
    container_path = Path(result["artifact_path"])
    assert container_path.is_absolute()
    artifact = artifact_root / container_path.relative_to("/backups")
    assert result["validated_sha256"] == _sha256(artifact)
    assert result["validated_size_bytes"] == artifact.stat().st_size
    return artifact


def _run_backup_with_active_progress(
    runner_image: str,
    source_container: str,
    source_config: Path,
    source_metadata: Path,
    artifact_root: Path,
    item_id: str,
) -> Path:
    token = _login(source_container, _ROOT_USERNAME, _ROOT_PASSWORD)["accessToken"]
    stop = threading.Event()
    started = threading.Event()
    errors: list[BaseException] = []
    writes = 0

    def write_progress() -> None:
        nonlocal writes
        try:
            while not stop.is_set():
                _request(
                    source_container,
                    "PATCH",
                    f"/api/me/progress/{item_id}",
                    token=token,
                    body={
                        "duration": 4,
                        "currentTime": 1,
                        "progress": 0.25,
                        "isFinished": False,
                    },
                )
                writes += 1
                started.set()
                stop.wait(0.02)
        except BaseException as exc:
            errors.append(exc)
            started.set()

    writer = threading.Thread(target=write_progress, daemon=True)
    writer.start()
    assert started.wait(10), "progress writer did not start"
    try:
        artifact = _run_backup(
            runner_image,
            source_config,
            source_metadata,
            artifact_root,
        )
    finally:
        stop.set()
        writer.join(10)
    assert not writer.is_alive()
    assert not errors
    assert writes >= 2
    return artifact


def _run_restore(
    runner_image: str,
    artifact_root: Path,
    artifact: Path,
    restore_root: Path,
) -> dict[str, Any]:
    relative_artifact = artifact.relative_to(artifact_root)
    script = f"""
import asyncio
import json
from pathlib import Path
from app.core.plugins.base import RestoreContext
from app.plugins.audiobookshelf.plugin import AudiobookshelfPlugin
{_RUNNER_GUARDS}

async def main():
    assert_runner_guards(())
    plugin = AudiobookshelfPlugin(name='audiobookshelf')
    config = {{
        'config_path': '/restore/config',
        'metadata_path': '/restore/metadata',
    }}
    result = await plugin.restore(RestoreContext(
        job_id='audiobookshelf-restore',
        source_target_id='audiobookshelf-source',
        destination_target_id='audiobookshelf-restore',
        config=config,
        artifact_path='/backups/{relative_artifact.as_posix()}',
        metadata={{'source_target_slug': 'audiobookshelf-drill'}},
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
        "/tmp:rw,noexec,nosuid,size=256m",
        "--security-opt",
        "no-new-privileges:true",
        "--cap-drop",
        "ALL",
        "-v",
        f"{artifact_root}:/backups:ro",
        "-v",
        f"{restore_root}:/restore:rw",
        runner_image,
        "python",
        "-c",
        script,
    )
    return _json_result(completed)


def _inspect_artifact(
    artifact: Path,
    expected_image_digest: str,
) -> dict[str, str]:
    assert artifact.is_file() and not artifact.is_symlink()
    assert artifact.suffix == ".audiobookshelf"
    assert artifact.stat().st_size > 0
    assert stat.S_IMODE(artifact.stat().st_mode) == 0o600
    sidecar_path = Path(f"{artifact}.meta.json")
    assert sidecar_path.is_file() and not sidecar_path.is_symlink()

    sidecar = read_backup_sidecar(str(artifact))
    assert sidecar is not None
    assert sidecar["plugin_name"] == "audiobookshelf"
    assert sidecar["target_slug"] == "audiobookshelf-drill"
    assert sidecar["artifact_path"] == str(
        Path("/backups") / artifact.relative_to(artifact.parents[2])
    )
    assert sidecar["created_at"]

    with zipfile.ZipFile(artifact) as archive:
        assert archive.testzip() is None
        files = [entry for entry in archive.infolist() if not entry.is_dir()]
        names = [entry.filename for entry in files]
        assert len(names) == len(set(names))
        assert "absdatabase.sqlite" in names
        assert "details" in names
        assert all(not name.startswith(("/", "../")) and "/../" not in name for name in names)
        assert {name.split("/", 1)[0] for name in names} <= {
            "absdatabase.sqlite",
            "details",
            "metadata-items",
            "metadata-authors",
        }
        assert not any(Path(name).suffix.lower() in _MEDIA_EXTENSIONS for name in names)
        assert any(
            name.startswith("metadata-items/") and name.endswith("metadata.json") for name in names
        )
        item_images = [
            name
            for name in names
            if name.startswith("metadata-items/")
            and Path(name).suffix.lower() in {".jpg", ".png", ".webp"}
        ]
        author_images = [
            name
            for name in names
            if name.startswith("metadata-authors/")
            and Path(name).suffix.lower() in {".jpg", ".png", ".webp"}
        ]
        assert item_images and author_images
        item_hashes = {hashlib.sha256(archive.read(name)).hexdigest() for name in item_images}
        author_hashes = {hashlib.sha256(archive.read(name)).hexdigest() for name in author_images}
        assert expected_image_digest in item_hashes
        assert expected_image_digest in author_hashes
        details = json.loads(archive.read("details"))
        assert details["serverVersion"] == _EXPECTED_VERSION
        return {
            "artifact": _sha256(artifact),
            "database": hashlib.sha256(archive.read("absdatabase.sqlite")).hexdigest(),
            "item_image": expected_image_digest,
            "author_image": expected_image_digest,
        }


def _assert_restored_files(config_path: Path, metadata_path: Path) -> None:
    database = config_path / "absdatabase.sqlite"
    assert database.is_file() and not database.is_symlink()
    assert stat.S_IMODE(database.stat().st_mode) == 0o600
    assert (metadata_path / "items").is_dir()
    assert (metadata_path / "authors").is_dir()
    assert not any(path.suffix.lower() in _MEDIA_EXTENSIONS for path in config_path.rglob("*"))
    assert not any(path.suffix.lower() in _MEDIA_EXTENSIONS for path in metadata_path.rglob("*"))
    assert not (config_path / _CONFIG_SENTINEL_NAME).exists()
    assert not (metadata_path / _METADATA_SENTINEL_NAME).exists()


def _assert_application_state(
    container: str,
    ids: dict[str, str],
    *,
    phase: str,
    image_digest: str,
    api_key_active: bool,
) -> None:
    status = _request_json(container, "GET", "/status")
    assert status["serverVersion"] == _EXPECTED_VERSION
    assert status["isInit"] is True
    root = _login(container, _ROOT_USERNAME, _ROOT_PASSWORD)
    token = root["accessToken"]
    assert root["type"] == "root"

    users = _request_json(container, "GET", "/api/users", token=token)["users"]
    assert {_ROOT_USERNAME, _EXTRA_USERNAME} <= {user["username"] for user in users}
    _login(container, _EXTRA_USERNAME, _EXTRA_PASSWORD)

    libraries = _request_json(container, "GET", "/api/libraries", token=token)["libraries"]
    assert [(library["id"], library["name"]) for library in libraries] == [
        (ids["library_id"], "Synthetic Library")
    ]
    item = _request_json(container, "GET", f"/api/items/{ids['item_id']}", token=token)
    assert item["media"]["metadata"]["title"] == f"{phase}-title"
    assert item["media"]["metadata"]["description"] == f"{phase}-description"
    assert item["path"].startswith("/audiobooks/")
    assert item["media"]["audioFiles"]
    assert item["media"]["audioFiles"][0]["metadata"]["path"].startswith("/audiobooks/")

    _, cover = _request(container, "GET", f"/api/items/{ids['item_id']}/cover?raw=1")
    _, author_image = _request(container, "GET", f"/api/authors/{ids['author_id']}/image?raw=1")
    assert hashlib.sha256(cover).hexdigest() == image_digest
    assert hashlib.sha256(author_image).hexdigest() == image_digest

    collections = _request_json(container, "GET", "/api/collections", token=token)["collections"]
    assert [(collection["id"], collection["name"]) for collection in collections] == [
        (ids["collection_id"], f"{phase}-collection")
    ]
    playlists = _request_json(container, "GET", "/api/playlists", token=token)["playlists"]
    expected_playlists = [(ids["playlist_id"], f"{phase}-playlist")]
    if phase == "phase-b":
        expected_playlists.append((ids["phase_b_playlist_id"], "phase-b-membership-playlist"))
    assert sorted((playlist["id"], playlist["name"]) for playlist in playlists) == sorted(
        expected_playlists
    )
    progress = _request_json(container, "GET", "/api/me/progress", token=token)["mediaProgress"]
    assert len(progress) == 1
    assert progress[0]["libraryItemId"] == ids["item_id"]
    if phase == "phase-a":
        assert progress[0]["currentTime"] in {0, 1, 3}
    else:
        assert progress[0]["currentTime"] == 4
    if phase == "phase-b":
        assert progress[0]["isFinished"] is True
    bookmarks = _request_json(container, "GET", "/api/me/bookmarks", token=token)["bookmarks"]
    assert [(bookmark["time"], bookmark["title"]) for bookmark in bookmarks] == [
        (1, f"{phase}-bookmark")
    ]
    api_keys = _request_json(container, "GET", "/api/api-keys", token=token)["apiKeys"]
    drill_key = next(key for key in api_keys if key["id"] == ids["api_key_id"])
    assert drill_key["isActive"] is api_key_active
    sessions = _request_json(
        container,
        "GET",
        "/api/me/listening-sessions?itemsPerPage=20",
        token=token,
    )["sessions"]
    assert ids["completed_session_id"] in {session["id"] for session in sessions}


def test_two_live_backups_restore_to_fresh_exact_audiobookshelf_images(
    tmp_path: Path,
) -> None:
    if os.getuid() != 1000 or os.getgid() != 1000:
        pytest.skip("the exact bind-mount drill requires host UID:GID 1000:1000")
    plugin_package = importlib.util.find_spec("app.plugins.audiobookshelf")
    plugin_module = (
        importlib.util.find_spec("app.plugins.audiobookshelf.plugin")
        if plugin_package is not None
        else None
    )
    assert (
        plugin_module is not None
    ), "RED: Plan 009's audiobookshelf plugin has not been implemented"

    disposable_root = tmp_path.resolve()
    assert not str(disposable_root).startswith(("/docker-apps", "/mnt/nas"))
    suffix = uuid.uuid4().hex[:10]
    source_container = f"codex-audiobookshelf-source-{suffix}"
    restore_containers = [f"codex-audiobookshelf-restore-{number}-{suffix}" for number in (1, 2)]
    containers = [source_container, *restore_containers]
    runner_image = f"codex-homelab-backup-audiobookshelf-runner:{suffix}"
    source_config = tmp_path / "source-config"
    source_metadata = tmp_path / "source-metadata"
    media_path = tmp_path / "external-media"
    fixture_path = tmp_path / "image-fixtures"
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir(mode=0o700)

    try:
        _generate_synthetic_files(media_path, fixture_path)
        media_file = next(media_path.rglob("*.mp3"))
        media_digest = _sha256(media_file)
        image_digests = {
            phase: _sha256(fixture_path / f"{phase}.png") for phase in ("phase-a", "phase-b")
        }
        assert image_digests["phase-a"] != image_digests["phase-b"]

        # Stage both local image variants before the source container starts so
        # the host test never needs to write through a root-owned application tree.
        cache = source_metadata / "cache"
        cache.mkdir(mode=0o700, parents=True)
        for phase in ("phase-a", "phase-b"):
            shutil.copyfile(fixture_path / f"{phase}.png", cache / f"{phase}.png")

        _docker("build", "-t", runner_image, str(_BACKEND_ROOT))
        _start_audiobookshelf(
            source_container,
            source_config,
            source_metadata,
            media_path,
            initialized=False,
        )
        ids = _initialize_source(source_container)
        _replace_disposable_author_image(
            source_container,
            source_config,
            source_metadata,
            fixture_path / "phase-a.png",
            ids["author_id"],
        )
        ids["completed_session_id"] = _create_completed_session(
            source_container,
            ids["item_id"],
        )

        artifacts: list[Path] = []
        signatures: list[dict[str, str]] = []
        for run_number, phase in enumerate(("phase-a", "phase-b"), start=1):
            if phase == "phase-b":
                _mutate_to_phase_b(source_container, ids)
                _replace_disposable_author_image(
                    source_container,
                    source_config,
                    source_metadata,
                    fixture_path / "phase-b.png",
                    ids["author_id"],
                )
            _assert_application_state(
                source_container,
                ids,
                phase=phase,
                image_digest=image_digests[phase],
                api_key_active=phase == "phase-a",
            )

            if phase == "phase-a":
                artifact = _run_backup_with_active_progress(
                    runner_image,
                    source_container,
                    source_config,
                    source_metadata,
                    artifact_root,
                    ids["item_id"],
                )
            else:
                artifact = _run_backup(
                    runner_image,
                    source_config,
                    source_metadata,
                    artifact_root,
                )
            signature = _inspect_artifact(artifact, image_digests[phase])
            assert signature["artifact"] != media_digest
            artifacts.append(artifact)
            signatures.append(signature)

            restore_root = tmp_path / f"restore-{run_number}"
            restored_config = restore_root / "config"
            restored_metadata = restore_root / "metadata"
            restored_config.mkdir(mode=0o700, parents=True)
            restored_metadata.mkdir(mode=0o700, parents=True)
            (restored_config / _CONFIG_SENTINEL_NAME).write_text(
                _RESTORE_SENTINEL_CONTENT,
                encoding="utf-8",
            )
            (restored_metadata / _METADATA_SENTINEL_NAME).write_text(
                _RESTORE_SENTINEL_CONTENT,
                encoding="utf-8",
            )
            restore_result = _run_restore(
                runner_image,
                artifact_root,
                artifact,
                restore_root,
            )
            assert restore_result["status"] == "partial"
            _assert_restored_files(restored_config, restored_metadata)

            _start_audiobookshelf(
                restore_containers[run_number - 1],
                restored_config,
                restored_metadata,
                media_path,
                initialized=True,
            )
            _assert_application_state(
                restore_containers[run_number - 1],
                ids,
                phase=phase,
                image_digest=image_digests[phase],
                api_key_active=phase == "phase-a",
            )
            _docker("restart", restore_containers[run_number - 1])
            _wait_for_status(restore_containers[run_number - 1], initialized=True)
            _assert_application_state(
                restore_containers[run_number - 1],
                ids,
                phase=phase,
                image_digest=image_digests[phase],
                api_key_active=phase == "phase-a",
            )
            assert _sha256(media_file) == media_digest

        assert artifacts[0] != artifacts[1]
        assert signatures[0]["artifact"] != signatures[1]["artifact"]
        assert signatures[0]["database"] != signatures[1]["database"]
        assert signatures[0]["item_image"] != signatures[1]["item_image"]
        assert signatures[0]["author_image"] != signatures[1]["author_image"]
    finally:
        for container in containers:
            _docker("rm", "-f", container, check=False)
        _docker("image", "rm", "-f", runner_image, check=False)
