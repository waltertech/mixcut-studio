"""Single-instance file locking on both POSIX and Windows hosts."""
from __future__ import annotations

import os


class LockUnavailable(BlockingIOError):
    """Raised when another process already holds the lock."""


def acquire(handle):
    """Take a non-blocking exclusive lock on an open file handle."""
    if os.name == 'nt':
        import msvcrt
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise LockUnavailable(str(exc)) from exc
    else:
        import fcntl
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise LockUnavailable(str(exc)) from exc
    return handle


def release(handle):
    """Best-effort release; the OS drops the lock when the handle closes."""
    try:
        if os.name == 'nt':
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except (OSError, ValueError):
        pass
