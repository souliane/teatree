"""Serialize artifact dependencies with deletion on the artifact inode.

Eviction takes a non-blocking lock before its final scan and keeps it through
deletion. Symlink writers take the same lock before checking the source and keep
it until the link exists. If deletion wins, a waiting writer observes the missing
source and creates no dangling link; if the writer wins, the final scan sees its
link and protects the target.
"""

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from fnmatch import fnmatchcase
from pathlib import Path

#: The provisioned dependencies eviction may remove. ``.venv-hook*`` is a GLOB because the
#: hook environment's name is platform-scoped (``scripts/hooks/lib/resolve-uv.sh``), so a
#: bind-mounted clone accumulates one per platform that ever ran a hook in it — plus
#: whatever husks a rebuild left beside them.
ARTIFACT_NAMES = (".venv", ".venv-hook*", "node_modules", ".nx", ".angular")

_DIRECTORY_OPEN_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


@contextmanager
def artifact_source_lock(artifact: Path, *, blocking: bool) -> Iterator[bool]:
    """Lock *artifact* while a dependency is created or the directory is removed.

    The directory inode is the shared anchor: it is visible from every checkout
    venue, leaves no lock file behind, and survives a concurrent unlink long enough
    for a waiting writer to re-check that the source path disappeared. ``False``
    means the directory could not be opened or a non-blocking deletion lost the lock.
    """
    try:
        descriptor = os.open(artifact, _DIRECTORY_OPEN_FLAGS)
    except OSError:
        yield False
        return
    locked = False
    try:
        operation = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        try:
            fcntl.flock(descriptor, operation)
        except OSError:
            yield False
            return
        locked = True
        yield True
    finally:
        if locked:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def artifact_name_pattern(name: str) -> str | None:
    """The :data:`ARTIFACT_NAMES` entry *name* matches — a literal or a glob — else ``None``.

    The one place a directory name is resolved to its entry, so discovery, the symlink
    writer and the per-name rebuild inputs cannot disagree about what is an artifact.
    """
    return next((pattern for pattern in ARTIFACT_NAMES if fnmatchcase(name, pattern)), None)


def is_artifact_source(path: Path) -> bool:
    """Whether a provisioned dependency can be removed by artifact eviction."""
    return artifact_name_pattern(path.name) is not None


def artifact_lock_refusal_reason(artifact: Path) -> str:
    """Explain why eviction could not lock *artifact* without weakening safety."""
    if artifact.is_symlink():
        return "it became a symlink"
    return "it is busy or could not be locked for deletion"


__all__ = [
    "ARTIFACT_NAMES",
    "artifact_lock_refusal_reason",
    "artifact_name_pattern",
    "artifact_source_lock",
    "is_artifact_source",
]
