from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from .targets import Target


class TagBase(BaseModel):
    name: str = Field(
        ..., description="Tag name (will be normalized for uniqueness)", max_length=255
    )


class TagCreate(TagBase):
    pass


class TagUpdate(BaseModel):
    name: Optional[str] = Field(
        None, description="Tag name (will be normalized for uniqueness)", max_length=255
    )


class Tag(BaseModel):
    id: int
    slug: str
    display_name: str
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class TagTargetAttachment(BaseModel):
    target: "Target"
    origin: Literal["AUTO", "DIRECT", "GROUP"]
    source_group_id: Optional[int] = None
