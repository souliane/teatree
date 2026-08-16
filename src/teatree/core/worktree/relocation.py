"""Whether ``workspace relocate`` would move a registered worktree — and why not (#4368).

ONE refusal policy, consulted by two callers that must never disagree: the
``workspace relocate`` engine, which acts on it, and the ``t3 doctor``
canonical-root check, which prescribes ``relocate`` only for the worktrees this
reports movable. A doctor that counts a worktree relocate refuses forever
prescribes a remedy that cannot discharge its own finding, so the WARN recurs on
every run with nothing an operator can do about it.

The structural refusal is the MOUNT-POINT boundary. ``git worktree move`` is a
``rename(2)``, which returns ``EXDEV`` between mount points even when both sides
report the same ``st_dev`` — two bind mounts of one filesystem do exactly that, so
a device-keyed guard calls the move safe and it fails anyway. It is checked BEFORE
the transient refusals (dirty, busy, locked) because it is the only one no
operator action can ever clear: reporting "uncommitted changes" for a worktree
that is ALSO across a boundary invites a commit-and-retry that cannot succeed.

Safety doctrine (unchanged): a worktree is refused when it is git-locked, dirty,
or live mid-task (its ticket has a live session/active task, or the CWD is inside
it). A ``git status`` that errors is treated as "might be dirty" and keeps the
worktree — a flaky probe must never strand a live edit.

A recorded path missing from disk is not automatically stale: when its target
already exists as a git worktree, a PRIOR relocate moved it and only the DB save
failed (:func:`half_move_target`). ``workspace relocate`` heals that row by
reconcile rather than a fresh move, so it must not be counted as a refusal here —
a doctor that named it un-relocatable would withhold the one command that fixes it.
"""

from dataclasses import dataclass
from pathlib import Path

from teatree.core.models import Worktree
from teatree.core.worktree.clone_paths import find_clone_path
from teatree.utils import git
from teatree.utils.git_worktree_query import canonical_repo_root
from teatree.utils.mount_points import mount_boundary_between
from teatree.utils.run import CommandFailedError


@dataclass(frozen=True)
class RelocationCandidate:
    """One worktree row resolved for relocation: the row, its on-disk paths, its clone."""

    worktree: Worktree
    old: Path
    old_resolved: Path
    clone: str | None

    @classmethod
    def of(cls, worktree: Worktree, old: Path) -> "RelocationCandidate":
        return cls(worktree=worktree, old=old, old_resolved=old.resolve(), clone=resolve_source_clone(worktree, old))


def relocation_target(old: Path, target_root: Path) -> Path:
    """Where *old* lands under *target_root* — ``<target_root>/<branch-dir>/<repo-dir>``."""
    return target_root / old.parent.name / old.name


def active_cwd() -> Path | None:
    # Residual gap (acknowledged): this only sees THIS process's cwd, so a
    # concurrent agent process whose cwd is inside the worktree is not caught
    # here — the session/task liveness check covers that real agent case, so a
    # live mid-task worktree is still refused.
    try:
        return Path.cwd().resolve()
    except OSError:
        return None


def resolve_source_clone(worktree: Worktree, old: Path) -> str | None:
    """The source clone ``git worktree move`` runs from (NOT *old* itself).

    Three tiers, most authoritative first:

    1.  the provision-time ``extra['clone_path']``;
    2.  ``git rev-parse --git-common-dir`` read from the checkout itself
        (:func:`canonical_repo_root`) — git's own answer, true for ANY layout;
    3.  a scan of the OLD workspace root, kept as the last resort for a row whose
        directory is gone from disk (git cannot be asked about a dir that is not
        there).

    Tier 2 is what makes relocate work on the worktrees that most need relocating.
    Tier 3 assumes the canonical ``<old_ws>/<branch>/<repo>`` layout — it derives
    the workspace root as ``old.parent.parent`` and looks for a clone named
    ``repo_path`` under it — so on a NON-canonical path (exactly the case relocate
    exists to repair) it scanned the wrong directory for the wrong name and
    reported "source clone not found", skipping the worktree forever. Measured on
    an ``<root>/<repo>/<branch>`` pair: both were refused, while
    ``git rev-parse --git-common-dir`` named the clone correctly from inside each.
    """
    stored = (worktree.extra or {}).get("clone_path")
    if stored:
        return str(stored)
    from_git = canonical_repo_root(old)
    if from_git is not None:
        return str(from_git)
    found = find_clone_path(old.parent.parent, worktree.repo_path)
    return str(found) if found is not None else None


def relocation_refusal(candidate: RelocationCandidate, target_root: Path, *, active_path: Path | None) -> str | None:
    """The reason this worktree must NOT be moved, or ``None`` when it is movable.

    Structural refusals come first, so the reported reason is the one that decides
    whether relocation is possible AT ALL rather than merely blocked today. A
    recorded path already gone from disk short-circuits everything below it: either
    its target already exists as a git worktree — a prior run moved it on disk and
    only the DB save failed, which ``workspace relocate`` heals as a reconcile
    rather than a fresh ``git worktree move`` — or it is a genuinely stale row,
    refused outright. Neither disposition has anything left at the old path to
    check for "dirty" or "locked", so both skip the checks below.
    """
    target_root_resolved = target_root.resolve()
    old_resolved = candidate.old_resolved
    if target_root_resolved == old_resolved or target_root_resolved in old_resolved.parents:
        return f"already under {target_root_resolved}"
    if not candidate.old.exists():
        if half_move_target(candidate.old, target_root_resolved) is not None:
            return None
        return "worktree path missing on disk (stale row)"
    structural = _structural_refusal(candidate, target_root_resolved)
    if structural is not None:
        return structural
    return _transient_refusal(candidate, active_path=active_path)


def half_move_target(old: Path, target_root_resolved: Path) -> Path | None:
    """Where *old* already landed, or ``None`` when it did not.

    A worktree row records ``<old_ws>/<branch>/<repo>``; its post-move home is
    ``<target_root>/<branch>/<repo>``. When *old* is gone from disk but that target
    exists AS a git worktree (a ``.git`` entry), a prior run moved it on disk + git
    but the DB save threw — the row is reconcilable, not stale, and the caller
    (``workspace relocate``'s half-move healer, and this module's own
    :func:`relocation_refusal`) must treat it as such. Pure check: no DB write, no
    filesystem mutation.
    """
    target = relocation_target(old, target_root_resolved)
    return target if (target / ".git").exists() else None


def _structural_refusal(candidate: RelocationCandidate, target_root_resolved: Path) -> str | None:
    """A refusal no operator action can clear: no clone, mount-point boundary.

    Only reached once *candidate.old* is confirmed to exist on disk — the
    already-under and missing-on-disk/half-move dispositions are decided in
    :func:`relocation_refusal` before this ever runs.
    """
    if candidate.clone is None:
        return "source clone not found"
    old_resolved = candidate.old_resolved
    boundary = mount_boundary_between(old_resolved, relocation_target(old_resolved, target_root_resolved))
    if boundary is None:
        return None
    source_mount, target_mount = boundary
    return (
        f"mount-point boundary {source_mount} -> {target_mount}: git worktree move is a rename(2), "
        "which returns EXDEV between mount points even on one device"
    )


def _transient_refusal(candidate: RelocationCandidate, *, active_path: Path | None) -> str | None:
    """A refusal that clears once the worktree is idle and committed."""
    if _is_active_cwd(candidate.old_resolved, active_path):
        return "active worktree (current working directory)"
    if candidate.worktree.ticket.has_active_work():
        return "ticket has a live session or active/claimed task"
    if candidate.old_resolved in {Path(path) for path in git.locked_worktree_paths(str(candidate.clone))}:
        return "git-locked"
    return _dirty_reason(candidate.old)


def _is_active_cwd(old_resolved: Path, active_path: Path | None) -> bool:
    """True iff *active_path* is the worktree's own dir or a child of it."""
    if active_path is None:
        return False
    return active_path == old_resolved or old_resolved in active_path.parents


def _dirty_reason(old: Path) -> str | None:
    """The refusal for a dirty / undeterminable worktree, or ``None`` when clean.

    Fail-closed: a ``git status`` error keeps the worktree (treated as "might be
    dirty") so a flaky probe can't strand a live edit.
    """
    try:
        dirty = bool(git.status_porcelain_strict(str(old)).strip())
    except CommandFailedError:
        return "could not determine git status (kept)"
    return "uncommitted changes" if dirty else None


__all__ = [
    "RelocationCandidate",
    "active_cwd",
    "half_move_target",
    "relocation_refusal",
    "relocation_target",
    "resolve_source_clone",
]
