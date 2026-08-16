from __future__ import annotations

import asyncio

import pytest

from app.core.subprocesses import run_process_with_timeout


class HangingProcess:
    returncode = None

    def __init__(self) -> None:
        self.killed = False
        self.reaped = False

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def terminate(self) -> None:
        self.kill()

    async def wait(self) -> int:
        self.reaped = True
        return -9


class StubbornProcess:
    """Model a killed child whose wait remains pending through repeated cancellation."""

    returncode = None

    def __init__(self) -> None:
        self.killed = asyncio.Event()
        self.release = asyncio.Event()
        self.reaped = False

    def kill(self) -> None:
        self.killed.set()

    def terminate(self) -> None:
        self.killed.set()

    async def wait(self) -> int:
        await self.release.wait()
        self.returncode = -9
        self.reaped = True
        return -9


@pytest.mark.asyncio
async def test_timeout_kills_and_reaps_child() -> None:
    process = HangingProcess()

    async def never() -> None:
        await asyncio.Event().wait()

    with pytest.raises(RuntimeError, match="backup timed out"):
        await run_process_with_timeout(
            process,  # type: ignore[arg-type]
            never(),
            operation="backup",
            timeout_seconds=0.001,
        )

    assert process.killed is True
    assert process.reaped is True


@pytest.mark.asyncio
async def test_stream_failure_kills_and_reaps_child() -> None:
    process = HangingProcess()

    async def fails() -> None:
        raise OSError("artifact write failed")

    with pytest.raises(OSError, match="artifact write failed"):
        await run_process_with_timeout(
            process,  # type: ignore[arg-type]
            fails(),
            operation="backup",
            timeout_seconds=1,
        )

    assert process.killed is True
    assert process.reaped is True


@pytest.mark.asyncio
async def test_cancellation_kills_and_reaps_child() -> None:
    process = HangingProcess()

    async def never() -> None:
        await asyncio.Event().wait()

    task = asyncio.create_task(
        run_process_with_timeout(
            process,  # type: ignore[arg-type]
            never(),
            operation="restore",
            timeout_seconds=60,
        )
    )
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert process.killed is True
    assert process.reaped is True


@pytest.mark.asyncio
async def test_repeated_cancellation_waits_until_the_child_is_reaped() -> None:
    process = StubbornProcess()

    async def never() -> None:
        await asyncio.Event().wait()

    task = asyncio.create_task(
        run_process_with_timeout(
            process,  # type: ignore[arg-type]
            never(),
            operation="restore",
            timeout_seconds=60,
        )
    )
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.wait_for(process.killed.wait(), timeout=1)
    task.cancel()
    await asyncio.sleep(0)
    assert task.done() is False

    process.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.reaped is True


@pytest.mark.asyncio
async def test_timeout_terminates_then_kills_and_reaps_stubborn_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class TerminateResistantProcess:
        returncode = None

        def __init__(self) -> None:
            self.terminated = False
            self.killed = False
            self.reaped = False
            self.release = asyncio.Event()

        def terminate(self) -> None:
            self.terminated = True

        def kill(self) -> None:
            assert self.terminated is True
            self.killed = True
            self.release.set()

        async def wait(self) -> int:
            await self.release.wait()
            self.returncode = -9
            self.reaped = True
            return -9

    process = TerminateResistantProcess()
    monkeypatch.setattr("app.core.subprocesses.PROCESS_STOP_TIMEOUT_SECONDS", 0.001)

    async def never() -> None:
        await asyncio.Event().wait()

    with pytest.raises(RuntimeError, match="backup timed out"):
        await run_process_with_timeout(
            process,  # type: ignore[arg-type]
            never(),
            operation="backup",
            timeout_seconds=0.001,
        )

    assert process.terminated is True
    assert process.killed is True
    assert process.reaped is True
