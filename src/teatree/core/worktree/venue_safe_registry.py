"""What this venue may conclude from a clone's worktree REGISTRY (#4287).

``git worktree prune`` deregisters every registration whose checkout it cannot
find, and "cannot find" is answered in the READING venue. Pointed at a clone
whose checkouts are not all mounted here — the shared deploy checkout, read from
inside the container — it deletes the admin dir of every checkout that merely
lives elsewhere, and those checkouts then answer ``fatal: not a git repository``.
Measured once on this box: 86 host checkouts deregistered by a single pass, one
of them holding 25 uncommitted files.

The #706 data-loss guard fails the same way in the other direction. A checkout
this venue cannot read shows no work, so a guard answering "no work" for it
authorises the branch delete that drops the last reference to the real one.

Both are absence read as proof, so one predicate answers both —
:func:`venue_may_call_absent_dead` — and both fail CLOSED when it says no.

Its scope is :func:`~teatree.core.worktree.worktree_roots.canonical_worktree_root`
alone, deliberately narrower than the reapers' scanned set: a ``Worktree`` row
recording a path directly under ``$HOME`` puts the whole home directory into that
set, and ``$HOME`` is exactly where the ad-hoc host checkouts this module
protects live. A readable neighbourhood is necessary and nowhere near
sufficient — ``~/wt-4390-factory`` is absent-with-a-readable-parent here and
alive on the host.

``git worktree prune`` takes no per-entry scope, so its gate is all-or-nothing:
one registration this venue cannot vouch for withholds the whole prune, because
a registration is the only thing linking a checkout to its history.
"""

import logging
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from teatree.core.worktree.checkout_liveness import claims_to_be_a_checkout
from teatree.core.worktree.venue import VenueObservation, observe, venue_can_observe
from teatree.core.worktree.worktree_roots import canonical_worktree_root
from teatree.utils import git
from teatree.utils.run import CommandFailedError

logger = logging.getLogger(__name__)

# Enough offenders to recognise the class without turning one refusal into a wall.
_NAMED_IN_REFUSAL = 5


class WorkPresence(StrEnum):
    """Would tearing a checkout down destroy work — and is that knowable HERE?

    ``UNKNOWN`` is what the boolean this replaces could not say: the checkout is
    unreadable in this execution context, so its working tree may be dirty and
    its tip unpushed with nothing here able to see either. Only ``NONE``
    authorises a teardown.
    """

    HOLDS_WORK = "holds-work"
    NONE = "none"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class Registration:
    """One record of ``git worktree list --porcelain`` — what git believes exists."""

    path: str
    branch: str
    locked: bool


def registrations(repo: str) -> list[Registration]:
    """Every worktree git has REGISTERED for *repo*, main checkout included.

    Registration — not the filesystem — is what refuses a ``git worktree add``: a
    branch is "already checked out" while any registration claims it, even one
    whose directory was deleted. Raises rather than parsing empty on a failed
    read, because a clone with no worktrees produces the same bytes and a
    destructive caller must not read one as the other. A detached entry carries
    no ``branch`` line and yields an empty ``branch``.
    """
    entries: list[Registration] = []
    path, branch, locked = "", "", False
    raw = git.run_strict(repo=repo, args=["worktree", "list", "--porcelain"])
    for line in [*raw.splitlines(), "worktree "]:  # trailing sentinel flushes the last record
        if line.startswith("worktree "):
            if path:
                entries.append(Registration(path=path, branch=branch, locked=locked))
            path, branch, locked = line.removeprefix("worktree "), "", False
        elif line.startswith("branch refs/heads/"):
            branch = line.removeprefix("branch refs/heads/")
        elif line == "locked" or line.startswith("locked "):
            locked = True
    return entries


def worktree_map(repo: str) -> dict[str, str]:
    """Return ``{branch_name: worktree_path}`` for active git worktrees."""
    try:
        return {entry.branch: entry.path for entry in registrations(repo) if entry.branch}
    except CommandFailedError:
        return {}


def worktree_branches(repo: str) -> set[str]:
    """Return branch names linked to active git worktrees (safe to skip)."""
    return set(worktree_map(repo))


def venue_may_call_absent_dead(path: Path) -> bool:
    """May this venue read *path*'s absence as a checkout that was deleted?

    Two halves, both required: *path* lies under the root this venue itself
    provisions into, and the directory that would hold it is readable here.
    Absence anywhere else is missing evidence — a checkout mounted only where it
    was created is absent here in the same ``ENOENT`` a deleted one produces.
    """
    return venue_can_observe(path.expanduser(), (canonical_worktree_root().expanduser(),))


def _prune_would_drop(checkout: Path) -> bool:
    """Would ``git worktree prune`` deregister *checkout* in THIS venue?

    Git drops a registration whose directory is gone, and one whose directory no
    longer carries the ``.git`` entry pointing back at its admin dir.
    """
    return observe(checkout) is not VenueObservation.PRESENT or not claims_to_be_a_checkout(checkout)


def unprovable_registrations(repo: str) -> list[str]:
    """*repo*'s registrations a prune here would drop with no proof they are dead.

    A locked registration is excluded because ``git worktree prune`` already
    skips it, which is what makes ``git worktree lock`` the operator's way to
    release a refused prune without moving anything.
    """
    return sorted(
        entry.path
        for entry in registrations(repo)
        if not entry.locked
        and _prune_would_drop(Path(entry.path).expanduser())
        and not venue_may_call_absent_dead(Path(entry.path))
    )


def prune_refusal(repo: str) -> str:
    """Why ``git worktree prune`` must not run against *repo* here; ``""`` when it may."""
    try:
        unprovable = unprovable_registrations(repo)
    except CommandFailedError as exc:
        return f"could not read the worktree registry ({exc}) — a prune of unknown scope is unbounded"
    if not unprovable:
        return ""
    rest = len(unprovable) - _NAMED_IN_REFUSAL
    return (
        f"{len(unprovable)} registration(s) are unreadable in this execution context and lie outside "
        f"{canonical_worktree_root()}, the only root it provisions into, so a prune cannot tell them from "
        f"deleted checkouts and would delete the admin dir their checkouts depend on: "
        f"{', '.join(unprovable[:_NAMED_IN_REFUSAL])}{f' and {rest} more' if rest > 0 else ''}. Prune from "
        f"the venue that owns them, or `git worktree lock` each one so this prune can skip it."
    )


def prune_worktrees(repo: str) -> str:
    """Prune *repo*'s stale registrations, or refuse — returning the refusal, ``""`` on a prune."""
    refusal = prune_refusal(repo)
    if refusal:
        logger.warning("Refusing `git worktree prune` in %s: %s", repo, refusal)
        return refusal
    git.run(repo=repo, args=["worktree", "prune"])
    return ""


def unsalvageable_work_state(wt_path: str) -> WorkPresence:
    """Whether tearing down *wt_path* would destroy the only copy of some work.

    The #706 data-loss guard, mirroring
    :func:`teatree.core.worktree.reconcile._unpushed_work_for_worktree` and the
    ``recover`` sweeps: a worktree is protected when it holds uncommitted
    changes, or when its HEAD carries commits reachable from NO remote. ``--not
    --remotes`` is empty as soon as the tip was pushed anywhere, so a
    pushed-but-unmerged branch is correctly reapable while a genuinely-local tip
    is not.

    **Fails closed twice.** An inconclusive probe (``CommandFailedError`` —
    corrupt repo, dangling ref, no commits yet) reads as ``HOLDS_WORK``, and a
    checkout whose absence this venue may not call death reads as ``UNKNOWN``.
    """
    path = Path(wt_path)
    if not path.is_dir():
        return WorkPresence.NONE if venue_may_call_absent_dead(path) else WorkPresence.UNKNOWN
    if git.status_porcelain(wt_path).strip():
        return WorkPresence.HOLDS_WORK
    try:
        unpushed = git.commits_absent_from_all_remotes(wt_path, "HEAD")
    except CommandFailedError:
        return WorkPresence.HOLDS_WORK
    return WorkPresence.HOLDS_WORK if unpushed else WorkPresence.NONE


__all__ = [
    "Registration",
    "WorkPresence",
    "prune_refusal",
    "prune_worktrees",
    "registrations",
    "unprovable_registrations",
    "unsalvageable_work_state",
    "venue_may_call_absent_dead",
    "worktree_branches",
    "worktree_map",
]
