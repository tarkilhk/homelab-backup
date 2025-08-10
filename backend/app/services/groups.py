from __future__ import annotations

from typing import Iterable, List, Optional, Sequence

from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from app.models import Group, Tag, GroupTag, Target, TargetTag
from app.models import slugify


class GroupService:
    """Group CRUD + tag and target membership management.

    - add/remove tags: create-missing tags; propagate/de-propagate to member targets idempotently.
    - add/remove targets: moves targets across groups atomically and adjusts GROUP-origin tags.
    """

    def __init__(self, db: Session) -> None:
        self.db = db

    # CRUD
    def create(self, name: str, description: Optional[str] = None) -> Group:
        g = Group(name=name, description=description)
        self.db.add(g)
        self.db.commit()
        self.db.refresh(g)
        return g

    def get(self, group_id: int) -> Optional[Group]:
        return self.db.get(Group, group_id)

    def list(self) -> List[Group]:
        return list(self.db.query(Group).order_by(Group.name.asc()).all())

    def update(self, group_id: int, *, name: Optional[str] = None, description: Optional[str] = None) -> Optional[Group]:
        g = self.db.get(Group, group_id)
        if g is None:
            return None
        if name is not None:
            g.name = name
        if description is not None:
            g.description = description
        self.db.commit()
        self.db.refresh(g)
        return g

    def delete(self, group_id: int) -> bool:
        g = self.db.get(Group, group_id)
        if g is None:
            return False
        # Block delete if group has targets
        has_targets = self.db.query(Target.id).filter(Target.group_id == group_id).first() is not None
        if has_targets:
            raise IntegrityError("Cannot delete non-empty group", params=None, orig=None)  # type: ignore[arg-type]
        self.db.delete(g)
        self.db.commit()
        return True

    # Tag management
    def add_tags(self, group_id: int, tag_names: Sequence[str]) -> List[Tag]:
        g = self.db.get(Group, group_id)
        if g is None:
            raise KeyError("group_not_found")

        # Ensure tags exist (create if missing)
        existing_by_slug: dict[str, Tag] = {
            t.slug: t for t in self.db.query(Tag).filter(Tag.slug.in_([slugify(n) for n in tag_names])).all()
        }
        to_attach: List[Tag] = []
        for name in tag_names:
            slug = slugify(name)
            tag = existing_by_slug.get(slug)
            if tag is None:
                tag = Tag(display_name=name)
                self.db.add(tag)
                self.db.flush()
                existing_by_slug[slug] = tag
            to_attach.append(tag)

        # Link group_tags idempotently
        existing_pairs = {
            (gt.group_id, gt.tag_id) for gt in self.db.query(GroupTag).filter(GroupTag.group_id == group_id).all()
        }
        for tag in to_attach:
            if (group_id, tag.id) not in existing_pairs:
                self.db.add(GroupTag(group_id=group_id, tag_id=tag.id))

        self.db.flush()

        # Propagate to member targets: add TargetTag with origin='GROUP'
        member_targets: list[Target] = self.db.query(Target).filter(Target.group_id == group_id).all()
        for tgt in member_targets:
            # Fetch existing GROUP-origin attachments for this group to avoid duplicates
            existing_tt = {
                (tt.tag_id, tt.origin, tt.source_group_id)
                for tt in self.db.query(TargetTag)
                .filter(
                    TargetTag.target_id == tgt.id,
                    TargetTag.origin == "GROUP",
                    TargetTag.source_group_id == group_id,
                )
                .all()
            }
            for tag in to_attach:
                key = (tag.id, "GROUP", group_id)
                if key not in existing_tt:
                    self.db.add(
                        TargetTag(
                            target_id=tgt.id,
                            tag_id=tag.id,
                            origin="GROUP",
                            source_group_id=group_id,
                        )
                    )

        self.db.commit()
        return to_attach

    def remove_tags(self, group_id: int, tag_names: Sequence[str]) -> None:
        g = self.db.get(Group, group_id)
        if g is None:
            raise KeyError("group_not_found")
        slugs = [slugify(n) for n in tag_names]
        tags = self.db.query(Tag).filter(Tag.slug.in_(slugs)).all()
        tag_ids = [t.id for t in tags]

        # Remove group_tags
        self.db.query(GroupTag).filter(
            GroupTag.group_id == group_id, GroupTag.tag_id.in_(tag_ids)
        ).delete(synchronize_session="fetch")

        # De-propagate GROUP-origin attachments contributed by this group
        self.db.query(TargetTag).filter(
            TargetTag.origin == "GROUP",
            TargetTag.source_group_id == group_id,
            TargetTag.tag_id.in_(tag_ids),
        ).delete(synchronize_session="fetch")

        self.db.commit()

    # Target membership
    def add_targets(self, group_id: int, target_ids: Sequence[int]) -> List[Target]:
        g = self.db.get(Group, group_id)
        if g is None:
            raise KeyError("group_not_found")

        targets: list[Target] = self.db.query(Target).filter(Target.id.in_(list(target_ids))).all()
        # Get group tags to propagate
        group_tags = self.db.query(GroupTag).filter(GroupTag.group_id == group_id).all()
        tag_ids = [gt.tag_id for gt in group_tags]

        for tgt in targets:
            prev_group_id = tgt.group_id
            tgt.group_id = group_id
            self.db.flush()
            # Remove GROUP-origin rows from previous group
            if prev_group_id is not None and prev_group_id != group_id:
                self.db.query(TargetTag).filter(
                    TargetTag.target_id == tgt.id,
                    TargetTag.origin == "GROUP",
                    TargetTag.source_group_id == prev_group_id,
                ).delete(synchronize_session="fetch")
            # Add GROUP-origin rows for new group tags idempotently
            existing_tt = {
                (tt.tag_id, tt.origin, tt.source_group_id)
                for tt in self.db.query(TargetTag)
                .filter(
                    TargetTag.target_id == tgt.id,
                    TargetTag.origin == "GROUP",
                    TargetTag.source_group_id == group_id,
                )
                .all()
            }
            for tag_id in tag_ids:
                key = (tag_id, "GROUP", group_id)
                if key not in existing_tt:
                    self.db.add(
                        TargetTag(
                            target_id=tgt.id,
                            tag_id=tag_id,
                            origin="GROUP",
                            source_group_id=group_id,
                        )
                    )

        self.db.commit()
        return targets

    def remove_targets(self, group_id: int, target_ids: Sequence[int]) -> None:
        # Move targets out of the group; remove only GROUP-origin rows for this group
        targets: list[Target] = self.db.query(Target).filter(Target.id.in_(list(target_ids))).all()
        for tgt in targets:
            if tgt.group_id == group_id:
                tgt.group_id = None
                self.db.flush()
                self.db.query(TargetTag).filter(
                    TargetTag.target_id == tgt.id,
                    TargetTag.origin == "GROUP",
                    TargetTag.source_group_id == group_id,
                ).delete(synchronize_session="fetch")
        self.db.commit()


