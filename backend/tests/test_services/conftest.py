from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.db import Base


@pytest.fixture()
def db():
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


