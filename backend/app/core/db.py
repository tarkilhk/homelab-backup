"""Database configuration and session management.

SQLite location is fixed to the container path `/app/db`.
The filename is hardcoded to `homelab_backup.db`.

Mount whatever host directory you prefer to `/app/db` via Docker Compose.
If `/app/db` is not accessible at runtime, the backend logs an error and stops.
"""

from typing import Generator
from pathlib import Path
import logging

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
import os
from sqlalchemy.orm import Session, sessionmaker, declarative_base

# Resolve DB location (no DATABASE_URL support). Always use /app/db inside the container
DEFAULT_DB_FILENAME = "homelab_backup.db"
DB_DIR = Path("/app/db")

logger = logging.getLogger(__name__)

logger.info("DB path init | using fixed dir=/app/db cwd=%s", Path.cwd())

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

def _build_sqlite_url(db_dir: Path) -> str:
    db_file = db_dir / DEFAULT_DB_FILENAME
    logger.info("DB file path: %s", db_file)
    # `sqlite:///` + absolute path results in four slashes (sqlite:////...) which SQLAlchemy expects
    return f"sqlite:///{db_file.resolve()}"

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


_engine: Engine | None = None
SessionLocal: sessionmaker | None = None

# Create base class for models
Base = declarative_base()


def get_engine() -> Engine:
    """Create the SQLAlchemy engine lazily.

    Ensures `/app/db` exists and is writable. If not, logs an error and exits.
    """
    global _engine, SessionLocal
    if _engine is not None:
        return _engine

    ok, reason = _ensure_dir(DB_DIR)
    if not ok:
        logger.error("Database directory '/app/db' is not usable: %s", reason)
        raise SystemExit(1)

    logger.info("DB path resolved | using_dir=%s", DB_DIR)
    sqlite_url = _build_sqlite_url(DB_DIR)
    logger.info("SQLite URL: %s", sqlite_url)

    _engine = create_engine(
        sqlite_url,
        connect_args={"check_same_thread": False},  # Required for SQLite
        echo=_resolve_sql_echo(),
    )

    # Bind a session factory
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=_engine)
    return _engine


def get_session() -> Generator[Session, None, None]:
    """Get database session."""
    # Ensure engine and session factory are initialized
    if SessionLocal is None:
        get_engine()
        assert SessionLocal is not None
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def run_migrations() -> None:
    """Run database migrations from SQL files.
    
    Migrations are applied in alphabetical order (by filename).
    Errors are logged but don't stop the process (migrations may already be applied).
    """
    # Migrations directory is at /app/migrations in the container
    # Path from backend/app/core/db.py -> backend/migrations
    # In container: /app/app/core/db.py -> /app/migrations
    migrations_dir = Path("/app/migrations")
    if not migrations_dir.exists():
        logger.warning("Migrations directory not found: %s", migrations_dir)
        return
    
    engine = get_engine()
    migration_files = sorted(migrations_dir.glob("*.sql"))
    
    if not migration_files:
        logger.info("No migration files found")
        return
    
    logger.info("Running %d migration(s)...", len(migration_files))
    
    for migration_file in migration_files:
        logger.info("Applying migration: %s", migration_file.name)
        migration_sql = migration_file.read_text(encoding="utf-8")
        statements_executed = 0
        statements_skipped = 0
        
        # Execute each statement in its own transaction to allow partial success
        for statement in migration_sql.split(";"):
            statement = statement.strip()
            # Remove comment lines from statement (they may precede actual SQL)
            lines = [line for line in statement.split('\n') if not line.strip().startswith('--')]
            statement = '\n'.join(lines).strip()
            # Skip empty statements
            if statement:
                try:
                    with engine.begin() as conn:
                        result = conn.execute(text(statement))
                        # Force commit by exiting the context manager
                    statements_executed += 1
                    logger.info("Executed: %s", statement[:80])
                except Exception as e:
                    error_msg = str(e).lower()
                    # SQLite errors for already-existing columns/tables
                    # Check for the exact SQLite error message format
                    if any(keyword in error_msg for keyword in [
                        "already exists", "duplicate column", "duplicate column name",
                        "duplicate column name: retention_policy_json"
                    ]):
                        statements_skipped += 1
                        logger.info("Statement already applied (skipping): %s", statement[:80])
                    else:
                        # Unknown error - log the full error and re-raise to see what's happening
                        logger.error("Migration statement FAILED: %s", statement[:100])
                        logger.error("Full error: %s", str(e))
                        logger.error("Error type: %s", type(e).__name__)
                        # Re-raise to see the actual error in logs
                        raise
        
        if statements_executed > 0:
            logger.info("Migration applied: %s (%d executed, %d skipped)", 
                       migration_file.name, statements_executed, statements_skipped)
        elif statements_skipped > 0:
            logger.info("Migration already applied: %s (all %d statements skipped)", 
                       migration_file.name, statements_skipped)
        else:
            logger.warning("Migration had no executable statements: %s", migration_file.name)
    
    logger.info("Migrations completed")


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
        Settings,
    )  # noqa: F401

    # Only create missing tables; do not drop/alter existing schema here
    logger.info("init_db: creating tables if missing")
    engine = get_engine()
    Base.metadata.create_all(bind=engine)
    logger.info("init_db: ensured tables exist")
    
    # Run migrations to apply schema changes to existing databases
    run_migrations()


def drop_all_tables() -> None:  # pragma: no cover - utility, run manually only
    """Dangerous helper to drop all tables.

    Not called anywhere by the application. Use only during development or
    via explicit operator action.
    """
    eng = get_engine()
    Base.metadata.drop_all(bind=eng)
    logger.warning("All database tables dropped.")


def bootstrap_db() -> None:
    """Bootstrap database with initial data if needed."""
    from sqlalchemy.orm import Session
    
    if SessionLocal is None:
        get_engine()
        assert SessionLocal is not None
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
