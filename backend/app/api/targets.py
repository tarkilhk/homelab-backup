"""Targets API router.

Adds detailed logging around the create/update flow so we can trace
exactly what happens when saving a `Target` record.
"""

from typing import List
import json

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.db import get_session
from app.models import Target as TargetModel
from app.core.plugins.loader import get_plugin_schema_path
from app.schemas import Target, TargetCreate, TargetUpdate
import logging


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/targets", tags=["targets"])


@router.get("/", response_model=List[Target])
def list_targets(db: Session = Depends(get_session)) -> List[TargetModel]:
    """List all targets."""
    return db.query(TargetModel).all()


@router.post("/", response_model=Target, status_code=status.HTTP_201_CREATED)
def create_target(payload: TargetCreate, db: Session = Depends(get_session)) -> TargetModel:
    """Create a new target with verbose logging for diagnostics."""
    logger.info(
        "create_target called | name=%s plugin=%s payload_slug=%s plugin_cfg_len=%s",
        payload.name,
        payload.plugin_name,
        payload.slug,
        len(payload.plugin_config_json or ""),
    )

    # Validate plugin config against plugin's JSON schema if provided
    if payload.plugin_name and payload.plugin_config_json:
        schema_path = get_plugin_schema_path(payload.plugin_name)
        logger.debug("plugin schema path resolved | plugin=%s path=%s", payload.plugin_name, schema_path)
        if schema_path:
            with open(schema_path, "r", encoding="utf-8") as f:
                schema = json.load(f)
            # Prefer jsonschema if available; otherwise, do a minimal fallback validation
            try:  # pragma: no cover - simple import
                import jsonschema  # type: ignore
                logger.debug("validating plugin_config_json against schema via jsonschema")
                jsonschema.validate(instance=json.loads(payload.plugin_config_json), schema=schema)  # type: ignore
                logger.debug("plugin_config_json validation OK")
            except ModuleNotFoundError:
                logger.warning("jsonschema not installed; performing minimal required-field validation only")
                try:
                    instance = json.loads(payload.plugin_config_json)
                except Exception as exc:  # malformed JSON
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail=f"Invalid plugin_config_json: {exc}",
                    )
                required = schema.get("required", []) if isinstance(schema, dict) else []
                missing = [k for k in required if k not in instance]
                if missing:
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail=f"Invalid plugin_config_json: missing required {missing}",
                    )
            except Exception as exc:
                logger.warning("plugin_config_json validation failed: %s", exc)
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Invalid plugin_config_json: {exc}",
                )

    # Generate a slug if not provided
    def _slugify(value: str) -> str:
        return (
            value.strip().lower().replace(" ", "-").replace("_", "-")
        )

    resolved_slug = payload.slug or _slugify(payload.name)
    logger.debug("resolved slug=%s", resolved_slug)

    target = TargetModel(
        name=payload.name,
        slug=resolved_slug,
        plugin_name=payload.plugin_name,
        plugin_config_json=payload.plugin_config_json,
    )
    try:
        logger.debug("adding Target to session")
        db.add(target)
        logger.debug("flushing session (pre-commit)")
        db.flush()  # Ensures INSERT is emitted before commit for logging/ids
        logger.debug("committing session")
        db.commit()
        logger.debug("refreshing Target from DB")
        db.refresh(target)
        logger.info("Target saved | id=%s name=%s slug=%s", target.id, target.name, target.slug)
        return target
    except Exception as exc:  # pragma: no cover - safety logging
        logger.exception("Failed to create Target: %s", exc)
        db.rollback()
        raise


@router.get("/{target_id}", response_model=Target)
def get_target(target_id: int, db: Session = Depends(get_session)) -> TargetModel:
    """Get target by ID."""
    target = db.get(TargetModel, target_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Target not found")
    return target


@router.put("/{target_id}", response_model=Target)
def update_target(target_id: int, payload: TargetUpdate, db: Session = Depends(get_session)) -> TargetModel:
    """Update an existing target with diagnostic logging."""
    target = db.get(TargetModel, target_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Target not found")

    update_data = payload.model_dump(exclude_unset=True)
    logger.info("update_target called | id=%s fields=%s", target_id, list(update_data.keys()))
    for key, value in update_data.items():
        setattr(target, key, value)

    try:
        logger.debug("committing Target update | id=%s", target_id)
        db.add(target)
        db.commit()
        db.refresh(target)
        logger.info("Target updated | id=%s", target_id)
        return target
    except Exception as exc:  # pragma: no cover - safety logging
        logger.exception("Failed to update Target id=%s: %s", target_id, exc)
        db.rollback()
        raise


@router.delete("/{target_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_target(target_id: int, db: Session = Depends(get_session)) -> None:
    """Delete a target by ID with diagnostic logging."""
    target = db.get(TargetModel, target_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Target not found")
    try:
        logger.info("deleting Target | id=%s slug=%s", target.id, target.slug)
        db.delete(target)
        db.commit()
        logger.info("Target deleted | id=%s", target_id)
    except Exception as exc:  # pragma: no cover - safety logging
        logger.exception("Failed to delete Target id=%s: %s", target_id, exc)
        db.rollback()
    return None


