from __future__ import annotations

from typing import Optional, List
from datetime import datetime

from pydantic import BaseModel, Field, ConfigDict


class GroupBase(BaseModel):
    name: str = Field(..., description="Group name", max_length=255)
    description: Optional[str] = Field(None, description="Group description")


class GroupCreate(GroupBase):
    pass


class GroupUpdate(BaseModel):
    name: Optional[str] = Field(None, description="Group name", max_length=255)
    description: Optional[str] = Field(None, description="Group description")


class Group(GroupBase):
    id: int
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class GroupWithTargets(Group):
    targets: List["Target"] = Field(default_factory=list)


class GroupWithTags(Group):
    tags: List["Tag"] = Field(default_factory=list)


class AddTargetsToGroup(BaseModel):
    target_ids: List[int] = Field(..., description="List of target IDs to add to group")


class RemoveTargetsFromGroup(BaseModel):
    target_ids: List[int] = Field(..., description="List of target IDs to remove from group")


class AddTagsToGroup(BaseModel):
    tag_names: List[str] = Field(..., description="List of tag names to add to group")


class RemoveTagsFromGroup(BaseModel):
    tag_names: List[str] = Field(..., description="List of tag names to remove from group")


