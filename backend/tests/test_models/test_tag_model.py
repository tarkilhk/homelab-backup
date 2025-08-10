from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from app.models import Tag, ValidationError422


def test_tag_normalization_and_uniqueness(db) -> None:
    t1 = Tag(display_name="  Prod  ")
    db.add(t1)
    db.commit()
    db.refresh(t1)
    assert t1.slug == "prod"
    assert t1.display_name == "  Prod  "

    t2 = Tag(display_name="PROD")
    db.add(t2)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()

    t1.display_name = "Prod-DB"
    db.commit()
    db.refresh(t1)
    assert t1.slug == "prod-db"

    with pytest.raises(ValidationError422):
        t1.display_name = "   "
        db.flush()
    db.rollback()


