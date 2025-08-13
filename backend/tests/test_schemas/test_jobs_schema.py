from __future__ import annotations

from datetime import datetime, timezone

from app.schemas import JobCreate, JobUpdate, Job


def test_job_create_schema() -> None:
    data = {
        "tag_id": 1,
        "name": "Daily Backup",
        "schedule_cron": "0 2 * * *",
        "enabled": True,
    }
    job = JobCreate(**data)
    assert job.enabled is True


def test_job_update_schema() -> None:
    update = JobUpdate(name="Weekly Backup", schedule_cron="0 2 * * 0", enabled=False)
    assert update.name == "Weekly Backup"
    assert update.schedule_cron == "0 2 * * 0"
    assert update.enabled is False


def test_job_response_schema() -> None:
    now = datetime.now(timezone.utc)
    data = {
        "id": 1,
        "tag_id": 1,
        "name": "Daily Backup",
        "schedule_cron": "0 2 * * *",
        "enabled": True,
        "created_at": now,
        "updated_at": now,
    }
    job = Job(**data)
    assert job.id == 1
    assert job.tag_id == 1


