"""Bounded execution helpers for external backup and restore tools."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import TypeVar

T = TypeVar("T")


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
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise RuntimeError(f"{operation} timed out after {timeout_seconds:g} seconds") from exc
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
