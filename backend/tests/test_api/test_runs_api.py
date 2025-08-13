from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Run as RunModel


def test_runs_filters_status_date_target(client: TestClient, db_session_override: Session) -> None:
    """Verify runs list endpoint supports status, date range, and target filters."""
    # Create two targets
    r = client.post(
        "/api/v1/targets/",
        json={"name": "T1", "plugin_name": "dummy", "plugin_config_json": "{}"},
    )
    assert r.status_code == 201
    t1_id = r.json()["id"]

    r = client.post(
        "/api/v1/targets/",
        json={"name": "T2", "plugin_name": "dummy", "plugin_config_json": "{}"},
    )
    assert r.status_code == 201
    t2_id = r.json()["id"]

    # Resolve auto-tags for each target
    r = client.get("/api/v1/tags/")
    assert r.status_code == 200
    tags = r.json()
    t1_tag_id = next(t["id"] for t in tags if t.get("display_name") == "T1")
    t2_tag_id = next(t["id"] for t in tags if t.get("display_name") == "T2")

    # Create jobs for each tag
    r = client.post(
        "/api/v1/jobs/",
        json={"tag_id": t1_tag_id, "name": "Job1", "schedule_cron": "0 1 * * *", "enabled": True},
    )
    assert r.status_code == 201
    j1_id = r.json()["id"]

    r = client.post(
        "/api/v1/jobs/",
        json={"tag_id": t2_tag_id, "name": "Job2", "schedule_cron": "0 2 * * *", "enabled": True},
    )
    assert r.status_code == 201
    j2_id = r.json()["id"]

    # Insert runs with distinct statuses and dates
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    db = db_session_override
    db.add_all(
        [
            RunModel(job_id=j1_id, status="success", started_at=now - timedelta(days=2)),
            RunModel(job_id=j1_id, status="failed", started_at=now - timedelta(days=1, hours=1)),
            RunModel(job_id=j2_id, status="success", started_at=now - timedelta(hours=12)),
        ]
    )
    db.commit()

    # Filter by status
    r = client.get("/api/v1/runs/?status=success")
    assert r.status_code == 200
    items = r.json()
    assert all(it["status"] == "success" for it in items)

    # Filter by date range (last 24h)
    start = (now - timedelta(days=1)).isoformat()
    r = client.get(f"/api/v1/runs/?start_date={start}")
    assert r.status_code == 200
    items = r.json()
    assert all(it["started_at"] >= start for it in [*items])

    # Filter by target via target_id
    r = client.get(f"/api/v1/runs/?target_id={t1_id}")
    assert r.status_code == 200
    items = r.json()
    # All items should belong to runs of jobs associated with T1's tag
    assert all(it["job"]["tag_id"] == t1_tag_id for it in items)


