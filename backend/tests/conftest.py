"""Root conftest for tests directory."""

from __future__ import annotations

import io
import sqlite3
import zipfile

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.db import Base


@pytest.fixture()
def make_servarr_zip(tmp_path):
    """Build a structurally valid Servarr backup archive for plugin tests."""

    def build(database_name: str) -> bytes:
        database_path = tmp_path / database_name
        with sqlite3.connect(database_path) as connection:
            connection.execute("CREATE TABLE proof (id INTEGER PRIMARY KEY, value TEXT)")
            connection.execute("INSERT INTO proof(value) VALUES ('restorable')")
        database_bytes = database_path.read_bytes()
        database_path.unlink()

        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w") as archive:
            archive.writestr("Config.xml", "<Config><ApiKey>test-key</ApiKey></Config>")
            archive.writestr("INFO", "Version: test\nCreated: now\n")
            archive.writestr(database_name, database_bytes)
        return payload.getvalue()

    return build


@pytest.fixture()
def db_session() -> Session:
    """Provide a test DB session."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    # Ensure models are imported
    import app.models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    try:
        yield TestingSessionLocal()
    finally:
        Base.metadata.drop_all(bind=engine)
