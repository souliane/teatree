"""Worktree resolution + follow-up-PR adoption for ``pr create`` (#3327).

Extracted from ``pr.py`` (module-health LOC cap): the worktree-or-adopt
resolution is a cohesive helper the command composes. ``WorktreeMissingError``
lives here (re-exported by ``pr.py`` so ``pr.WorktreeMissingError`` keeps
resolving) because the resolver returns it.
"""

from pathlib import Path
from typing import TypedDict

from teatree.core.invocation_cwd import INVOCATION_CWD_ENV, declared_invocation_cwd
from teatree.core.models import Ticket, Worktree
from teatree.core.models.ticket_review_state import has_passed_review
from teatree.core.provision.worktree_adopt import NotAWorktreeError, WorktreeAdoptError, adopt_worktree_for_ticket


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


def _undeclared_invocation_cwd_error(process_cwd: Path) -> WorktreeMissingError:
    """Blame the lost propagation, not the directory it degraded to (#4281).

    ``deploy/t3`` starts the CLI in the image WORKDIR, so an undeclared
    invocation cwd refuses a directory the operator never stood in — and "not a
    git worktree" then sends them auditing that path instead of the variable
    that failed to cross the container boundary.
    """
    return WorktreeMissingError(
        error=(
            f"Refusing to adopt: {INVOCATION_CWD_ENV} is unset, so adoption fell back to the "
            f"process cwd {process_cwd}, which is not a git worktree. Under `deploy/t3` that is "
            "the container's WORKDIR rather than where you stood — run pr create from the "
            f"follow-up PR's worktree, or export {INVOCATION_CWD_ENV} to its container-side path."
        ),
    )


def _resolve_or_adopt_worktree(ticket: Ticket, *, adopt_worktree: bool) -> Worktree | WorktreeMissingError:
    """Return *ticket*'s worktree row, adopting the invoking one for a follow-up PR (#3327).

    The ticket's first row when one exists. Otherwise: without ``--adopt-worktree``
    the refusal (naming the recovery when apt); with it, the invoking on-disk
    worktree is attached as a new row through the guarded core seam, and any
    guardrail failure surfaces as the same :class:`WorktreeMissingError` contract.

    ``--adopt-worktree``'s whole contract is "resolve from where I am", so the
    directory is the DECLARED invocation cwd, falling back to the process cwd
    when nothing declared one (a host-native run, unchanged). The split is
    :func:`~teatree.core.invocation_cwd.declared_invocation_cwd` rather than
    ``invocation_cwd`` because only the absence tells a propagation failure from
    an operator genuinely standing outside a worktree.
    """
    worktree = ticket.worktrees.first()  # ty: ignore[unresolved-attribute]
    if worktree is not None:
        return worktree
    if not adopt_worktree:
        return _worktree_missing_error(ticket)
    declared = declared_invocation_cwd()
    cwd = declared if declared is not None else Path.cwd()
    try:
        return adopt_worktree_for_ticket(ticket, cwd=str(cwd))
    except NotAWorktreeError as exc:
        if declared is None:
            return _undeclared_invocation_cwd_error(cwd)
        return WorktreeMissingError(error=str(exc))
    except WorktreeAdoptError as exc:
        return WorktreeMissingError(error=str(exc))
