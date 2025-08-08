"""Central logging configuration for the backend.

This module configures Python logging with sane defaults and is intended to be
invoked from `app.main` during startup.
"""

from __future__ import annotations

import logging
import os
from typing import Optional


def setup_logging(level: Optional[str] = None) -> None:
    """Initialize application logging.

    - Level is taken from the `LOG_LEVEL` environment variable if not provided.
    - Uses a concise, structured-ish format with timestamps.
    """

    log_level = (level or os.getenv("LOG_LEVEL", "INFO")).upper()

    # Configure root logger only once to avoid duplicate handlers in reloads
    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=log_level,
            format=(
                "%(asctime)s | %(levelname)s | %(name)s | "
                "%(message)s"
            ),
            datefmt="%Y-%m-%d %H:%M:%S",
        )

    # Align common third-party loggers to our level (uvicorn, sqlalchemy)
    logging.getLogger("uvicorn").setLevel(log_level)
    logging.getLogger("uvicorn.error").setLevel(log_level)
    logging.getLogger("uvicorn.access").setLevel(log_level)
    # SQLAlchemy engine echo controlled elsewhere; keep INFO for statements
    logging.getLogger("sqlalchemy.engine").setLevel(logging.INFO)


