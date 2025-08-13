from __future__ import annotations

from app.models import Target


def test_target_model(db) -> None:
    target = Target(
        name="Test Database",
        slug="test-db-target",
        plugin_name="pihole",
        plugin_config_json='{"base_url":"http://pihole.local","token":"abc"}',
    )

    db.add(target)
    db.commit()
    db.refresh(target)

    assert target.id is not None
    assert target.name == "Test Database"
    assert target.slug == "test-db-target"
    assert target.created_at is not None
    assert target.updated_at is not None
    assert target.plugin_name == "pihole"
    assert target.plugin_config_json.startswith("{")
    assert "Test Database" in str(target)


def test_target_slug_generated_and_immutable(db) -> None:
    tg = Target(name="My Target", slug="")
    db.add(tg)
    db.commit()
    db.refresh(tg)
    assert tg.slug
    original_slug = tg.slug
    tg.slug = "changed"
    tg.name = "My Target Renamed"
    db.commit()
    db.refresh(tg)
    assert tg.slug == original_slug


