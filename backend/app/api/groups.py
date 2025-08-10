"""Groups API router."""

from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends, HTTPException, status, Response
from sqlalchemy.orm import Session

from app.core.db import get_session
from app.models import Group as GroupModel, Tag as TagModel, Target as TargetModel
from app.schemas import (
    Group as GroupSchema,
    GroupCreate,
    GroupUpdate,
    GroupWithTargets,
    GroupWithTags,
    AddTargetsToGroup,
    RemoveTargetsFromGroup,
    AddTagsToGroup,
    RemoveTagsFromGroup,
    Tag as TagSchema,
    Target as TargetSchema,
)
from app.services import GroupService


router = APIRouter(prefix="/groups", tags=["groups"])


@router.get("/", response_model=List[GroupSchema])
def list_groups(db: Session = Depends(get_session)) -> List[GroupModel]:
    svc = GroupService(db)
    return svc.list()


@router.post("/", response_model=GroupSchema, status_code=status.HTTP_201_CREATED)
def create_group(payload: GroupCreate, db: Session = Depends(get_session)) -> GroupModel:
    svc = GroupService(db)
    return svc.create(name=payload.name, description=payload.description)


@router.get("/{group_id}", response_model=GroupSchema)
def get_group(group_id: int, db: Session = Depends(get_session)) -> GroupModel:
    svc = GroupService(db)
    g = svc.get(group_id)
    if g is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Group not found")
    return g


@router.put("/{group_id}", response_model=GroupSchema)
def update_group(group_id: int, payload: GroupUpdate, db: Session = Depends(get_session)) -> GroupModel:
    svc = GroupService(db)
    g = svc.update(group_id, name=payload.name, description=payload.description)
    if g is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Group not found")
    return g


@router.delete("/{group_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
def delete_group(group_id: int, db: Session = Depends(get_session)) -> Response:
    svc = GroupService(db)
    try:
        ok = svc.delete(group_id)
    except Exception as exc:
        # Let IntegrityError bubble to global handler, but ensure message clarity otherwise
        msg = str(exc) or "Cannot delete non-empty group"
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=msg)
    if not ok:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Group not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{group_id}/targets", response_model=GroupWithTargets)
def get_group_targets(group_id: int, db: Session = Depends(get_session)) -> dict:
    svc = GroupService(db)
    g = svc.get(group_id)
    if g is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Group not found")
    # Eager load targets via relationship
    return {"id": g.id, "name": g.name, "description": g.description, "created_at": g.created_at, "updated_at": g.updated_at, "targets": list(g.targets)}


@router.post("/{group_id}/targets", response_model=GroupWithTargets)
def add_targets_to_group(group_id: int, payload: AddTargetsToGroup, db: Session = Depends(get_session)) -> dict:
    svc = GroupService(db)
    try:
        svc.add_targets(group_id, payload.target_ids)
    except KeyError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Group not found")
    g = svc.get(group_id)
    assert g is not None
    return {"id": g.id, "name": g.name, "description": g.description, "created_at": g.created_at, "updated_at": g.updated_at, "targets": list(g.targets)}


@router.delete("/{group_id}/targets", response_model=GroupWithTargets)
def remove_targets_from_group(group_id: int, payload: RemoveTargetsFromGroup, db: Session = Depends(get_session)) -> dict:
    svc = GroupService(db)
    svc.remove_targets(group_id, payload.target_ids)
    g = svc.get(group_id)
    if g is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Group not found")
    return {"id": g.id, "name": g.name, "description": g.description, "created_at": g.created_at, "updated_at": g.updated_at, "targets": list(g.targets)}


@router.get("/{group_id}/tags", response_model=GroupWithTags)
def get_group_tags(group_id: int, db: Session = Depends(get_session)) -> dict:
    svc = GroupService(db)
    g = svc.get(group_id)
    if g is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Group not found")
    tags = [gt.tag for gt in g.group_tags]
    return {"id": g.id, "name": g.name, "description": g.description, "created_at": g.created_at, "updated_at": g.updated_at, "tags": tags}


@router.post("/{group_id}/tags", response_model=GroupWithTags)
def add_tags_to_group(group_id: int, payload: AddTagsToGroup, db: Session = Depends(get_session)) -> dict:
    svc = GroupService(db)
    try:
        svc.add_tags(group_id, payload.tag_names)
    except KeyError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Group not found")
    g = svc.get(group_id)
    assert g is not None
    tags = [gt.tag for gt in g.group_tags]
    return {"id": g.id, "name": g.name, "description": g.description, "created_at": g.created_at, "updated_at": g.updated_at, "tags": tags}


@router.delete("/{group_id}/tags", response_model=GroupWithTags)
def remove_tags_from_group(group_id: int, payload: RemoveTagsFromGroup, db: Session = Depends(get_session)) -> dict:
    svc = GroupService(db)
    try:
        svc.remove_tags(group_id, payload.tag_names)
    except KeyError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Group not found")
    g = svc.get(group_id)
    assert g is not None
    tags = [gt.tag for gt in g.group_tags]
    return {"id": g.id, "name": g.name, "description": g.description, "created_at": g.created_at, "updated_at": g.updated_at, "tags": tags}


