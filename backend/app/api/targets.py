"""Targets API router (thin) delegating to services."""

from typing import List
import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.db import get_session
from app.models import Target as TargetModel
from app.schemas import (
    Target,
    TargetCreate,
    TargetUpdate,
    TargetTagWithOrigin,
    AddTagsToTarget,
    RemoveTagsFromTarget,
)
from app.services import TargetService, TagService
from app.models import Tag as TagModel, TargetTag as TargetTagModel


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/targets", tags=["targets"])


@router.get("/", response_model=List[Target])
def list_targets(db: Session = Depends(get_session)) -> List[TargetModel]:
    # Service will compute derived fields: has_schedule, schedule_names
    svc = TargetService(db)
    return svc.list()


@router.post("/", response_model=Target, status_code=status.HTTP_201_CREATED)
def create_target(payload: TargetCreate, db: Session = Depends(get_session)) -> TargetModel:
    svc = TargetService(db)
    try:
        return svc.create(
            name=payload.name,
            plugin_name=payload.plugin_name,
            plugin_config_json=payload.plugin_config_json,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))


@router.get("/{target_id}", response_model=Target)
def get_target(target_id: int, db: Session = Depends(get_session)) -> TargetModel:
    svc = TargetService(db)
    target = svc.get(target_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Target not found")
    return target


@router.put("/{target_id}", response_model=Target)
def update_target(target_id: int, payload: TargetUpdate, db: Session = Depends(get_session)) -> TargetModel:
    svc = TargetService(db)
    update_data = payload.model_dump(exclude_unset=True)
    try:
        return svc.update(target_id, **update_data)
    except KeyError as exc:
        if str(exc).strip("'") == "target_not_found":
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Target not found")
        raise


@router.delete("/{target_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_target(target_id: int, db: Session = Depends(get_session)) -> None:
    svc = TargetService(db)
    try:
        svc.delete(target_id)
    except KeyError as exc:
        if str(exc).strip("'") == "target_not_found":
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Target not found")
        raise
    return None



@router.post("/{target_id}/test")
async def test_target_connectivity(target_id: int, db: Session = Depends(get_session)) -> dict:
    svc = TargetService(db)
    try:
        return await svc.test_connectivity(target_id)
    except KeyError as exc:
        code = status.HTTP_404_NOT_FOUND if str(exc) in {"target_not_found", "plugin_not_found"} else status.HTTP_404_NOT_FOUND
        raise HTTPException(status_code=code, detail="Unknown plugin for target" if str(exc) == "plugin_not_found" else "Target not found")
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


@router.get("/{target_id}/schedules", response_model=List[str])
def list_target_schedules(target_id: int, db: Session = Depends(get_session)) -> List[str]:
    # Ensure target exists
    target = db.get(TargetModel, target_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Target not found")
    svc = TargetService(db)
    return svc.list_enabled_job_names_for_target(target_id)


@router.get("/{target_id}/tags", response_model=List[TargetTagWithOrigin])
def list_target_tags(target_id: int, db: Session = Depends(get_session)) -> List[dict]:
    target = db.get(TargetModel, target_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Target not found")
    rows = (
        db.query(TagModel, TargetTagModel)
        .join(TargetTagModel, TargetTagModel.tag_id == TagModel.id)
        .filter(TargetTagModel.target_id == target_id)
        .all()
    )
    result: list[dict] = []
    for tag, tt in rows:
        result.append({"tag": tag, "origin": tt.origin, "source_group_id": tt.source_group_id})
    return result


@router.post("/{target_id}/tags", response_model=List[TargetTagWithOrigin])
def add_direct_tags_to_target(target_id: int, payload: AddTagsToTarget, db: Session = Depends(get_session)) -> List[dict]:
    # Ensure target exists
    target = db.get(TargetModel, target_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Target not found")
    # Create-if-missing tags and attach with origin='DIRECT'
    svc_tag = TagService(db)
    for name in payload.tag_names:
        tag = svc_tag.create(name)
        exists = (
            db.query(TargetTagModel.id)
            .filter(
                TargetTagModel.target_id == target_id,
                TargetTagModel.tag_id == tag.id,
                TargetTagModel.origin == "DIRECT",
            )
            .first()
            is not None
        )
        if not exists:
            db.add(TargetTagModel(target_id=target_id, tag_id=tag.id, origin="DIRECT"))
    db.commit()
    # Return updated list
    rows = (
        db.query(TagModel, TargetTagModel)
        .join(TargetTagModel, TargetTagModel.tag_id == TagModel.id)
        .filter(TargetTagModel.target_id == target_id)
        .all()
    )
    return [{"tag": tag, "origin": tt.origin, "source_group_id": tt.source_group_id} for tag, tt in rows]


@router.delete("/{target_id}/tags", response_model=List[TargetTagWithOrigin])
def remove_direct_tags_from_target(target_id: int, payload: RemoveTagsFromTarget, db: Session = Depends(get_session)) -> List[dict]:
    target = db.get(TargetModel, target_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Target not found")
    # Normalize names
    names = [n.strip().lower() for n in payload.tag_names]
    tags = db.query(TagModel).filter(TagModel.slug.in_(names)).all()
    tag_ids = [t.id for t in tags]
    db.query(TargetTagModel).filter(
        TargetTagModel.target_id == target_id,
        TargetTagModel.tag_id.in_(tag_ids),
        TargetTagModel.origin == "DIRECT",
    ).delete(synchronize_session="fetch")
    db.commit()
    rows = (
        db.query(TagModel, TargetTagModel)
        .join(TargetTagModel, TargetTagModel.tag_id == TagModel.id)
        .filter(TargetTagModel.target_id == target_id)
        .all()
    )
    return [{"tag": tag, "origin": tt.origin, "source_group_id": tt.source_group_id} for tag, tt in rows]

