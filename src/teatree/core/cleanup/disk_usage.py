"""How much a directory would return if it were removed.

One answer for every reaper that reports what it reclaimed, so a plan's estimate
and the bytes it later claims to have freed are measured the same way.
"""

import os
from pathlib import Path

_GIB = 1024 * 1024 * 1024


def dir_size_bytes(directory: Path) -> int:
    """The bytes *directory* holds, counting links by their own size, never their target.

    Best-effort by design: a file that vanishes mid-walk, or one this process may
    not stat, contributes nothing rather than aborting the measurement — the
    caller is reclaiming disk, not auditing it.
    """
    total = 0
    for root, _dirs, files in os.walk(directory):
        for name in files:
            try:
                total += (Path(root) / name).lstat().st_size
            except OSError:
                continue
    return total


def dir_size_gb(directory: Path) -> float:
    """:func:`dir_size_bytes` in the unit the freeing plans report."""
    return dir_size_bytes(directory) / _GIB


__all__ = ["dir_size_bytes", "dir_size_gb"]
