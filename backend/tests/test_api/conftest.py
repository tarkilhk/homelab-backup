from __future__ import annotations

from typing import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import Session, sessionmaker

from app.main import app
from app.core.db import Base, get_session


class _DummyScheduler:
    def start(self) -> None:  # noqa: D401
        """No-op start."""
        return None

    def shutdown(self) -> None:  # noqa: D401
        """No-op shutdown."""
        return None


@pytest.fixture
def db_session_override() -> Generator[Session, None, None]:
    """Provide a test DB session and override FastAPI dependency."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    # Ensure models are registered with Base before creating tables
    import app.models  # noqa: F401
    Base.metadata.create_all(bind=engine)

    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture
def client(db_session_override: Session, monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient, None, None]:
    """FastAPI TestClient with DB and scheduler overrides."""

    def override_get_session() -> Generator[Session, None, None]:
        try:
            yield db_session_override
        finally:
            pass

    # Override DB dependency
    app.dependency_overrides[get_session] = override_get_session

    # Avoid touching real DB or scheduling during app startup in tests
    monkeypatch.setattr("app.main.init_db", lambda: None, raising=True)
    monkeypatch.setattr("app.main.bootstrap_db", lambda: None, raising=True)
    monkeypatch.setattr("app.main.schedule_jobs_on_startup", lambda scheduler, db: None, raising=True)

    # Stub scheduler to avoid starting real APScheduler in tests
    monkeypatch.setattr("app.main.get_scheduler", lambda: _DummyScheduler(), raising=True)

    with TestClient(app) as test_client:
        yield test_client

    # Cleanup overrides
    app.dependency_overrides.clear()


