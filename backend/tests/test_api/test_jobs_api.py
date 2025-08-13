from __future__ import annotations

from fastapi.testclient import TestClient
import tempfile
import pytest
from typing import Any, Dict
from app.core.plugins.base import BackupPlugin, BackupContext
from sqlalchemy.orm import Session


def test_jobs_crud_and_run_now(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    # Provide a minimal success plugin so the run-now path can complete
    class _SuccessPlugin(BackupPlugin):
        async def validate_config(self, config: Dict[str, Any]) -> bool:  # noqa: ARG002
            return True
        async def test(self, config: Dict[str, Any]) -> bool:  # noqa: ARG002
            return True
        async def backup(self, context: BackupContext) -> Dict[str, Any]:  # noqa: ARG002
            fd, path = tempfile.mkstemp(prefix="backup-test-", suffix=".txt")
            return {"artifact_path": path}
        async def restore(self, context: BackupContext) -> Dict[str, Any]:  # noqa: ARG002
            return {"ok": True}
        async def get_status(self, context: BackupContext) -> Dict[str, Any]:  # noqa: ARG002
            return {"ok": True}

    import app.core.scheduler as sched
    monkeypatch.setattr(sched, "get_plugin", lambda name: _SuccessPlugin(name))
    # Need a target first (auto-tag should be created for target name)
    target_payload = {
        "name": "Test Service",
        "plugin_name": "dummy",
        "plugin_config_json": "{}",
    }
    r = client.post("/api/v1/targets/", json=target_payload)
    assert r.status_code == 201
    target_id = r.json()["id"]

    # Resolve auto-tag id for this target via Tags API
    r = client.get("/api/v1/tags/")
    assert r.status_code == 200
    tags = r.json()
    tag_id = next(t["id"] for t in tags if t.get("display_name") == "Test Service")

    # Create job by tag
    job_payload = {
        "tag_id": tag_id,
        "name": "Daily Backup",
        "schedule_cron": "0 2 * * *",
        "enabled": True,
    }
    r = client.post("/api/v1/jobs/", json=job_payload)
    assert r.status_code == 201, r.text
    job = r.json()
    job_id = job["id"]

    # List jobs
    r = client.get("/api/v1/jobs/")
    assert r.status_code == 200
    assert any(item["id"] == job_id for item in r.json())

    # Get by id
    r = client.get(f"/api/v1/jobs/{job_id}")
    assert r.status_code == 200
    assert r.json()["id"] == job_id

    # Update
    r = client.put(f"/api/v1/jobs/{job_id}", json={"name": "Nightly Backup"})
    assert r.status_code == 200
    assert r.json()["name"] == "Nightly Backup"

    # Trigger run now (dummy)
    r = client.post(f"/api/v1/jobs/{job_id}/run")
    assert r.status_code == 200
    run = r.json()
    assert run["job_id"] == job_id
    assert run["status"] == "success"
    assert run["started_at"] is not None
    assert run["finished_at"] is not None

    run_id = run["id"]

    # Runs listing should include it
    r = client.get("/api/v1/runs/")
    assert r.status_code == 200
    assert any(item["id"] == run_id for item in r.json())

    # Runs get by id
    r = client.get(f"/api/v1/runs/{run_id}")
    assert r.status_code == 200
    assert r.json()["id"] == run_id

    # Delete job
    r = client.delete(f"/api/v1/jobs/{job_id}")
    assert r.status_code == 204

    # 404 after delete
    r = client.get(f"/api/v1/jobs/{job_id}")
    assert r.status_code == 404


def test_failure_triggers_email_notifier(client: TestClient, monkeypatch: object) -> None:
    # Create target and job
    r = client.post(
        "/api/v1/targets/",
        json={"name": "Svc2", "plugin_name": "dummy", "plugin_config_json": "{}"},
    )
    assert r.status_code == 201
    target = r.json()
    # Resolve auto-tag id for this target
    r = client.get("/api/v1/tags/")
    assert r.status_code == 200
    tags = r.json()
    tag_id = next(t["id"] for t in tags if t.get("display_name") == target["name"]) 

    r = client.post(
        "/api/v1/jobs/",
        json={
            "tag_id": tag_id,
            "name": "Will Fail",
            "schedule_cron": "0 3 * * *",
            "enabled": True,
        },
    )
    assert r.status_code == 201
    job_id = r.json()["id"]

    # Monkeypatch scheduler to use a plugin that raises
    class _FailingPlugin:
        def __init__(self, name: str) -> None:
            self.name = name

        async def backup(self, ctx):  # type: ignore[no-untyped-def]
            raise RuntimeError("boom")

    import app.core.scheduler as sched

    monkeypatch.setattr(sched, "get_plugin", lambda name: _FailingPlugin(name))

    sent = {"called": 0}

    def _fake_send(subj: str, body: str) -> None:  # noqa: ARG001
        sent["called"] += 1

    monkeypatch.setattr(sched, "send_failure_email", _fake_send)

    # Trigger run now -> should fail and call notifier
    r = client.post(f"/api/v1/jobs/{job_id}/run")
    assert r.status_code == 200
    payload = r.json()
    assert payload["status"] == "failed"
    assert sent["called"] == 1


