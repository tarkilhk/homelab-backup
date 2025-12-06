from .targets import router as targets_router  # noqa: F401
from .jobs import router as jobs_router  # noqa: F401
from .runs import router as runs_router  # noqa: F401
from .restores import router as restores_router  # noqa: F401
from .plugins import router as plugins_router  # noqa: F401
from .metrics import router as metrics_router  # noqa: F401
from .tags import router as tags_router  # noqa: F401
from .groups import router as groups_router  # noqa: F401
from .backups import router as backups_router  # noqa: F401
"""API package exports for routers."""

from . import health, targets, jobs, runs, restores, backups  # noqa: F401

"""API routers package."""
