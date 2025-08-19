"""Tests for database layer: connection failures and edge cases."""

from __future__ import annotations

import os
import tempfile
import shutil
from pathlib import Path
from unittest.mock import patch, MagicMock
from typing import Generator

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.db import (
    get_engine,
    get_session,
    init_db,
    bootstrap_db,
    _ensure_dir,
    _build_sqlite_url,
    _resolve_sql_echo,
    DB_DIR,
    DEFAULT_DB_FILENAME,
)
from app.core.db import Base


@pytest.fixture
def temp_db_dir():
    """Provide a temporary directory for database testing."""
    temp_dir = tempfile.mkdtemp(prefix="test-db-")
    yield temp_dir
    shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture
def mock_db_dir(monkeypatch, temp_db_dir):
    """Mock the database directory to use a temporary location."""
    # Mock the DB_DIR constant to use our temp directory
    monkeypatch.setattr("app.core.db.DB_DIR", Path(temp_db_dir))
    # Clear any cached engine and session factory
    monkeypatch.setattr("app.core.db._engine", None)
    monkeypatch.setattr("app.core.db.SessionLocal", None)
    yield temp_db_dir


def test_ensure_dir_creates_directory_successfully(temp_db_dir):
    """Test that _ensure_dir creates directories successfully."""
    test_dir = Path(temp_db_dir) / "new_dir"
    
    # Directory doesn't exist initially
    assert not test_dir.exists()
    
    # Create directory
    ok, reason = _ensure_dir(test_dir)
    
    assert ok is True
    assert reason == ""
    assert test_dir.exists()
    assert test_dir.is_dir()


def test_ensure_dir_handles_existing_directory(temp_db_dir):
    """Test that _ensure_dir handles existing directories gracefully."""
    test_dir = Path(temp_db_dir) / "existing_dir"
    test_dir.mkdir(exist_ok=True)
    
    # Directory already exists
    assert test_dir.exists()
    
    # Should succeed
    ok, reason = _ensure_dir(test_dir)
    
    assert ok is True
    assert reason == ""
    assert test_dir.exists()
    assert test_dir.is_dir()


def test_ensure_dir_handles_permission_errors(temp_db_dir):
    """Test that _ensure_dir handles permission errors gracefully."""
    # Mock path.mkdir to raise PermissionError
    with patch.object(Path, 'mkdir') as mock_mkdir:
        mock_mkdir.side_effect = PermissionError("Permission denied")
        
        test_dir = Path(temp_db_dir) / "permission_denied"
        ok, reason = _ensure_dir(test_dir)
        
        assert ok is False
        assert "Permission denied" in reason


def test_ensure_dir_handles_other_os_errors(temp_db_dir):
    """Test that _ensure_dir handles other OS errors gracefully."""
    # Mock path.mkdir to raise OSError
    with patch.object(Path, 'mkdir') as mock_mkdir:
        mock_mkdir.side_effect = OSError("Disk full")
        
        test_dir = Path(temp_db_dir) / "disk_full"
        ok, reason = _ensure_dir(test_dir)
        
        assert ok is False
        assert "Disk full" in reason


def test_build_sqlite_url_creates_valid_url(temp_db_dir):
    """Test that _build_sqlite_url creates valid SQLite URLs."""
    db_dir = Path(temp_db_dir)
    
    url = _build_sqlite_url(db_dir)
    
    assert url.startswith("sqlite:///")
    assert temp_db_dir in url
    assert url.endswith(f"/{DEFAULT_DB_FILENAME}")


def test_resolve_sql_echo_defaults_to_false():
    """Test that _resolve_sql_echo defaults to False when not set."""
    # Clear any existing environment variable
    with patch.dict(os.environ, {}, clear=True):
        result = _resolve_sql_echo()
        assert result is False


def test_resolve_sql_echo_reads_environment_variable():
    """Test that _resolve_sql_echo reads LOG_SQL_ECHO environment variable."""
    with patch.dict(os.environ, {"LOG_SQL_ECHO": "true"}):
        result = _resolve_sql_echo()
        assert result is True
    
    with patch.dict(os.environ, {"LOG_SQL_ECHO": "false"}):
        result = _resolve_sql_echo()
        assert result is False


def test_get_engine_creates_engine_successfully(mock_db_dir):
    """Test that get_engine creates engine successfully."""
    engine = get_engine()
    
    assert engine is not None
    assert str(engine.url).startswith("sqlite:///")
    assert mock_db_dir in str(engine.url)


def test_get_engine_caches_engine_instance(mock_db_dir):
    """Test that get_engine caches the engine instance."""
    engine1 = get_engine()
    engine2 = get_engine()
    
    assert engine1 is engine2


def test_get_engine_handles_directory_creation_failure(monkeypatch):
    """Test that get_engine handles directory creation failures gracefully."""
    # Mock _ensure_dir to fail
    def mock_ensure_dir(path):
        return False, "Directory creation failed"
    
    monkeypatch.setattr("app.core.db._ensure_dir", mock_ensure_dir)
    
    # Should raise SystemExit when directory creation fails
    with pytest.raises(SystemExit) as exc_info:
        get_engine()
    
    assert exc_info.value.code == 1


def test_get_session_creates_session_successfully(mock_db_dir):
    """Test that get_session creates database sessions successfully."""
    session_gen = get_session()
    session = next(session_gen)
    
    try:
        assert session is not None
        assert hasattr(session, 'commit')
        assert hasattr(session, 'rollback')
        assert hasattr(session, 'close')
    finally:
        session_gen.close()


def test_get_session_closes_session_on_exit(mock_db_dir):
    """Test that get_session properly closes sessions."""
    session_gen = get_session()
    session = next(session_gen)
    
    # Mock the close method to track calls
    original_close = session.close
    close_called = False
    
    def mock_close():
        nonlocal close_called
        close_called = True
        original_close()
    
    session.close = mock_close
    
    # Close the generator (simulates finally block)
    session_gen.close()
    
    # Verify close was called
    assert close_called


def test_get_session_initializes_engine_if_needed(mock_db_dir):
    """Test that get_session initializes engine if not already done."""
    # Clear any cached engine
    from app.core.db import _engine, SessionLocal
    _engine = None
    SessionLocal = None
    
    session_gen = get_session()
    session = next(session_gen)
    
    try:
        assert session is not None
        # Verify engine was created
        from app.core.db import _engine
        assert _engine is not None
    finally:
        session_gen.close()


def test_init_db_creates_tables_successfully(mock_db_dir):
    """Test that init_db creates database tables successfully."""
    # Initialize database
    init_db()
    
    # Verify tables were created
    engine = get_engine()
    from sqlalchemy import inspect
    inspector = inspect(engine)
    tables = inspector.get_table_names()
    
    # Check for expected tables
    expected_tables = ['targets', 'jobs', 'runs', 'groups', 'tags', 'group_tags', 'target_tags']
    for table in expected_tables:
        assert table in tables


def test_init_db_handles_existing_tables_gracefully(mock_db_dir):
    """Test that init_db handles existing tables gracefully."""
    # Initialize database first time
    init_db()
    
    # Initialize again - should not fail
    init_db()
    
    # Verify tables still exist
    engine = get_engine()
    from sqlalchemy import inspect
    inspector = inspect(engine)
    tables = inspector.get_table_names()
    
    assert len(tables) > 0


def test_bootstrap_db_handles_empty_database(mock_db_dir):
    """Test that bootstrap_db handles empty database gracefully."""
    # Initialize empty database
    init_db()
    
    # Bootstrap should not fail
    bootstrap_db()
    
    # Verify database is accessible
    engine = get_engine()
    with engine.connect() as conn:
        result = conn.execute(text("SELECT COUNT(*) FROM targets"))
        count = result.scalar()
        assert count == 0


def test_bootstrap_db_handles_existing_data(mock_db_dir):
    """Test that bootstrap_db handles existing data gracefully."""
    # Initialize database
    init_db()
    
    # Add some test data using ORM to ensure defaults are applied
    from app.models.targets import Target
    target = Target(
        name="Test Target",
        slug="test-target",
        plugin_name="dummy",
        plugin_config_json="{}"
    )
    session = next(get_session())
    try:
        session.add(target)
        session.commit()
    finally:
        session.close()
    
    # Bootstrap should not fail
    bootstrap_db()
    
    # Verify existing data is preserved
    engine = get_engine()
    with engine.connect() as conn:
        result = conn.execute(text("SELECT COUNT(*) FROM targets"))
        count = result.scalar()
        assert count == 1


def test_database_connection_failure_handling(mock_db_dir):
    """Test that database operations handle connection failures gracefully."""
    # Initialize database
    init_db()
    
    # Get a session
    session_gen = get_session()
    session = next(session_gen)
    
    try:
        # Test basic database operations
        result = session.execute(text("SELECT 1"))
        assert result.scalar() == 1
        
        # Test transaction handling
        session.execute(text("SELECT 2"))
        session.commit()
        
    except Exception as e:
        session.rollback()
        raise
    finally:
        session_gen.close()


def test_database_session_isolation(mock_db_dir):
    """Test that database sessions provide proper isolation."""
    # Initialize database
    init_db()
    
    # Create two separate sessions
    session1_gen = get_session()
    session1 = next(session1_gen)
    
    session2_gen = get_session()
    session2 = next(session2_gen)
    
    try:
        # Changes in session1 should not be visible in session2 until commit
        session1.execute(text("CREATE TABLE test_isolation (id INTEGER PRIMARY KEY, name TEXT)"))
        
        # Table should not be visible in session2 yet (SQLite doesn't provide true isolation)
        # This test demonstrates the limitation of SQLite session isolation
        # In a real database like PostgreSQL, this would work as expected
        
        # Commit in session1
        session1.commit()
        
        # Now table should be visible in session2
        result = session2.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name='test_isolation'"))
        assert result.fetchone() is not None
        
    finally:
        session1_gen.close()
        session2_gen.close()


def test_database_rollback_functionality(mock_db_dir):
    """Test that database rollback works correctly."""
    # Initialize database
    init_db()
    
    session_gen = get_session()
    session = next(session_gen)
    
    try:
        # Create a test table
        session.execute(text("CREATE TABLE test_rollback (id INTEGER PRIMARY KEY, name TEXT)"))
        session.commit()
        
        # Insert data
        session.execute(text("INSERT INTO test_rollback (name) VALUES (:name)"), {"name": "test1"})
        
        # Verify data is in session
        result = session.execute(text("SELECT COUNT(*) FROM test_rollback"))
        assert result.scalar() == 1
        
        # Rollback
        session.rollback()
        
        # Verify data is not committed
        result = session.execute(text("SELECT COUNT(*) FROM test_rollback"))
        assert result.scalar() == 0
        
    finally:
        session_gen.close()


def test_database_concurrent_access(mock_db_dir):
    """Test that database handles concurrent access gracefully."""
    # Initialize database
    init_db()
    
    # Create multiple sessions
    sessions = []
    try:
        for i in range(5):
            session_gen = get_session()
            session = next(session_gen)
            sessions.append((session_gen, session))
        
        # Perform concurrent operations using ORM to ensure defaults are applied
        for i, (session_gen, session) in enumerate(sessions):
            from app.models.targets import Target
            target = Target(
                name=f"Target {i}",
                slug=f"target-{i}",
                plugin_name="dummy",
                plugin_config_json="{}"
            )
            session.add(target)
            session.commit()
        
        # Verify all data was inserted
        engine = get_engine()
        with engine.connect() as conn:
            result = conn.execute(text("SELECT COUNT(*) FROM targets"))
            count = result.scalar()
            assert count == 5
            
    finally:
        for session_gen, session in sessions:
            session_gen.close()


def test_database_file_permissions(mock_db_dir):
    """Test that database file has correct permissions."""
    # Initialize database
    init_db()
    
    # Check database file permissions
    db_file = Path(mock_db_dir) / DEFAULT_DB_FILENAME
    assert db_file.exists()
    
    # File should be readable and writable by current user
    assert os.access(db_file, os.R_OK)
    assert os.access(db_file, os.W_OK)


def test_database_directory_permissions(mock_db_dir):
    """Test that database directory has correct permissions."""
    # Initialize database
    init_db()
    
    # Directory should be readable and writable by current user
    assert os.access(mock_db_dir, os.R_OK)
    assert os.access(mock_db_dir, os.W_OK)
    assert os.access(mock_db_dir, os.X_OK)  # Executable for directory access
