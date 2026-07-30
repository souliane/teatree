"""Per-worktree git checks for a ticket (extracted from ticket.py, #1983 split).

Pure helpers over a ``Worktree``'s on-disk git state, used by ``Ticket`` to
decide whether a worktree has shippable commits or uncommitted tracked changes.
Kept at module scope — no DB access, no ``Ticket`` import — so ``ticket.py``
stays under the module-health LOC cap.
"""

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from django.apps import apps

from teatree.utils import git
from teatree.utils.run import CommandFailedError

if TYPE_CHECKING:
    from teatree.core.models.ticket import Ticket
    from teatree.core.models.worktree import Worktree

logger = logging.getLogger(__name__)


class WorktreeProbeUnverifiableError(RuntimeError):
    """A worktree git probe could not be verified — the result is UNKNOWN, not a verdict.

    Raised by :func:`worktree_has_commits_ahead` when a PRESENT on-disk checkout's
    commit-count probe fails (a transient git error, a lock, a corrupt ref). The
    old code swallowed such a failure to ``False``, which the shippable-diff path
    reads as "no diff" and routes ``review() → dispose_unshippable_review() →
    ticket.ignore()`` — terminally ABANDONING a live ticket on a transient git
    failure (#F1.4). A distinct typed error lets a caller tell "could not verify"
    apart from "verified: nothing to ship" and HOLD/skip the tick (retry later)
    rather than dispose. A genuinely-missing path/branch is NOT this error — that
    is an honest ``False`` (nothing on disk to ship).
    """


def dispatch_worktree_path(ticket: "Ticket") -> str:
    """On-disk worktree path a dispatched agent runs in for *ticket*, or ''.

    The PR-12 dispatch-preflight detection root: skill + overlay resolution
    keys on this so a dispatch reads the ticket's OWN checkout, not the
    orchestrator's ambient cwd (the loop's clone). Returns the first
    materialised worktree whose recorded path still exists on disk (ordered by
    pk for determinism); '' before provisioning or when every recorded path is
    gone, so the caller falls back to the ambient cwd. Lives here (not on
    ``Ticket``) so ``ticket.py`` stays under its module-health LOC cap.
    """
    worktree_model = apps.get_model("core", "Worktree")
    for worktree in worktree_model.objects.filter(ticket=ticket).order_by("pk"):
        path = worktree.worktree_path
        if path and Path(path).is_dir():
            return path
    return ""


def worktree_has_commits_ahead(worktree: "Worktree") -> bool:
    """Whether ``worktree`` has commits ahead of its base branch.

    Returns ``False`` for the genuinely-nothing-to-ship cases — no recorded path
    or branch, or a recorded path that is no longer a directory on disk (the
    checkout is gone, so there is provably nothing to lose).

    Raises :class:`WorktreeProbeUnverifiableError` when a PRESENT on-disk checkout
    is there but its commit-count probe FAILS (a transient git error, a lock, a
    corrupt ref). That case must NOT be flattened to ``False``: the shippable-diff
    path reads ``False`` as "no diff" and terminally ``ticket.ignore()``s the
    ticket, so a transient git failure would abandon live work (#F1.4). The typed
    error lets the caller HOLD/skip the tick and retry, never dispose on
    uncertainty.
    """
    repo_path = (worktree.extra or {}).get("worktree_path") or worktree.repo_path
    branch = worktree.branch
    if not repo_path or not branch:
        return False
    if not Path(repo_path).is_dir():
        # The recorded checkout is gone from disk — genuinely nothing to ship,
        # NOT a probe failure. Honest False (safe to dispose).
        return False
    base = _resolve_base_branch(repo_path)
    try:
        return git.rev_count(repo=repo_path, range_spec=f"{base}..{branch}") > 0
    except (CommandFailedError, ValueError, OSError) as exc:
        # A PRESENT checkout whose commit-count probe could not be verified. Do
        # NOT return False (which routes review() → dispose_unshippable_review()
        # → ticket.ignore(), abandoning a live ticket on a transient git error).
        # Surface it typed so the caller holds/skips the tick.
        logger.warning(
            "worktree_has_commits_ahead: could not verify commits ahead for %s (%s..%s): %s",
            repo_path,
            base,
            branch,
            exc,
        )
        msg = f"could not verify commits ahead for {repo_path} ({base}..{branch})"
        raise WorktreeProbeUnverifiableError(msg) from exc


def worktree_tracked_dirty_path(worktree: "Worktree") -> str | None:
    """Return the worktree's on-disk path iff it has uncommitted *tracked* changes.

    Reuses the existing :func:`git.status_porcelain` helper (the same one
    ``cleanup`` uses) and applies the #925
    tracked-vs-untracked distinction: ``git status --porcelain`` prefixes
    an untracked entry with ``"?? "``, so lines that do *not* start with
    ``??`` are the tracked modifications a transition must refuse. Untracked
    scratch never blocks (the loop legitimately leaves it around, and a
    fast-forward never conflicts with it).

    Path resolution mirrors :func:`worktree_has_commits_ahead`
    (``extra['worktree_path']`` then ``repo_path``). An unresolvable or
    non-git path returns ``None`` (not dirty): the guard must not block on
    a state it cannot verify — "couldn't determine" is not "is dirty", and
    over-blocking a legitimately-clean ticket would stall the loop.
    """
    repo_path = (worktree.extra or {}).get("worktree_path") or worktree.repo_path
    if not repo_path:
        return None
    try:
        porcelain = git.status_porcelain(repo_path)
    except (CommandFailedError, OSError):
        return None
    tracked_dirty = any(line and not line.startswith("??") for line in porcelain.splitlines())
    return repo_path if tracked_dirty else None


def collect_dirty_worktree_paths(ticket: "Ticket") -> list[str]:
    """Return the on-disk paths of every ``ticket`` worktree with uncommitted tracked changes.

    Backs ``Ticket._refuse_if_worktree_dirty`` (#884 preflight, moved out here
    in the #1983 LOC-ratchet split): a worktree with uncommitted *tracked*
    changes must not let its ticket's FSM advance — the agent has to commit
    or discard first. We do NOT auto-stash: teatree worktrees share one
    ``.git`` so a stash is repo-global and would clobber an unrelated
    branch's work (the foreign-stash hazard, near-miss class #806).

    Untracked-only files do not block (the #925 distinction): a
    fast-forward never conflicts with untracked scratch, and the loop
    legitimately leaves scratch files around — only a tracked modification
    is the refusal trigger, via :func:`worktree_tracked_dirty_path`.
    """
    worktree_model = apps.get_model("core", "Worktree")
    return [
        path
        for wt in worktree_model.objects.filter(ticket=ticket)
        if (path := worktree_tracked_dirty_path(wt)) is not None
    ]


def _resolve_base_branch(repo_path: str) -> str:
    try:
        return f"origin/{git.default_branch(repo_path)}"
    except (CommandFailedError, RuntimeError) as exc:
        # No origin remote (fresh clones, tests under tmp_path) — fall back to
        # the local default. ``RuntimeError`` covers ``default_branch``'s own
        # "could not detect" path. Log which fallback fired so a silent "main"
        # base (which changes what the commits-ahead probe measures against) is
        # observable instead of an invisible default (#F1.4).
        logger.debug(
            "_resolve_base_branch: could not resolve origin default for %s (%s) — falling back to local 'main'",
            repo_path,
            exc,
        )
        return "main"
