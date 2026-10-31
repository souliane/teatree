"""Reclaim abandoned verify-gates and merge-tree run directories."""

import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

from teatree.core.retention.scratch_tree import tree_stats
from teatree.utils.run_scratch import remove_tree

logger = logging.getLogger(__name__)

RUN_SCRATCH_MAX_AGE_SECONDS = 3 * 60 * 60
_RUN_DIR = re.compile(r"t3-(?:verify-gates|merge-tree)-[a-z0-9_]{8}\Z")


@dataclass(frozen=True, slots=True)
class RunScratchSweepResult:
    freed_bytes: int = 0
    removed: int = 0
    refusal: str = ""


def _readable_cwd(pid: Path) -> Path:
    try:
        return (pid / "cwd").readlink()
    except PermissionError:
        return Path()


def _readable_environ(pid: Path) -> bytes:
    try:
        return (pid / "environ").read_bytes()
    except PermissionError:
        return b""


def _is_zombie(pid: Path) -> bool:
    try:
        return (pid / "stat").read_bytes().rpartition(b")")[2].split()[:1] == [b"Z"]
    except OSError:
        return False


def _process_references(proc_root: Path) -> tuple[tuple[tuple[Path, bytes], ...], str]:
    try:
        pids = [entry for entry in proc_root.iterdir() if entry.name.isdigit()]
    except OSError as exc:
        return (), f"process table unreadable: {exc}"
    if not pids:
        return (), "process table has no numeric pids"
    references: list[tuple[Path, bytes]] = []
    for pid in pids:
        try:
            if pid.stat().st_uid != os.geteuid():
                continue
            cmdline = (pid / "cmdline").read_bytes()
            cwd = _readable_cwd(pid)
            environ = _readable_environ(pid)
        except FileNotFoundError:
            if pid.exists() and not _is_zombie(pid):
                return (), f"same-uid pid {pid.name} has an unreadable process reference"
            continue
        except OSError as exc:
            return (), f"same-uid pid {pid.name} has an unreadable process reference: {exc}"
        references.append((cwd, cmdline + b"\0" + environ))
    return tuple(references), ""


def _held(path: Path, references: tuple[tuple[Path, bytes], ...]) -> bool:
    encoded = os.fsencode(path)
    return any(cwd == path or cwd.is_relative_to(path) or encoded in text for cwd, text in references)


def _candidate_size(path: Path, *, cutoff: float, deadline: float | None) -> int | None:
    if not _RUN_DIR.fullmatch(path.name) or path.is_symlink() or not path.is_dir():
        return None
    try:
        if path.stat().st_uid != os.geteuid():
            return None
    except OSError:
        return None
    stats = tree_stats(path, deadline=deadline)
    return stats.size_bytes if stats.newest_mtime is not None and stats.newest_mtime < cutoff else None


def _remove(path: Path) -> bool:
    try:
        remove_tree(path)
    except OSError:
        logger.warning("could not remove run scratch %s", path, exc_info=True)
        return False
    return not path.exists()


def sweep_run_scratch(
    root: Path, *, proc_root: Path = Path("/proc"), deadline: float | None = None
) -> RunScratchSweepResult:
    """Remove only aged, unused owned directories from the two run families."""
    if not root.exists():
        return RunScratchSweepResult()
    try:
        entries = tuple(root.iterdir())
    except OSError as exc:
        return RunScratchSweepResult(refusal=f"scratch root unreadable: {exc}")
    references, refusal = _process_references(proc_root)
    if refusal:
        return RunScratchSweepResult(refusal=refusal)
    cutoff = time.time() - RUN_SCRATCH_MAX_AGE_SECONDS
    freed = 0
    removed = 0
    for path in entries:
        if deadline is not None and time.monotonic() >= deadline:
            return RunScratchSweepResult(freed, removed, "pass budget exhausted")
        size = _candidate_size(path, cutoff=cutoff, deadline=deadline)
        if size is None or _held(path, references):
            continue
        latest, refusal = _process_references(proc_root)
        if refusal:
            return RunScratchSweepResult(freed, removed, refusal)
        if _held(path, latest):
            continue
        if _remove(path):
            freed += size
            removed += 1
    return RunScratchSweepResult(freed, removed)
