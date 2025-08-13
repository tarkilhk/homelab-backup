from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import Run as RunModel
from app.core.scheduler import scheduled_tick_with_session


def test_scheduler_tick_runs_jobs_by_tag_and_skips_overlap(client, db_session_override: Session) -> None:
    # Create two targets
    r = client.post(
        "/api/v1/targets/",
        json={"name": "SvcX", "plugin_name": "dummy", "plugin_config_json": "{}"},
    )
    assert r.status_code == 201
    r = client.post(
        "/api/v1/targets/",
        json={"name": "SvcY", "plugin_name": "dummy", "plugin_config_json": "{}"},
    )
    assert r.status_code == 201

    # Resolve auto-tag for SvcX
    r = client.get("/api/v1/tags/")
    assert r.status_code == 200
    tags = r.json()
    tag_id = next(t["id"] for t in tags if t.get("display_name") == "SvcX")

    # Create one job tied to that tag
    r = client.post(
        "/api/v1/jobs/",
        json={"tag_id": tag_id, "name": "TickJob", "schedule_cron": "* * * * *", "enabled": True},
    )
    assert r.status_code == 201
    job_id = r.json()["id"]

    # Count runs before
    before = db_session_override.query(RunModel).count()

    summary = scheduled_tick_with_session(db_session_override, job_id)
    assert summary.get("started") is True
    assert isinstance(summary.get("results"), list)
    after = db_session_override.query(RunModel).count()
    assert after >= before + 1

    # Simulate overlap by holding the job lock and invoking again
    from app.services.jobs import _get_job_lock
    lk = _get_job_lock(job_id)
    assert lk.acquire(blocking=False) is True
    try:
        summary2 = scheduled_tick_with_session(db_session_override, job_id)
        assert summary2.get("started") is False
    finally:
        try:
            lk.release()
        except Exception:
            pass


