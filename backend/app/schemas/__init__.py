"""Pydantic schemas package (split from monolithic schemas.py).

Public re-exports keep existing import paths stable.
"""

from .targets import (
    TargetBase,
    TargetCreate,
    TargetUpdate,
    Target,
    AddTagsToTarget,
    RemoveTagsFromTarget,
    TargetTagWithOrigin,
)  # noqa: F401
from .groups import (
    GroupBase,
    GroupCreate,
    GroupUpdate,
    Group,
    GroupWithTargets,
    GroupWithTags,
    AddTargetsToGroup,
    RemoveTargetsFromGroup,
    AddTagsToGroup,
    RemoveTagsFromGroup,
)  # noqa: F401
from .tags import (
    TagBase,
    TagCreate,
    TagUpdate,
    Tag,
    TagTargetAttachment,
)  # noqa: F401
from .jobs import (
    JobBase,
    JobCreate,
    JobUpdate,
    Job,
    UpcomingJob,
    JobWithRuns,
)  # noqa: F401
from .runs import (
    RunBase,
    RunCreate,
    RunUpdate,
    Run,
    TargetRun,
    RunWithJob,
)  # noqa: F401
from .backups import (
    BackupFromDiskResponse,
)  # noqa: F401

# Resolve forward references across modules to satisfy Pydantic v2
try:
    from pydantic import BaseModel  # type: ignore

    _ns = dict(globals())
    for _name, _obj in list(globals().items()):
        try:
            if isinstance(_obj, type) and issubclass(_obj, BaseModel):
                _obj.model_rebuild(_types_namespace=_ns)  # type: ignore[attr-defined]
        except Exception:
            # Best-effort; ignore classes that aren't Pydantic models
            pass
except Exception:
    # If pydantic not available at import time, skip (tests/uvicorn will import later)
    pass


