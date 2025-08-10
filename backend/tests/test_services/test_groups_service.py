from __future__ import annotations

from app.models import TargetTag
from app.services import GroupService, TargetService


def test_group_add_remove_tags_propagates(db):
    gsvc = GroupService(db)
    tsvc = TargetService(db)
    # Create group and targets
    g = gsvc.create("G1")
    a = tsvc.create(name="A", plugin_name="p", plugin_config_json="{}")
    b = tsvc.create(name="B", plugin_name="p", plugin_config_json="{}")
    # Add targets to group
    gsvc.add_targets(g.id, [a.id, b.id])

    # Add tags to group (create-missing)
    tags = gsvc.add_tags(g.id, ["  Prod  ", "db"])
    norms = sorted([t.slug for t in tags])
    assert norms == ["db", "prod"]

    # Both targets should have GROUP-origin rows for each tag
    for tgt in (a, b):
        rows = (
            db.query(TargetTag)
            .filter(
                TargetTag.target_id == tgt.id,
                TargetTag.origin == "GROUP",
                TargetTag.source_group_id == g.id,
            )
            .all()
        )
        assert len(rows) == 2

    # Removing one tag de-propagates
    gsvc.remove_tags(g.id, ["prod"])  # by normalized name
    for tgt in (a, b):
        rows = (
            db.query(TargetTag)
            .filter(
                TargetTag.target_id == tgt.id,
                TargetTag.origin == "GROUP",
                TargetTag.source_group_id == g.id,
            )
            .all()
        )
        # Only "db" remains
        assert len(rows) == 1


def test_group_add_remove_targets_moves_and_adjusts_group_origin(db):
    gsvc = GroupService(db)
    tsvc = TargetService(db)
    # Groups and tags
    g1 = gsvc.create("G1")
    g2 = gsvc.create("G2")
    gsvc.add_tags(g1.id, ["A"])  # tag A on G1
    gsvc.add_tags(g2.id, ["B"])  # tag B on G2

    # Target joins G1 then move to G2
    tgt = tsvc.create(name="T", plugin_name="p", plugin_config_json="{}")
    gsvc.add_targets(g1.id, [tgt.id])
    # Should have GROUP:A
    rows = db.query(TargetTag).filter(TargetTag.target_id == tgt.id, TargetTag.origin == "GROUP").all()
    assert {(r.tag_id, r.source_group_id) for r in rows}

    # Move to G2 via add_targets
    gsvc.add_targets(g2.id, [tgt.id])
    rows = db.query(TargetTag).filter(TargetTag.target_id == tgt.id, TargetTag.origin == "GROUP").all()
    # Only rows from G2 remain
    assert all(r.source_group_id == g2.id for r in rows)

    # Remove from group
    gsvc.remove_targets(g2.id, [tgt.id])
    rows = db.query(TargetTag).filter(TargetTag.target_id == tgt.id, TargetTag.origin == "GROUP").all()
    assert rows == []


