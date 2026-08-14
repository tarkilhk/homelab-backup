"""Pydantic schemas package (split from monolithic schemas.py).

Public re-exports keep existing import paths stable.
"""

from .backups import (  # noqa: F401
    BackupFromDiskResponse,
)
from .groups import (  # noqa: F401
    AddTagsToGroup,
    AddTargetsToGroup,
    Group,
    GroupBase,
    GroupCreate,
    GroupUpdate,
    GroupWithTags,
    GroupWithTargets,
    RemoveTagsFromGroup,
    RemoveTargetsFromGroup,
)
from .jobs import (  # noqa: F401
    Job,
    JobBase,
    JobCreate,
    JobUpdate,
    JobWithRuns,
    UpcomingJob,
)
from .runs import (  # noqa: F401
    Run,
    RunBase,
    RunCreate,
    RunUpdate,
    RunWithJob,
    TargetRun,
)
from .settings import (  # noqa: F401
    RetentionPolicy,
    RetentionRule,
    Settings,
    SettingsBase,
    SettingsUpdate,
)
from .tags import (  # noqa: F401
    Tag,
    TagBase,
    TagCreate,
    TagTargetAttachment,
    TagUpdate,
)
from .targets import (  # noqa: F401
    AddTagsToTarget,
    RemoveTagsFromTarget,
    Target,
    TargetBase,
    TargetCreate,
    TargetTagWithOrigin,
    TargetUpdate,
)

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
