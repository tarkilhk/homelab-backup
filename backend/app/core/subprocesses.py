"""Bounded execution helpers for external backup and restore tools."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import TypeVar

T = TypeVar("T")
PROCESS_STOP_TIMEOUT_SECONDS = 5.0


async def _stop_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        await process.wait()
        return
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=PROCESS_STOP_TIMEOUT_SECONDS)
    except TimeoutError:
        if process.returncode is None:
            process.kill()
        await process.wait()


async def _stop_process_before_return(process: asyncio.subprocess.Process) -> None:
    """Terminate, kill if needed, and shield reaping from repeated cancellation."""
    stop_task = asyncio.create_task(_stop_process(process))
    while not stop_task.done():
        try:
            await asyncio.shield(stop_task)
        except asyncio.CancelledError:
            continue
    await stop_task


async def run_process_with_timeout(
    process: asyncio.subprocess.Process,
    awaitable: Awaitable[T],
    *,
    operation: str,
    timeout_seconds: float,
) -> T:
    """Await process work, killing and reaping the child when its deadline expires."""

    try:
        return await asyncio.wait_for(awaitable, timeout=timeout_seconds)
    except TimeoutError as exc:
        await _stop_process_before_return(process)
        raise RuntimeError(f"{operation} timed out after {timeout_seconds:g} seconds") from exc
    except BaseException:
        await _stop_process_before_return(process)
        raise
