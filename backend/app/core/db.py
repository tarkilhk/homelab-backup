"""Database configuration and session management."""

from typing import Generator

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker, declarative_base

# SQLite database URL
DATABASE_URL = "sqlite:///./homelab_backup.db"

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
