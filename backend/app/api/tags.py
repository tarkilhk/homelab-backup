"""Tags API router."""

from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends, HTTPException, status, Response
from sqlalchemy.orm import Session

from app.core.db import get_session
from app.models import Tag as TagModel, Target as TargetModel, TargetTag as TargetTagModel
from app.schemas import Tag as TagSchema
from app.schemas import TagTargetAttachment
from app.services import TagService


router = APIRouter(prefix="/tags", tags=["tags"])


@router.get("/", response_model=List[TagSchema])
def list_tags(db: Session = Depends(get_session)) -> List[TagModel]:
    svc = TagService(db)
    return svc.list()


@router.get("/{tag_id}", response_model=TagSchema)
def get_tag(tag_id: int, db: Session = Depends(get_session)) -> TagModel:
    svc = TagService(db)
    tag = svc.get(tag_id)
    if tag is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tag not found")
    return tag


@router.get("/{tag_id}/targets", response_model=List[TagTargetAttachment])
def list_targets_for_tag(tag_id: int, db: Session = Depends(get_session)) -> List[dict]:
    # Verify tag exists
    tag = db.get(TagModel, tag_id)
    if tag is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tag not found")
    rows = (
        db.query(TargetModel, TargetTagModel)
        .join(TargetTagModel, TargetTagModel.target_id == TargetModel.id)
        .filter(TargetTagModel.tag_id == tag_id)
        .all()
    )
    # Return per-attachment entries
    result: list[dict] = []
    for target, tt in rows:
        result.append(
            {
                "target": target,
                "origin": tt.origin,
                "source_group_id": tt.source_group_id,
            }
        )
    return result


@router.delete("/{tag_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
def delete_tag(tag_id: int, db: Session = Depends(get_session)) -> Response:
    from sqlalchemy.exc import IntegrityError
    svc = TagService(db)
    try:
        ok = svc.delete(tag_id)
    except IntegrityError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    if not ok:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tag not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


