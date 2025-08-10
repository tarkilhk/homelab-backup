from __future__ import annotations

import pytest

from app.models import Group, Tag, Target, TargetTag


def test_target_tags_provenance_rules(db) -> None:
    g = Group(name="G1")
    tag = Tag(display_name="A")
    tgt = Target(name="T1", slug="t1")
    db.add_all([g, tag, tgt])
    db.commit()

    tt = TargetTag(target_id=tgt.id, tag_id=tag.id, origin="GROUP", source_group_id=None)
    db.add(tt)
    with pytest.raises(Exception):
        db.flush()
    db.rollback()

    tt = TargetTag(target_id=tgt.id, tag_id=tag.id, origin="AUTO", source_group_id=g.id)
    db.add(tt)
    with pytest.raises(Exception):
        db.flush()
    db.rollback()

    tt = TargetTag(target_id=tgt.id, tag_id=tag.id, origin="DIRECT", source_group_id=g.id)
    db.add(tt)
    with pytest.raises(Exception):
        db.flush()
    db.rollback()

    ok1 = TargetTag(target_id=tgt.id, tag_id=tag.id, origin="AUTO", is_auto_tag=True)
    ok2 = TargetTag(target_id=tgt.id, tag_id=tag.id, origin="DIRECT")
    ok3 = TargetTag(target_id=tgt.id, tag_id=tag.id, origin="GROUP", source_group_id=g.id)
    db.add_all([ok1, ok2, ok3])
    db.commit()


def test_target_tags_uniqueness_by_origin(db) -> None:
    g = Group(name="G2")
    tag = Tag(display_name="B")
    tgt = Target(name="T2", slug="t2")
    db.add_all([g, tag, tgt])
    db.commit()

    d1 = TargetTag(target_id=tgt.id, tag_id=tag.id, origin="DIRECT")
    db.add(d1)
    db.commit()

    d2 = TargetTag(target_id=tgt.id, tag_id=tag.id, origin="DIRECT")
    db.add(d2)
    with pytest.raises(Exception):
        db.commit()
    db.rollback()

    g1 = TargetTag(target_id=tgt.id, tag_id=tag.id, origin="GROUP", source_group_id=g.id)
    db.add(g1)
    db.commit()


