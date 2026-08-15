"""Process-local mutual exclusion for operations that mutate or snapshot one target."""

from __future__ import annotations

import threading

_LOCKS: dict[int, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def get_target_operation_lock(target_id: int) -> threading.Lock:
    """Return the shared lock for a persisted target identifier."""

    with _LOCKS_GUARD:
        return _LOCKS.setdefault(target_id, threading.Lock())
