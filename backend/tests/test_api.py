"""API tests for targets, jobs, and runs routers."""

from __future__ import annotations

from typing import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import Session, sessionmaker

from app.main import app
from app.core.db import Base, get_session
from app.models import Run as RunModel


class _DummyScheduler:
    def start(self) -> None:  # noqa: D401
        """No-op start."""
        return None

    def shutdown(self) -> None:  # noqa: D401
        """No-op shutdown."""
        return None


@pytest.fixture
def db_session_override() -> Generator[Session, None, None]:
    """Provide a test DB session and override FastAPI dependency."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    # Create tables
    # Ensure models are registered with Base before creating tables
    import app.models  # noqa: F401
    Base.metadata.create_all(bind=engine)

    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()
        # Drop all tables after test
        Base.metadata.drop_all(bind=engine)


@pytest.fixture
def client(db_session_override: Session, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient, None, None]:
    """FastAPI TestClient with DB and scheduler overrides."""

    def override_get_session() -> Generator[Session, None, None]:
        try:
            yield db_session_override
        finally:
            pass

    # Override DB dependency
    app.dependency_overrides[get_session] = override_get_session

    # Stub scheduler to avoid starting real APScheduler in tests
    monkeypatch.setattr("app.main.get_scheduler", lambda: _DummyScheduler(), raising=True)

    with TestClient(app) as test_client:
        yield test_client

    # Cleanup overrides
    app.dependency_overrides.clear()


def test_targets_crud(client: TestClient) -> None:
    # Create
    create_payload = {
        "name": "Test Database",
        "slug": "test-db",
        "plugin_name": "dummy",
        "plugin_config_json": "{}",
    }
    r = client.post("/api/v1/targets/", json=create_payload)
    assert r.status_code == 201, r.text
    target = r.json()
    assert target["id"] > 0
    assert target["slug"] == "test-db"

    target_id = target["id"]

    # List
    r = client.get("/api/v1/targets/")
    assert r.status_code == 200
    items = r.json()
    assert isinstance(items, list)
    assert any(item["id"] == target_id for item in items)

    # Get by id
    r = client.get(f"/api/v1/targets/{target_id}")
    assert r.status_code == 200
    assert r.json()["id"] == target_id

    # Update
    r = client.put(f"/api/v1/targets/{target_id}", json={"name": "Renamed Database"})
    assert r.status_code == 200
    assert r.json()["name"] == "Renamed Database"

    # Delete
    r = client.delete(f"/api/v1/targets/{target_id}")
    assert r.status_code == 204

    # 404 after delete
    r = client.get(f"/api/v1/targets/{target_id}")
    assert r.status_code == 404


def test_create_plugin_target_with_schema_validation(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    # Provide a fake schema for plugin 'pihole'
    schema_obj = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "required": ["base_url", "token"],
        "properties": {
            "base_url": {"type": "string", "format": "uri"},
            "token": {"type": "string"},
        },
        "additionalProperties": False,
    }

    # Patch loader to return a temporary schema path
    import json, tempfile, os
    from app.core.plugins import loader

    with tempfile.TemporaryDirectory() as td:
        schema_path = os.path.join(td, "schema.json")
        with open(schema_path, "w", encoding="utf-8") as f:
            json.dump(schema_obj, f)
        monkeypatch.setattr(loader, "get_plugin_schema_path", lambda key: schema_path)

        # Valid payload
        valid_payload = {
            "name": "Pi-hole",
            "slug": "pihole",
            "plugin_name": "pihole",
            "plugin_config_json": json.dumps({"base_url": "http://example.com", "token": "abc"}),
        }
        r = client.post("/api/v1/targets/", json=valid_payload)
        assert r.status_code == 201, r.text

        # Invalid payload (missing token)
        invalid_payload = {
            "name": "Pi-hole2",
            "slug": "pihole2",
            "plugin_name": "pihole",
            "plugin_config_json": json.dumps({"base_url": "http://example.com"}),
        }
        r = client.post("/api/v1/targets/", json=invalid_payload)
        assert r.status_code == 422


def test_jobs_crud_and_run_now(client: TestClient) -> None:
    # Need a target first
    target_payload = {
        "name": "Test Service",
        "slug": "test-svc",
        "plugin_name": "dummy",
        "plugin_config_json": "{}",
    }
    r = client.post("/api/v1/targets/", json=target_payload)
    assert r.status_code == 201
    target_id = r.json()["id"]

    # Create job
    job_payload = {
        "target_id": target_id,
        "name": "Daily Backup",
        "schedule_cron": "0 2 * * *",
        "enabled": "true",
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


# Metrics and notifier tests
def test_metrics_endpoint_counts(client: TestClient, db_session_override: Session) -> None:
    # Create a target and a job
    r = client.post(
        "/api/v1/targets/",
        json={"name": "Svc", "slug": "svc", "plugin_name": "dummy", "plugin_config_json": "{}"},
    )
    assert r.status_code == 201
    target_id = r.json()["id"]

    r = client.post(
        "/api/v1/jobs/",
        json={
            "target_id": target_id,
            "name": "Job A",
            "schedule_cron": "0 2 * * *",
            "enabled": "true",
        },
    )
    assert r.status_code == 201
    job_id = r.json()["id"]

    # Insert runs directly: 2 success, 1 failed
    from datetime import datetime, timezone
    db = db_session_override
    db.add_all(
        [
            RunModel(job_id=job_id, started_at=datetime.now(timezone.utc), status="success"),
            RunModel(job_id=job_id, started_at=datetime.now(timezone.utc), status="success"),
            RunModel(job_id=job_id, started_at=datetime.now(timezone.utc), status="failed"),
        ]
    )
    db.commit()

    # Fetch metrics
    r = client.get("/metrics")
    assert r.status_code == 200
    body = r.text
    # Expect counters and gauge present
    assert f'job_success_total{{job_id="{job_id}"' in body
    assert f'job_failure_total{{job_id="{job_id}"' in body
    # Validate numeric values
    assert f'job_success_total{{job_id="{job_id}"' in body and " 2" in body
    assert f'job_failure_total{{job_id="{job_id}"' in body and " 1" in body
    assert f'last_run_timestamp{{job_id="{job_id}"' in body


def test_failure_triggers_email_notifier(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    # Create target and job
    r = client.post(
        "/api/v1/targets/",
        json={"name": "Svc2", "slug": "svc2", "plugin_name": "dummy", "plugin_config_json": "{}"},
    )
    assert r.status_code == 201
    target_id = r.json()["id"]

    r = client.post(
        "/api/v1/jobs/",
        json={
            "target_id": target_id,
            "name": "Will Fail",
            "schedule_cron": "0 3 * * *",
            "enabled": "true",
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

    from app import core as _core  # noqa: F401  # ensure package import path
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

