from __future__ import annotations

from app.models import TargetTag, Target, Tag, GroupTag, slugify
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

    # Both targets should have GROUP-origin rows for the group's auto-tag plus each added tag
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
        assert len(rows) == 3

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
        # Group auto-tag + "db" remain
        assert len(rows) == 2


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



def test_delete_group_detaches_targets_and_cleans_group_origin_tags(db):
    gsvc = GroupService(db)
    tsvc = TargetService(db)
    # Setup: group with two targets and two tags propagated (plus the group's auto-tag)
    g = gsvc.create("G")
    t1 = tsvc.create(name="T1", plugin_name="p", plugin_config_json="{}")
    t2 = tsvc.create(name="T2", plugin_name="p", plugin_config_json="{}")
    gsvc.add_targets(g.id, [t1.id, t2.id])
    gsvc.add_tags(g.id, ["prod", "db"])  # two tags + group's auto-tag -> 6 GROUP-origin rows

    assert (
        db.query(TargetTag)
        .filter(TargetTag.origin == "GROUP", TargetTag.source_group_id == g.id)
        .count()
        == 6
    )

    # Delete the non-empty group
    ok = gsvc.delete(g.id)
    assert ok is True

    # Group is gone; targets are detached
    assert gsvc.get(g.id) is None
    t1_db = db.get(Target, t1.id)
    t2_db = db.get(Target, t2.id)
    assert t1_db is not None and t1_db.group_id is None
    assert t2_db is not None and t2_db.group_id is None

    # All GROUP-origin rows referencing the deleted group are removed
    assert (
        db.query(TargetTag)
        .filter(TargetTag.origin == "GROUP", TargetTag.source_group_id == g.id)
        .count()
        == 0
    )


def test_group_create_creates_auto_tag_and_links(db):
    gsvc = GroupService(db)
    g = gsvc.create("Ops Team")

    expected_slug = slugify("Ops Team")
    tag = db.query(Tag).filter(Tag.slug == expected_slug).one_or_none()
    assert tag is not None
    assert tag.display_name == "Ops Team"

    link = (
        db.query(GroupTag)
        .filter(GroupTag.group_id == g.id, GroupTag.tag_id == tag.id)
        .one_or_none()
    )
    assert link is not None


def test_group_create_reuses_existing_tag_by_slug(db):
    # Pre-create tag that matches the group's slugified name
    t = Tag(display_name="Eng Team")
    db.add(t)
    db.commit()
    db.refresh(t)

    gsvc = GroupService(db)
    g = gsvc.create("Eng Team")

    # Tag reused; link exists
    reused = db.query(Tag).filter(Tag.slug == slugify("Eng Team")).one_or_none()
    assert reused is not None and reused.id == t.id
    link = (
        db.query(GroupTag)
        .filter(GroupTag.group_id == g.id, GroupTag.tag_id == t.id)
        .one_or_none()
    )
    assert link is not None

