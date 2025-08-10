from __future__ import annotations

import pytest
from app.models import Target, Tag, TargetTag, Job, ValidationError422


def test_job_model(db) -> None:
    target = Target(
        name="Test Database",
        slug="test-db-job",
        plugin_name="pihole",
        plugin_config_json='{"base_url":"http://pihole.local","token":"abc"}',
    )
    db.add(target)
    db.commit()
    db.refresh(target)

    tag = Tag(display_name="Daily Backup")
    db.add(tag)
    db.commit()
    db.refresh(tag)
    db.add(TargetTag(target_id=target.id, tag_id=tag.id, origin="DIRECT"))
    db.commit()

    job = Job(
        tag_id=tag.id,
        name="Daily Backup",
        schedule_cron="0 2 * * *",
        enabled=True,
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    assert job.id is not None
    assert job.tag_id == tag.id
    assert job.name == "Daily Backup"
    assert job.schedule_cron == "0 2 * * *"
    assert job.enabled is True


def test_job_boolean_enabled_and_cron_validation(db) -> None:
    tag = Tag(display_name="C")
    db.add(tag)
    db.commit()
    db.refresh(tag)

    j = Job(tag_id=tag.id, name="J1", schedule_cron="0 2 * * *", enabled=True)
    db.add(j)
    db.commit()
    db.refresh(j)
    assert j.enabled is True

    bad = Job(tag_id=tag.id, name="J2", schedule_cron="BAD CRON", enabled=False)
    db.add(bad)
    with pytest.raises(ValidationError422):
        db.flush()
    db.rollback()


