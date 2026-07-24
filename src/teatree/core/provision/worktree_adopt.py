"""Adopt an on-disk worktree onto a ticket for a follow-up PR (#3327).

One ticket → N PRs is a normal delivery shape core half-acknowledges (#776
taught ``pr create`` to resolve the ship branch from the invoking worktree
*because* a ticket can span several PRs) but could not complete: after PR-A
merges, its ``Worktree`` row is torn down and the ticket sits in a terminal
state, so ``pr create`` for PR-B refused with "ticket has no worktree" — the
one seam a repo that ships small, reviewable PRs hits the moment PR-A lands.

This module is the first-class seam that closes it. Core owns the row shape
(mirrors :func:`teatree.core.intake.resolve._get_or_refresh_worktree`) and the
follow-up terminal-state predicate, so an overlay no longer reaches into core's
ORM with its own adoption plumbing and its own copy of "which states are
terminal". The load-bearing guardrails live here: adoption must never become a
way to re-ship merged work.
"""

from pathlib import Path

from teatree.core.models import Ticket, Worktree
from teatree.core.work_lease import WorkIdentity, register_work_claim
from teatree.core.worktree.worktree_paths import _candidate_paths
from teatree.instance_id import instance_id
from teatree.utils import git

# The terminal states a follow-up adoption must reopen to reach a shippable FSM
# state. Only MERGED/DELIVERED need the dedicated edge: SHIPPED is already a
# legal ``ship()`` source and IN_REVIEW/RETROSPECTED are legal
# ``reconcile_reviewed`` sources (the shipping gate walks them to REVIEWED).
# IGNORED (abandoned) is deliberately absent — an abandoned ticket is not
# reopened for a new PR.
_FOLLOWUP_REOPEN_STATES: frozenset[str] = frozenset(
    {Ticket.State.MERGED, Ticket.State.DELIVERED},
)

_NON_FEATURE_BRANCHES: frozenset[str] = frozenset({"HEAD", "main", "master"})


class WorktreeAdoptError(RuntimeError):
    """Raised when an on-disk worktree cannot be adopted onto a ticket.

    Each guardrail failure (not a git worktree, not on a feature branch, a row
    already claiming this ``(ticket, repo, branch)`` or this path) raises with a
    message ``pr create`` surfaces verbatim, so the caller learns why adoption
    was refused instead of hitting a raw ORM error.
    """


def adopt_worktree_for_ticket(ticket: Ticket, *, cwd: str) -> Worktree:
    """Attach the on-disk git worktree at *cwd* to *ticket* as a new ``Worktree`` row.

    The follow-up-PR seam: after PR-A merged and its row was torn down, the
    fresh branch + on-disk worktree for PR-B has no ``Worktree`` row. This
    creates one so the #776 invoking-branch resolver and the rest of
    ``pr create`` proceed through the managed path.

    Guardrails (each raises :class:`WorktreeAdoptError`):

    - *cwd* must be a git *worktree* — ``.git`` present as a FILE. A main clone
        keeps ``.git`` as a directory and is refused (mirrors the #752 refusal).
    - the checkout must be on a feature branch (not ``HEAD``/``main``/``master``).
    - no ``Worktree`` row may already exist for this ``(ticket, repo, branch)``.
    - no row (any ticket) may already record this on-disk path — two rows
        claiming one directory is the collision core forecloses everywhere.

    The merged-branch refusal is NOT re-implemented here: the #788 hollow-ship
    check in ``pr create`` gates the branch (≥1 commit ahead of base) once the
    row exists, so an already-merged branch is refused before any FSM advance.
    """
    cwd_path = Path(cwd).resolve()
    git_marker = cwd_path / ".git"
    if not git_marker.is_file():
        msg = (
            f"Refusing to adopt {cwd_path}: not a git worktree (its .git is not a file). "
            "Run pr create from the follow-up PR's worktree directory."
        )
        raise WorktreeAdoptError(msg)

    branch = git.current_branch(repo=str(cwd_path))
    if not branch or branch in _NON_FEATURE_BRANCHES:
        msg = f"Refusing to adopt {cwd_path}: not on a feature branch (branch={branch or '<none>'!r})."
        raise WorktreeAdoptError(msg)

    repo_name = cwd_path.name
    if Worktree.objects.filter(ticket=ticket, repo_path=repo_name, branch=branch).exists():
        msg = (
            f"Ticket {ticket.pk} already has a worktree row for repo {repo_name!r} on branch "
            f"{branch!r}; refusing to adopt a duplicate."
        )
        raise WorktreeAdoptError(msg)

    path_owner = Worktree.objects.filter(extra__worktree_path__in=_candidate_paths(str(cwd_path))).first()
    if path_owner is not None:
        msg = (
            f"Worktree #{path_owner.pk} (ticket {path_owner.ticket_id}) already records {cwd_path}; "
            "refusing to adopt a path another row owns."
        )
        raise WorktreeAdoptError(msg)

    worktree = Worktree.objects.create(
        ticket=ticket,
        overlay=ticket.overlay,
        repo_path=repo_name,
        branch=branch,
        extra={"worktree_path": str(cwd_path)},
    )
    # Adoption is one of the two moments raw branch work becomes visible to the
    # lifecycle, so it is where the branch/PR lease is registered (#3561) — the
    # loop then sees this session's work instead of racing it on the same branch.
    register_work_claim(WorkIdentity(repo=repo_name, branch=branch, issue_url=ticket.issue_url), owner=instance_id())
    return worktree


def reopen_ticket_for_followup(ticket: Ticket) -> None:
    """Reopen a terminally-shipped *ticket* to REVIEWED so a follow-up ``ship()`` is legal.

    A no-op unless the ticket sits in a state with no path back to a shippable
    one (:data:`_FOLLOWUP_REOPEN_STATES` — MERGED/DELIVERED). Fired by
    ``pr create --adopt-worktree`` AFTER the #788 hollow-ship guard passes, so
    the merged-branch refusal — not this edge — is what forecloses re-shipping
    already-merged work; this only unblocks a fresh branch with real commits.
    """
    if ticket.state in _FOLLOWUP_REOPEN_STATES:
        ticket.reopen_for_followup()
        ticket.save()
