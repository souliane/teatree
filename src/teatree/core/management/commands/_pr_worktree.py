"""Worktree resolution + follow-up-PR adoption for ``pr create`` (#3327).

Extracted from ``pr.py`` (module-health LOC cap): the worktree-or-adopt
resolution is a cohesive helper the command composes. ``WorktreeMissingError``
lives here (re-exported by ``pr.py`` so ``pr.WorktreeMissingError`` keeps
resolving) because the resolver returns it.
"""

from pathlib import Path
from typing import TYPE_CHECKING, TypedDict, cast

from teatree.core.invocation_cwd import INVOCATION_CWD_ENV, declared_invocation_cwd, invocation_cwd
from teatree.core.models import Ticket, Worktree
from teatree.core.models.ticket_review_state import has_passed_review
from teatree.core.provision.worktree_adopt import NotAWorktreeError, WorktreeAdoptError, adopt_worktree_for_ticket
from teatree.core.runners.ship import ShipWorktreeAmbiguousError, resolve_and_reconcile_branch, resolve_ship_worktree
from teatree.core.worktree.worktree_paths import _candidate_paths
from teatree.utils import git

if TYPE_CHECKING:
    from teatree.core.models.types import TicketExtra

# A branch the ship must never take its bearings from: detached, or a default.
_NON_INVOKING_BRANCHES: frozenset[str] = frozenset({"HEAD", "main", "master"})


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


def _adopt_invoking_worktree(ticket: Ticket) -> Worktree | WorktreeMissingError:
    """Attach the INVOKING on-disk worktree to *ticket*, or say why it could not be.

    The directory is the DECLARED invocation cwd, falling back to the process cwd when
    nothing declared one (a host-native run, unchanged). The split is
    :func:`~teatree.core.invocation_cwd.declared_invocation_cwd` rather than
    ``invocation_cwd`` because only the absence tells a propagation failure from an
    operator genuinely standing outside a worktree.

    A worktree this ticket already records resolves to that row, so adoption is
    idempotent; otherwise it goes through the guarded core seam and any guardrail
    failure surfaces as the :class:`WorktreeMissingError` contract.
    """
    declared = declared_invocation_cwd()
    cwd = declared if declared is not None else Path.cwd()
    recorded = ticket.worktrees.filter(  # ty: ignore[unresolved-attribute]
        extra__worktree_path__in=_candidate_paths(str(cwd)),
    ).first()
    if recorded is not None:
        return recorded
    try:
        return adopt_worktree_for_ticket(ticket, cwd=str(cwd))
    except NotAWorktreeError as exc:
        return _undeclared_invocation_cwd_error(cwd) if declared is None else WorktreeMissingError(error=str(exc))
    except WorktreeAdoptError as exc:
        return WorktreeMissingError(error=str(exc))


def _resolve_or_adopt_worktree(ticket: Ticket, *, adopt_worktree: bool) -> Worktree | WorktreeMissingError:
    """Return *ticket*'s worktree row, adopting the invoking one for a follow-up PR (#3327).

    ``--adopt-worktree`` means what its help says: the INVOKING on-disk worktree
    is the row, whether or not the ticket already has others. A ticket spanning
    several repos always has others, so short-circuiting on ``first()`` made the
    flag a no-op exactly where it was needed — :func:`_adopt_invoking_worktree`
    owns that path.

    Without the flag: the ticket's first row, else the refusal (naming the
    recovery when apt).
    """
    if adopt_worktree:
        return _adopt_invoking_worktree(ticket)
    worktree = ticket.worktrees.first()  # ty: ignore[unresolved-attribute]
    if worktree is not None:
        return worktree
    return _worktree_missing_error(ticket)


def _invoking_bearings(cwd: str) -> dict[str, str]:
    """Where the operator stands, as the keys :func:`resolve_ship_worktree` reads.

    The checkout ROOT (not the raw cwd) so standing in a subdirectory still names
    the worktree row, and the row's ``extra['worktree_path']`` is that root. Each
    key is omitted rather than blanked when unreadable, so a run from outside any
    checkout leaves the previous invocation's bearings intact.
    """
    bearings: dict[str, str] = {}
    branch = git.current_branch(repo=cwd)
    if branch and branch not in _NON_INVOKING_BRANCHES:
        bearings["ship_invoking_branch"] = branch
    toplevel = git.run(repo=cwd, args=["rev-parse", "--show-toplevel"])
    if toplevel:
        bearings["ship_invoking_path"] = toplevel
    return bearings


def resolve_ship_target(ticket: Ticket, fallback: Worktree) -> Worktree | WorktreeMissingError:
    """Record where the operator stands, then resolve + reconcile the ship's worktree.

    #776: a ticket can span several PRs, so what ShipExecutor must push is the
    INVOKING worktree's current git branch, never the earliest ``worktrees.first()``
    row. The cwd those bearings are read from is the DECLARED one — under the
    containerized ``t3`` the process cwd is the image WORKDIR and carries no
    checkout at all, so ``"."`` recorded nothing and the ship silently fell
    through to another repo's stale row. Both the invoking PATH and its branch are
    recorded, because the canonical ``<workspace>/<branch>/<repo-leaf>`` layout
    puts two repos on the same branch name and only the path tells them apart.
    They persist on ``ticket.extra`` rather than being re-read live, because the
    async ``execute_ship`` worker resolves the same row in a process that never
    saw the operator's cwd. #1587: the recorded branch is reconciled to the
    worktree's actual one before any gate reads it.
    """
    bearings = _invoking_bearings(str(invocation_cwd()))
    if bearings:
        ticket.merge_extra(set_keys=cast("TicketExtra", bearings))
    try:
        ship_worktree = resolve_ship_worktree(ticket, cast("TicketExtra", ticket.extra or {})) or fallback
    except ShipWorktreeAmbiguousError as exc:
        return WorktreeMissingError(error=str(exc))
    repo_path = (ship_worktree.extra or {}).get("worktree_path", "") or ship_worktree.repo_path
    if repo_path:
        resolve_and_reconcile_branch(ticket, ship_worktree, repo_path)
    return ship_worktree
