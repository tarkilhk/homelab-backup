"""Database configuration and session management."""

from typing import Generator

from sqlalchemy import create_engine
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
    """Initialize database tables."""
    # Import models to ensure they are registered with Base
    from app.models import Target, Job, Run  # noqa: F401
    
    Base.metadata.create_all(bind=engine)
    print("Database tables created successfully")


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
