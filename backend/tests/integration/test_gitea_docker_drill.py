from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any

import pytest

from app.core.plugins.base import BackupContext, RestoreContext
from app.core.plugins.sidecar import read_backup_sidecar
from app.plugins.gitea import plugin as gitea_module
from app.plugins.gitea.plugin import GiteaPlugin

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_GITEA_DOCKER_DRILL") != "1",
    reason="set RUN_GITEA_DOCKER_DRILL=1 to run the destructive disposable-container drill",
)

_IMAGE = "gitea/gitea:1.27.1"
_USERNAME = "drill"
_PASSWORD = "local-drill-password"


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


def _start_gitea(
    container: str,
    volume: str,
    package_volume: str,
    *,
    restore_destination: bool,
) -> None:
    command = [
        "run",
        "-d",
        "--name",
        container,
        "--health-cmd",
        "curl -fsS http://127.0.0.1:3000/api/healthz",
        "--health-interval",
        "1s",
        "--health-timeout",
        "2s",
        "--health-retries",
        "60",
        "-e",
        "GITEA__database__DB_TYPE=sqlite3",
        "-e",
        "GITEA__database__PATH=/data/gitea/gitea.db",
        "-e",
        "GITEA__security__INSTALL_LOCK=true",
        "-v",
        f"{volume}:/data",
        "-v",
        f"{package_volume}:/data/gitea/packages",
    ]
    if restore_destination:
        command.extend(
            [
                "--label",
                "asia.hollinger.homelab-backup.restore-destination=true",
            ]
        )
    command.append(_IMAGE)
    _docker(*command)
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        state = _docker(
            "inspect",
            "--format",
            "{{if .State.Health}}{{.State.Health.Status}}{{end}}",
            container,
        ).stdout.strip()
        if state == "healthy":
            return
        if state == "unhealthy":
            raise RuntimeError(f"Disposable Gitea container {container} became unhealthy")
        time.sleep(1)
    raise RuntimeError(f"Disposable Gitea container {container} did not become healthy")


def _create_user(container: str) -> None:
    _docker(
        "exec",
        "--user",
        "1000:1000",
        container,
        "gitea",
        "--config",
        "/data/gitea/conf/app.ini",
        "admin",
        "user",
        "create",
        "--username",
        _USERNAME,
        "--password",
        _PASSWORD,
        "--email",
        "drill@example.invalid",
        "--admin",
        "--must-change-password=false",
    )


def _api(
    container: str,
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    data: str | None = None,
) -> str:
    command = [
        "exec",
        *(("-i",) if data is not None else ()),
        container,
        "curl",
        "-fsS",
        "-u",
        f"{_USERNAME}:{_PASSWORD}",
        "-X",
        method,
    ]
    if body is not None:
        command.extend(["-H", "Content-Type: application/json", "-d", json.dumps(body)])
    if data is not None:
        command.extend(["--upload-file", "-"])
    command.append(f"http://127.0.0.1:3000{path}")
    return _docker(*command, input_text=data).stdout


def _api_status(container: str, path: str) -> int:
    result = _docker(
        "exec",
        container,
        "curl",
        "-sS",
        "-o",
        "/dev/null",
        "-w",
        "%{http_code}",
        "-u",
        f"{_USERNAME}:{_PASSWORD}",
        f"http://127.0.0.1:3000{path}",
    )
    return int(result.stdout)


def _create_repository(container: str, name: str) -> None:
    _api(container, "POST", "/api/v1/user/repos", body={"name": name})


def _upload_release_attachment(container: str) -> None:
    release = json.loads(
        _api(
            container,
            "POST",
            "/api/v1/repos/drill/project/releases",
            body={
                "tag_name": "v1.0.0",
                "target_commitish": "main",
                "name": "Drill release",
            },
        )
    )
    _docker(
        "exec",
        "-i",
        container,
        "curl",
        "-fsS",
        "-u",
        f"{_USERNAME}:{_PASSWORD}",
        "-F",
        "attachment=@-;filename=release-marker.txt",
        f"http://127.0.0.1:3000/api/v1/repos/drill/project/releases/{release['id']}/assets",
        input_text="release-marker",
    )


def _upload_large_package(container: str) -> None:
    _docker(
        "exec",
        container,
        "/bin/sh",
        "-ceu",
        "head -c 67108864 /dev/urandom | "
        f"curl -fsS -u {_USERNAME}:{_PASSWORD} --upload-file - "
        "http://127.0.0.1:3000/api/packages/drill/generic/"
        "drill-package/1.0.0/large-marker.bin",
    )


def _peak_rss_kib() -> int:
    for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines():
        if line.startswith("VmHWM:"):
            return int(line.split()[1])
    raise RuntimeError("Linux process peak memory was unavailable")


def _assert_restored_state(container: str, issue_count: int) -> None:
    repository = json.loads(_api(container, "GET", "/api/v1/repos/drill/project"))
    assert repository["name"] == "project"
    readme = json.loads(_api(container, "GET", "/api/v1/repos/drill/project/contents/README.md"))
    assert base64.b64decode(readme["content"]).decode() == "backup-drill-marker\n"
    issues = json.loads(_api(container, "GET", "/api/v1/repos/drill/project/issues"))
    assert len(issues) == issue_count
    releases = json.loads(_api(container, "GET", "/api/v1/repos/drill/project/releases"))
    assert releases[0]["tag_name"] == "v1.0.0"
    assert any(asset["name"] == "release-marker.txt" for asset in releases[0]["assets"])
    assert (
        _api_status(
            container,
            "/api/packages/drill/generic/drill-package/1.0.0/marker.txt",
        )
        == 200
    )
    assert (
        _api_status(
            container,
            "/api/packages/drill/generic/drill-package/1.0.0/large-marker.bin",
        )
        == 200
    )
    database_count = _docker(
        "exec",
        container,
        "sqlite3",
        "/data/gitea/gitea.db",
        "SELECT count(*) FROM repository WHERE name='project';",
    ).stdout.strip()
    assert database_count == "1"
    _docker(
        "exec",
        container,
        "git",
        "--git-dir=/data/git/repositories/drill/project.git",
        "fsck",
        "--no-dangling",
    )


@pytest.mark.asyncio
async def test_two_consecutive_backup_restore_drills_against_exact_gitea_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    suffix = uuid.uuid4().hex[:10]
    source = f"codex-gitea-source-{suffix}"
    destinations = [f"codex-gitea-restore-{drill}-{suffix}" for drill in (1, 2)]
    rollback_destination = f"codex-gitea-rollback-{suffix}"
    source_volume = f"codex-gitea-source-data-{suffix}"
    source_package_volume = f"codex-gitea-source-packages-{suffix}"
    destination_volumes = [f"codex-gitea-restore-data-{drill}-{suffix}" for drill in (1, 2)]
    destination_package_volumes = [
        f"codex-gitea-restore-packages-{drill}-{suffix}" for drill in (1, 2)
    ]
    rollback_volume = f"codex-gitea-rollback-data-{suffix}"
    rollback_package_volume = f"codex-gitea-rollback-packages-{suffix}"
    containers = [source, *destinations, rollback_destination]
    volumes = [
        source_volume,
        source_package_volume,
        *destination_volumes,
        *destination_package_volumes,
        rollback_volume,
        rollback_package_volume,
    ]

    try:
        for volume in volumes:
            _docker("volume", "create", volume)
        _start_gitea(
            source,
            source_volume,
            source_package_volume,
            restore_destination=False,
        )
        for destination, destination_volume, package_volume in zip(
            destinations,
            destination_volumes,
            destination_package_volumes,
            strict=True,
        ):
            _start_gitea(
                destination,
                destination_volume,
                package_volume,
                restore_destination=True,
            )
        _start_gitea(
            rollback_destination,
            rollback_volume,
            rollback_package_volume,
            restore_destination=True,
        )
        _create_user(source)
        for destination in [*destinations, rollback_destination]:
            _create_user(destination)
        _create_repository(source, "project")
        _api(
            source,
            "POST",
            "/api/v1/repos/drill/project/contents/README.md",
            body={"content": "YmFja3VwLWRyaWxsLW1hcmtlcgo=", "message": "marker"},
        )
        _api(
            source,
            "POST",
            "/api/v1/repos/drill/project/issues",
            body={"title": "first marker", "body": "restore evidence one"},
        )
        _api(
            source,
            "PUT",
            "/api/packages/drill/generic/drill-package/1.0.0/marker.txt",
            data="package-marker",
        )
        _upload_large_package(source)
        _upload_release_attachment(source)
        for drill_number, destination in enumerate(destinations, start=1):
            _create_repository(destination, f"destination-only-{drill_number}")
        _create_repository(rollback_destination, "rollback-marker")

        monkeypatch.setattr(gitea_module, "BACKUP_BASE_PATH", str(tmp_path))
        plugin = GiteaPlugin(name="gitea")
        config = {
            "container_name": source,
            "allow_service_stop": True,
            "timeout_seconds": 600,
        }
        digests: list[str] = []
        sizes: list[int] = []
        peak_before = _peak_rss_kib()

        for drill_number, destination in enumerate(destinations, start=1):
            backup_result = await plugin.backup(
                BackupContext(
                    job_id=f"backup-{drill_number}",
                    target_id="source",
                    config=config,
                    metadata={"target_slug": "gitea-source"},
                )
            )
            artifact = Path(backup_result["artifact_path"])
            sidecar = read_backup_sidecar(str(artifact))
            assert sidecar is not None
            assert sidecar["plugin_name"] == "gitea"
            assert sidecar["target_slug"] == "gitea-source"
            assert isinstance(sidecar["created_at"], str)
            sizes.append(artifact.stat().st_size)
            with artifact.open("rb") as artifact_file:
                digests.append(hashlib.file_digest(artifact_file, "sha256").hexdigest())
            with zipfile.ZipFile(artifact) as archive:
                names = {member.filename.rstrip("/") for member in archive.infolist()}
                assert "gitea-db.sql" in names
                assert any(name.startswith("repos/drill/project.git/") for name in names)
                assert any(name.startswith("data/packages/") for name in names)
                with archive.open("gitea-db.sql") as sql_file:
                    assert b"CREATE TABLE" in sql_file.read(1024 * 1024).upper()
            assert _api_status(source, "/api/healthz") == 200

            restore_result = await plugin.restore(
                RestoreContext(
                    job_id=f"restore-{drill_number}",
                    source_target_id="source",
                    destination_target_id="destination",
                    config={**config, "container_name": destination},
                    artifact_path=str(artifact),
                )
            )
            assert restore_result["status"] == "success"
            _assert_restored_state(destination, drill_number)
            assert (
                _api_status(
                    destination,
                    f"/api/v1/repos/drill/destination-only-{drill_number}",
                )
                == 404
            )

            if drill_number == 1:
                _api(
                    source,
                    "POST",
                    "/api/v1/repos/drill/project/issues",
                    body={"title": "second marker", "body": "restore evidence two"},
                )

        assert all(size > 64 * 1024 * 1024 for size in sizes)
        assert all(len(digest) == 64 for digest in digests)
        assert digests[0] != digests[1]
        peak_growth_bytes = max(_peak_rss_kib() - peak_before, 0) * 1024
        assert peak_growth_bytes < min(sizes) // 2

        latest_artifact = sorted(tmp_path.rglob("*.zip"))[-1]
        original_apply_restore = plugin._apply_restore

        async def fail_after_destructive_restore(*args: Any, **kwargs: Any) -> None:
            await original_apply_restore(*args, **kwargs)
            raise RuntimeError("injected failure after data replacement")

        monkeypatch.setattr(plugin, "_apply_restore", fail_after_destructive_restore)
        with pytest.raises(RuntimeError, match="previous destination data was restored"):
            await plugin.restore(
                RestoreContext(
                    job_id="rollback-proof",
                    source_target_id="source",
                    destination_target_id="rollback-destination",
                    config={**config, "container_name": rollback_destination},
                    artifact_path=str(latest_artifact),
                )
            )
        assert (
            _api_status(
                rollback_destination,
                "/api/v1/repos/drill/rollback-marker",
            )
            == 200
        )
        assert (
            _api_status(
                rollback_destination,
                "/api/v1/repos/drill/project",
            )
            == 404
        )
        assert _api_status(rollback_destination, "/api/healthz") == 200
    finally:
        for container in containers:
            _docker("rm", "-f", container, check=False)
        for volume in volumes:
            _docker("volume", "rm", "-f", volume, check=False)
