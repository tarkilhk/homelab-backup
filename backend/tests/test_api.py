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
        "type": "postgres",
        "config_json": "{\"host\": \"localhost\", \"port\": 5432, \"database\": \"test\"}",
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


def test_jobs_crud_and_run_now(client: TestClient) -> None:
    # Need a target first
    target_payload = {
        "name": "Test Service",
        "slug": "test-svc",
        "type": "custom",
        "config_json": "{}",
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
        "plugin": "dummy",
        "plugin_version": "1.0.0",
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


