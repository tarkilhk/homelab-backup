"""Database configuration and session management.

Supports two ways to configure the SQLite database location:
- `DB_FOLDER` (+ optional `DB_FILENAME`) to construct the SQLite URL from a
  directory path. This is preferred to avoid mistakes with URL formatting.
- Fallback to `DATABASE_URL` for full control; if unset, uses a local file
  `./homelab_backup.db`.
"""

from typing import Generator
from pathlib import Path

from sqlalchemy import create_engine, text
import os
from sqlalchemy.orm import Session, sessionmaker, declarative_base

# Resolve DB location
# Priority: DB_FOLDER (and optional DB_FILENAME) -> DATABASE_URL -> default file
DEFAULT_DB_FILENAME = "homelab_backup.db"

_db_folder = os.getenv("DB_FOLDER")
if _db_folder:
    # Expand and ensure directory exists
    db_dir = Path(_db_folder).expanduser()
    try:
        db_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        # Directory creation failure shouldn't crash import; engine creation may still fail later
        pass
    db_file = db_dir / os.getenv("DB_FILENAME", DEFAULT_DB_FILENAME)
    # `sqlite:///` + absolute path results in four slashes (sqlite:////...) which SQLAlchemy expects
    DATABASE_URL = f"sqlite:///{db_file.resolve()}"
else:
    # Full URL provided or default local file
    DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///./{DEFAULT_DB_FILENAME}")

# Create engine
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},  # Required for SQLite
    echo=True,  # Set to False in production
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
    from app.models import Target, Job, Run  # noqa: F401

    # Only create missing tables; do not drop/alter existing schema here
    Base.metadata.create_all(bind=engine)
    print("Database tables ensured (create if missing)")


def drop_all_tables() -> None:  # pragma: no cover - utility, run manually only
    """Dangerous helper to drop all tables.

    Not called anywhere by the application. Use only during development or
    via explicit operator action.
    """
    Base.metadata.drop_all(bind=engine)
    print("All database tables dropped.")


def bootstrap_db() -> None:
    """Bootstrap database with initial data if needed."""
    from sqlalchemy.orm import Session
    
    db = SessionLocal()
    try:
        # Check if we have any targets
        from app.models import Target
        
        target_count = db.query(Target).count()
        if target_count == 0:
            print("Database is empty. Ready for initial data.")
        else:
            print(f"Database contains {target_count} targets.")
    finally:
        db.close()
