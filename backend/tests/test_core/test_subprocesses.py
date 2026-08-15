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

    async def wait(self) -> int:
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
