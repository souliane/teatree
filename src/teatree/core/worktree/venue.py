"""Whether a recorded path is reachable HERE — and if not, why, in one word.

``t3`` runs in Docker and nowhere else. A path this process cannot resolve is
therefore either a checkout that was genuinely removed, or a path some *other*
process wrote into the control DB — a host-side registration that had no business
existing. Both arrive as the same ``ENOENT``, and telling them apart is the whole
job of this module: one is an ordinary recovery, the other is a boundary
violation that must be refused loudly rather than crashed into.

Three-valued, because two values is what conflates them:

*   ``PRESENT`` — the path resolves to a directory here. Act on it.
*   ``ABSENT`` — it does not resolve, AND the directory that would contain it is
    readable. Genuine absence; the checkout is gone.
*   ``UNOBSERVABLE`` — the containing directory is unreadable too, so this
    process is looking at a subtree that was never mounted. MISSING EVIDENCE —
    never proof of anything.

Nothing here TRANSLATES a path, and nothing here should ever learn to. A helper
that knew a path's other name would make a host-written path work, and a
boundary that works is a boundary that stays. The only supported repair for an
``UNOBSERVABLE`` path is to re-provision the worktree through the containerized
CLI so the recorded path is one this venue owns; anything the CLI must reach is
mounted deliberately, never reachable by luck.

The reapers (#3912, #3853, #3872) already refuse to delete on ``UNOBSERVABLE``;
:func:`venue_can_observe` is their form of the question, expressed in terms of
:func:`observe` so a reaper and a runner can never disagree about what this
process is entitled to conclude from an unresolvable path.
"""

from enum import StrEnum
from pathlib import Path


class VenueObservation(StrEnum):
    PRESENT = "present"
    ABSENT = "absent"
    UNOBSERVABLE = "unobservable"


def observe(path: Path) -> VenueObservation:
    """What this venue can say about *path* — resolved, absent, or out of reach.

    ``Path.is_dir()`` answers ``False`` for an unreadable parent as readily as for
    a missing one, which is precisely the conflation this function exists to
    undo: the parent is probed separately so an unreadable neighbourhood reports
    ``UNOBSERVABLE`` rather than being mistaken for an empty one.
    """
    if path.is_dir():
        return VenueObservation.PRESENT
    return VenueObservation.ABSENT if path.parent.is_dir() else VenueObservation.UNOBSERVABLE


def venue_can_observe(path: Path, scanned_roots: tuple[Path, ...]) -> bool:
    """Whether this venue could have SEEN *path* had it existed.

    A venue earns the right to call a checkout dead by reading the directory that
    would hold it and finding it absent. Both halves are load-bearing: the path
    must lie under a root this venue walked at all, and :func:`observe` must be
    able to reach that neighbourhood.
    """
    if not any(path.is_relative_to(root) for root in scanned_roots):
        return False
    return observe(path) is not VenueObservation.UNOBSERVABLE


def unusable_path_reason(path: Path, *, subject: str) -> str | None:
    """Why *path* cannot be operated on here, or ``None`` when it resolves.

    The two verdicts need different words because they need different actions. An
    ``ABSENT`` checkout was deleted and can be re-created. An ``UNOBSERVABLE``
    one is the case where the operator can ``ls`` the directory on their own
    machine while this process reports it missing — a path written by a host
    process that should never have registered a worktree. A generic "not found"
    sends them hunting for a deletion that never happened, so the message names
    the real cause and the one supported repair.
    """
    match observe(path):
        case VenueObservation.PRESENT:
            return None
        case VenueObservation.ABSENT:
            return f"{subject} records {path}, which no longer exists — the checkout was removed"
        case VenueObservation.UNOBSERVABLE:
            return (
                f"{subject} records {path}, which is unreachable from this process: not even its "
                f"parent directory is readable here. It is not missing — it was recorded by a "
                f"process outside the container, and `t3` runs only in Docker, so no mount makes "
                f"that path resolve. Re-provision the worktree through the containerized CLI so "
                f"its recorded path is one this venue owns; refusing to continue because the next "
                f"step would tear down containers this worktree can no longer replace."
            )
