"""Worktree resolution + follow-up-PR adoption for ``pr create`` (#3327).

Extracted from ``pr.py`` (module-health LOC cap): the worktree-or-adopt
resolution is a cohesive helper the command composes. ``WorktreeMissingError``
lives here (re-exported by ``pr.py`` so ``pr.WorktreeMissingError`` keeps
resolving) because the resolver returns it.
"""

from typing import TypedDict

from teatree.core.models import Ticket, Worktree
from teatree.core.models.ticket_review_state import has_passed_review
from teatree.core.provision.worktree_adopt import WorktreeAdoptError, adopt_worktree_for_ticket


class WorktreeMissingError(TypedDict):
    error: str


def _worktree_missing_error(ticket: Ticket) -> WorktreeMissingError:
    """Refuse a ship with no worktree row — naming the follow-up recovery when apt.

    A ticket that already passed review but whose ``Worktree`` row was torn down
    (the follow-up-PR-on-a-terminal-ticket case, #3327) is told to adopt the
    current on-disk worktree with ``--adopt-worktree``. A never-provisioned
    ticket gets the plain refusal — adoption is not the right fix there, a proper
    ``workspace ticket`` provision is.
    """
    if has_passed_review(ticket):
        return WorktreeMissingError(
            error=(
                "ticket has no worktree — its prior PR's row was torn down. Pass "
                "--adopt-worktree to attach the current on-disk worktree for a follow-up PR."
            ),
        )
    return WorktreeMissingError(error="ticket has no worktree")


def _resolve_or_adopt_worktree(ticket: Ticket, *, adopt_worktree: bool) -> Worktree | WorktreeMissingError:
    """Return *ticket*'s worktree row, adopting the invoking one for a follow-up PR (#3327).

    The ticket's first row when one exists. Otherwise: without ``--adopt-worktree``
    the refusal (naming the recovery when apt); with it, the invoking on-disk
    worktree is attached as a new row through the guarded core seam, and any
    guardrail failure surfaces as the same :class:`WorktreeMissingError` contract.
    """
    worktree = ticket.worktrees.first()  # ty: ignore[unresolved-attribute]
    if worktree is not None:
        return worktree
    if not adopt_worktree:
        return _worktree_missing_error(ticket)
    try:
        return adopt_worktree_for_ticket(ticket, cwd=".")
    except WorktreeAdoptError as exc:
        return WorktreeMissingError(error=str(exc))
