from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from app.models import Tag, TargetTag
from app.services import GroupService, TargetService


def test_target_create_creates_auto_tag_and_optional_group_propagation(db):
    gsvc = GroupService(db)
    tsvc = TargetService(db)
    g = gsvc.create("G")
    gsvc.add_tags(g.id, ["A", "B"])  # create tags and link group
    tgt = tsvc.create(name="svc", plugin_name="p", plugin_config_json="{}", group_id=g.id)
    # Has AUTO
    auto = db.query(TargetTag).filter(TargetTag.target_id == tgt.id, TargetTag.origin == "AUTO").one_or_none()
    assert auto is not None
    # Has GROUP for A and B
    group_rows = (
        db.query(TargetTag)
        .filter(
            TargetTag.target_id == tgt.id,
            TargetTag.origin == "GROUP",
            TargetTag.source_group_id == g.id,
        )
        .all()
    )
    assert len(group_rows) == 2


def test_target_rename_updates_auto_tag_and_detects_collision(db):
    tsvc = TargetService(db)
    # Create two targets (auto-tags: x, y)
    t1 = tsvc.create(name="x", plugin_name="p", plugin_config_json="{}")
    t2 = tsvc.create(name="y", plugin_name="p", plugin_config_json="{}")

    # Rename t1 to collide with t2's auto tag
    with pytest.raises(IntegrityError):
        tsvc.rename(t1.id, "y")

    # Happy path rename
    updated = tsvc.rename(t1.id, "x-new")
    assert updated.name == "x-new"
    # Auto-tag should be normalized to x-new
    auto_tt = db.query(TargetTag).filter(TargetTag.target_id == t1.id, TargetTag.origin == "AUTO").one()
    auto_tag = db.get(Tag, auto_tt.tag_id)
    assert auto_tag is not None and auto_tag.slug == "x-new"


def test_target_move_and_remove_group_adjusts_only_group_origin(db):
    gsvc = GroupService(db)
    tsvc = TargetService(db)
    g1 = gsvc.create("G1")
    g2 = gsvc.create("G2")
    gsvc.add_tags(g1.id, ["A"])  # tag A
    gsvc.add_tags(g2.id, ["B"])  # tag B

    tgt = tsvc.create(name="svc", plugin_name="p", plugin_config_json="{}")
    # Attach a DIRECT tag
    direct_tag = db.query(Tag).filter(Tag.slug == "manual").one_or_none()
    if direct_tag is None:
        direct_tag = Tag(display_name="manual")
        db.add(direct_tag)
        db.commit()
        db.refresh(direct_tag)
    db.add(TargetTag(target_id=tgt.id, tag_id=direct_tag.id, origin="DIRECT"))
    db.commit()

    tsvc.move_to_group(tgt.id, g1.id)
    # Now move to g2
    tsvc.move_to_group(tgt.id, g2.id)
    # DIRECT must remain, GROUP from g1 removed, g2 added
    rows = db.query(TargetTag).filter(TargetTag.target_id == tgt.id).all()
    origins = sorted([(r.origin, r.source_group_id) for r in rows])
    assert ("DIRECT", None) in origins
    assert all(r.origin != "GROUP" or r.source_group_id == g2.id for r in rows if r.origin == "GROUP")

    # Remove from group clears g2 origin rows
    tsvc.remove_from_group(tgt.id)
    rows = db.query(TargetTag).filter(TargetTag.target_id == tgt.id, TargetTag.origin == "GROUP").all()
    assert rows == []


