from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import time
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from app.core.plugins.artifacts import validate_backup_artifact
from app.core.plugins.base import BackupContext, RestoreContext
from app.core.plugins.sidecar import read_backup_sidecar
from app.plugins.homelab_backup import plugin as homelab_backup_module
from app.plugins.homelab_backup.plugin import (
    RESTORE_SENTINEL_CONTENT,
    RESTORE_SENTINEL_NAME,
    HomelabBackupPlugin,
)

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_HOMELAB_BACKUP_DOCKER_DRILL") != "1",
    reason="set RUN_HOMELAB_BACKUP_DOCKER_DRILL=1 for the disposable local drill",
)

_IMAGE = os.getenv(
    "HOMELAB_BACKUP_DOCKER_DRILL_IMAGE",
    "homelab-backup-backend:self-backup-dev",
)


@pytest.fixture
def anyio_backend() -> tuple[str, dict[str, bool]]:
    return ("asyncio", {"use_uvloop": True})


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


def _start_backend(container: str, database_dir: Path, backups_dir: Path) -> None:
    _docker(
        "run",
        "-d",
        "--name",
        container,
        "--network",
        "none",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "-v",
        f"{database_dir}:/app/db",
        "-v",
        f"{backups_dir}:/backups",
        _IMAGE,
    )
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        result = _docker(
            "exec",
            container,
            "python",
            "-c",
            (
                "import urllib.request; "
                "print(urllib.request.urlopen('http://127.0.0.1:8080/ready', "
                "timeout=2).read().decode())"
            ),
            check=False,
        )
        if result.returncode == 0 and '"ready"' in result.stdout:
            return
        state = _docker("inspect", "--format", "{{.State.Status}}", container).stdout.strip()
        if state == "exited":
            logs = _docker("logs", container, check=False).stderr[-2000:]
            raise RuntimeError(f"Disposable Homelab Backup backend exited: {logs}")
        time.sleep(0.5)
    raise RuntimeError("Disposable Homelab Backup backend did not become ready")


def _api(
    container: str,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
) -> Any:
    request_payload = json.dumps(
        {
            "method": method,
            "path": path,
            "body": body,
        }
    )
    script = """
import json
import sys
import urllib.request

request_data = json.loads(sys.stdin.read())
body = request_data["body"]
payload = None if body is None else json.dumps(body).encode("utf-8")
request = urllib.request.Request(
    "http://127.0.0.1:8080" + request_data["path"],
    data=payload,
    method=request_data["method"],
    headers={"content-type": "application/json"},
)
with urllib.request.urlopen(request, timeout=10) as response:
    sys.stdout.write(response.read().decode("utf-8"))
""".strip()
    result = _docker(
        "exec",
        "-i",
        container,
        "python",
        "-c",
        script,
        input_text=request_payload,
    )
    return json.loads(result.stdout)


def _seed_source(container: str, secret_marker: str) -> None:
    group = _api(
        container,
        "POST",
        "/api/v1/groups/",
        {"name": "Drill group", "description": "isolated evidence"},
    )
    proof = _api(
        container,
        "POST",
        "/api/v1/targets/",
        {
            "name": "Secret proof",
            "plugin_name": "pihole",
            "plugin_config_json": json.dumps(
                {"base_url": "http://invalid.local", "password": secret_marker}
            ),
        },
    )
    self_target = _api(
        container,
        "POST",
        "/api/v1/targets/",
        {
            "name": "Self backup",
            "plugin_name": "homelab_backup",
            "plugin_config_json": json.dumps({"database_path": "/app/db/homelab_backup.db"}),
        },
    )
    _api(
        container,
        "POST",
        f"/api/v1/groups/{group['id']}/targets",
        {"target_ids": [proof["id"], self_target["id"]]},
    )
    tags = _api(
        container,
        "POST",
        f"/api/v1/targets/{self_target['id']}/tags",
        {"tag_names": ["self-drill"]},
    )
    _api(
        container,
        "POST",
        "/api/v1/jobs/",
        {
            "tag_id": tags[0]["tag"]["id"],
            "name": "Never execute in drill",
            "schedule_cron": "0 4 * * *",
            "enabled": False,
            "retention_policy_json": json.dumps(
                {"rules": [{"unit": "day", "window": 7, "keep": 1}]}
            ),
        },
    )
    _api(
        container,
        "PUT",
        "/api/v1/settings/",
        {
            "global_retention_policy_json": json.dumps(
                {"rules": [{"unit": "week", "window": 4, "keep": 1}]}
            )
        },
    )


def _assert_restored_backend(
    container: str,
    *,
    expected_target_count: int,
    expected_secret_sha256: str,
) -> None:
    targets = _api(container, "GET", "/api/v1/targets/")
    groups = _api(container, "GET", "/api/v1/groups/")
    tags = _api(container, "GET", "/api/v1/tags/")
    jobs = _api(container, "GET", "/api/v1/jobs/")
    runs = _api(container, "GET", "/api/v1/runs/")
    settings = _api(container, "GET", "/api/v1/settings/")
    openapi = _api(container, "GET", "/api/openapi.json")

    assert len(targets) == expected_target_count
    assert len(groups) == 1
    expected_tag_slugs = {
        "drill-group",
        "secret-proof",
        "self-backup",
        "self-drill",
    }
    if expected_target_count == 3:
        expected_tag_slugs.add("later-state")
    assert {tag["slug"] for tag in tags} == expected_tag_slugs
    assert len(jobs) == 1
    assert jobs[0]["enabled"] is False
    assert runs == []
    assert "week" in settings["global_retention_policy_json"]
    assert openapi["info"]["version"] == "0.2.1"
    proof_config = next(
        target["plugin_config_json"] for target in targets if target["name"] == "Secret proof"
    )
    restored_secret = json.loads(proof_config)["password"]
    assert hashlib.sha256(restored_secret.encode()).hexdigest() == expected_secret_sha256


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@pytest.mark.anyio
async def test_two_consecutive_self_backup_restore_and_exact_image_boot_drills(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    suffix = uuid.uuid4().hex[:10]
    source_container = f"codex-homelab-backup-source-{suffix}"
    destination_containers = [
        f"codex-homelab-backup-restore-{number}-{suffix}" for number in (1, 2)
    ]
    source_db_dir = tmp_path / "source-db"
    source_backups_dir = tmp_path / "source-backups"
    artifact_root = tmp_path / "artifacts"
    source_db_dir.mkdir()
    source_backups_dir.mkdir()
    artifact_root.mkdir()
    secret_marker = f"synthetic-{suffix}-secret"
    secret_sha256 = hashlib.sha256(secret_marker.encode()).hexdigest()
    all_containers = [source_container, *destination_containers]

    try:
        _start_backend(source_container, source_db_dir, source_backups_dir)
        _seed_source(source_container, secret_marker)
        plugin = HomelabBackupPlugin(name="homelab_backup")
        monkeypatch.setattr(
            homelab_backup_module,
            "BACKUP_BASE_PATH",
            str(artifact_root),
        )
        context = BackupContext(
            job_id="self-drill",
            target_id="self-source",
            config={"database_path": str(source_db_dir / "homelab_backup.db")},
            metadata={"target_slug": "self-drill"},
        )
        artifacts: list[Path] = []
        expected_target_counts = [2, 3]

        for drill_number in (1, 2):
            if drill_number == 2:
                _api(
                    source_container,
                    "POST",
                    "/api/v1/targets/",
                    {
                        "name": "Later state",
                        "plugin_name": "jellyfin",
                        "plugin_config_json": json.dumps(
                            {
                                "base_url": "http://invalid.local",
                                "api_key": "synthetic",
                                "backup_path": "/isolated-jellyfin-backups",
                            }
                        ),
                    },
                )
            backup_result = await plugin.backup(context)
            artifact_path = Path(backup_result["artifact_path"])
            validated = validate_backup_artifact(artifact_path.as_posix(), plugin, context)
            assert validated.size_bytes == artifact_path.stat().st_size
            assert validated.sha256 == _sha256(artifact_path)
            sidecar = read_backup_sidecar(str(artifact_path))
            assert sidecar is not None
            assert sidecar["artifact_path"] == str(artifact_path)
            assert sidecar["plugin_name"] == "homelab_backup"
            assert sidecar["target_slug"] == "self-drill"
            assert datetime.fromisoformat(sidecar["created_at"]).utcoffset() is not None
            with zipfile.ZipFile(artifact_path) as archive:
                manifest = json.loads(archive.read("manifest.json"))
            assert manifest["row_counts"]["targets"] == expected_target_counts[drill_number - 1]
            assert manifest["database"]["size_bytes"] > 0
            artifacts.append(artifact_path)

            restore_db_dir = tmp_path / f"restore-db-{drill_number}"
            restore_backups_dir = tmp_path / f"restore-backups-{drill_number}"
            restore_db_dir.mkdir()
            restore_backups_dir.mkdir()
            (restore_db_dir / RESTORE_SENTINEL_NAME).write_text(
                RESTORE_SENTINEL_CONTENT,
                encoding="utf-8",
            )
            destination_db = restore_db_dir / "homelab_backup.db"
            restore_result = await plugin.restore(
                RestoreContext(
                    job_id=f"restore-{drill_number}",
                    source_target_id="self-source",
                    destination_target_id=f"self-destination-{drill_number}",
                    config={"database_path": str(destination_db)},
                    artifact_path=str(artifact_path),
                )
            )
            assert restore_result["status"] == "partial"
            assert destination_db.stat().st_size == manifest["database"]["size_bytes"]
            assert _sha256(destination_db) == manifest["database"]["sha256"]

            _start_backend(
                destination_containers[drill_number - 1],
                restore_db_dir,
                restore_backups_dir,
            )
            await asyncio.to_thread(
                _assert_restored_backend,
                destination_containers[drill_number - 1],
                expected_target_count=expected_target_counts[drill_number - 1],
                expected_secret_sha256=secret_sha256,
            )

        assert artifacts[0] != artifacts[1]
        assert _sha256(artifacts[0]) != _sha256(artifacts[1])
    finally:
        for container in all_containers:
            _docker("rm", "-f", container, check=False)
