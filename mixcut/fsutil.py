"""Filesystem primitives that behave identically on POSIX and Windows."""
from __future__ import annotations

import os


def flush_to_disk(path):
    """Force a freshly written file out of the operating system cache.

    Windows implements this with FlushFileBuffers, which requires a handle that
    was opened with write access; fsync on a read-only handle raises EBADF there
    even though POSIX accepts it.
    """
    mode = 'r+b' if os.name == 'nt' else 'rb'
    with open(path, mode) as stream:
        os.fsync(stream.fileno())


def publish(temporary, target):
    """Publish a finished temporary file under *target* without overwriting it.

    A hard link is the cheapest atomic publish, but not every Windows filesystem
    (exFAT, FAT32, some network shares) supports them, so an atomic rename on the
    same volume is used as a fallback.  Callers keep their "never overwrite"
    contract in both paths.  The temporary file is consumed either way.
    """
    if target.exists():
        raise FileExistsError(f'目标已存在：{target}')
    try:
        os.link(temporary, target)
        temporary.unlink()
    except OSError:
        os.replace(temporary, target)
