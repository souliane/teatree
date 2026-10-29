"""Bounded, fail-closed tree statistics for reclaimable scratch and database copies."""

import os
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class TreeStats:
    """What one recursive walk of a candidate tree answers, computed together.

    ``newest_mtime`` is the NEWEST ``st_mtime`` of any node anywhere in the
    tree (every file's own mtime, every directory's own mtime, and the top
    entry's), never just the top-level entry's own stat. A directory's own
    mtime moves only when an entry is added, removed, or renamed DIRECTLY
    inside it — editing a file two levels down never touches it — so a
    top-level-only check reads an actively-written working tree as stale the
    moment its own last direct-child change aged out of the window, even
    though content underneath was written seconds ago.

    ``holds_git_repo`` is True when a ``.git`` entry (directory for a clone,
    file for a worktree) exists anywhere in the tree — the ad-hoc-repo guard
    that catches a git checkout the ``Worktree`` table never learned about.
    ``None`` for ``newest_mtime`` means the top entry itself could not be
    stat-ed, OR some directory anywhere in the tree could not be SCANNED (an
    ``EACCES`` on a subdirectory this uid cannot search) — either way never
    removable, because an unscanned subtree could hold content written a
    moment ago and silently under-reporting it is the same fail-open shape
    as reading only the top-level entry's own mtime.
    """

    size_bytes: int
    newest_mtime: float | None
    holds_git_repo: bool


def tree_stats(path: Path, *, deadline: float | None = None) -> TreeStats:
    """Size, tree-wide newest mtime, and ad-hoc-repo detection in ONE walk."""
    try:
        top = path.lstat()
    except OSError:
        return TreeStats(size_bytes=0, newest_mtime=None, holds_git_repo=False)
    if path.is_symlink() or not path.is_dir():
        return TreeStats(size_bytes=top.st_size, newest_mtime=top.st_mtime, holds_git_repo=False)
    total_size = 0
    newest = top.st_mtime
    try:
        # A directory this uid cannot search (EACCES) raises here, same as it
        # will on os.walk's own scandir below — caught there via `unreadable`.
        holds_git = (path / ".git").exists()
    except OSError:
        holds_git = False
    unreadable = False

    def _blind(_error: OSError) -> None:
        # os.walk's default onerror=None silently drops whatever a subtree it
        # cannot scandir would have contributed — the same fail-open shape as
        # reading only the top-level mtime, one level deeper. Recorded, not
        # swallowed: newest_mtime becomes None below, so the entry is kept.
        nonlocal unreadable
        unreadable = True

    for root, dirs, files in os.walk(path, onerror=_blind, followlinks=False):
        if deadline is not None and time.monotonic() >= deadline:
            unreadable = True
            break
        if ".git" in dirs or ".git" in files:
            holds_git = True
        file_stats, files_denied = _lstat_all(root, files)
        dir_stats, dirs_denied = _lstat_all(root, dirs)
        unreadable = unreadable or files_denied or dirs_denied
        for info in file_stats:
            total_size += info.st_size
            newest = max(newest, info.st_mtime)
        for info in dir_stats:
            newest = max(newest, info.st_mtime)
    return TreeStats(
        size_bytes=total_size,
        newest_mtime=None if unreadable else newest,
        holds_git_repo=holds_git,
    )


def _lstat_all(root: str, names: list[str]) -> tuple[list[os.stat_result], bool]:
    """``lstat()`` each of *names* under *root*, plus whether a read was DENIED rather than absent.

    A directory readable but not searchable (0444) lets ``os.walk``'s scandir
    succeed — so ``onerror`` never fires — while every child ``lstat`` raises
    EACCES. Swallowing that alongside the vanished-entry case reports the tree's
    newest mtime as the parent's, so content written seconds ago classifies as
    stale; a denial is a blind spot, not an absence.

    ONLY ``ENOENT`` is an absence. Every other errno — ELOOP, ENAMETOOLONG, EIO,
    a stale NFS handle — is a read this walk could not complete, and scoring it as
    "the entry was not there" is the same absence/blindness conflation one branch
    lower.
    """
    stats = []
    denied = False
    for name in names:
        try:
            stats.append((Path(root) / name).lstat())
        except FileNotFoundError:
            continue
        except OSError:
            denied = True
    return stats, denied
