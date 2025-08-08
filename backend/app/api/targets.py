"""Targets API router."""

from typing import List
import json

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.db import get_session
from app.models import Target as TargetModel
from app.core.plugins.loader import get_plugin_schema_path
from app.schemas import Target, TargetCreate, TargetUpdate


router = APIRouter(prefix="/targets", tags=["targets"])


@router.get("/", response_model=List[Target])
def list_targets(db: Session = Depends(get_session)) -> List[TargetModel]:
    """List all targets."""
    return db.query(TargetModel).all()


@router.post("/", response_model=Target, status_code=status.HTTP_201_CREATED)
def create_target(payload: TargetCreate, db: Session = Depends(get_session)) -> TargetModel:
    """Create a new target."""
    # Validate plugin config against plugin's JSON schema if provided
    if payload.plugin_name and payload.plugin_config_json:
        schema_path = get_plugin_schema_path(payload.plugin_name)
        if schema_path:
            try:
                import jsonschema  # type: ignore
            except Exception as exc:  # pragma: no cover - import error handling
                raise HTTPException(status_code=500, detail=f"jsonschema missing: {exc}")
            with open(schema_path, "r", encoding="utf-8") as f:
                schema = json.load(f)
            try:
                jsonschema.validate(instance=json.loads(payload.plugin_config_json), schema=schema)  # type: ignore
            except Exception as exc:
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"Invalid plugin_config_json: {exc}")
    # Generate a slug if not provided
    def _slugify(value: str) -> str:
        return (
            value.strip().lower()
            .replace(" ", "-")
            .replace("_", "-")
        )

    target = TargetModel(
        name=payload.name,
        slug=payload.slug or _slugify(payload.name),
        plugin_name=payload.plugin_name,
        plugin_config_json=payload.plugin_config_json,
    )
    db.add(target)
    db.commit()
    db.refresh(target)
    return target


@router.get("/{target_id}", response_model=Target)
def get_target(target_id: int, db: Session = Depends(get_session)) -> TargetModel:
    """Get target by ID."""
    target = db.get(TargetModel, target_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Target not found")
    return target


@router.put("/{target_id}", response_model=Target)
def update_target(target_id: int, payload: TargetUpdate, db: Session = Depends(get_session)) -> TargetModel:
    """Update an existing target."""
    target = db.get(TargetModel, target_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Target not found")

    update_data = payload.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(target, key, value)

    db.add(target)
    db.commit()
    db.refresh(target)
    return target


@router.delete("/{target_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_target(target_id: int, db: Session = Depends(get_session)) -> None:
    """Delete a target by ID."""
    target = db.get(TargetModel, target_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Target not found")
    db.delete(target)
    db.commit()
    return None


