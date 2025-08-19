"""Central logging configuration for the backend.

This module configures Python logging with sane defaults and is intended to be
invoked from `app.main` during startup.
"""

from __future__ import annotations

import logging
import os
from typing import Optional


class HealthCheckFilter(logging.Filter):
    """Filter out health check requests from uvicorn access logs."""
    
    def filter(self, record: logging.LogRecord) -> bool:
        # Filter out health check endpoints from access logs
        if hasattr(record, 'getMessage'):
            message = record.getMessage()
            # Check for health check endpoints in the log message
            if any(health_endpoint in message for health_endpoint in ['/health', '/ready']):
                return False
        # Also check the record's message directly
        if hasattr(record, 'msg') and isinstance(record.msg, str):
            if any(health_endpoint in record.msg for health_endpoint in ['/health', '/ready']):
                return False
        return True


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
    
    # Completely suppress uvicorn access logs to eliminate health check spam
    # This will hide all access logs, but eliminates the health check spam
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

    # SQLAlchemy: keep quiet by default; detailed SQL is controlled via engine echo
    # Only show SQL engine logs when DEBUG is enabled at the root
    sqlalchemy_engine_level = logging.DEBUG if root_logger.level == logging.DEBUG else logging.WARNING
    logging.getLogger("sqlalchemy.engine").setLevel(sqlalchemy_engine_level)


