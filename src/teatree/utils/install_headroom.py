"""The free-space precondition for the DESTRUCTIVE editable reinstall (#4338).

``uv tool install … --reinstall`` deletes the working tool venv before rebuilding it, so
a filesystem that fills mid-build leaves neither the old install nor a new one. Measured:
391 MB free, 124 packages present, ``click`` missing, every ``t3`` invocation dead at
``import typer``, the worker crash-looping for 13 hours.

Fail direction is the opposite of :mod:`teatree.core.process_freshness`'s, and for the
same reason stated the other way round: refusing here leaves the PREVIOUS, working venv
intact — every container keeps running old-but-functional code, which is recoverable —
while proceeding destroys a known-good venv and, out of space, cannot rebuild it. Fail
closed when proceeding is irreversible.

An UNMEASURABLE filesystem proceeds. An absent reading is not evidence of no room, and a
permanent refusal on it would disarm self-update with nothing to clear it — the lockout
shape #4217 fixed. Only a measured shortfall refuses.

The default floor is duplicated in ``deploy/entrypoint.sh`` because the boot-time install
runs before any Python exists to ask; ``tests/test_deploy_entrypoint_install_headroom.py``
pins the two spellings to the same number so they cannot drift.
"""

import os
import shutil
from collections.abc import Mapping
from pathlib import Path

#: Env override for the floor, matching the ``TEATREE_DISK_CRIT_PERCENT`` precedent
#: rather than a ``ConfigSetting`` (which the boot-time installer could not read).
INSTALL_MIN_FREE_MB_ENV = "TEATREE_INSTALL_MIN_FREE_MB"

#: Megabytes that must be free before the destructive reinstall is allowed to start.
DEFAULT_INSTALL_MIN_FREE_MB = 2048

_BYTES_PER_MB = 1024 * 1024


def free_bytes(path: Path) -> int | None:
    """Free bytes on the filesystem holding *path*, walking up to the nearest existing parent.

    The walk matters on a fresh box, where the uv tool dir does not exist yet: its parent
    is on the same filesystem it is about to be created on. ``None`` when nothing on the
    way up can be stat'ed.
    """
    for candidate in (path, *path.parents):
        try:
            return shutil.disk_usage(candidate).free
        except OSError:
            continue
    return None


def install_min_free_mb(env: Mapping[str, str] | None = None) -> int:
    resolved = os.environ if env is None else env
    raw = resolved.get(INSTALL_MIN_FREE_MB_ENV, "").strip()
    try:
        return max(int(raw), 0)
    except ValueError:
        return DEFAULT_INSTALL_MIN_FREE_MB


def install_headroom_refusal(target: Path | None, env: Mapping[str, str] | None = None) -> str:
    """Why the destructive reinstall must not start, or ``""`` to proceed.

    *target* is the directory the tool venv is rebuilt in — measure THAT filesystem, not
    ``/``: the containerized deployment mounts the uv tool dir as a named volume, so ``/``
    can be a different device and the gate would be vacuous or spuriously firing.
    """
    if target is None:
        return ""
    free = free_bytes(target)
    if free is None:
        return ""
    floor_mb = install_min_free_mb(env)
    free_mb = free // _BYTES_PER_MB
    if free_mb >= floor_mb:
        return ""
    return (
        f"refusing the destructive editable reinstall: {free_mb} MB free on {target}, floor "
        f"{floor_mb} MB ({INSTALL_MIN_FREE_MB_ENV}). `--reinstall` deletes the working tool "
        f"venv before rebuilding it, so a truncated build leaves no runnable CLI at all; the "
        f"previous venv is left intact instead. Reclaim disk and retry."
    )


__all__ = [
    "DEFAULT_INSTALL_MIN_FREE_MB",
    "INSTALL_MIN_FREE_MB_ENV",
    "free_bytes",
    "install_headroom_refusal",
    "install_min_free_mb",
]
