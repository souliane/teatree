"""Report worktree dirs this venue cannot resolve — and delete none of them (#3912, #3853).

A dir carrying a ``.git`` whose pointer does not resolve here used to be removed
outright, on the reasoning that a broken checkout can hold no recoverable git
work. That reasoning does not survive contact with a second execution context: a
checkout records its admin dir as an absolute path written by whatever context
created it, so one that is perfectly healthy in its own context produces exactly
the evidence this pass read as proof of death. Measured, the misread covered
every registered worktree created in a context other than the sweep's.

So the pass keeps its discovery and loses its deletion. It walks the same
candidates — immediate child dirs of a worktree root that carry a ``.git`` entry
— and reports each one that this venue cannot resolve, as UNKNOWN. Nothing here
may remove a directory, because no evidence available to a single venue can prove
a checkout dead, and the evidence it does have is produced equally by live work.

The two recovery paths the report names are the ones that ask something better
than an unreadable dir: ``t3 <overlay> workspace salvage`` for the work, and
:mod:`teatree.core.worktree.broken_checkout` for a registered row, which decides
from the BRANCH in the source clone.
"""

from pathlib import Path

from teatree.core.worktree.broken_checkout import unresolved_checkout_reason
from teatree.core.worktree.worktree_roots import CheckoutState, probe_checkout


def _candidate_dirs(root: Path) -> list[Path]:
    """Immediate child dirs of *root* that carry a ``.git`` entry (checkouts, live or not)."""
    if not root.is_dir():
        return []
    return sorted(child for child in root.iterdir() if child.is_dir() and (child / ".git").exists())


def report_unresolvable_worktree_dirs(*roots: Path) -> list[str]:
    """Name every candidate under *roots* whose checkout this venue cannot resolve.

    A root is scanned only for its immediate children. Passing several roots is
    how the alternate-root split stays visible: an operator who accumulated
    worktrees outside the canonical :func:`teatree.config.worktree_root` hands
    both roots in and reads one report.

    Read-only by construction — the returned lines are the whole effect, which is
    why a ``clean-all`` preview and a live run cannot disagree about this pass.
    """
    outcomes: list[str] = []
    seen: set[Path] = set()
    for root in roots:
        for candidate in _candidate_dirs(root):
            if candidate in seen:
                continue
            seen.add(candidate)
            outcomes.extend(_report_one(candidate))
    return outcomes


def _report_one(candidate: Path) -> list[str]:
    """One candidate's line, or none when it resolves cleanly.

    A raw dir names no clone — that link exists only for a registered row — so the
    probe runs without one and an unresolvable pointer stays UNKNOWN. Over a pass
    that only reports, the cost of not upgrading is one informational line, which
    is the right way round: the row reaper, which DOES act, is the surface that
    consults the clone.
    """
    if probe_checkout(candidate) is CheckoutState.CHECKOUT:
        return []
    line = (
        f"UNKNOWN worktree dir '{candidate.name}': {unresolved_checkout_reason(candidate)} — "
        "reported, never removed; salvage it with t3 <overlay> workspace salvage"
    )
    return [line]


__all__ = ["report_unresolvable_worktree_dirs"]
