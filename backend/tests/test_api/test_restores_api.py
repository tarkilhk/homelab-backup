from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Job as JobModel, Run as RunModel, TargetRun as TargetRunModel, Target as TargetModel


def _create_target(client: TestClient, name: str, plugin: str, config: dict) -> int:
    response = client.post(
        "/api/v1/targets/",
        json={
            "name": name,
            "plugin_name": plugin,
            "plugin_config_json": json.dumps(config),
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _get_tag_id_for_target(client: TestClient, target_name: str) -> int:
    tags_resp = client.get("/api/v1/tags/")
    assert tags_resp.status_code == 200
    tags = tags_resp.json()
    for tag in tags:
        if tag.get("display_name") == target_name:
            return int(tag["id"])
    raise AssertionError(f"Tag for target {target_name} not found")


def _create_job(db: Session, tag_id: int, name: str) -> JobModel:
    job = JobModel(tag_id=tag_id, name=name, schedule_cron="0 2 * * *", enabled=True)
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def _create_source_target_run(
    db: Session,
    *,
    job_id: int,
    target_id: int,
    artifact_path: Path,
) -> TargetRunModel:
    artifact_bytes = artifact_path.read_bytes()
    sha = hashlib.sha256(artifact_bytes).hexdigest()

    run = RunModel(
        job_id=job_id,
        status="success",
        operation="backup",
        message="Backup completed",
        logs_text="Backup logs",
        started_at=datetime.now(timezone.utc),
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    target_run = TargetRunModel(
        run_id=run.id,
        target_id=target_id,
        status="success",
        operation="backup",
        message="OK",
        artifact_path=str(artifact_path),
        artifact_bytes=len(artifact_bytes),
        sha256=sha,
        logs_text="Target logs",
        started_at=datetime.now(timezone.utc),
    )
    db.add(target_run)
    db.commit()
    db.refresh(target_run)
    return target_run


def test_restore_success(
    client: TestClient,
    db_session_override: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: restore clones artifact metadata into a new run."""
    import asyncio
    
    class DummyProcess:
        def __init__(self):
            self.returncode = 0
        async def communicate(self):
            return b"", b""
    
    async def fake_exec(*args, **kwargs):
        # Verify psql command structure
        assert args[0] == "psql", f"Expected psql, got {args[0]}"
        return DummyProcess()
    
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(
        "app.plugins.postgresql.plugin.BACKUP_BASE_PATH",
        str(tmp_path / "backups"),
        raising=True,
    )
    source_target_id = _create_target(
        client,
        "Source Postgres",
        "postgresql",
        {"host": "db.local", "user": "postgres", "password": "secret"},
    )
    source_tag_id = _get_tag_id_for_target(client, "Source Postgres")
    job = _create_job(db_session_override, source_tag_id, "Backup Source Postgres")

    dest_target_id = _create_target(
        client,
        "Destination Postgres",
        "postgresql",
        {"host": "db.other", "user": "postgres", "password": "secret"},
    )

    artifact_path = tmp_path / "postgres-backup.sql"
    artifact_path.write_text("dummy backup data")

    source_target_run = _create_source_target_run(
        db_session_override,
        job_id=job.id,
        target_id=source_target_id,
        artifact_path=artifact_path,
    )

    resp = client.post(
        "/api/v1/restores/",
        json={
            "source_target_run_id": source_target_run.id,
            "destination_target_id": dest_target_id,
        },
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["operation"] == "restore"
    assert data["status"] == "success"
    target_runs = data["target_runs"]
    assert isinstance(target_runs, list) and len(target_runs) == 1
    tr = target_runs[0]
    assert tr["operation"] == "restore"
    assert isinstance(tr["artifact_path"], str)

    dest_target = db_session_override.get(TargetModel, dest_target_id)
    assert dest_target is not None
    assert data["display_job_name"] == f"{dest_target.name} Restore"
    assert data.get("display_tag_name") == dest_target.name

    # PostgreSQL plugin returns the original artifact path (doesn't copy it)
    # because the restore actually executes via psql
    assert tr["artifact_path"] == str(artifact_path)
    assert tr["artifact_bytes"] == len("dummy backup data")


def test_restore_rejects_plugin_mismatch(client: TestClient, db_session_override: Session, tmp_path: Path) -> None:
    source_target_id = _create_target(
        client,
        "Source PiHole",
        "pihole",
        {"base_url": "http://pihole.local", "login": "admin", "password": "pw"},
    )
    source_tag_id = _get_tag_id_for_target(client, "Source PiHole")
    job = _create_job(db_session_override, source_tag_id, "Backup PiHole")

    dest_target_id = _create_target(
        client,
        "Destination MySQL",
        "mysql",
        {"host": "db.local", "user": "root", "password": "pw", "database": "test"},
    )

    artifact_path = tmp_path / "pihole.zip"
    artifact_path.write_text("zip data")
    source_target_run = _create_source_target_run(
        db_session_override,
        job_id=job.id,
        target_id=source_target_id,
        artifact_path=artifact_path,
    )

    resp = client.post(
        "/api/v1/restores/",
        json={
            "source_target_run_id": source_target_run.id,
            "destination_target_id": dest_target_id,
        },
    )
    assert resp.status_code == 400
    assert "same plugin" in resp.json()["detail"]


def test_restore_missing_artifact_path(client: TestClient, db_session_override: Session, tmp_path: Path) -> None:
    source_target_id = _create_target(
        client,
        "Source Radarr",
        "radarr",
        {"base_url": "http://radarr.local", "api_key": "token"},
    )
    source_tag_id = _get_tag_id_for_target(client, "Source Radarr")
    job = _create_job(db_session_override, source_tag_id, "Backup Radarr")

    dest_target_id = _create_target(
        client,
        "Destination Radarr",
        "radarr",
        {"base_url": "http://radarr.local", "api_key": "token"},
    )

    missing_path = tmp_path / "radarr.json"
    # Create the file first so _create_source_target_run can read it
    missing_path.write_text("radarr backup data")
    source_target_run = _create_source_target_run(
        db_session_override,
        job_id=job.id,
        target_id=source_target_id,
        artifact_path=missing_path,
    )
    # Remove artifact to force missing file
    missing_path.unlink()

    resp = client.post(
        "/api/v1/restores/",
        json={
            "source_target_run_id": source_target_run.id,
            "destination_target_id": dest_target_id,
        },
    )
    assert resp.status_code == 400
    assert "Artifact file not found" in resp.json()["detail"]
