"""Per-worktree git checks for a ticket (extracted from ticket.py, #1983 split).

Pure helpers over a ``Worktree``'s on-disk git state, used by ``Ticket`` to
decide whether a worktree has shippable commits or uncommitted tracked changes.
Kept at module scope — no DB access, no ``Ticket`` import — so ``ticket.py``
stays under the module-health LOC cap.
"""

from typing import TYPE_CHECKING

from django.apps import apps

from teatree.utils import git
from teatree.utils.run import CommandFailedError

if TYPE_CHECKING:
    from teatree.core.models.ticket import Ticket
    from teatree.core.models.worktree import Worktree


def worktree_has_commits_ahead(worktree: "Worktree") -> bool:
    repo_path = (worktree.extra or {}).get("worktree_path") or worktree.repo_path
    branch = worktree.branch
    if not repo_path or not branch:
        return False
    base = _resolve_base_branch(repo_path)
    try:
        return git.rev_count(repo=repo_path, range_spec=f"{base}..{branch}") > 0
    except (CommandFailedError, ValueError, OSError):
        # Missing path, missing branch, missing git remote — all mean no
        # shippable diff. Fail closed so the auto-FSM stops at REVIEWED.
        return False


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
    except (CommandFailedError, RuntimeError):
        # No origin remote (fresh clones, tests under tmp_path) — fall back to
        # the local default. ``RuntimeError`` covers ``default_branch``'s own
        # "could not detect" path.
        return "main"
