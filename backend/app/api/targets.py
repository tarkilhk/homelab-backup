"""Targets API router."""

from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.db import get_session
from app.models import Target as TargetModel
from app.schemas import Target, TargetCreate, TargetUpdate


router = APIRouter(prefix="/targets", tags=["targets"])


@router.get("/", response_model=List[Target])
def list_targets(db: Session = Depends(get_session)) -> List[TargetModel]:
    """List all targets."""
    return db.query(TargetModel).all()


@router.post("/", response_model=Target, status_code=status.HTTP_201_CREATED)
def create_target(payload: TargetCreate, db: Session = Depends(get_session)) -> TargetModel:
    """Create a new target."""
    target = TargetModel(
        name=payload.name,
        slug=payload.slug,
        type=payload.type,
        config_json=payload.config_json,
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


