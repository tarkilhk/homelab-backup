from __future__ import annotations

from typing import List, Optional, Sequence
import json
import logging

from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from app.models import Group, Tag, Target, TargetTag, slugify
from app.core.plugins import loader as plugins_loader


logger = logging.getLogger(__name__)

class TargetService:
    """Target operations per spec.

    - create: unique name; slug via model; auto-tag create/reuse; link TargetTag(origin='AUTO', is_auto_tag=True);
      optional group_id propagation adds GROUP-origin rows for group's tags.
    - rename: update target.name; sync auto-tag display_name (which updates slug); reject if normalized name collides with another tag.
    - move/remove group: adjust only GROUP-origin rows.
    """

    def __init__(self, db: Session) -> None:
        self.db = db

    def list(self) -> List[Target]:
        return list(self.db.query(Target).all())

    def get(self, target_id: int) -> Optional[Target]:
        return self.db.get(Target, target_id)

    def create(
        self,
        *,
        name: str,
        plugin_name: Optional[str],
        plugin_config_json: Optional[str],
        group_id: Optional[int] = None,
    ) -> Target:
        # Validate plugin config against plugin schema if exists
        if plugin_name and plugin_config_json:
            schema_path = plugins_loader.get_plugin_schema_path(plugin_name)
            logger.debug("plugin schema path resolved | plugin=%s path=%s", plugin_name, schema_path)
            if schema_path:
                import json as _json
                with open(schema_path, "r", encoding="utf-8") as f:
                    schema = _json.load(f)
                try:  # pragma: no cover - optional dependency
                    import jsonschema  # type: ignore
                    logger.debug("validating plugin_config_json against schema via jsonschema")
                    jsonschema.validate(instance=json.loads(plugin_config_json), schema=schema)  # type: ignore
                    logger.debug("plugin_config_json validation OK")
                except ModuleNotFoundError:
                    logger.warning("jsonschema not installed; performing minimal required-field validation only")
                    try:
                        instance = json.loads(plugin_config_json)
                    except Exception as exc:
                        raise ValueError(f"Invalid plugin_config_json: {exc}")
                    required = schema.get("required", []) if isinstance(schema, dict) else []
                    missing = [k for k in required if k not in instance]
                    if missing:
                        raise ValueError(f"Invalid plugin_config_json: missing required {missing}")
                except Exception as exc:
                    logger.warning("plugin_config_json validation failed: %s", exc)
                    raise ValueError(f"Invalid plugin_config_json: {exc}")

        # Ensure unique name via DB constraint; create Target
        # Slug is generated from name here (service is source of truth)
        # slugify is imported from app.models

        target = Target(
            name=name,
            slug=slugify(name),
            plugin_name=plugin_name,
            plugin_config_json=plugin_config_json,
            group_id=group_id,
        )
        self.db.add(target)
        self.db.flush()  # assigns target.id and slug

        # Auto-tag: create or reuse Tag with slugified name of target
        slug_str = slugify(name)
        tag = self.db.query(Tag).filter(Tag.slug == slug_str).one_or_none()
        if tag is None:
            tag = Tag(display_name=name)
            self.db.add(tag)
            self.db.flush()

        # Link TargetTag AUTO idempotently
        exists = (
            self.db.query(TargetTag.id)
            .filter(
                TargetTag.target_id == target.id,
                TargetTag.tag_id == tag.id,
                TargetTag.origin == "AUTO",
            )
            .first()
            is not None
        )
        if not exists:
            self.db.add(
                TargetTag(
                    target_id=target.id,
                    tag_id=tag.id,
                    origin="AUTO",
                    is_auto_tag=True,
                )
            )

        # Optional group propagation
        if group_id is not None:
            group = self.db.get(Group, group_id)
            if group is None:
                raise KeyError("group_not_found")
            group_tag_ids = [gt.tag_id for gt in group.group_tags]
            for tag_id in group_tag_ids:
                exists = (
                    self.db.query(TargetTag.id)
                    .filter(
                        TargetTag.target_id == target.id,
                        TargetTag.tag_id == tag_id,
                        TargetTag.origin == "GROUP",
                        TargetTag.source_group_id == group_id,
                    )
                    .first()
                    is not None
                )
                if not exists:
                    self.db.add(
                        TargetTag(
                            target_id=target.id,
                            tag_id=tag_id,
                            origin="GROUP",
                            source_group_id=group_id,
                        )
                    )

        self.db.commit()
        self.db.refresh(target)
        return target

    def update(self, target_id: int, **fields: object) -> Target:
        # If renaming, delegate to rename() for proper tag sync and collision checks
        new_name = fields.pop("name", None)
        target = self.db.get(Target, target_id)
        if target is None:
            raise KeyError("target_not_found")
        if new_name is not None and isinstance(new_name, str) and new_name != target.name:
            target = self.rename(target_id, new_name)
        # Apply remaining fields (e.g., plugin fields, slug will be enforced immutable by model hook)
        for k, v in fields.items():
            setattr(target, k, v)
        self.db.add(target)
        self.db.commit()
        self.db.refresh(target)
        return target

    def delete(self, target_id: int) -> None:
        target = self.db.get(Target, target_id)
        if target is None:
            raise KeyError("target_not_found")
        self.db.delete(target)
        self.db.commit()

    def rename(self, target_id: int, new_name: str) -> Target:
        target = self.db.get(Target, target_id)
        if target is None:
            raise KeyError("target_not_found")

        # Collision check: if another tag exists with this slug and it's not the target's own auto-tag
        slug_str = slugify(new_name)
        existing_tag = self.db.query(Tag).filter(Tag.slug == slug_str).one_or_none()

        # Find target's current auto-tag (origin AUTO)
        auto_tt = (
            self.db.query(TargetTag)
            .filter(
                TargetTag.target_id == target_id,
                TargetTag.origin == "AUTO",
            )
            .one_or_none()
        )
        if existing_tag is not None and (auto_tt is None or existing_tag.id != auto_tt.tag_id):
            raise IntegrityError("auto_tag_name_collision", params=None, orig=None)  # type: ignore[arg-type]

        # Update target name
        target.name = new_name
        self.db.flush()

        # Ensure an auto-tag exists; update its display_name to new_name
        if auto_tt is None:
            # Should not happen normally; create one
            tag = existing_tag or Tag(display_name=new_name)
            if tag.id is None:
                self.db.add(tag)
                self.db.flush()
            self.db.add(
                TargetTag(target_id=target.id, tag_id=tag.id, origin="AUTO", is_auto_tag=True)
            )
        else:
            tag = self.db.get(Tag, auto_tt.tag_id)
            assert tag is not None
            tag.display_name = new_name  # validator updates slug

        self.db.commit()
        self.db.refresh(target)
        return target

    async def test_connectivity(self, target_id: int) -> dict:
        target = self.db.get(Target, target_id)
        if target is None:
            raise KeyError("target_not_found")
        if not target.plugin_name:
            raise ValueError("Target has no plugin configured")
        try:
            plugin = plugins_loader.get_plugin(target.plugin_name)
        except KeyError:
            raise KeyError("plugin_not_found")
        try:
            cfg = json.loads(target.plugin_config_json or "{}")
        except Exception as exc:
            raise ValueError(f"Invalid plugin_config_json: {exc}")
        try:
            ok = await plugin.test(cfg)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": bool(ok)}

    def move_to_group(self, target_id: int, group_id: int) -> Target:
        target = self.db.get(Target, target_id)
        if target is None:
            raise KeyError("target_not_found")
        group = self.db.get(Group, group_id)
        if group is None:
            raise KeyError("group_not_found")

        prev_group_id = target.group_id
        target.group_id = group_id
        self.db.flush()

        # Remove previous GROUP-origin rows
        if prev_group_id is not None and prev_group_id != group_id:
            self.db.query(TargetTag).filter(
                TargetTag.target_id == target.id,
                TargetTag.origin == "GROUP",
                TargetTag.source_group_id == prev_group_id,
            ).delete(synchronize_session=False)

        # Add new group's tags
        tag_ids = [gt.tag_id for gt in group.group_tags]
        existing_tt = {
            (tt.tag_id, tt.origin, tt.source_group_id)
            for tt in self.db.query(TargetTag)
            .filter(
                TargetTag.target_id == target.id,
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
                        target_id=target.id,
                        tag_id=tag_id,
                        origin="GROUP",
                        source_group_id=group_id,
                    )
                )

        self.db.commit()
        self.db.refresh(target)
        return target

    def remove_from_group(self, target_id: int) -> Target:
        target = self.db.get(Target, target_id)
        if target is None:
            raise KeyError("target_not_found")
        if target.group_id is None:
            return target
        gid = target.group_id
        target.group_id = None
        self.db.flush()
        self.db.query(TargetTag).filter(
            TargetTag.target_id == target.id,
            TargetTag.origin == "GROUP",
            TargetTag.source_group_id == gid,
        ).delete(synchronize_session=False)
        self.db.commit()
        self.db.refresh(target)
        return target


