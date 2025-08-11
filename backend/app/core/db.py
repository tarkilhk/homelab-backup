"""Database configuration and session management.

SQLite location is configured via a single environment variable:
- `DB_FOLDER`: directory where the database file will be stored. The filename
  is hardcoded to `homelab_backup.db`.

If `DB_FOLDER` is not set, the default folder `./db` is used.
"""

from typing import Generator
from pathlib import Path
import logging

from sqlalchemy import create_engine
import os
from sqlalchemy.orm import Session, sessionmaker, declarative_base

# Resolve DB location based on DB_FOLDER only (no DATABASE_URL support)
DEFAULT_DB_FILENAME = "homelab_backup.db"
DEFAULT_DB_FOLDER = "./db_default"

logger = logging.getLogger(__name__)

requested_dir = Path(os.getenv("DB_FOLDER", DEFAULT_DB_FOLDER)).expanduser()
logger.info(
    "DB path init | DB_FOLDER_env=%s default_folder=%s requested_dir=%s cwd=%s",
    os.getenv("DB_FOLDER"),
    DEFAULT_DB_FOLDER,
    requested_dir,
    Path.cwd(),
)

def _ensure_dir(path: Path) -> tuple[bool, str]:
    try:
        if not path.exists():
            logger.warning("DB dir does not exist: %s. Attempting to create and fall back to default", path)
        path.mkdir(parents=True, exist_ok=True)
        if not os.access(path, os.W_OK):
            return False, "directory not writable"
        return True, ""
    except Exception as exc:  # pragma: no cover - safety net
        return False, str(exc)

# Try requested directory; if unusable, fall back to default relative folder
db_dir = requested_dir
ok, reason = _ensure_dir(db_dir)
if not ok:
    logger.warning(
        "DB dir unusable | requested=%s reason=%s -> falling back to default=%s",
        db_dir,
        reason,
        DEFAULT_DB_FOLDER,
    )
    db_dir = Path(DEFAULT_DB_FOLDER)
    ok2, reason2 = _ensure_dir(db_dir)
    if not ok2:
        # As a last resort, use current working directory
        logger.warning(
            "Default folder unusable | default=%s reason=%s -> using cwd=%s",
            DEFAULT_DB_FOLDER,
            reason2,
            Path.cwd(),
        )
        db_dir = Path.cwd()
        _ensure_dir(db_dir)

logger.info("DB path resolved | using_dir=%s", db_dir)

db_file = db_dir / DEFAULT_DB_FILENAME
logger.info("DB file path: %s", db_file)
# `sqlite:///` + absolute path results in four slashes (sqlite:////...) which SQLAlchemy expects
SQLITE_URL = f"sqlite:///{db_file.resolve()}"
logger.info("SQLite URL: %s", SQLITE_URL)

def _resolve_sql_echo() -> bool | str:
    """Resolve SQL echo flag from environment.

    Supports the following values for `LOG_SQL_ECHO`:
    - "" (unset or empty): returns False (no SQL echo)
    - truthy ("1", "true", "yes", "on"): returns True (INFO-level statements)
    - "debug": returns "debug" (DEBUG-level with parameter values)
    Any other value defaults to False.
    """
    raw = os.getenv("LOG_SQL_ECHO", "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("debug", "2", "verbose"):
        return "debug"
    return False


# Create engine
engine = create_engine(
    SQLITE_URL,
    connect_args={"check_same_thread": False},  # Required for SQLite
    echo=_resolve_sql_echo(),
)

# Create session factory
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Create base class for models
Base = declarative_base()


def get_session() -> Generator[Session, None, None]:
    """Get database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Initialize database tables.

    Safety principle: NEVER drop tables automatically in application code.
    This function only attempts to create missing tables.
    """
    # Import models to ensure they are registered with Base
    # Important: include all models so Base.metadata has the complete schema
    from app.models import (
        Target,
        Job,
        Run,
        Group,
        Tag,
        GroupTag,
        TargetTag,
    )  # noqa: F401

    # Only create missing tables; do not drop/alter existing schema here
    logger.info("init_db: creating tables if missing")
    Base.metadata.create_all(bind=engine)
    logger.info("init_db: ensured tables exist")


def drop_all_tables() -> None:  # pragma: no cover - utility, run manually only
    """Dangerous helper to drop all tables.

    Not called anywhere by the application. Use only during development or
    via explicit operator action.
    """
    Base.metadata.drop_all(bind=engine)
    logger.warning("All database tables dropped.")


def bootstrap_db() -> None:
    """Bootstrap database with initial data if needed."""
    from sqlalchemy.orm import Session
    
    db = SessionLocal()
    try:
        # Check if we have any targets
        from app.models import Target
        
        target_count = db.query(Target).count()
        if target_count == 0:
            logger.info("Database is empty. Ready for initial data.")
        else:
            logger.info("Database contains %s targets.", target_count)
    finally:
        db.close()
