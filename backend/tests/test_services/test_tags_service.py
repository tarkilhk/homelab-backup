from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from app.models import TargetTag, Job
from app.services import TagService, TargetService


def test_tag_service_create_normalizes_and_idempotent(db):
    svc = TagService(db)
    t1 = svc.create("  Prod  ")
    assert t1.slug == "prod"
    t2 = svc.create("PROD")
    assert t1.id == t2.id


def test_tag_service_delete_blocks_auto_and_jobs(db):
    svc = TagService(db)
    # Create target and auto-tag via TargetService
    tsvc = TargetService(db)
    target = tsvc.create(name="alpha", plugin_name="p", plugin_config_json="{}")
    # Find the auto-tag
    auto_tt = (
        db.query(TargetTag)
        .filter(TargetTag.target_id == target.id, TargetTag.origin == "AUTO")
        .one()
    )
    auto_tag_id = auto_tt.tag_id
    # Deleting auto-tag should raise IntegrityError
    with pytest.raises(IntegrityError):
        svc.delete(auto_tag_id)

    # Create a manual tag and a job using it
    manual = svc.create("manual")
    db.add(Job(tag_id=manual.id, name="J", schedule_cron="* * * * *", enabled=True))
    db.commit()
    with pytest.raises(IntegrityError):
        svc.delete(manual.id)


