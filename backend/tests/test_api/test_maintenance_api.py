"""Tests for maintenance API endpoints."""

from __future__ import annotations

import json
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import MaintenanceJob as MaintenanceJobModel, MaintenanceRun as MaintenanceRunModel
from app.domain.enums import RunStatus, MaintenanceJobType


def test_list_maintenance_jobs_default_visible(client: TestClient, db_session_override: Session):
    """Test listing maintenance jobs defaults to visible_in_ui=true."""
    # Create visible and hidden jobs
    visible_job = MaintenanceJobModel(
        key="visible_job",
        job_type=MaintenanceJobType.RETENTION_CLEANUP.value,
        name="Visible Job",
        schedule_cron="0 3 * * *",
        visible_in_ui=True,
    )
    hidden_job = MaintenanceJobModel(
        key="hidden_job",
        job_type=MaintenanceJobType.RETENTION_CLEANUP.value,
        name="Hidden Job",
        schedule_cron="0 4 * * *",
        visible_in_ui=False,
    )
    db_session_override.add(visible_job)
    db_session_override.add(hidden_job)
    db_session_override.commit()
    
    response = client.get("/api/v1/maintenance/jobs")
    assert response.status_code == 200
    jobs = response.json()
    assert len(jobs) == 1
    assert jobs[0]["key"] == "visible_job"


def test_list_maintenance_jobs_all(client: TestClient, db_session_override: Session):
    """Test listing all maintenance jobs with visible_in_ui=false."""
    visible_job = MaintenanceJobModel(
        key="visible_job",
        job_type=MaintenanceJobType.RETENTION_CLEANUP.value,
        name="Visible Job",
        schedule_cron="0 3 * * *",
        visible_in_ui=True,
    )
    hidden_job = MaintenanceJobModel(
        key="hidden_job",
        job_type=MaintenanceJobType.RETENTION_CLEANUP.value,
        name="Hidden Job",
        schedule_cron="0 4 * * *",
        visible_in_ui=False,
    )
    db_session_override.add(visible_job)
    db_session_override.add(hidden_job)
    db_session_override.commit()
    
    response = client.get("/api/v1/maintenance/jobs?visible_in_ui=false")
    assert response.status_code == 200
    jobs = response.json()
    assert len(jobs) == 1
    assert jobs[0]["key"] == "hidden_job"


def test_get_maintenance_job(client: TestClient, db_session_override: Session):
    """Test getting a specific maintenance job."""
    job = MaintenanceJobModel(
        key="test_job",
        job_type=MaintenanceJobType.RETENTION_CLEANUP.value,
        name="Test Job",
        schedule_cron="0 3 * * *",
    )
    db_session_override.add(job)
    db_session_override.commit()
    db_session_override.refresh(job)
    
    response = client.get(f"/api/v1/maintenance/jobs/{job.id}")
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == job.id
    assert data["key"] == "test_job"
    assert data["name"] == "Test Job"


def test_get_maintenance_job_not_found(client: TestClient):
    """Test getting a non-existent maintenance job returns 404."""
    response = client.get("/api/v1/maintenance/jobs/99999")
    assert response.status_code == 404


def test_list_maintenance_runs(client: TestClient, db_session_override: Session):
    """Test listing maintenance runs."""
    job = MaintenanceJobModel(
        key="test_job",
        job_type=MaintenanceJobType.RETENTION_CLEANUP.value,
        name="Test Job",
        schedule_cron="0 3 * * *",
    )
    db_session_override.add(job)
    db_session_override.commit()
    db_session_override.refresh(job)
    
    run1 = MaintenanceRunModel(
        maintenance_job_id=job.id,
        status=RunStatus.SUCCESS.value,
        message="Success",
        result_json=json.dumps({"targets_processed": 5, "deleted_count": 2}),
    )
    run2 = MaintenanceRunModel(
        maintenance_job_id=job.id,
        status=RunStatus.FAILED.value,
        message="Failed",
        result_json=json.dumps({"error": "Test error"}),
    )
    db_session_override.add(run1)
    db_session_override.add(run2)
    db_session_override.commit()
    
    response = client.get("/api/v1/maintenance/runs")
    assert response.status_code == 200
    runs = response.json()
    assert len(runs) == 2
    # Should be sorted by most recent first
    assert runs[0]["started_at"] >= runs[1]["started_at"]
    
    # Check that result_json is parsed
    assert runs[0]["result"] is not None
    if runs[0]["status"] == "success":
        assert "targets_processed" in runs[0]["result"]
    else:
        assert "error" in runs[0]["result"]


def test_list_maintenance_runs_with_limit(client: TestClient, db_session_override: Session):
    """Test listing maintenance runs with limit."""
    job = MaintenanceJobModel(
        key="test_job",
        job_type=MaintenanceJobType.RETENTION_CLEANUP.value,
        name="Test Job",
        schedule_cron="0 3 * * *",
    )
    db_session_override.add(job)
    db_session_override.commit()
    db_session_override.refresh(job)
    
    for i in range(5):
        run = MaintenanceRunModel(
            maintenance_job_id=job.id,
            status=RunStatus.SUCCESS.value,
        )
        db_session_override.add(run)
    db_session_override.commit()
    
    response = client.get("/api/v1/maintenance/runs?limit=3")
    assert response.status_code == 200
    runs = response.json()
    assert len(runs) == 3


def test_get_maintenance_run(client: TestClient, db_session_override: Session):
    """Test getting a specific maintenance run."""
    job = MaintenanceJobModel(
        key="test_job",
        job_type=MaintenanceJobType.RETENTION_CLEANUP.value,
        name="Test Job",
        schedule_cron="0 3 * * *",
    )
    db_session_override.add(job)
    db_session_override.commit()
    db_session_override.refresh(job)
    
    run = MaintenanceRunModel(
        maintenance_job_id=job.id,
        status=RunStatus.SUCCESS.value,
        message="Test message",
        result_json=json.dumps({"targets_processed": 10, "deleted_count": 3}),
    )
    db_session_override.add(run)
    db_session_override.commit()
    db_session_override.refresh(run)
    
    response = client.get(f"/api/v1/maintenance/runs/{run.id}")
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == run.id
    assert data["status"] == "success"
    assert data["message"] == "Test message"
    assert data["result"] is not None
    assert data["result"]["targets_processed"] == 10
    assert data["result"]["deleted_count"] == 3


def test_get_maintenance_run_not_found(client: TestClient):
    """Test getting a non-existent maintenance run returns 404."""
    response = client.get("/api/v1/maintenance/runs/99999")
    assert response.status_code == 404
