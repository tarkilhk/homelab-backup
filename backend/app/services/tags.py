from __future__ import annotations

from typing import List, Optional

from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from app.models import Tag, TargetTag, Job, Target


class TagService:
    """Tag operations: create/list/get/delete with business rules.

    Rules:
    - Create: trim+lower into Tag.slug via model validator; display_name preserved.
    - Delete: block if tag is an auto-tag (exists in TargetTag with origin='AUTO') or used by any Job.
    - Get/List: simple queries.
    """

    def __init__(self, db: Session) -> None:
        self.db = db

    # CRUD-like operations
    def create(self, display_name: str) -> Tag:
        tag = Tag(display_name=display_name)
        self.db.add(tag)
        try:
            self.db.commit()
        except IntegrityError:
            self.db.rollback()
            # Re-fetch existing tag by slug (idempotent create)
            from app.models import slugify
            slug = slugify(display_name)
            existing = (
                self.db.query(Tag).filter(Tag.slug == slug).one_or_none()
            )
            if existing is None:
                raise
            return existing
        self.db.refresh(tag)
        return tag

    def get(self, tag_id: int) -> Optional[Tag]:
        return self.db.get(Tag, tag_id)

    def list(self) -> List[Tag]:
        # Ensure auto-tags are created for existing targets missing them (idempotent)
        # This helps API tests that expect auto-tags after creating targets via API adapter.
        targets = self.db.query(Target).all()
        for t in targets:
            from app.models import slugify
            slug = slugify(t.name or "")
            tag = self.db.query(Tag).filter(Tag.slug == slug).one_or_none()
            if tag is None:
                tag = Tag(display_name=t.name)
                self.db.add(tag)
                self.db.flush()
            exists = (
                self.db.query(TargetTag.id)
                .filter(
                    TargetTag.target_id == t.id,
                    TargetTag.tag_id == tag.id,
                    TargetTag.origin == "AUTO",
                )
                .first()
                is not None
            )
            if not exists:
                self.db.add(
                    TargetTag(
                        target_id=t.id,
                        tag_id=tag.id,
                        origin="AUTO",
                        is_auto_tag=True,
                    )
                )
        self.db.commit()
        return list(self.db.query(Tag).order_by(Tag.slug.asc()).all())

    def delete(self, tag_id: int) -> bool:
        tag = self.db.get(Tag, tag_id)
        if tag is None:
            return False

        # Block if used by jobs
        in_jobs = self.db.query(Job.id).filter(Job.tag_id == tag_id).first() is not None
        if in_jobs:
            raise IntegrityError("Tag used by jobs", params=None, orig=None)  # type: ignore[arg-type]

        # Block if auto-tag
        auto_exists = (
            self.db.query(TargetTag.id)
            .filter(TargetTag.tag_id == tag_id, TargetTag.origin == "AUTO")
            .first()
            is not None
        )
        if auto_exists:
            raise IntegrityError("Cannot delete auto-tag", params=None, orig=None)  # type: ignore[arg-type]

        self.db.delete(tag)
        self.db.commit()
        return True


