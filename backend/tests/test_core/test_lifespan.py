from collections.abc import Generator

import pytest

import app.main as main_module
from app.core.db import get_session


@pytest.mark.asyncio
async def test_application_lifespan_initializes_and_stops_scheduler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class DummySession:
        def close(self) -> None:
            events.append("session.close")

    class DummyScheduler:
        def start(self) -> None:
            events.append("scheduler.start")

        def shutdown(self) -> None:
            events.append("scheduler.shutdown")

    session = DummySession()
    scheduler = DummyScheduler()

    def override_session() -> Generator[DummySession, None, None]:
        yield session

    monkeypatch.setattr(main_module, "setup_logging", lambda: events.append("logging"))
    monkeypatch.setattr(main_module, "init_db", lambda: events.append("db.init"))
    monkeypatch.setattr(main_module, "bootstrap_db", lambda: events.append("db.bootstrap"))
    monkeypatch.setattr(main_module, "get_scheduler", lambda: scheduler)
    monkeypatch.setattr(
        main_module,
        "schedule_jobs_on_startup",
        lambda actual_scheduler, actual_session: events.append(
            "jobs.schedule"
            if actual_scheduler is scheduler and actual_session is session
            else "jobs.schedule.invalid"
        ),
    )

    previous_overrides = dict(main_module.app.dependency_overrides)
    main_module.app.dependency_overrides[get_session] = override_session
    try:
        async with main_module.lifespan(main_module.app):
            assert events == [
                "logging",
                "db.init",
                "db.bootstrap",
                "jobs.schedule",
                "session.close",
                "scheduler.start",
            ]
    finally:
        main_module.app.dependency_overrides.clear()
        main_module.app.dependency_overrides.update(previous_overrides)

    assert events[-1] == "scheduler.shutdown"
