from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import multiprocessing
import os
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import threading
import warnings
import zipfile
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy.orm import Session

from app.core.plugins.base import BackupContext, RestoreContext
from app.core.plugins.loader import get_plugin, get_plugin_schema_path, list_plugins
from app.core.plugins.sidecar import read_backup_sidecar
from app.models import Run, Target, TargetRun
from app.services.restores import RestoreService

SOURCE_CONFIG: dict[str, object] = {
    "mode": "source",
    "database_path": "/sources/profilarr/profilarr.db",
    "repository_path": "/sources/profilarr/repository",
}

RESTORE_DESTINATION_CONFIG: dict[str, object] = {
    "mode": "restore_destination",
    "restore_directory": "/tmp/homelab-backup-isolated-restore/profilarr-alpha",
}

RESTORE_SENTINEL_NAME = ".profilarr-restore-destination"
RESTORE_SENTINEL_CONTENT = "profilarr-v1.1.5-isolated-restore-v1\n"

EXPECTED_PROFILARR_SCHEMA = {
    "arr_config": (
        "id",
        "name",
        "type",
        "tags",
        "arr_server",
        "api_key",
        "data_to_sync",
        "last_sync_time",
        "sync_percentage",
        "sync_method",
        "sync_interval",
        "import_as_unique",
        "import_task_id",
        "created_at",
        "updated_at",
    ),
    "auth": ("username", "password_hash", "api_key", "session_id", "created_at"),
    "backups": ("id", "filename", "created_at", "status"),
    "failed_attempts": ("id", "ip_address", "attempt_time"),
    "format_renames": ("format_name",),
    "language_import_config": ("id", "score", "updated_at"),
    "migrations": ("id", "version", "name", "applied_at"),
    "scheduled_tasks": (
        "id",
        "name",
        "type",
        "interval_minutes",
        "last_run",
        "status",
        "created_at",
    ),
    "settings": ("id", "key", "value", "updated_at"),
}

EXPECTED_MIGRATIONS = (
    (1, "initial_schema"),
    (2, "format_renames"),
    (3, "language_import_score"),
    (4, "update_language_score_default"),
)

AUTHORITATIVE_DIRECTORIES = (
    "regex_patterns",
    "custom_formats",
    "profiles",
    "media_management",
)


class _NoResultConnection:
    def __init__(self) -> None:
        self.closed = False

    def poll(self) -> bool:
        return False

    def recv(self) -> tuple[str, str, object]:
        raise AssertionError("No capture worker result is available")

    def close(self) -> None:
        self.closed = True


class _ResultConnection(_NoResultConnection):
    def __init__(self, result: tuple[str, str, object]) -> None:
        super().__init__()
        self.result = result

    def poll(self) -> bool:
        return True

    def recv(self) -> tuple[str, str, object]:
        return self.result


class _CompletedProcess:
    exitcode = 1

    def join(self, timeout: float) -> None:
        del timeout

    def is_alive(self) -> bool:
        return False


class _BlockingProcess:
    def __init__(self, *, hold_after_terminate: bool = False) -> None:
        self.pid: int | None = None
        self.exitcode: int | None = None
        self.join_started = threading.Event()
        self.terminate_called = threading.Event()
        self.kill_called = threading.Event()
        self.release = threading.Event()
        self._alive = True
        self._hold_after_terminate = hold_after_terminate

    def join(self, timeout: float) -> None:
        self.join_started.set()
        released = self.release.wait(timeout)
        if released and self.terminate_called.is_set():
            self._alive = False
            self.exitcode = -9 if self.kill_called.is_set() else -15

    def is_alive(self) -> bool:
        return self._alive

    def terminate(self) -> None:
        self.terminate_called.set()
        if not self._hold_after_terminate:
            self.release.set()

    def kill(self) -> None:
        self.terminate_called.set()
        self.kill_called.set()
        self.release.set()


def _stubborn_process_tree_worker(pid_path: str) -> None:
    os.setsid()
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    child = subprocess.Popen(
        (
            sys.executable,
            "-c",
            "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); " "time.sleep(60)",
        )
    )
    Path(pid_path).write_text(str(child.pid), encoding="ascii")
    child.wait()


def _process_is_live(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text(encoding="ascii").split()[2]
    except (FileNotFoundError, OSError, IndexError):
        return False
    return state != "Z"


def _plugin_class() -> type[Any]:
    plugin_class = importlib.import_module("app.plugins.profilarr.plugin").ProfilarrPlugin
    return cast(type[Any], plugin_class)


def _run_git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ("git", "-C", str(repository), *arguments),
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return completed.stdout.strip()


def _write_exact_database(path: Path, *, foreign_key_violation: bool = False) -> None:
    import_task_column = "INTEGER DEFAULT NULL"
    if foreign_key_violation:
        import_task_column += " REFERENCES scheduled_tasks(id)"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            f"""
            PRAGMA journal_mode=DELETE;
            CREATE TABLE backups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filename TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                status TEXT DEFAULT 'pending'
            );
            CREATE TABLE arr_config (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                type TEXT NOT NULL,
                tags TEXT,
                arr_server TEXT NOT NULL,
                api_key TEXT NOT NULL,
                data_to_sync TEXT,
                last_sync_time TIMESTAMP,
                sync_percentage INTEGER DEFAULT 0,
                sync_method TEXT DEFAULT 'manual',
                sync_interval INTEGER DEFAULT 0,
                import_as_unique BOOLEAN DEFAULT 0,
                import_task_id {import_task_column},
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE scheduled_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                type TEXT NOT NULL,
                interval_minutes INTEGER NOT NULL,
                last_run TIMESTAMP,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE settings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key TEXT UNIQUE NOT NULL,
                value TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE auth (
                username TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                api_key TEXT,
                session_id TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE failed_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ip_address TEXT NOT NULL,
                attempt_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE format_renames (
                format_name TEXT PRIMARY KEY NOT NULL
            );
            CREATE TABLE language_import_config (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                score INTEGER NOT NULL DEFAULT -99999,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE migrations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                version INTEGER NOT NULL,
                name TEXT NOT NULL,
                applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        connection.executemany(
            "INSERT INTO migrations(version, name) VALUES (?, ?)",
            EXPECTED_MIGRATIONS,
        )
        connection.execute(
            "INSERT INTO scheduled_tasks(name, type, interval_minutes) VALUES (?, ?, ?)",
            ("Repository Sync", "Sync", 2),
        )
        connection.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?)",
            ("gitRepo", "https://example.invalid/profiles.git"),
        )
        connection.execute(
            "INSERT INTO auth(username, password_hash, api_key, session_id) " "VALUES (?, ?, ?, ?)",
            ("synthetic-user", "synthetic-hash", "database-secret", "synthetic-session"),
        )
        connection.execute(
            "INSERT INTO language_import_config(score) VALUES (?)",
            (-999999,),
        )
        if foreign_key_violation:
            connection.execute(
                "INSERT INTO arr_config(name, type, arr_server, api_key, import_task_id) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    "synthetic-arr",
                    "radarr",
                    "http://arr.invalid",
                    "arr-secret",
                    999,
                ),
            )
        connection.commit()


def _write_clean_repository(repository: Path) -> None:
    repository.mkdir()
    _run_git(repository, "init", "--initial-branch=main")
    _run_git(repository, "config", "user.name", "Homelab Backup Tests")
    _run_git(repository, "config", "user.email", "tests@homelab.invalid")
    _run_git(
        repository,
        "config",
        "remote.origin.url",
        "https://synthetic-user:git-secret@example.invalid/private.git",
    )
    payloads = {
        "regex_patterns": (
            "name: Synthetic Regex\npattern: synthetic\ndescription: Test\n"
            "tags: [test]\ntests: [{input: synthetic, expected: true}]\n"
        ),
        "custom_formats": (
            "name: Synthetic Format\ndescription: Test\ntags: [test]\n"
            "conditions: [{name: Synthetic, type: release_title, required: true, "
            "negate: false, pattern: Synthetic Regex}]\n"
            "tests: [{input: synthetic, expected: true}]\n"
        ),
        "profiles": (
            "name: Synthetic Profile\ndescription: Test\ntags: [test]\n"
            "upgradesAllowed: true\nminCustomFormatScore: 0\nupgradeUntilScore: 100\n"
            "minScoreIncrement: 1\ncustom_formats: []\ncustom_formats_radarr: []\n"
            "custom_formats_sonarr: []\nqualities: [WEB-1080p]\n"
            "upgrade_until: WEB-1080p\nlanguage: Original\n"
        ),
    }
    for directory in AUTHORITATIVE_DIRECTORIES:
        current = repository / directory
        current.mkdir()
        if directory in payloads:
            (current / "default.yml").write_text(payloads[directory], encoding="utf-8")
    (repository / "media_management/misc.yml").write_text(
        "radarr: {propersRepacks: preferAndUpgrade, enableMediaInfo: true}\n"
        "sonarr: {propersRepacks: doNotPrefer, enableMediaInfo: false}\n",
        encoding="utf-8",
    )
    (repository / "media_management/naming.yml").write_text(
        "radarr: {rename: true, movieFormat: Synthetic Movie}\n"
        "sonarr: {rename: true, standardEpisodeFormat: Synthetic Episode}\n",
        encoding="utf-8",
    )
    (repository / "media_management/quality_definitions.yml").write_text(
        "qualityDefinitions: {radarr: {WEB-1080p: {preferred: 10}}, "
        "sonarr: {HDTV-720p: {preferred: 5}}}\n",
        encoding="utf-8",
    )
    (repository / ".gitignore").write_text(
        "profiles/ignored.yml\n",
        encoding="utf-8",
    )
    _run_git(repository, "add", ".")
    _run_git(repository, "commit", "-m", "Create exact Profilarr fixture")
    head = _run_git(repository, "rev-parse", "HEAD")
    _run_git(repository, "update-ref", "refs/heads/local-only", head)
    _run_git(repository, "tag", "fixture-v1", head)


def _source_tree_snapshot(root: Path) -> dict[str, tuple[int, int, int, str]]:
    snapshot: dict[str, tuple[int, int, int, str]] = {}
    for path in sorted((root, *root.rglob("*"))):
        status = path.lstat()
        if stat.S_ISREG(status.st_mode):
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        elif stat.S_ISLNK(status.st_mode):
            digest = f"link:{os.readlink(path)}"
        else:
            digest = "directory"
        snapshot[str(path.relative_to(root))] = (
            stat.S_IFMT(status.st_mode),
            stat.S_IMODE(status.st_mode),
            status.st_size,
            digest,
        )
    return snapshot


def _install_source_mounts(
    monkeypatch: pytest.MonkeyPatch,
    database: Path,
    repository: Path,
    *,
    mounted: frozenset[Path] | None = None,
    read_only: frozenset[Path] | None = None,
) -> list[Path]:
    expected_mounts = mounted if mounted is not None else frozenset({database, repository})
    expected_read_only = read_only if read_only is not None else frozenset({database, repository})
    observations: list[Path] = []

    def fake_is_mount(path: str | os.PathLike[str]) -> bool:
        current = Path(path)
        observations.append(current)
        return current in expected_mounts

    def fake_statvfs(path: str | os.PathLike[str]) -> SimpleNamespace:
        current = Path(path)
        observations.append(current)
        return SimpleNamespace(f_flag=os.ST_RDONLY if current in expected_read_only else 0)

    monkeypatch.setattr(os.path, "ismount", fake_is_mount)
    monkeypatch.setattr(os, "statvfs", fake_statvfs)
    return observations


def _prepare_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    foreign_key_violation: bool = False,
) -> tuple[dict[str, object], Path, Path, list[Path]]:
    source_root = tmp_path / "profilarr-source"
    source_root.mkdir()
    database = source_root / "profilarr.db"
    repository = source_root / "repository"
    _write_exact_database(database, foreign_key_violation=foreign_key_violation)
    _write_clean_repository(repository)
    plugin_module = importlib.import_module("app.plugins.profilarr.plugin")
    monkeypatch.setattr(plugin_module, "_SOURCE_ROOT", source_root, raising=False)
    observations = _install_source_mounts(monkeypatch, database, repository)
    return (
        {
            "mode": "source",
            "database_path": str(database),
            "repository_path": str(repository),
        },
        database,
        repository,
        observations,
    )


def _mutate_repository(repository: Path, case: str) -> None:
    git_directory = repository / ".git"
    if case == "dirty-tracked":
        (repository / "profiles/default.yml").write_text("name: dirty\n", encoding="utf-8")
    elif case == "staged":
        (repository / "profiles/default.yml").write_text("name: staged\n", encoding="utf-8")
        _run_git(repository, "add", "profiles/default.yml")
    elif case == "untracked-authority":
        (repository / "profiles/untracked.yml").write_text("name: unique\n", encoding="utf-8")
    elif case == "ignored-authority":
        (repository / "profiles/ignored.yml").write_text("name: ignored\n", encoding="utf-8")
    elif case == "executable-authority":
        path = repository / "profiles/default.yml"
        path.chmod(0o755)
        _run_git(repository, "add", "profiles/default.yml")
        _run_git(repository, "commit", "-m", "Make authoritative YAML executable")
    elif case == "index-lock":
        (git_directory / "index.lock").write_bytes(b"")
    elif case == "merge":
        (git_directory / "MERGE_HEAD").write_text(
            _run_git(repository, "rev-parse", "HEAD") + "\n",
            encoding="ascii",
        )
    elif case == "rebase":
        (git_directory / "rebase-merge").mkdir()
    elif case == "cherry-pick":
        (git_directory / "CHERRY_PICK_HEAD").write_text(
            _run_git(repository, "rev-parse", "HEAD") + "\n",
            encoding="ascii",
        )
    elif case == "bisect":
        (git_directory / "BISECT_LOG").write_text("synthetic bisect\n", encoding="utf-8")
    elif case == "shallow":
        (git_directory / "shallow").write_text(
            _run_git(repository, "rev-parse", "HEAD") + "\n",
            encoding="ascii",
        )
    elif case == "partial":
        _run_git(repository, "config", "core.repositoryformatversion", "1")
        _run_git(repository, "config", "extensions.partialclone", "origin")
        _run_git(repository, "config", "remote.origin.promisor", "true")
    elif case == "alternates":
        info = git_directory / "objects/info"
        info.mkdir(parents=True, exist_ok=True)
        (info / "alternates").write_text("/external/objects\n", encoding="utf-8")
    elif case == "submodule":
        (repository / ".gitmodules").write_text(
            '[submodule "external"]\n\tpath = profiles/external\n' "\turl = ../external.git\n",
            encoding="utf-8",
        )
        _run_git(repository, "add", ".gitmodules")
        _run_git(repository, "commit", "-m", "Add unsupported submodule declaration")
    elif case == "lfs":
        (repository / "custom_formats/default.yml").write_text(
            "version https://git-lfs.github.com/spec/v1\n"
            "oid sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef\n"
            "size 123\n",
            encoding="ascii",
        )
        _run_git(repository, "add", "custom_formats/default.yml")
        _run_git(repository, "commit", "-m", "Add unsupported LFS pointer")
    elif case == "replace-ref":
        head = _run_git(repository, "rev-parse", "HEAD")
        _run_git(repository, "update-ref", f"refs/replace/{head}", head)
    elif case == "detached":
        _run_git(repository, "checkout", "--detach")
    elif case == "unborn":
        _run_git(repository, "symbolic-ref", "HEAD", "refs/heads/unborn")
    elif case in {"missing-object", "corrupt-object"}:
        object_id = _run_git(repository, "rev-parse", "HEAD:profiles/default.yml")
        object_path = git_directory / "objects" / object_id[:2] / object_id[2:]
        if case == "missing-object":
            object_path.unlink()
        else:
            object_path.chmod(0o600)
            object_path.write_bytes(b"corrupt-object")
    else:
        raise AssertionError(f"Unknown Git fixture case: {case}")


def _install_no_restore_io(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("restore-destination probe attempted external or source I/O")

    async def forbidden_async(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("restore-destination probe attempted a subprocess")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden_async)
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)


def _backup_context(config: dict[str, object], backup_root: Path) -> BackupContext:
    return BackupContext(
        job_id="profilarr-backup",
        target_id="profilarr-source",
        config=config,
        metadata={
            "target_slug": "profilarr-source",
            "backup_root": str(backup_root),
        },
    )


def _prepare_restore_destination(
    tmp_path: Path,
    *,
    name: str = "profilarr-restore",
) -> tuple[Path, Path, Path]:
    parent = tmp_path / f"{name}-parent"
    parent.mkdir(mode=0o700)
    sentinel = parent / RESTORE_SENTINEL_NAME
    sentinel.write_text(RESTORE_SENTINEL_CONTENT, encoding="utf-8")
    sentinel.chmod(0o600)
    return parent, parent / name, sentinel


def _restore_context(
    artifact: Path,
    destination: Path,
    *,
    metadata: dict[str, object] | None = None,
) -> RestoreContext:
    return RestoreContext(
        job_id="profilarr-restore",
        source_target_id="profilarr-source",
        destination_target_id="profilarr-restore",
        config={"mode": "restore_destination", "restore_directory": str(destination)},
        artifact_path=str(artifact),
        metadata=(
            {
                "source_target_run_id": 42,
                "artifact_bytes": artifact.stat().st_size,
                "artifact_sha256": _sha256(artifact.read_bytes()),
            }
            if metadata is None
            else metadata
        ),
    )


async def _create_backup_for_restore(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Any, Path, dict[str, object], Path]:
    config, _database, repository, _observations = _prepare_source(tmp_path, monkeypatch)
    plugin_module = importlib.import_module("app.plugins.profilarr.plugin")
    backup_root = tmp_path / "backups"
    monkeypatch.setattr(plugin_module, "BACKUP_BASE_PATH", str(backup_root), raising=False)
    plugin = _plugin_class()(name="profilarr")
    result = await plugin.backup(_backup_context(config, backup_root))
    return plugin, Path(result["artifact_path"]), config, repository


def _configure_local_restore(
    monkeypatch: pytest.MonkeyPatch,
    restore_parent: Path,
) -> Any:
    plugin_module = importlib.import_module("app.plugins.profilarr.plugin")
    monkeypatch.setattr(plugin_module, "_RESTORE_ROOTS", (restore_parent,), raising=False)
    monkeypatch.setattr(plugin_module, "_network_interfaces", lambda: {"lo"})
    monkeypatch.setenv(plugin_module.ISOLATED_RESTORE_ENV, "1")
    return plugin_module


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _repository_refs(repository: Path) -> list[dict[str, str]]:
    output = _run_git(
        repository,
        "for-each-ref",
        "--format=%(refname)%00%(objectname)",
    )
    return [
        {"name": name, "oid": object_id}
        for name, object_id in sorted(line.split("\x00", 1) for line in output.splitlines())
    ]


def _authoritative_inventory(repository: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    count = 0
    for directory in AUTHORITATIVE_DIRECTORIES:
        for path in sorted((repository / directory).rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(repository).as_posix().encode("utf-8")
            digest.update(relative)
            digest.update(b"\x00")
            digest.update(hashlib.sha256(path.read_bytes()).digest())
            digest.update(b"\n")
            count += 1
    return count, digest.hexdigest()


def _private_zip_member(name: str) -> zipfile.ZipInfo:
    member = zipfile.ZipInfo(name)
    member.create_system = 3
    member.external_attr = (stat.S_IFREG | 0o600) << 16
    member.compress_type = zipfile.ZIP_DEFLATED
    return member


def _published_files(backup_root: Path) -> list[Path]:
    if not backup_root.exists():
        return []
    return [path for path in backup_root.rglob("*") if path.is_file()]


def _rewrite_malformed_capture(path: Path, case: str) -> None:
    with zipfile.ZipFile(path) as source:
        payloads = {name: source.read(name) for name in source.namelist()}
    manifest = json.loads(payloads["manifest.json"])
    if case == "payload-hash":
        manifest["database"]["sha256"] = "0" * 64
    elif case == "invalid-manifest":
        payloads["manifest.json"] = b"{"
    elif case == "invalid-bundle":
        payloads["repository.bundle"] = b"not a Git bundle"
        manifest["bundle"] = {
            "size": len(payloads["repository.bundle"]),
            "sha256": _sha256(payloads["repository.bundle"]),
        }
    elif case == "invalid-database":
        payloads["profilarr.db"] = b"not a SQLite database"
        manifest["database"]["size"] = len(payloads["profilarr.db"])
        manifest["database"]["sha256"] = _sha256(payloads["profilarr.db"])
    if case not in {"invalid-manifest", "invalid-bundle", "invalid-database"}:
        payloads["manifest.json"] = json.dumps(
            manifest,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    elif case in {"invalid-bundle", "invalid-database"}:
        payloads["manifest.json"] = json.dumps(
            manifest,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    path.unlink()
    with zipfile.ZipFile(path, mode="w") as archive:
        names = ["profilarr.db", "repository.bundle", "manifest.json"]
        if case == "traversal":
            names[-1] = "../manifest.json"
        for original_name, output_name in zip(
            ("profilarr.db", "repository.bundle", "manifest.json"),
            names,
        ):
            member = _private_zip_member(output_name)
            if case == "link" and original_name == "repository.bundle":
                member.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(member, payloads[original_name])
        if case == "extra":
            archive.writestr(_private_zip_member("unexpected"), b"no")
        elif case == "duplicate":
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                archive.writestr(
                    _private_zip_member("manifest.json"),
                    payloads["manifest.json"],
                )
    if case == "encrypted":
        raw = bytearray(path.read_bytes())
        local = raw.index(b"PK\x03\x04")
        central = raw.index(b"PK\x01\x02")
        local_flags = int.from_bytes(raw[local + 6 : local + 8], "little") | 0x1
        central_flags = int.from_bytes(raw[central + 8 : central + 10], "little") | 0x1
        raw[local + 6 : local + 8] = local_flags.to_bytes(2, "little")
        raw[central + 8 : central + 10] = central_flags.to_bytes(2, "little")
        path.write_bytes(raw)


@pytest.mark.asyncio
async def test_profilarr_discovery_schema_and_automatic_restore_contract() -> None:
    plugin_class = _plugin_class()
    plugin = get_plugin("profilarr")

    assert isinstance(plugin, plugin_class)
    assert plugin.restore_capability == "automatic"
    assert any(
        item["key"] == "profilarr" and item["restore_capability"] == "automatic"
        for item in list_plugins()
    )

    schema_path = get_plugin_schema_path("profilarr")
    assert schema_path is not None
    schema = json.loads(Path(schema_path).read_text(encoding="utf-8"))
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["mode"]
    assert set(schema["properties"]) == {
        "mode",
        "database_path",
        "repository_path",
        "restore_directory",
    }
    assert schema["properties"]["mode"]["enum"] == [
        "source",
        "restore_destination",
    ]
    assert schema["properties"]["mode"]["default"] == "source"
    assert all(
        schema["properties"][key]["type"] == "string"
        for key in ("database_path", "repository_path", "restore_directory")
    )
    required_by_mode = {
        branch["if"]["properties"]["mode"]["const"]: set(branch["then"]["required"])
        for branch in schema["allOf"]
    }
    assert required_by_mode == {
        "source": {"database_path", "repository_path"},
        "restore_destination": {"restore_directory"},
    }


@pytest.mark.asyncio
async def test_profilarr_configuration_is_strict_and_mode_aware() -> None:
    plugin = _plugin_class()(name="profilarr")

    assert await plugin.validate_config(dict(SOURCE_CONFIG)) is True
    assert await plugin.validate_config(dict(RESTORE_DESTINATION_CONFIG)) is True

    invalid_configs: tuple[object, ...] = (
        None,
        [],
        {},
        {key: value for key, value in SOURCE_CONFIG.items() if key != "mode"},
        {**SOURCE_CONFIG, "mode": "legacy"},
        {key: value for key, value in SOURCE_CONFIG.items() if key != "database_path"},
        {key: value for key, value in SOURCE_CONFIG.items() if key != "repository_path"},
        {**SOURCE_CONFIG, "database_path": 123},
        {**SOURCE_CONFIG, "database_path": "relative/profilarr.db"},
        {**SOURCE_CONFIG, "database_path": "/config"},
        {**SOURCE_CONFIG, "database_path": "/sources/profilarr/../profilarr.db"},
        {**SOURCE_CONFIG, "database_path": "/sources/profilarr/profilarr.db\n.invalid"},
        {**SOURCE_CONFIG, "repository_path": None},
        {**SOURCE_CONFIG, "repository_path": "relative/repository"},
        {**SOURCE_CONFIG, "repository_path": "/config"},
        {**SOURCE_CONFIG, "repository_path": "/sources/profilarr/../repository"},
        {
            **SOURCE_CONFIG,
            "repository_path": SOURCE_CONFIG["database_path"],
        },
        {**SOURCE_CONFIG, "restore_directory": "/tmp/profilarr-restore"},
        {**SOURCE_CONFIG, "legacy_path": "/sources/profilarr"},
        {
            key: value
            for key, value in RESTORE_DESTINATION_CONFIG.items()
            if key != "restore_directory"
        },
        {**RESTORE_DESTINATION_CONFIG, "restore_directory": 123},
        {**RESTORE_DESTINATION_CONFIG, "restore_directory": "relative/restore"},
        {**RESTORE_DESTINATION_CONFIG, "restore_directory": "/"},
        {**RESTORE_DESTINATION_CONFIG, "restore_directory": "/config"},
        {**RESTORE_DESTINATION_CONFIG, "restore_directory": "/backups"},
        {**RESTORE_DESTINATION_CONFIG, "restore_directory": "/tmp"},
        {
            **RESTORE_DESTINATION_CONFIG,
            "restore_directory": "/tmp/isolated/../profilarr",
        },
        {
            **RESTORE_DESTINATION_CONFIG,
            "database_path": "/sources/profilarr/profilarr.db",
        },
        {**RESTORE_DESTINATION_CONFIG, "legacy_path": "/tmp/profilarr-restore"},
    )
    for config in invalid_configs:
        assert await plugin.validate_config(config) is False  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_source_probe_accepts_exact_read_only_sqlite_and_clean_git_without_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, database, repository, mount_observations = _prepare_source(
        tmp_path,
        monkeypatch,
    )
    fsmonitor_marker = tmp_path / "fsmonitor-executed"
    fsmonitor = tmp_path / "synthetic-fsmonitor"
    fsmonitor.write_text(
        f"#!/bin/sh\nprintf executed > {fsmonitor_marker}\n",
        encoding="utf-8",
    )
    fsmonitor.chmod(0o700)
    _run_git(repository, "config", "core.fsmonitor", str(fsmonitor))
    before = _source_tree_snapshot(database.parent)
    real_run = subprocess.run
    git_calls: list[tuple[str, ...]] = []

    def guarded_run(*args: Any, **kwargs: Any) -> Any:
        command = args[0] if args else kwargs["args"]
        arguments = tuple(str(argument) for argument in command)
        if Path(arguments[0]).name == "git":
            git_calls.append(arguments)
            assert not {
                "clone",
                "fetch",
                "ls-remote",
                "pull",
                "push",
            }.intersection(arguments)
            environment = kwargs.get("env")
            assert "--no-optional-locks" in arguments or (
                isinstance(environment, dict) and environment.get("GIT_OPTIONAL_LOCKS") == "0"
            )
            timeout = kwargs.get("timeout")
            assert isinstance(timeout, (float, int)) and timeout > 0
        return real_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", guarded_run)

    importlib.import_module("app.plugins.profilarr.plugin")._validate_source_content(
        database,
        repository,
    )
    assert await _plugin_class()(name="profilarr").test(config) is True
    assert git_calls
    assert not fsmonitor_marker.exists()
    assert {database, repository}.issubset(set(mount_observations))
    assert _source_tree_snapshot(database.parent) == before


@pytest.mark.asyncio
async def test_source_probe_timeout_stops_and_reaps_the_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, _database, _repository, _observations = _prepare_source(tmp_path, monkeypatch)
    plugin_module = importlib.import_module("app.plugins.profilarr.plugin")
    process = _BlockingProcess(hold_after_terminate=True)
    connection = _NoResultConnection()
    monkeypatch.setattr(plugin_module, "_PROBE_WORKER_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(plugin_module, "_WORKER_STOP_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(
        plugin_module,
        "_start_probe_process",
        lambda _database, _repository: (process, connection),
    )

    with pytest.raises(TimeoutError, match="timed out"):
        await _plugin_class()(name="profilarr").test(config)

    assert process.terminate_called.is_set()
    assert process.kill_called.is_set()
    assert process.exitcode == -signal.SIGKILL
    assert not process.is_alive()
    assert connection.closed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "expected_exception", "message"),
    (
        ("database-missing", FileNotFoundError, "database"),
        ("database-directory", ValueError, "regular file|database"),
        ("database-symlink", ValueError, "symlink"),
        ("repository-missing", FileNotFoundError, "repository"),
        ("repository-file", ValueError, "directory|repository"),
        ("repository-symlink", ValueError, "symlink"),
        ("database-not-mounted", RuntimeError, "mount"),
        ("repository-writable", RuntimeError, "read-only"),
    ),
)
async def test_source_probe_requires_exact_non_symlink_types_and_narrow_read_only_mounts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    expected_exception: type[Exception],
    message: str,
) -> None:
    config, database, repository, _observations = _prepare_source(tmp_path, monkeypatch)
    if case == "database-missing":
        database.unlink()
    elif case == "database-directory":
        database.unlink()
        database.mkdir()
    elif case == "database-symlink":
        external = tmp_path / "external.db"
        database.rename(external)
        database.symlink_to(external)
    elif case == "repository-missing":
        repository.rename(tmp_path / "missing-repository")
    elif case == "repository-file":
        repository.rename(tmp_path / "repository-directory")
        repository.write_bytes(b"not a repository")
    elif case == "repository-symlink":
        external_repository = tmp_path / "external-repository"
        repository.rename(external_repository)
        repository.symlink_to(external_repository, target_is_directory=True)
    elif case == "database-not-mounted":
        _install_source_mounts(
            monkeypatch,
            database,
            repository,
            mounted=frozenset({repository}),
        )
    elif case == "repository-writable":
        _install_source_mounts(
            monkeypatch,
            database,
            repository,
            read_only=frozenset({database}),
        )
    else:
        raise AssertionError(f"Unknown source fixture case: {case}")

    with pytest.raises(expected_exception, match=f"(?i){message}"):
        await _plugin_class()(name="profilarr").test(config)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "message"),
    (
        ("wal", "rollback|journal"),
        ("hot-journal", "journal"),
        ("corrupt", "SQLite|integrity"),
        ("wrong-column", "schema|column"),
        ("wrong-migrations", "migration"),
        ("foreign-key", "foreign key"),
    ),
)
async def test_source_probe_rejects_incompatible_or_incoherent_sqlite_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    message: str,
) -> None:
    config, database, _repository, _observations = _prepare_source(
        tmp_path,
        monkeypatch,
        foreign_key_violation=case == "foreign-key",
    )
    if case == "wal":
        with sqlite3.connect(database) as connection:
            assert connection.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
    elif case == "hot-journal":
        database.with_name(f"{database.name}-journal").write_bytes(b"synthetic-hot-journal")
    elif case == "corrupt":
        database.write_bytes(b"not a SQLite database")
    elif case == "wrong-column":
        with sqlite3.connect(database) as connection:
            connection.execute("ALTER TABLE settings RENAME COLUMN value TO wrong_value")
    elif case == "wrong-migrations":
        with sqlite3.connect(database) as connection:
            connection.execute("DELETE FROM migrations WHERE version = 4")
    elif case != "foreign-key":
        raise AssertionError(f"Unknown SQLite fixture case: {case}")

    with pytest.raises(RuntimeError, match=f"(?i){message}"):
        await _plugin_class()(name="profilarr").test(config)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "message"),
    (
        ("dirty-tracked", "clean|dirty"),
        ("staged", "clean|staged|dirty"),
        ("untracked-authority", "untracked|clean"),
        ("ignored-authority", "ignored|clean"),
        ("executable-authority", "executable|mode"),
        ("index-lock", "lock|operation"),
        ("merge", "merge|operation"),
        ("rebase", "rebase|operation"),
        ("cherry-pick", "cherry|operation"),
        ("bisect", "bisect|operation"),
        ("shallow", "shallow"),
        ("partial", "partial|promisor"),
        ("alternates", "alternate"),
        ("submodule", "submodule"),
        ("lfs", "LFS"),
        ("replace-ref", "replace"),
        ("detached", "detached|symbolic branch"),
        ("unborn", "unborn|HEAD"),
        ("missing-object", "object|fsck|corrupt"),
        ("corrupt-object", "object|fsck|corrupt"),
    ),
)
async def test_source_probe_rejects_non_stable_or_externally_dependent_git_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    message: str,
) -> None:
    config, _database, repository, _observations = _prepare_source(tmp_path, monkeypatch)
    _mutate_repository(repository, case)

    with pytest.raises(RuntimeError, match=f"(?i){message}"):
        await _plugin_class()(name="profilarr").test(config)


@pytest.mark.asyncio
async def test_restore_destination_probe_is_authorized_empty_create_only_and_source_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOMELAB_BACKUP_ALLOW_ISOLATED_RESTORE", "1")
    plugin_module = importlib.import_module("app.plugins.profilarr.plugin")
    monkeypatch.setattr(plugin_module, "_network_interfaces", lambda: {"lo"}, raising=False)
    parent = tmp_path / "isolated-parent"
    parent.mkdir(mode=0o700)
    parent.chmod(0o700)
    sentinel = parent / RESTORE_SENTINEL_NAME
    sentinel.write_text(RESTORE_SENTINEL_CONTENT, encoding="utf-8")
    sentinel.chmod(0o600)
    destination = parent / "profilarr-alpha"
    _install_no_restore_io(monkeypatch)

    assert (
        await _plugin_class()(name="profilarr").test(
            {"mode": "restore_destination", "restore_directory": str(destination)}
        )
        is True
    )
    assert destination.exists() is False
    assert set(parent.iterdir()) == {sentinel}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "expected_exception", "message"),
    (
        ("unauthorized", RuntimeError, "disabled|authorized isolated"),
        ("networked", RuntimeError, "loopback-only"),
        ("public-parent", RuntimeError, "private"),
        ("missing-sentinel", FileNotFoundError, "sentinel"),
        ("wrong-sentinel", ValueError, "sentinel"),
        ("fifo-sentinel", ValueError, "sentinel"),
        ("nonexclusive-parent", ValueError, "only.*sentinel|empty"),
        ("existing-destination", FileExistsError, "destination"),
    ),
)
async def test_restore_destination_probe_rejects_unsafe_local_authorization_or_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    expected_exception: type[Exception],
    message: str,
) -> None:
    monkeypatch.setenv("HOMELAB_BACKUP_ALLOW_ISOLATED_RESTORE", "1")
    plugin_module = importlib.import_module("app.plugins.profilarr.plugin")
    interfaces = {"lo", "eth0"} if case == "networked" else {"lo"}
    monkeypatch.setattr(
        plugin_module,
        "_network_interfaces",
        lambda: interfaces,
        raising=False,
    )
    parent = tmp_path / "isolated-parent"
    parent.mkdir(mode=0o700)
    parent.chmod(0o777 if case == "public-parent" else 0o700)
    sentinel = parent / RESTORE_SENTINEL_NAME
    if case == "fifo-sentinel":
        os.mkfifo(sentinel, 0o600)
    elif case != "missing-sentinel":
        sentinel.write_text(
            "wrong-sentinel\n" if case == "wrong-sentinel" else RESTORE_SENTINEL_CONTENT,
            encoding="utf-8",
        )
        sentinel.chmod(0o600)
    destination = parent / "profilarr-alpha"
    if case == "nonexclusive-parent":
        (parent / "unexpected").write_text("must reject\n", encoding="utf-8")
    elif case == "existing-destination":
        destination.mkdir()
    elif case == "unauthorized":
        monkeypatch.delenv("HOMELAB_BACKUP_ALLOW_ISOLATED_RESTORE")
    _install_no_restore_io(monkeypatch)

    with pytest.raises(expected_exception, match=f"(?i){message}"):
        await _plugin_class()(name="profilarr").test(
            {"mode": "restore_destination", "restore_directory": str(destination)}
        )
    assert destination.exists() is (case == "existing-destination")


@pytest.mark.asyncio
async def test_get_status_reports_only_a_fresh_probe_and_redacts_source_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, _database, repository, _observations = _prepare_source(tmp_path, monkeypatch)
    plugin = _plugin_class()(name="profilarr")
    context = BackupContext(
        job_id="profilarr-status",
        target_id="profilarr-source",
        config=config,
    )

    assert await plugin.get_status(context) == {"status": "ok"}

    secret = "source-secret-must-not-leak"
    (repository / f"profiles/{secret}.yml").write_text("name: secret\n", encoding="utf-8")
    failed_status = await plugin.get_status(context)
    assert failed_status["status"] == "error"
    assert isinstance(failed_status["error"], str)
    assert (
        "untracked" in failed_status["error"].lower() or "clean" in failed_status["error"].lower()
    )
    assert secret not in json.dumps(failed_status)


@pytest.mark.asyncio
async def test_backup_publishes_private_sqlite_bundle_manifest_and_sidecar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, database, repository, _observations = _prepare_source(tmp_path, monkeypatch)
    plugin_module = importlib.import_module("app.plugins.profilarr.plugin")
    backup_root = tmp_path / "backups"
    monkeypatch.setattr(plugin_module, "BACKUP_BASE_PATH", str(backup_root), raising=False)
    source_before = _source_tree_snapshot(database.parent)
    plugin = _plugin_class()(name="profilarr")
    context = BackupContext(
        job_id="profilarr-backup",
        target_id="profilarr-source",
        config=config,
        metadata={"target_slug": "profilarr-source"},
    )

    result = await plugin.backup(context)
    artifact = Path(result["artifact_path"])

    assert artifact.is_relative_to(backup_root / "profilarr-source")
    assert artifact.suffix == ".profilarr"
    assert stat.S_IMODE(artifact.stat().st_mode) == 0o600
    sidecar = read_backup_sidecar(str(artifact))
    assert sidecar is not None
    assert sidecar["plugin_name"] == "profilarr"
    assert sidecar["target_slug"] == "profilarr-source"
    assert sidecar["artifact_bytes"] == artifact.stat().st_size
    assert sidecar["sha256"] == hashlib.sha256(artifact.read_bytes()).hexdigest()

    with zipfile.ZipFile(artifact) as archive:
        assert archive.namelist() == ["profilarr.db", "repository.bundle", "manifest.json"]
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["format_version"] == 1
        assert manifest["profilarr_version"] == "1.1.5"
        assert (
            manifest["database"]["sha256"]
            == hashlib.sha256(archive.read("profilarr.db")).hexdigest()
        )
        assert (
            manifest["bundle"]["sha256"]
            == hashlib.sha256(archive.read("repository.bundle")).hexdigest()
        )
        assert manifest["git"]["branch"] == "main"
        assert manifest["git"]["head"] == _run_git(repository, "rev-parse", "HEAD")
        assert "refs/heads/local-only" in {item["name"] for item in manifest["git"]["refs"]}
        restored_database = tmp_path / "artifact.db"
        restored_database.write_bytes(archive.read("profilarr.db"))
        with sqlite3.connect(f"file:{restored_database}?mode=ro", uri=True) as connection:
            assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)
            assert (
                tuple(
                    connection.execute(
                        "SELECT version, name FROM migrations ORDER BY version"
                    ).fetchall()
                )
                == EXPECTED_MIGRATIONS
            )
        bundle = tmp_path / "repository.bundle"
        bundle.write_bytes(archive.read("repository.bundle"))
        _run_git(repository, "bundle", "verify", str(bundle))

    public_metadata = json.dumps(sidecar, sort_keys=True)
    assert "database-secret" not in public_metadata
    assert "git-secret" not in public_metadata
    assert "profiles/default.yml" not in public_metadata
    assert _source_tree_snapshot(database.parent) == source_before


@pytest.mark.asyncio
async def test_backup_retries_one_repository_fence_change_then_publishes_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, _database, _repository, _observations = _prepare_source(tmp_path, monkeypatch)
    plugin_module = importlib.import_module("app.plugins.profilarr.plugin")
    backup_root = tmp_path / "backups"
    monkeypatch.setattr(plugin_module, "BACKUP_BASE_PATH", str(backup_root), raising=False)
    original_fence = plugin_module._repository_fence
    original_bundle = plugin_module._create_bundle
    fence_calls = 0
    bundle_calls = 0

    def one_change(repository: Path) -> object:
        nonlocal fence_calls
        fence_calls += 1
        fence = original_fence(repository)
        if fence_calls == 2:
            return replace(
                fence,
                refs=(*fence.refs, ("refs/heads/synthetic-race", fence.head)),
            )
        return fence

    def counted_bundle(repository: Path, bundle_path: Path) -> None:
        nonlocal bundle_calls
        bundle_calls += 1
        original_bundle(repository, bundle_path)

    monkeypatch.setattr(plugin_module, "_repository_fence", one_change)
    monkeypatch.setattr(plugin_module, "_create_bundle", counted_bundle)

    workspace = tmp_path / "capture"
    workspace.mkdir(mode=0o700)
    artifact = backup_root / "capture.profilarr"
    backup_root.mkdir()
    plugin_module._capture_composite(
        Path(cast(str, config["database_path"])),
        Path(cast(str, config["repository_path"])),
        artifact,
        workspace,
    )

    assert artifact.is_file()
    assert fence_calls == 4
    assert bundle_calls == 2
    assert list(workspace.iterdir()) == []


@pytest.mark.asyncio
async def test_backup_exhausts_three_repository_changes_without_publication_or_residue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, _database, _repository, _observations = _prepare_source(tmp_path, monkeypatch)
    plugin_module = importlib.import_module("app.plugins.profilarr.plugin")
    backup_root = tmp_path / "backups"
    capture_root = tmp_path / "capture-workspaces"
    capture_root.mkdir()
    monkeypatch.setattr(plugin_module, "BACKUP_BASE_PATH", str(backup_root), raising=False)
    original_fence = plugin_module._repository_fence
    original_bundle = plugin_module._create_bundle
    fence_calls = 0
    bundle_calls = 0

    def changing_fence(repository: Path) -> object:
        nonlocal fence_calls
        fence_calls += 1
        fence = original_fence(repository)
        if fence_calls % 2 == 0:
            return replace(
                fence,
                refs=(
                    *fence.refs,
                    (f"refs/heads/synthetic-race-{fence_calls // 2}", fence.head),
                ),
            )
        return fence

    def counted_bundle(repository: Path, bundle_path: Path) -> None:
        nonlocal bundle_calls
        bundle_calls += 1
        original_bundle(repository, bundle_path)

    monkeypatch.setattr(plugin_module, "_repository_fence", changing_fence)
    monkeypatch.setattr(plugin_module, "_create_bundle", counted_bundle)

    with pytest.raises(RuntimeError, match="changed|stabilize"):
        plugin_module._capture_composite(
            Path(cast(str, config["database_path"])),
            Path(cast(str, config["repository_path"])),
            backup_root / "capture.profilarr",
            capture_root,
        )

    assert fence_calls == 6
    assert bundle_calls == 3
    assert _published_files(backup_root) == []
    assert list(capture_root.iterdir()) == []


@pytest.mark.asyncio
async def test_backup_worker_timeout_escalates_to_kill_reaps_and_cleans_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, _database, _repository, _observations = _prepare_source(tmp_path, monkeypatch)
    plugin_module = importlib.import_module("app.plugins.profilarr.plugin")
    backup_root = tmp_path / "backups"
    monkeypatch.setattr(plugin_module, "BACKUP_BASE_PATH", str(backup_root), raising=False)
    monkeypatch.setattr(plugin_module, "_BACKUP_WORKER_TIMEOUT_SECONDS", 0.01, raising=False)
    monkeypatch.setattr(plugin_module, "_WORKER_STOP_TIMEOUT_SECONDS", 0.01, raising=False)
    process = _BlockingProcess(hold_after_terminate=True)
    connection = _NoResultConnection()
    owned_workspaces: list[Path] = []

    def start_blocked_worker(
        _database: Path,
        _repository: Path,
        artifact_path: Path,
        workspace: Path,
        _expected_source_identities: tuple[tuple[int, int], tuple[int, int]],
    ) -> tuple[object, object]:
        artifact_path.write_bytes(b"partial database-secret git-secret")
        (workspace / "secret-material").write_bytes(b"database-secret git-secret")
        owned_workspaces.append(workspace)
        return process, connection

    monkeypatch.setattr(
        plugin_module,
        "_start_backup_process",
        start_blocked_worker,
        raising=False,
    )

    with pytest.raises(TimeoutError, match="timed out"):
        await _plugin_class()(name="profilarr").backup(_backup_context(config, backup_root))

    assert process.terminate_called.is_set()
    assert process.kill_called.is_set()
    assert process.exitcode == -9
    assert process.is_alive() is False
    assert connection.closed
    assert owned_workspaces and all(not path.exists() for path in owned_workspaces)
    assert _published_files(backup_root) == []


@pytest.mark.asyncio
async def test_backup_worker_repeated_cancellation_waits_for_kill_reap_and_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, _database, _repository, _observations = _prepare_source(tmp_path, monkeypatch)
    plugin_module = importlib.import_module("app.plugins.profilarr.plugin")
    backup_root = tmp_path / "backups"
    monkeypatch.setattr(plugin_module, "BACKUP_BASE_PATH", str(backup_root), raising=False)
    monkeypatch.setattr(plugin_module, "_WORKER_STOP_TIMEOUT_SECONDS", 0.01, raising=False)
    process = _BlockingProcess(hold_after_terminate=True)
    connection = _NoResultConnection()
    owned_workspaces: list[Path] = []

    def start_blocked_worker(
        _database: Path,
        _repository: Path,
        artifact_path: Path,
        workspace: Path,
        _expected_source_identities: tuple[tuple[int, int], tuple[int, int]],
    ) -> tuple[object, object]:
        artifact_path.write_bytes(b"partial database-secret git-secret")
        (workspace / "secret-material").write_bytes(b"database-secret git-secret")
        owned_workspaces.append(workspace)
        return process, connection

    monkeypatch.setattr(
        plugin_module,
        "_start_backup_process",
        start_blocked_worker,
        raising=False,
    )
    task = asyncio.create_task(
        _plugin_class()(name="profilarr").backup(_backup_context(config, backup_root))
    )
    assert await asyncio.to_thread(process.join_started.wait, 2)

    task.cancel()
    assert await asyncio.to_thread(process.terminate_called.wait, 2)
    task.cancel()
    assert await asyncio.to_thread(process.kill_called.wait, 2)

    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.exitcode == -9
    assert process.is_alive() is False
    assert connection.closed
    assert owned_workspaces and all(not path.exists() for path in owned_workspaces)
    assert _published_files(backup_root) == []


@pytest.mark.asyncio
async def test_backup_refuses_source_mount_inode_swap_before_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, database, _repository, _observations = _prepare_source(tmp_path, monkeypatch)
    plugin_module = importlib.import_module("app.plugins.profilarr.plugin")
    backup_root = tmp_path / "backups"
    monkeypatch.setattr(plugin_module, "BACKUP_BASE_PATH", str(backup_root))

    class SuccessfulProcess:
        pid = None
        exitcode = 0

        def join(self, _timeout: float) -> None:
            return None

        def is_alive(self) -> bool:
            return False

    class SuccessfulConnection:
        def __init__(self) -> None:
            self.closed = False

        def poll(self) -> bool:
            return True

        def recv(self) -> tuple[str, str, int, int]:
            return ("ok", "", 1, len(EXPECTED_PROFILARR_SCHEMA))

        def close(self) -> None:
            self.closed = True

    def swap_source_then_complete(
        _database: Path,
        _repository: Path,
        artifact_path: Path,
        _workspace: Path,
        _expected_source_identities: tuple[tuple[int, int], tuple[int, int]],
    ) -> tuple[object, object]:
        replacement = database.with_name("replacement.db")
        replacement.write_bytes(database.read_bytes())
        os.replace(replacement, database)
        artifact_path.write_bytes(b"synthetic validated capture")
        return SuccessfulProcess(), SuccessfulConnection()

    monkeypatch.setattr(plugin_module, "_start_backup_process", swap_source_then_complete)

    with pytest.raises(RuntimeError, match="mount identity changed"):
        await _plugin_class()(name="profilarr").backup(_backup_context(config, backup_root))

    assert _published_files(backup_root) == []


@pytest.mark.asyncio
async def test_real_worker_stop_kills_and_reaps_its_stubborn_process_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin_module = importlib.import_module("app.plugins.profilarr.plugin")
    monkeypatch.setattr(plugin_module, "_WORKER_STOP_TIMEOUT_SECONDS", 0.1)
    pid_path = tmp_path / "child.pid"
    process = multiprocessing.get_context("spawn").Process(
        target=_stubborn_process_tree_worker,
        args=(str(pid_path),),
        name="profilarr-process-tree-test",
    )
    process.start()
    child_pid: int | None = None
    try:
        for _attempt in range(200):
            if pid_path.exists():
                child_pid = int(pid_path.read_text(encoding="ascii"))
                break
            await asyncio.sleep(0.01)
        assert child_pid is not None

        await plugin_module._stop_worker_process(process, operation="test")

        assert process.exitcode == -signal.SIGKILL
        assert not process.is_alive()
        for _attempt in range(200):
            if not _process_is_live(child_pid):
                break
            await asyncio.sleep(0.01)
        assert not _process_is_live(child_pid)
    finally:
        if process.is_alive():
            try:
                if process.pid is not None:
                    os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.kill()
            process.join(2)
        process.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    (
        "duplicate",
        "extra",
        "traversal",
        "link",
        "encrypted",
        "payload-hash",
        "invalid-manifest",
        "invalid-bundle",
        "invalid-database",
    ),
)
async def test_backup_strictly_validates_generated_capture_before_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    config, _database, _repository, _observations = _prepare_source(tmp_path, monkeypatch)
    plugin_module = importlib.import_module("app.plugins.profilarr.plugin")
    backup_root = tmp_path / "backups"
    monkeypatch.setattr(plugin_module, "BACKUP_BASE_PATH", str(backup_root), raising=False)
    original_write = plugin_module._write_capture_archive

    def malformed_write(
        archive_path: Path,
        database_path: Path,
        bundle_path: Path,
        manifest: dict[str, object],
    ) -> None:
        original_write(archive_path, database_path, bundle_path, manifest)
        _rewrite_malformed_capture(archive_path, case)

    monkeypatch.setattr(plugin_module, "_write_capture_archive", malformed_write)

    archive = backup_root / "capture.profilarr"
    workspace = tmp_path / "capture"
    validation = tmp_path / "validation"
    backup_root.mkdir()
    workspace.mkdir(mode=0o700)
    validation.mkdir(mode=0o700)
    plugin_module._capture_composite(
        Path(cast(str, config["database_path"])),
        Path(cast(str, config["repository_path"])),
        archive,
        workspace,
    )

    with pytest.raises(RuntimeError, match="artifact|bundle|database|SQLite|ZIP|manifest"):
        plugin_module._validate_artifact_to_workspace(
            archive,
            validation,
            expected_size=None,
            expected_sha256=None,
        )

    assert not list(backup_root.glob("*.meta.json"))


@pytest.mark.asyncio
async def test_backup_disk_failure_removes_artifact_sidecar_and_sensitive_temps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, _database, _repository, _observations = _prepare_source(tmp_path, monkeypatch)
    plugin_module = importlib.import_module("app.plugins.profilarr.plugin")
    backup_root = tmp_path / "backups"
    monkeypatch.setattr(plugin_module, "BACKUP_BASE_PATH", str(backup_root), raising=False)
    original_fsync = os.fsync

    def fail_artifact_fsync(file_descriptor: int) -> None:
        opened = Path(f"/proc/self/fd/{file_descriptor}").resolve(strict=True)
        if opened.is_relative_to(backup_root):
            raise OSError("synthetic disk failure")
        original_fsync(file_descriptor)

    monkeypatch.setattr(os, "fsync", fail_artifact_fsync)

    with pytest.raises(OSError, match="synthetic disk failure"):
        await _plugin_class()(name="profilarr").backup(_backup_context(config, backup_root))

    assert _published_files(backup_root) == []
    assert not list(backup_root.rglob("*.tmp"))
    assert not list(backup_root.rglob("*.meta.json"))


@pytest.mark.asyncio
async def test_restore_from_staged_context_reconstructs_exact_private_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_config, _source_database, source_repository, _observations = _prepare_source(
        tmp_path,
        monkeypatch,
    )
    plugin_module = importlib.import_module("app.plugins.profilarr.plugin")
    backup_root = tmp_path / "backups"
    monkeypatch.setattr(plugin_module, "BACKUP_BASE_PATH", str(backup_root), raising=False)
    plugin = _plugin_class()(name="profilarr")
    backup = await plugin.backup(_backup_context(source_config, backup_root))
    artifact = Path(backup["artifact_path"])
    artifact_before = artifact.read_bytes()

    restore_parent = tmp_path / "restore"
    restore_parent.mkdir(mode=0o700)
    sentinel = restore_parent / RESTORE_SENTINEL_NAME
    sentinel.write_text(RESTORE_SENTINEL_CONTENT, encoding="utf-8")
    sentinel.chmod(0o600)
    destination = restore_parent / "profilarr"
    monkeypatch.setattr(plugin_module, "_RESTORE_ROOTS", (restore_parent,), raising=False)
    monkeypatch.setattr(plugin_module, "_network_interfaces", lambda: {"lo"})
    monkeypatch.setenv(plugin_module.ISOLATED_RESTORE_ENV, "1")

    result = await plugin.restore(
        RestoreContext(
            job_id="profilarr-restore-drill",
            source_target_id="profilarr-source",
            destination_target_id="profilarr-restore",
            config={"mode": "restore_destination", "restore_directory": str(destination)},
            artifact_path=str(artifact),
            metadata={
                "artifact_bytes": artifact.stat().st_size,
                "artifact_sha256": hashlib.sha256(artifact_before).hexdigest(),
                "source_target_run_id": 42,
            },
        )
    )

    assert result == {
        "status": "success",
        "message": "All authoritative Profilarr 1.1.5 state was restored",
        "restored_path": str(destination),
    }
    assert not sentinel.exists()
    assert artifact.read_bytes() == artifact_before
    assert stat.S_IMODE((destination / "profilarr.db").stat().st_mode) == 0o600
    assert _source_tree_snapshot(destination / "db")
    with sqlite3.connect(
        f"file:{destination / 'profilarr.db'}?mode=ro",
        uri=True,
    ) as connection:
        assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)
        assert connection.execute("SELECT api_key FROM auth").fetchone() == ("database-secret",)
    assert _run_git(destination / "db", "status", "--porcelain=v2") == ""
    assert _run_git(destination / "db", "rev-parse", "HEAD") == _run_git(
        source_repository,
        "rev-parse",
        "HEAD",
    )
    assert _run_git(destination / "db", "show-ref") == _run_git(
        source_repository,
        "show-ref",
    )
    assert _run_git(destination / "db", "remote", "get-url", "origin") == (
        "https://example.invalid/profiles.git"
    )
    assert not (destination / "db/.git/hooks").exists()
    assert not (destination / "db/.git/logs").exists()


def test_restore_service_stages_a_verified_artifact_and_records_provenance(
    db_session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin, artifact, source_config, _repository = asyncio.run(
        _create_backup_for_restore(tmp_path, monkeypatch)
    )
    source = Target(
        name="Profilarr Source",
        slug="profilarr-source",
        plugin_name="profilarr",
        plugin_config_json=json.dumps(source_config),
    )
    parent, destination, sentinel = _prepare_restore_destination(
        tmp_path,
        name="profilarr-service-restore",
    )
    destination_target = Target(
        name="Profilarr Isolated Restore",
        slug="profilarr-isolated-restore",
        plugin_name="profilarr",
        plugin_config_json=json.dumps(
            {"mode": "restore_destination", "restore_directory": str(destination)}
        ),
    )
    db_session.add_all([source, destination_target])
    db_session.commit()
    source_run = Run(
        status="success",
        operation="backup",
        started_at=datetime.now(timezone.utc),
        finished_at=datetime.now(timezone.utc),
    )
    db_session.add(source_run)
    db_session.commit()
    source_target_run = TargetRun(
        run_id=source_run.id,
        target_id=source.id,
        status="success",
        operation="backup",
        artifact_path=str(artifact),
        artifact_bytes=artifact.stat().st_size,
        sha256=_sha256(artifact.read_bytes()),
        started_at=source_run.started_at,
        finished_at=source_run.finished_at,
    )
    db_session.add(source_target_run)
    db_session.commit()

    plugin_module = _configure_local_restore(monkeypatch, parent)
    observed: dict[str, object] = {}
    real_restore = plugin.restore

    async def observe_staging(context: RestoreContext) -> dict[str, Any]:
        staged = Path(context.artifact_path)
        observed["path"] = staged
        observed["inode"] = staged.stat().st_ino
        observed["sha256"] = _sha256(staged.read_bytes())
        observed["metadata"] = dict(context.metadata or {})
        return cast(dict[str, Any], await real_restore(context))

    monkeypatch.setenv("BACKUP_BASE_PATH", str(tmp_path / "backups"))
    monkeypatch.setattr(plugin, "restore", observe_staging)
    monkeypatch.setattr("app.services.restores.get_plugin", lambda _: plugin)

    result = RestoreService(db_session).restore(
        source_target_run_id=source_target_run.id,
        destination_target_id=destination_target.id,
        triggered_by="isolated_profilarr_service_test",
    )

    assert result.status == "success"
    assert len(result.target_runs) == 1
    restored_run = result.target_runs[0]
    assert restored_run.status == "success"
    assert restored_run.operation == "restore"
    assert restored_run.target_id == destination_target.id
    assert restored_run.artifact_path == str(destination)
    assert "isolated_profilarr_service_test" in (result.logs_text or "")
    staged_path = observed["path"]
    assert isinstance(staged_path, Path)
    assert staged_path != artifact
    assert observed["inode"] != artifact.stat().st_ino
    assert observed["sha256"] == source_target_run.sha256
    assert staged_path.exists() is False
    assert not list(artifact.parent.glob(".homelab-backup-restore-*"))
    metadata = observed["metadata"]
    assert isinstance(metadata, dict)
    assert metadata["source_target_run_id"] == source_target_run.id
    assert metadata["source_run_id"] == source_run.id
    assert metadata["source_target_id"] == source.id
    assert metadata["source_target_slug"] == source.slug
    assert metadata["artifact_bytes"] == source_target_run.artifact_bytes
    assert metadata["artifact_sha256"] == source_target_run.sha256
    assert "source_database_identity" not in metadata
    assert artifact.is_file()
    assert Path(f"{artifact}.meta.json").is_file()
    assert sentinel.exists() is False
    assert set(parent.iterdir()) == {destination}
    assert (destination / "profilarr.db").is_file()
    assert (destination / "db/.git").is_dir()
    assert plugin_module.ISOLATED_RESTORE_ENV in os.environ


@pytest.mark.asyncio
async def test_restore_refuses_missing_or_invalid_service_provenance_without_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin, artifact, _source_config, _repository = await _create_backup_for_restore(
        tmp_path,
        monkeypatch,
    )
    valid = {
        "source_target_run_id": 42,
        "artifact_bytes": artifact.stat().st_size,
        "artifact_sha256": _sha256(artifact.read_bytes()),
    }
    cases: tuple[dict[str, object], ...] = (
        {},
        {**valid, "source_target_run_id": True},
        {**valid, "source_target_run_id": 0},
        {**valid, "source_target_run_id": "42"},
        {key: value for key, value in valid.items() if key != "artifact_bytes"},
        {**valid, "artifact_bytes": True},
        {**valid, "artifact_bytes": 0},
        {key: value for key, value in valid.items() if key != "artifact_sha256"},
        {**valid, "artifact_sha256": "not-a-sha256"},
    )

    for index, metadata in enumerate(cases):
        parent, destination, sentinel = _prepare_restore_destination(
            tmp_path,
            name=f"profilarr-provenance-{index}",
        )
        _configure_local_restore(monkeypatch, parent)
        with pytest.raises(RuntimeError, match="provenance"):
            await plugin.restore(_restore_context(artifact, destination, metadata=metadata))
        assert destination.exists() is False
        assert set(parent.iterdir()) == {sentinel}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ("invalid-manifest", "traversal", "extra"))
async def test_restore_prevalidation_failure_leaves_only_the_authorization_sentinel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    plugin, original, _source_config, _repository = await _create_backup_for_restore(
        tmp_path,
        monkeypatch,
    )
    artifact = tmp_path / f"malformed-{case}.profilarr"
    artifact.write_bytes(original.read_bytes())
    _rewrite_malformed_capture(artifact, case)
    parent, destination, sentinel = _prepare_restore_destination(tmp_path)
    _configure_local_restore(monkeypatch, parent)

    with pytest.raises(RuntimeError):
        await plugin.restore(_restore_context(artifact, destination))

    assert artifact.is_file()
    assert destination.exists() is False
    assert set(parent.iterdir()) == {sentinel}


@pytest.mark.asyncio
async def test_restore_timeout_kills_reaps_and_scrubs_parent_owned_secret_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin, artifact, _source_config, _repository = await _create_backup_for_restore(
        tmp_path,
        monkeypatch,
    )
    parent, destination, sentinel = _prepare_restore_destination(tmp_path)
    plugin_module = _configure_local_restore(monkeypatch, parent)
    monkeypatch.setattr(plugin_module, "_RESTORE_WORKER_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(plugin_module, "_WORKER_STOP_TIMEOUT_SECONDS", 0.01)
    process = _BlockingProcess(hold_after_terminate=True)
    connection = _NoResultConnection()
    owned_paths: list[Path] = []

    def start_blocked_worker(
        _artifact: Path,
        parent_path: Path,
        _parent_identity: tuple[int, int],
        staging_name: str,
        _staging_identity: tuple[int, int],
        validation_name: str,
        _validation_identity: tuple[int, int],
        _expected_size: int,
        _expected_sha256: str,
        _expected_artifact_identity: tuple[int, int],
    ) -> tuple[object, object]:
        staging = parent_path / staging_name
        validation = parent_path / validation_name
        (staging / "profilarr.db").write_bytes(b"restored secret")
        (validation / "repository.bundle").write_bytes(b"validated secret")
        owned_paths.extend((staging, validation))
        return process, connection

    monkeypatch.setattr(plugin_module, "_start_restore_process", start_blocked_worker)

    with pytest.raises(TimeoutError, match="timed out"):
        await plugin.restore(_restore_context(artifact, destination))

    assert process.terminate_called.is_set()
    assert process.kill_called.is_set()
    assert process.exitcode == -9
    assert not process.is_alive()
    assert connection.closed
    assert owned_paths and all(not path.exists() for path in owned_paths)
    assert artifact.is_file()
    assert set(parent.iterdir()) == {sentinel}


@pytest.mark.asyncio
async def test_restore_repeated_cancellation_waits_for_reap_before_scrubbing_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin, artifact, _source_config, _repository = await _create_backup_for_restore(
        tmp_path,
        monkeypatch,
    )
    parent, destination, sentinel = _prepare_restore_destination(tmp_path)
    plugin_module = _configure_local_restore(monkeypatch, parent)
    process = _BlockingProcess(hold_after_terminate=True)
    connection = _NoResultConnection()

    def start_blocked_worker(
        _artifact: Path,
        parent_path: Path,
        _parent_identity: tuple[int, int],
        staging_name: str,
        _staging_identity: tuple[int, int],
        validation_name: str,
        _validation_identity: tuple[int, int],
        _expected_size: int,
        _expected_sha256: str,
        _expected_artifact_identity: tuple[int, int],
    ) -> tuple[object, object]:
        (parent_path / staging_name / "profilarr.db").write_bytes(b"restored secret")
        (parent_path / validation_name / "repository.bundle").write_bytes(b"validated secret")
        return process, connection

    monkeypatch.setattr(plugin_module, "_start_restore_process", start_blocked_worker)
    task = asyncio.create_task(plugin.restore(_restore_context(artifact, destination)))
    assert await asyncio.to_thread(process.join_started.wait, 2)
    task.cancel()
    assert await asyncio.to_thread(process.terminate_called.wait, 2)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    process.release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.exitcode == -15
    assert not process.is_alive()
    assert connection.closed
    assert artifact.is_file()
    assert set(parent.iterdir()) == {sentinel}


@pytest.mark.asyncio
async def test_restore_publication_collision_preserves_foreign_destination_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin, artifact, _source_config, _repository = await _create_backup_for_restore(
        tmp_path,
        monkeypatch,
    )
    parent, destination, sentinel = _prepare_restore_destination(tmp_path)
    plugin_module = _configure_local_restore(monkeypatch, parent)
    foreign = destination / "foreign-state"

    def collide_at_publication(
        _parent_fd: int,
        _source_name: str,
        _destination_name: str,
    ) -> None:
        destination.mkdir(mode=0o700)
        foreign.write_text("must-survive", encoding="utf-8")
        raise FileExistsError("synthetic Profilarr publication race")

    monkeypatch.setattr(plugin_module, "_rename_directory_noreplace", collide_at_publication)

    with pytest.raises(FileExistsError, match="publication race"):
        await plugin.restore(_restore_context(artifact, destination))

    assert foreign.read_text(encoding="utf-8") == "must-survive"
    assert set(destination.iterdir()) == {foreign}
    assert sentinel.is_file()
    assert set(parent.iterdir()) == {sentinel, destination}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "replacement_content",
    ("foreign-marker\n", RESTORE_SENTINEL_CONTENT),
)
async def test_restore_sentinel_swap_is_refused_and_foreign_marker_survives(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement_content: str,
) -> None:
    plugin, artifact, _source_config, _repository = await _create_backup_for_restore(
        tmp_path,
        monkeypatch,
    )
    parent, destination, sentinel = _prepare_restore_destination(tmp_path)
    _configure_local_restore(monkeypatch, parent)
    original_rename = os.rename
    replaced = False

    def replace_sentinel_before_rename(
        source: str | bytes,
        destination_name: str | bytes,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        nonlocal replaced
        if source == RESTORE_SENTINEL_NAME and not replaced:
            replaced = True
            sentinel.unlink()
            sentinel.write_text(replacement_content, encoding="utf-8")
            sentinel.chmod(0o600)
        original_rename(
            source,
            destination_name,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(os, "rename", replace_sentinel_before_rename)

    with pytest.raises(ValueError, match="sentinel changed"):
        await plugin.restore(_restore_context(artifact, destination))

    assert replaced is True
    assert destination.exists() is False
    assert sentinel.read_text(encoding="utf-8") == replacement_content
    assert set(parent.iterdir()) == {sentinel}


@pytest.mark.asyncio
async def test_restore_refuses_staged_artifact_path_replacement_after_provenance_hashing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin, artifact, _source_config, _repository = await _create_backup_for_restore(
        tmp_path,
        monkeypatch,
    )
    parent, destination, sentinel = _prepare_restore_destination(tmp_path)
    plugin_module = _configure_local_restore(monkeypatch, parent)
    relocated = tmp_path / "verified-original.profilarr"
    original_payload = artifact.read_bytes()
    real_start = plugin_module._start_restore_process

    def substitute_then_start(*args: object) -> tuple[object, object]:
        artifact.rename(relocated)
        artifact.write_bytes(original_payload)
        return cast(tuple[object, object], real_start(*args))

    monkeypatch.setattr(plugin_module, "_start_restore_process", substitute_then_start)

    with pytest.raises(ValueError, match="verified staging identity"):
        await plugin.restore(_restore_context(artifact, destination))

    assert destination.exists() is False
    assert set(parent.iterdir()) == {sentinel}
