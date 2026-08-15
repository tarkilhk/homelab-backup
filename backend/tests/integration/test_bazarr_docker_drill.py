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
from typing import Any, cast

import pytest
import yaml  # type: ignore[import-untyped]

from app.core.plugins.sidecar import read_backup_sidecar

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_BAZARR_DOCKER_DRILL") != "1",
    reason="set RUN_BAZARR_DOCKER_DRILL=1 for the isolated Bazarr 1.5.6 drill",
)

_IMAGE = (
    "ghcr.io/linuxserver/bazarr@"
    "sha256:4b00f5886f3307563cf06c1068037eccfc529f04070d42e2aa47f53128eed17e"
)
_EXPECTED_VERSION = "1.5.6"
_EXPECTED_PACKAGE_VERSION = "v1.5.6-ls349 by linuxserver.io"
_EXPECTED_REVISION = "a7a7114ee805e7926cdbeea865691d10d69f821a"
_EXPECTED_MIGRATION = "df76a4410347"
_EXPECTED_TABLES = {
    "alembic_version",
    "system",
    "table_announcements",
    "table_blacklist",
    "table_blacklist_movie",
    "table_episodes",
    "table_history",
    "table_history_movie",
    "table_languages_profiles",
    "table_movies",
    "table_movies_rootfolder",
    "table_settings_languages",
    "table_settings_notifier",
    "table_shows",
    "table_shows_rootfolder",
}
_RESTORE_SENTINEL = ".bazarr-restore-destination"
_RESTORE_SENTINEL_CONTENT = "bazarr-v1.5.6-isolated-restore-v1\n"
_BACKEND_ROOT = Path(__file__).resolve().parents[2]


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
        # All resources are synthetic. Still keep the generated API key and
        # recovered config values out of a subprocess failure.
        stderr = (exc.stderr or "")[-4000:]
        stdout = (exc.stdout or "")[-4000:]
        raise RuntimeError(f"Disposable Bazarr Docker command failed:\n{stderr}\n{stdout}") from exc


def _sha256(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _request_json(container: str, path: str) -> Any:
    # Read the synthetic key and perform the request inside the disposable
    # service so neither the key nor an authenticated URL enters argv.
    script = r"""
import json, sys, urllib.error, urllib.request
request = json.load(sys.stdin)
try:
    import yaml
except ImportError:
    sys.path.insert(0, '/app/bazarr/bin/libs')
    import yaml
config = yaml.safe_load(open('/config/config/config.yaml', encoding='utf-8'))
key = config['auth']['apikey']
url = 'http://127.0.0.1:6767/api' + request['path']
response = urllib.request.urlopen(
    urllib.request.Request(url, headers={'X-API-KEY': key}), timeout=15
)
print(json.dumps({'status': response.status, 'body': json.load(response)}))
"""
    completed = _docker(
        "exec",
        "-i",
        container,
        "python3",
        "-c",
        script,
        input_text=json.dumps({"path": path}, separators=(",", ":")),
    )
    result = json.loads(completed.stdout)
    assert result["status"] == 200
    return result["body"]


def _wait_for_bazarr(container: str) -> dict[str, Any]:
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        state = _docker(
            "inspect", "--format", "{{.State.Status}}", container, check=False
        ).stdout.strip()
        if state == "exited":
            logs = _docker("logs", container, check=False).stdout[-3000:]
            raise RuntimeError(f"Disposable Bazarr exited before readiness: {logs}")
        ping = _docker(
            "exec",
            container,
            "curl",
            "-fsS",
            "http://127.0.0.1:6767/api/system/ping",
            check=False,
        )
        if ping.returncode != 0:
            time.sleep(0.5)
            continue
        try:
            status = cast(dict[str, Any], _request_json(container, "/system/status")["data"])
        except (KeyError, json.JSONDecodeError, RuntimeError, AssertionError):
            time.sleep(0.5)
            continue
        if (
            status.get("bazarr_version") == _EXPECTED_VERSION
            and status.get("package_version") == _EXPECTED_PACKAGE_VERSION
            and status.get("database_engine") == "Sqlite 3.51.2"
            and status.get("database_migration") == _EXPECTED_MIGRATION
        ):
            return status
        time.sleep(0.5)
    raise RuntimeError(f"Disposable Bazarr container {container} did not become ready")


def _assert_exact_container(container: str, network: str) -> None:
    configured_image = _docker("inspect", "--format", "{{.Config.Image}}", container).stdout.strip()
    assert configured_image == _IMAGE
    revision = _docker(
        "inspect",
        "--format",
        '{{index .Config.Labels "org.opencontainers.image.revision"}}',
        container,
    ).stdout.strip()
    build_version = _docker(
        "inspect",
        "--format",
        '{{index .Config.Labels "build_version"}}',
        container,
    ).stdout.strip()
    assert revision == _EXPECTED_REVISION
    assert "v1.5.6-ls349" in build_version
    assert (
        _docker("network", "inspect", "--format", "{{.Internal}}", network).stdout.strip() == "true"
    )
    assert (
        _docker("inspect", "--format", "{{.HostConfig.NetworkMode}}", container).stdout.strip()
        == network
    )


def _start_bazarr(
    container: str,
    network: str,
    config_root: Path,
    media_root: Path,
) -> None:
    config_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    media_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    television = media_root / "TVShows"
    movies = media_root / "Movies"
    television.mkdir(mode=0o700, exist_ok=True)
    movies.mkdir(mode=0o700, exist_ok=True)
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
        "-v",
        f"{television}:/TVShows:ro",
        "-v",
        f"{movies}:/Movies:ro",
        _IMAGE,
    )
    _assert_exact_container(container, network)
    _wait_for_bazarr(container)


def _write_api_key_secret(config_root: Path, secret_file: Path) -> None:
    config = yaml.safe_load((config_root / "config" / "config.yaml").read_text(encoding="utf-8"))
    key = config["auth"]["apikey"]
    assert isinstance(key, str) and key
    secret_file.write_text(key, encoding="utf-8")
    secret_file.chmod(0o600)


def _set_synthetic_config_phase(config_root: Path, phase: str) -> None:
    config_path = config_root / "config" / "config.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert isinstance(config, dict) and isinstance(config.get("general"), dict)
    config["general"]["instance_name"] = f"Synthetic Bazarr {phase.upper()}"
    for service, port in (("sonarr", 8989), ("radarr", 7878)):
        service_config = config.get(service)
        assert isinstance(service_config, dict)
        service_config["ip"] = f"{service}-{phase}.invalid"
        service_config["port"] = port
        service_config["ssl"] = False
        service_config["base_url"] = ""
        service_config["apikey"] = f"synthetic-{service}-key-{phase}"
    staged = config_path.with_name("config.yaml.drill-staged")
    staged.write_text(yaml.safe_dump(config, sort_keys=True), encoding="utf-8")
    staged.chmod(0o600)
    os.replace(staged, config_path)


def _seed_phase(config_root: Path, phase: str) -> None:
    assert phase in {"a", "b"}
    database = config_root / "db" / "bazarr.db"
    assert database.is_file()
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        if phase == "a":
            items = json.dumps(
                [
                    {
                        "id": 1,
                        "language": "en",
                        "audio_exclude": "False",
                        "forced": "False",
                        "hi": "False",
                    }
                ],
                separators=(",", ":"),
            )
            connection.execute(
                """
                INSERT INTO table_languages_profiles
                    (profileId, cutoff, originalFormat, items, name,
                     mustContain, mustNotContain, tag)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (101, 1, 0, items, "Synthetic Profile A", "['alpha']", "['omega']", "a"),
            )
            connection.execute(
                "UPDATE table_settings_languages SET enabled = 1 WHERE code3 IN ('eng', 'fra')"
            )
            connection.execute(
                "UPDATE table_settings_notifier SET enabled = 1, url = ? WHERE name = 'Discord'",
                ("json://synthetic-a.invalid",),
            )
            connection.execute(
                """
                INSERT INTO table_shows
                    (sonarrSeriesId, tvdbId, path, title, profileId, monitored,
                     seriesType, tags, year)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    1001,
                    2001,
                    "/TVShows/Synthetic Series A",
                    "Synthetic Series A",
                    101,
                    "True",
                    "standard",
                    "[]",
                    "2026",
                ),
            )
            connection.execute(
                """
                INSERT INTO table_episodes
                    (episode, season, sonarrEpisodeId, sonarrSeriesId, path,
                     title, monitored, subtitles)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    1,
                    1,
                    3001,
                    1001,
                    "/TVShows/Synthetic Series A/S01E01.mkv",
                    "Synthetic Episode A",
                    "True",
                    "[]",
                ),
            )
            connection.execute(
                """
                INSERT INTO table_history
                    (id, action, description, language, provider, score,
                     sonarrEpisodeId, sonarrSeriesId, subs_id, subtitles_path,
                     timestamp, video_path, matched, not_matched)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    4001,
                    1,
                    "Synthetic history A",
                    "en",
                    "synthetic",
                    360,
                    3001,
                    1001,
                    "synthetic-sub-a",
                    "/TVShows/Synthetic Series A/S01E01.en.srt",
                    "2026-01-01 00:00:00",
                    "/TVShows/Synthetic Series A/S01E01.mkv",
                    "[]",
                    "[]",
                ),
            )
            connection.execute(
                """
                INSERT INTO table_blacklist
                    (id, language, provider, sonarr_episode_id,
                     sonarr_series_id, subs_id, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (5001, "en", "synthetic", 3001, 1001, "synthetic-sub-a", "2026-01-01"),
            )
        else:
            items = json.dumps(
                [
                    {
                        "id": 2,
                        "language": "es",
                        "audio_exclude": "False",
                        "forced": "True",
                        "hi": "False",
                    }
                ],
                separators=(",", ":"),
            )
            connection.execute(
                """
                UPDATE table_languages_profiles
                SET name = ?, cutoff = ?, originalFormat = ?, items = ?,
                    mustContain = ?, mustNotContain = ?, tag = ?
                WHERE profileId = 101
                """,
                ("Synthetic Profile B", 2, 1, items, "['beta']", "['delta']", "b"),
            )
            connection.execute(
                "UPDATE table_settings_languages SET enabled = 1 WHERE code3 = 'spa'"
            )
            connection.execute(
                "UPDATE table_settings_notifier SET url = ? WHERE name = 'Discord'",
                ("json://synthetic-b.invalid",),
            )
            connection.execute(
                """
                UPDATE table_shows
                SET title = ?, path = ?, year = ?
                WHERE sonarrSeriesId = 1001
                """,
                ("Synthetic Series B", "/TVShows/Synthetic Series B", "2027"),
            )
            connection.execute(
                """
                UPDATE table_episodes
                SET title = ?, path = ?
                WHERE sonarrEpisodeId = 3001
                """,
                ("Synthetic Episode B", "/TVShows/Synthetic Series B/S01E01.mkv"),
            )
            connection.execute(
                """
                INSERT INTO table_history
                    (id, action, description, language, provider, score,
                     sonarrEpisodeId, sonarrSeriesId, subs_id, subtitles_path,
                     timestamp, video_path, matched, not_matched)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    4002,
                    2,
                    "Synthetic history B",
                    "es",
                    "synthetic",
                    320,
                    3001,
                    1001,
                    "synthetic-sub-b",
                    "/TVShows/Synthetic Series B/S01E01.es.srt",
                    "2026-01-02 00:00:00",
                    "/TVShows/Synthetic Series B/S01E01.mkv",
                    "[]",
                    "[]",
                ),
            )
            connection.execute(
                """
                INSERT INTO table_blacklist
                    (id, language, provider, sonarr_episode_id,
                     sonarr_series_id, subs_id, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (5002, "es", "synthetic", 3001, 1001, "synthetic-sub-b", "2026-01-02"),
            )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def _assert_application_state(container: str, phase: str) -> None:
    expected_history = 1 if phase == "a" else 2
    expected_languages = {"eng", "fra"} if phase == "a" else {"eng", "fra", "spa"}
    expected_profile = f"Synthetic Profile {phase.upper()}"
    expected_must = ["alpha"] if phase == "a" else ["beta"]
    expected_must_not = ["omega"] if phase == "a" else ["delta"]
    expected_profile_language = "en" if phase == "a" else "es"
    expected_series = f"Synthetic Series {phase.upper()}"
    expected_descriptions = {"Synthetic history A"}
    if phase == "b":
        expected_descriptions.add("Synthetic history B")
    status = _request_json(container, "/system/status")["data"]
    assert status["bazarr_version"] == _EXPECTED_VERSION
    assert status["package_version"] == _EXPECTED_PACKAGE_VERSION
    assert status["database_engine"] == "Sqlite 3.51.2"
    assert status["database_migration"] == _EXPECTED_MIGRATION
    profiles = _request_json(container, "/system/languages/profiles")
    assert len(profiles) == 1
    assert profiles[0]["name"] == expected_profile
    assert profiles[0]["tag"] == phase
    assert profiles[0]["cutoff"] == (1 if phase == "a" else 2)
    assert profiles[0]["originalFormat"] == (0 if phase == "a" else 1)
    assert profiles[0]["mustContain"] == expected_must
    assert profiles[0]["mustNotContain"] == expected_must_not
    assert profiles[0]["items"][0]["language"] == expected_profile_language
    enabled = {
        item["code3"] for item in _request_json(container, "/system/languages") if item["enabled"]
    }
    assert enabled == expected_languages
    history = _request_json(container, "/episodes/history")
    blacklist = _request_json(container, "/episodes/blacklist")
    assert history["total"] == expected_history
    assert len(history["data"]) == expected_history
    assert len(blacklist["data"]) == expected_history
    assert {item["description"] for item in history["data"]} == expected_descriptions
    expected_subtitles = {"synthetic-sub-a"}
    if phase == "b":
        expected_subtitles.add("synthetic-sub-b")
    assert {item["subs_id"] for item in history["data"]} == expected_subtitles
    assert {item["subs_id"] for item in blacklist["data"]} == expected_subtitles
    series = _request_json(container, "/series")
    assert series["total"] == 1
    assert len(series["data"]) == 1
    assert series["data"][0]["title"] == expected_series
    assert series["data"][0]["path"] == f"/TVShows/{expected_series}"
    assert series["data"][0]["year"] == ("2026" if phase == "a" else "2027")
    settings = _request_json(container, "/system/settings")
    assert settings["general"]["instance_name"] == f"Synthetic Bazarr {phase.upper()}"
    assert settings["sonarr"]["ip"] == f"sonarr-{phase}.invalid"
    assert settings["sonarr"]["port"] == 8989
    assert settings["radarr"]["ip"] == f"radarr-{phase}.invalid"
    assert settings["radarr"]["port"] == 7878
    discord = next(
        item for item in settings["notifications"]["providers"] if item["name"] == "Discord"
    )
    assert discord["enabled"] is True
    assert discord["url"].endswith(f"synthetic-{phase}.invalid")


_RUNNER_GUARDS = r"""
import errno
import os
import socket
from pathlib import Path

def assert_runner_guards(source=None, *, expect_network):
    interfaces = {name for _, name in socket.if_nameindex()}
    assert interfaces == ({'lo', 'eth0'} if expect_network else {'lo'})
    assert not Path('/var/run/docker.sock').exists()
    assert not Path('/Movies').exists()
    assert not Path('/TVShows').exists()
    cap_eff = next(
        line for line in Path('/proc/self/status').read_text().splitlines()
        if line.startswith('CapEff:')
    )
    assert int(cap_eff.split()[1], 16) == 0
    if source is not None:
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
            raise RuntimeError('Bazarr source backup mount is writable')
    try:
        Path('/rootfs-write-probe').write_text('must fail', encoding='utf-8')
    except OSError as exc:
        assert exc.errno in (errno.EROFS, errno.EACCES)
    else:
        raise RuntimeError('plugin runner root filesystem is writable')
"""


def _json_result(completed: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    for line in reversed(completed.stdout.splitlines()):
        if line.startswith("{"):
            result = json.loads(line)
            assert isinstance(result, dict)
            return result
    raise AssertionError(f"Plugin runner returned no JSON result: {completed.stderr}")


def _run_backup(
    runner_image: str,
    network: str,
    source_container: str,
    source_backup_directory: Path,
    api_key_file: Path,
    artifact_root: Path,
    run_number: int,
    runner_name: str,
) -> Path:
    script = f"""
import asyncio
import json
from pathlib import Path
from app.core.plugins.artifacts import validate_backup_artifact
from app.core.plugins.base import BackupContext
from app.plugins.bazarr.plugin import BazarrPlugin
{_RUNNER_GUARDS}

async def main():
    source = Path('/sources/bazarr/backups')
    assert_runner_guards(source, expect_network=True)
    plugin = BazarrPlugin(name='bazarr')
    config = {{
        'mode': 'source',
        'base_url': 'http://{source_container}:6767',
        'api_key': Path('/run/secrets/bazarr-api-key').read_text(encoding='utf-8'),
        'backup_directory': str(source),
    }}
    assert await plugin.test(config) is True
    context = BackupContext(
        job_id='bazarr-drill-{run_number}',
        target_id='bazarr-source',
        config=config,
        metadata={{'target_slug': 'bazarr-drill'}},
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
        "--name",
        runner_name,
        "--network",
        network,
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
        f"{source_backup_directory}:/sources/bazarr/backups:ro",
        "-v",
        f"{api_key_file}:/run/secrets/bazarr-api-key:ro",
        "-v",
        f"{artifact_root}:/backups:rw",
        runner_image,
        "python",
        "-c",
        script,
    )
    assert not (source_backup_directory / ".write-probe").exists()
    result = _json_result(completed)
    container_path = Path(result["artifact_path"])
    artifact = artifact_root / container_path.relative_to("/backups")
    assert result["validated_sha256"] == _sha256(artifact)
    assert result["validated_size_bytes"] == artifact.stat().st_size
    return artifact


def _inspect_artifact(artifact: Path, extraction_root: Path, phase: str) -> dict[str, Any]:
    assert artifact.is_file() and not artifact.is_symlink()
    assert stat.S_IMODE(artifact.stat().st_mode) == 0o600
    sidecar_path = Path(f"{artifact}.meta.json")
    assert sidecar_path.is_file() and not sidecar_path.is_symlink()
    assert stat.S_IMODE(sidecar_path.stat().st_mode) == 0o600
    sidecar = read_backup_sidecar(str(artifact))
    assert sidecar is not None
    assert sidecar["plugin_name"] == "bazarr"
    assert sidecar["target_slug"] == "bazarr-drill"
    assert sidecar["created_at"]
    assert sidecar["artifact_bytes"] == artifact.stat().st_size
    assert sidecar["sha256"] == _sha256(artifact)
    assert sidecar["application_version"] == _EXPECTED_VERSION
    assert sidecar["package_version"] == _EXPECTED_PACKAGE_VERSION
    assert sidecar["database_backend"] == "sqlite"
    assert sidecar["validation"] == "passed"
    assert set(sidecar["table_counts"]) == _EXPECTED_TABLES - {"sqlite_sequence"}
    serialized_sidecar = json.dumps(sidecar, sort_keys=True)
    for forbidden in (
        "synthetic-a.invalid",
        "synthetic-b.invalid",
        "synthetic-sonarr-key-a",
        "synthetic-sonarr-key-b",
        "synthetic-radarr-key-a",
        "synthetic-radarr-key-b",
        "/TVShows/",
        "/Movies/",
    ):
        assert forbidden not in serialized_sidecar
    with zipfile.ZipFile(artifact) as archive:
        assert archive.testzip() is None
        assert [entry.filename for entry in archive.infolist()] == ["bazarr.db", "config.yaml"]
        assert all(
            not entry.is_dir() and not (entry.flag_bits & 0x1) for entry in archive.infolist()
        )
        yaml_config = yaml.safe_load(archive.read("config.yaml"))
        assert isinstance(yaml_config, dict)
        assert yaml_config["postgresql"]["enabled"] is False
        database_path = extraction_root / f"bazarr-{phase}.db"
        with archive.open("bazarr.db") as source, database_path.open("xb") as destination:
            shutil.copyfileobj(source, destination)
        member_hashes = {
            name: hashlib.sha256(archive.read(name)).hexdigest()
            for name in ("bazarr.db", "config.yaml")
        }
    with sqlite3.connect(f"file:{database_path}?mode=ro", uri=True) as connection:
        assert connection.execute("PRAGMA quick_check").fetchall() == [("ok",)]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA user_version").fetchone() == (0,)
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert tables == _EXPECTED_TABLES
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
            _EXPECTED_MIGRATION,
        )
        profile = connection.execute(
            'SELECT name, tag FROM table_languages_profiles WHERE "profileId" = 101'
        ).fetchone()
        history_count = connection.execute("SELECT count(*) FROM table_history").fetchone()[0]
        blacklist_count = connection.execute("SELECT count(*) FROM table_blacklist").fetchone()[0]
    assert profile == (f"Synthetic Profile {phase.upper()}", phase)
    assert history_count == (1 if phase == "a" else 2)
    assert blacklist_count == history_count
    return {
        "artifact": _sha256(artifact),
        "size": artifact.stat().st_size,
        "database": member_hashes["bazarr.db"],
        "config": member_hashes["config.yaml"],
        "history_count": history_count,
    }


def _run_restore(
    runner_image: str,
    artifact_root: Path,
    artifact: Path,
    restore_parent: Path,
    runner_name: str,
) -> dict[str, Any]:
    relative_artifact = artifact.relative_to(artifact_root)
    artifact_identity = (artifact.stat().st_dev, artifact.stat().st_ino)
    artifact_sha256 = _sha256(artifact)
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

def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()

def main():
    assert_runner_guards(expect_network=False)
    artifact = Path('/backups/{relative_artifact.as_posix()}')
    engine = create_engine(
        'sqlite://',
        connect_args={{'check_same_thread': False}},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
    try:
        source = Target(
            name='Synthetic Bazarr Source',
            slug='bazarr-drill',
            plugin_name='bazarr',
            plugin_config_json=json.dumps({{'mode': 'source'}}),
        )
        destination = Target(
            name='Synthetic Bazarr Restore',
            slug='bazarr-restore',
            plugin_name='bazarr',
            plugin_config_json=json.dumps({{
                'mode': 'restore_destination',
                'restore_directory': '/restore/destination',
            }}),
        )
        session.add_all([source, destination])
        session.commit()
        source_run = Run(
            status='success',
            operation='backup',
            started_at=datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc),
        )
        session.add(source_run)
        session.commit()
        source_target_run = TargetRun(
            run_id=source_run.id,
            target_id=source.id,
            status='success',
            operation='backup',
            artifact_path=str(artifact),
            artifact_bytes=artifact.stat().st_size,
            sha256=sha256(artifact),
            source_identity_json=json.dumps({{
                'database_backend': 'sqlite',
                'database_migration': '{_EXPECTED_MIGRATION}',
            }}),
            started_at=source_run.started_at,
            finished_at=source_run.finished_at,
        )
        session.add(source_target_run)
        session.commit()
        result = RestoreService(session).restore(
            source_target_run_id=source_target_run.id,
            destination_target_id=destination.id,
            triggered_by='isolated_bazarr_exact_drill',
        )
        target_run = result.target_runs[0]
        print(json.dumps({{
            'status': result.status,
            'target_status': target_run.status,
            'restored_path': target_run.artifact_path,
            'message': target_run.message,
        }}, sort_keys=True))
    finally:
        session.close()

main()
"""
    completed = _docker(
        "run",
        "--rm",
        "--name",
        runner_name,
        "--network",
        "none",
        "-e",
        "HOMELAB_BACKUP_ALLOW_ISOLATED_RESTORE=1",
        "-e",
        "BACKUP_BASE_PATH=/backups",
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
        f"{artifact_root}:/backups:rw",
        "-v",
        f"{restore_parent}:/restore:rw",
        runner_image,
        "python",
        "-c",
        script,
    )
    result = _json_result(completed)
    assert (artifact.stat().st_dev, artifact.stat().st_ino) == artifact_identity
    assert _sha256(artifact) == artifact_sha256
    assert not list(artifact.parent.glob(".homelab-backup-restore-*"))
    return result


def _assert_restored_files(
    artifact: Path,
    restore_root: Path,
) -> None:
    files = {
        path.relative_to(restore_root).as_posix()
        for path in restore_root.rglob("*")
        if path.is_file()
    }
    assert files == {"config/config.yaml", "db/bazarr.db"}
    assert all(not path.is_symlink() for path in restore_root.rglob("*"))
    assert all(
        stat.S_IMODE((restore_root / relative).stat().st_mode) == 0o600 for relative in files
    )
    with zipfile.ZipFile(artifact) as archive:
        assert hashlib.sha256((restore_root / "config/config.yaml").read_bytes()).hexdigest() == (
            hashlib.sha256(archive.read("config.yaml")).hexdigest()
        )
        assert hashlib.sha256((restore_root / "db/bazarr.db").read_bytes()).hexdigest() == (
            hashlib.sha256(archive.read("bazarr.db")).hexdigest()
        )


def _write_payload(media_root: Path, phase: str) -> dict[str, str]:
    episode = media_root / "TVShows" / "Synthetic Series" / "S01E01.mkv"
    subtitle = media_root / "TVShows" / "Synthetic Series" / f"S01E01.{phase}.srt"
    episode.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    for previous_subtitle in episode.parent.glob("*.srt"):
        previous_subtitle.unlink()
    episode.write_bytes(f"synthetic-media-{phase}\n".encode())
    subtitle.write_bytes(f"synthetic-subtitle-payload-{phase}\n".encode())
    return {"episode": _sha256(episode), "subtitle": _sha256(subtitle)}


def _classify_payload(media_root: Path, expected: dict[str, str]) -> str:
    files = [path for path in media_root.rglob("*") if path.is_file()]
    if not files:
        return "missing"
    hashes = {_sha256(path) for path in files}
    return "matching" if hashes == set(expected.values()) else "mismatched"


def test_two_native_backups_restore_to_fresh_exact_bazarr_images(tmp_path: Path) -> None:
    if os.getuid() != 1000 or os.getgid() != 1000:
        pytest.skip("the exact bind-mount drill requires host UID:GID 1000:1000")
    plugin_package = importlib.util.find_spec("app.plugins.bazarr")
    plugin_module = (
        importlib.util.find_spec("app.plugins.bazarr.plugin")
        if plugin_package is not None
        else None
    )
    assert plugin_module is not None, "RED: Plan 012's Bazarr plugin has not been implemented"

    disposable_root = tmp_path.resolve()
    assert not str(disposable_root).startswith(("/docker-apps", "/mnt/nas"))
    suffix = uuid.uuid4().hex[:10]
    network = f"codex-bazarr-internal-{suffix}"
    source_container = f"codex-bazarr-source-{suffix}"
    restore_containers = [f"codex-bazarr-restore-{index}-{suffix}" for index in (1, 2)]
    backup_runners = [f"codex-bazarr-backup-runner-{index}-{suffix}" for index in (1, 2)]
    restore_runners = [f"codex-bazarr-restore-runner-{index}-{suffix}" for index in (1, 2)]
    containers = [source_container, *restore_containers, *backup_runners, *restore_runners]
    runner_image = f"codex-homelab-backup-bazarr-runner:{suffix}"
    source_config = tmp_path / "source-config"
    source_media = tmp_path / "source-media"
    api_key_file = tmp_path / "bazarr-api-key"
    artifact_root = tmp_path / "artifacts"
    inspection_root = tmp_path / "inspection"
    artifact_root.mkdir(mode=0o700)
    inspection_root.mkdir(mode=0o700)

    try:
        _docker("network", "create", "--internal", network)
        _docker("build", "-t", runner_image, str(_BACKEND_ROOT))
        _start_bazarr(source_container, network, source_config, source_media)
        _write_api_key_secret(source_config, api_key_file)

        _docker("stop", "--time", "30", source_container)
        payload_a = _write_payload(source_media, "a")
        _set_synthetic_config_phase(source_config, "a")
        _seed_phase(source_config, "a")
        _docker("start", source_container)
        _wait_for_bazarr(source_container)
        _assert_application_state(source_container, "a")

        artifacts: list[Path] = []
        signatures: list[dict[str, Any]] = []
        payloads: list[dict[str, str]] = [payload_a]
        payload_snapshots: dict[str, Path] = {}
        for run_number, phase in enumerate(("a", "b"), start=1):
            if phase == "b":
                _docker("stop", "--time", "30", source_container)
                payloads.append(_write_payload(source_media, "b"))
                _set_synthetic_config_phase(source_config, "b")
                _seed_phase(source_config, "b")
                _docker("start", source_container)
                _wait_for_bazarr(source_container)
                _assert_application_state(source_container, "b")
                # Native names have one-second resolution. Make a collision
                # impossible without weakening the plugin's exact attribution.
                time.sleep(1.1)

            payload_snapshot = tmp_path / f"payload-{phase}"
            shutil.copytree(source_media, payload_snapshot)
            payload_snapshots[phase] = payload_snapshot
            assert _classify_payload(payload_snapshot, payloads[-1]) == "matching"
            artifact = _run_backup(
                runner_image,
                network,
                source_container,
                source_config / "backup",
                api_key_file,
                artifact_root,
                run_number,
                backup_runners[run_number - 1],
            )
            signature = _inspect_artifact(artifact, inspection_root, phase)
            assert signature["artifact"] not in payloads[-1].values()
            with zipfile.ZipFile(artifact) as archive:
                recovered_bytes = archive.read("bazarr.db") + archive.read("config.yaml")
            assert f"synthetic-media-{phase}".encode() not in recovered_bytes
            assert f"synthetic-subtitle-payload-{phase}".encode() not in recovered_bytes
            artifacts.append(artifact)
            signatures.append(signature)

            restore_parent = tmp_path / f"restore-{run_number}"
            restore_parent.mkdir(mode=0o700)
            (restore_parent / _RESTORE_SENTINEL).write_text(
                _RESTORE_SENTINEL_CONTENT,
                encoding="utf-8",
            )
            result = _run_restore(
                runner_image,
                artifact_root,
                artifact,
                restore_parent,
                restore_runners[run_number - 1],
            )
            assert result["status"] == "partial"
            assert result["target_status"] == "partial"
            assert result["restored_path"] == "/restore/destination"
            restored_config = restore_parent / "destination"
            _assert_restored_files(artifact, restored_config)
            assert not (restore_parent / _RESTORE_SENTINEL).exists()
            assert set(restore_parent.iterdir()) == {restored_config}

            _start_bazarr(
                restore_containers[run_number - 1],
                network,
                restored_config,
                payload_snapshot,
            )
            _assert_application_state(restore_containers[run_number - 1], phase)
            _docker("restart", restore_containers[run_number - 1])
            _wait_for_bazarr(restore_containers[run_number - 1])
            _assert_application_state(restore_containers[run_number - 1], phase)

        assert artifacts[0] != artifacts[1]
        assert signatures[0]["artifact"] != signatures[1]["artifact"]
        assert signatures[0]["database"] != signatures[1]["database"]
        assert signatures[0]["config"] != signatures[1]["config"]
        assert artifacts[0].stat().st_size == signatures[0]["size"]
        assert _sha256(artifacts[0]) == signatures[0]["artifact"]
        with zipfile.ZipFile(artifacts[0]) as immutable_a:
            assert (
                hashlib.sha256(immutable_a.read("bazarr.db")).hexdigest()
                == signatures[0]["database"]
            )
            assert (
                hashlib.sha256(immutable_a.read("config.yaml")).hexdigest()
                == signatures[0]["config"]
            )
        sidecar_a = read_backup_sidecar(str(artifacts[0]))
        assert sidecar_a is not None
        assert sidecar_a["artifact_bytes"] == signatures[0]["size"]
        assert sidecar_a["sha256"] == signatures[0]["artifact"]
        assert signatures[0]["history_count"] == 1
        assert signatures[1]["history_count"] == 2
        assert payloads[0] != payloads[1]
        missing_payload = tmp_path / "payload-missing"
        missing_payload.mkdir(mode=0o700)
        assert _classify_payload(missing_payload, payloads[0]) == "missing"
        assert _classify_payload(payload_snapshots["a"], payloads[1]) == "mismatched"
        assert _classify_payload(payload_snapshots["b"], payloads[0]) == "mismatched"
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
