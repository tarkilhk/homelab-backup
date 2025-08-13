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

    root_logger = logging.getLogger()

    # Configure handlers once to avoid duplicates in reloads
    if not root_logger.handlers:
        logging.basicConfig(
            level=log_level,
            format=(
                "%(asctime)s | %(levelname)s | %(name)s | "
                "%(message)s"
            ),
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    
    # Always align root level (uvicorn may install handlers before we run)
    root_logger.setLevel(log_level)

    # Align common third-party loggers to our level (uvicorn)
    logging.getLogger("uvicorn").setLevel(log_level)
    logging.getLogger("uvicorn.error").setLevel(log_level)
    logging.getLogger("uvicorn.access").setLevel(log_level)

    # SQLAlchemy: keep quiet by default; detailed SQL is controlled via engine echo
    # Only show SQL engine logs when DEBUG is enabled at the root
    sqlalchemy_engine_level = logging.DEBUG if root_logger.level == logging.DEBUG else logging.WARNING
    logging.getLogger("sqlalchemy.engine").setLevel(sqlalchemy_engine_level)


