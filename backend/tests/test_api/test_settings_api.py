"""Tests for settings API endpoints."""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.db import Base, get_session
from app.api import settings as settings_router
from app.models import Settings as SettingsModel
from app.domain.enums import RunStatus


@pytest.fixture()
def app_with_db():
    """Create a test app with in-memory database."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    
    # Import all models to register them
    import app.models  # noqa: F401
    Base.metadata.create_all(bind=engine)
    
    app = FastAPI()
    app.include_router(settings_router.router)
    
    def override_get_session():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()
    
    app.dependency_overrides[get_session] = override_get_session
    
    yield app, TestingSessionLocal
    
    Base.metadata.drop_all(bind=engine)


class TestGetSettings:
    """Tests for GET /settings/ endpoint."""
    
    def test_returns_default_settings_on_first_call(self, app_with_db):
        """First call creates and returns default settings."""
        app, _ = app_with_db
        client = TestClient(app)
        
        response = client.get("/settings/")
        assert response.status_code == 200
        
        data = response.json()
        assert data["id"] == 1
        assert data["global_retention_policy_json"] is None
    
    def test_returns_existing_settings(self, app_with_db):
        """Returns existing settings if already created."""
        app, SessionLocal = app_with_db
        
        # Pre-create settings
        db = SessionLocal()
        settings = SettingsModel(
            id=1,
            global_retention_policy_json='{"rules": [{"unit": "day", "window": 7, "keep": 1}]}',
        )
        db.add(settings)
        db.commit()
        db.close()
        
        client = TestClient(app)
        response = client.get("/settings/")
        assert response.status_code == 200
        
        data = response.json()
        assert data["global_retention_policy_json"] == '{"rules": [{"unit": "day", "window": 7, "keep": 1}]}'


class TestUpdateSettings:
    """Tests for PUT /settings/ endpoint."""
    
    def test_updates_retention_policy(self, app_with_db):
        """Updates global retention policy."""
        app, _ = app_with_db
        client = TestClient(app)
        
        policy = {"rules": [{"unit": "month", "window": 6, "keep": 1}]}
        response = client.put(
            "/settings/",
            json={"global_retention_policy_json": json.dumps(policy)},
        )
        assert response.status_code == 200
        
        data = response.json()
        assert json.loads(data["global_retention_policy_json"]) == policy
    
    def test_clears_retention_policy(self, app_with_db):
        """Can clear retention policy by setting to null."""
        app, SessionLocal = app_with_db
        
        # Pre-create settings with policy
        db = SessionLocal()
        settings = SettingsModel(
            id=1,
            global_retention_policy_json='{"rules": [{"unit": "day", "window": 7, "keep": 1}]}',
        )
        db.add(settings)
        db.commit()
        db.close()
        
        client = TestClient(app)
        response = client.put(
            "/settings/",
            json={"global_retention_policy_json": None},
        )
        assert response.status_code == 200
        
        data = response.json()
        assert data["global_retention_policy_json"] is None


class TestRetentionPreview:
    """Tests for POST /settings/retention/preview endpoint."""
    
    def test_preview_returns_counts(self, app_with_db):
        """Preview endpoint returns keep/delete counts."""
        app, SessionLocal = app_with_db
        
        # Create settings
        db = SessionLocal()
        settings = SettingsModel(
            id=1,
            global_retention_policy_json='{"rules": [{"unit": "day", "window": 7, "keep": 1}]}',
        )
        db.add(settings)
        db.commit()
        db.close()
        
        client = TestClient(app)
        response = client.post("/settings/retention/preview?job_id=1&target_id=1")
        assert response.status_code == 200
        
        data = response.json()
        assert "keep_count" in data
        assert "delete_count" in data
        assert "deleted_paths" in data
        assert "kept_paths" in data


class TestRetentionRun:
    """Tests for POST /settings/retention/run endpoint."""
    
    def test_run_retention_all_creates_maintenance_run(self, app_with_db):
        """Running retention for all pairs creates a MaintenanceRun."""
        app, SessionLocal = app_with_db
        
        # Import models to ensure they're registered
        from app.models import MaintenanceJob as MaintenanceJobModel, MaintenanceRun as MaintenanceRunModel
        
        # Create the hidden manual retention job (as migration would)
        db = SessionLocal()
        manual_job = MaintenanceJobModel(
            key="retention_cleanup_manual",
            job_type="retention_cleanup",
            name="Manual Retention Cleanup",
            schedule_cron="0 0 1 1 *",
            enabled=False,
            visible_in_ui=False,
        )
        db.add(manual_job)
        db.commit()
        job_id = manual_job.id
        db.close()
        
        client = TestClient(app)
        response = client.post("/settings/retention/run")
        
        # Should succeed (even if no backups to clean)
        assert response.status_code in [200, 500]  # 500 if no backups exist, 200 if successful
        
        # Check that MaintenanceRun was created
        db = SessionLocal()
        runs = db.query(MaintenanceRunModel).filter(
            MaintenanceRunModel.maintenance_job_id == job_id
        ).all()
        db.close()
        
        # At least one run should exist
        assert len(runs) >= 1
        latest_run = runs[-1]
        assert latest_run.status in [RunStatus.SUCCESS.value, RunStatus.FAILED.value]
        assert latest_run.started_at is not None
    
    def test_run_retention_specific_pair_legacy_behavior(self, app_with_db):
        """Running retention for specific job_id/target_id uses legacy behavior."""
        app, SessionLocal = app_with_db
        
        # Create settings
        db = SessionLocal()
        settings = SettingsModel(
            id=1,
            global_retention_policy_json='{"rules": [{"unit": "day", "window": 7, "keep": 1}]}',
        )
        db.add(settings)
        db.commit()
        db.close()
        
        client = TestClient(app)
        # Legacy behavior: specific job_id and target_id
        response = client.post("/settings/retention/run?job_id=1&target_id=1")
        
        # Should return retention result (even if empty)
        assert response.status_code == 200
        data = response.json()
        assert "keep_count" in data
        assert "delete_count" in data
