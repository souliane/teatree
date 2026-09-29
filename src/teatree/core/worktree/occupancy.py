"""Advisory occupancy claim over a checkout — one agent at a time (#3952).

#3903 deduped the ``Task`` seam: two coding Tasks can no longer exist for one
ticket+phase. That does not stop two agents that already HOLD checkouts, and the
autonomous posture makes exactly that routine — the loop mints its own work while
operator-dispatched lanes are live, and both resolve one ticket to one worktree
path. Observed on a single branch in one window: a factory agent committed the
other agent's uncommitted edits under its own commit, a ``git add -A`` staged the
other's in-progress files, and an unpushed local merge was left behind mid-
verification. The trees happened to agree; two agents editing one file would have
lost work silently.

The worktree is the contended resource, so the claim lives on the ``Worktree``
row. The CAS primitives (:func:`acquire`, :func:`release`, :func:`occupancy_holder`,
:func:`task_holder_id`) live in :mod:`teatree.core.models.worktree_occupancy` — this
module re-exports them — because the two REAL terminal-status writers
(``task_claim.complete_claimed()``, ``Task.fail()``) must release a claim in the SAME
transaction as their status write, and ``core.models`` may not depend on ``core.worktree``
(``tach``'s DAG). This module owns the TICKET-level orchestration on top: resolving a
ticket to its checkout's ``Worktree`` row and holding/refusing/renewing that claim for a
dispatch's whole run.

**Advisory, and only advisory.** A refused requester is TOLD who holds the
checkout; nothing here deletes, evicts, reaps, kills or force-releases anything,
and no other code path is given a deletion signal to read. That constraint is the
ticket's, and it is not stylistic: this repo has already reaped live work by
inferring absence from one execution context. A lapsed lease therefore grants the
NEXT requester without touching the previous holder's process, files or branch —
the previous holder discovers the loss on its own :func:`renew_ticket_checkout`
and aborts, the same shape ``Task.renew_lease`` has today. The one exception is
:func:`_release_if_finished_task`'s self-heal (#4867): it only ever acts on a
DEFINITIVE terminal ``Task`` status read fresh from the DB, never an inferred
absence, so the advisory invariant holds.

Identity is the FULLY-QUALIFIED ``(holder, holder_session)`` pair everywhere —
CAS predicate, release, renewal, reporting. ``holder`` alone is never matched: two
runs of one task in different sessions are genuinely different occupants, and
matching the bare id would let a stale sibling refresh a claim it no longer owns.
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING

from teatree.core.models import Worktree
from teatree.core.models.ticket_worktree_checks import dispatch_worktree_path
from teatree.core.models.worktree_occupancy import (
    OccupancyHolder,
    WorktreeOccupancyLostError,
    WorktreeOccupiedError,
    _occupied_error,
    _worktree_at,
    acquire,
    occupancy_holder,
    release,
    release_task_occupancy,
    task_holder_id,
    terminal_holder_task_pk,
    terminal_task_pk,
)

if TYPE_CHECKING:
    from teatree.core.models.ticket import Ticket

__all__ = [
    "OccupancyHolder",
    "WorktreeOccupancyLostError",
    "WorktreeOccupiedError",
    "acquire",
    "held_worktrees",
    "occupancy_holder",
    "occupy_ticket_checkout",
    "refuse_if_ticket_checkout_occupied",
    "release",
    "release_task_occupancy",
    "renew_ticket_checkout",
    "task_holder_id",
    "terminal_holder_task_pk",
    "terminal_task_pk",
]

logger = logging.getLogger(__name__)


def _gate_enabled() -> bool:
    """Whether the occupancy gate refuses a second requester (the never-lockout kill switch)."""
    from teatree.config import get_effective_settings  # noqa: PLC0415 — deferred: keeps this leaf import-light

    return bool(get_effective_settings().worktree_occupancy_gate_enabled)


def _release_if_finished_task(worktree: Worktree) -> None:
    """Self-heal: release *worktree* if its holder is a ``Task`` that already finished (#4867).

    Covers every completion path that bypasses :func:`release_task_occupancy` (a
    pre-#4867 row, an out-of-process writer) plus the residual TOCTOU between a
    stale read and a new requester's own acquire. Called right before each of the
    two occupancy-consulting entry points acts, so neither refuses a checkout on
    behalf of a holder that can never come back to it.
    """
    current = occupancy_holder(worktree)
    if current is None or terminal_task_pk(current.holder) is None:
        return
    release(worktree, holder=current.holder, holder_session=current.holder_session)


@contextmanager
def occupy_ticket_checkout(
    ticket: "Ticket",
    *,
    holder: str,
    holder_session: str = "",
    lease_seconds: int | None = None,
    enabled: bool | None = None,
) -> Iterator[str]:
    """Hold *ticket*'s dispatch checkout for the duration of the block.

    Yields the on-disk path an agent should run in, and releases the claim on the
    way out — including when the body raises, so a crashed run frees the checkout
    at once instead of making the next requester wait out the TTL.

    Yields ``""`` and claims nothing when the ticket has no materialised checkout:
    there is no shared resource yet, so there is nothing to contend on and a
    pre-provision dispatch behaves exactly as it does today. Same when the gate is
    switched off — the kill switch hands the path out ungated rather than
    pretending the claim succeeded.
    """
    path = dispatch_worktree_path(ticket)
    worktree = _worktree_at(ticket, path) if path else None
    if worktree is None or not (_gate_enabled() if enabled is None else enabled):
        yield path
        return

    _release_if_finished_task(worktree)
    acquire(worktree, holder=holder, holder_session=holder_session, lease_seconds=lease_seconds)
    try:
        yield path
    finally:
        release(worktree, holder=holder, holder_session=holder_session)


def renew_ticket_checkout(
    ticket: "Ticket",
    *,
    holder: str,
    holder_session: str = "",
    lease_seconds: int | None = None,
) -> None:
    """Heartbeat this holder's claim on *ticket*'s checkout, or report that it moved on.

    Re-runs :func:`acquire`, which is idempotent for the same ``(holder,
    holder_session)`` and REPAIRS a claim that lapsed while still unclaimed —
    a starved heartbeat that let its own TTL slip must re-take the tree it is
    still writing to, not leave it advertised as free. A rival holding the
    checkout is the loss the caller has to abort on.

    Renews nothing when the ticket has no materialised checkout or when the gate
    is off: the heartbeat must never mint a claim the dispatch itself did not take.
    """
    if not _gate_enabled():
        return
    path = dispatch_worktree_path(ticket)
    worktree = _worktree_at(ticket, path) if path else None
    if worktree is None:
        return
    try:
        acquire(worktree, holder=holder, holder_session=holder_session, lease_seconds=lease_seconds)
    except WorktreeOccupiedError as exc:
        raise WorktreeOccupancyLostError(str(exc)) from exc


def refuse_if_ticket_checkout_occupied(ticket: "Ticket") -> None:
    """Refuse when a live agent already occupies *ticket*'s checkout (#3952).

    The read-only half of the chokepoint, for a caller that HANDS BACK a checkout
    without holding it for a bounded run — ``workspace ticket`` re-resolving a
    ticket whose worktree already exists. It takes no claim of its own (the
    command exits, so a claim it took would outlive it as a phantom holder) and
    it changes nothing: the whole effect is telling the second requester who is
    already in the tree instead of pointing them into it.
    """
    if not _gate_enabled():
        return
    path = dispatch_worktree_path(ticket)
    worktree = _worktree_at(ticket, path) if path else None
    if worktree is None:
        return
    _release_if_finished_task(worktree)
    if occupancy_holder(worktree) is not None:
        raise _occupied_error(worktree)


def held_worktrees() -> list[tuple[Worktree, OccupancyHolder]]:
    """Every worktree under a LIVE occupancy claim, for the doctor's report."""
    pairs = (
        (worktree, occupancy_holder(worktree)) for worktree in Worktree.objects.exclude(occupied_by="").order_by("pk")
    )
    return [(worktree, holder) for worktree, holder in pairs if holder is not None]
