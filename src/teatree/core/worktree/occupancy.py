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
module re-exports them — because a holder's own terminal write (``task_claim.complete_claimed()``,
``task_claim.fail(by_holder=True)``) must release its claim in the SAME transaction as the
status write, and ``core.models`` may not depend on ``core.worktree``
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
:func:`_release_if_finished_task`'s self-heal (#4867): it acts only on a terminal
``Task`` read fresh from the DB whose recorded owner is unrecorded or confirmed dead
(:func:`~teatree.core.models.worktree_occupancy.terminal_task_pk`), never on a claim a
third-party fail kept for a live holder (#4872), so the advisory invariant holds.

Identity is the FULLY-QUALIFIED ``(holder, holder_session)`` pair everywhere —
CAS predicate, release, renewal, reporting. ``holder`` alone is never matched: two
runs of one task in different sessions are genuinely different occupants, and
matching the bare id would let a stale sibling refresh a claim it no longer owns.
"""

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from django.db import transaction

from teatree.core.claim_liveness import RELEASED_CLAIM
from teatree.core.cleanup.process_table import read_process_table
from teatree.core.models import Task, Worktree
from teatree.core.models.ticket_worktree_checks import dispatch_worktree_path
from teatree.core.models.usage_window_state import LIMIT_PARKED_PREFIX
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
from teatree.core.worktree.checkout_fence import writes_checkout
from teatree.core.worktree.checkout_state import checkout_state
from teatree.core.worktree.survivor_notice import page_surviving_checkout

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


def _release_if_finished_task(worktree: Worktree) -> None:
    """Self-heal: release *worktree* if its holder is a finished ``Task`` whose owner is gone (#4867, #4872).

    Covers every completion path that bypasses :func:`release_task_occupancy` (a
    pre-#4867 row, an out-of-process writer) plus the residual TOCTOU between a
    stale read and a new requester's own acquire. Called right before each of the
    two occupancy-consulting entry points acts, so neither refuses a checkout on
    behalf of a holder that can never come back to it.
    """
    current = occupancy_holder(worktree)
    if current is None or terminal_task_pk(current.holder) is None:
        return
    if worktree.get_extra().get("attested_state"):
        table = read_process_table()
        if not table.usable or table.holds(Path(worktree.worktree_path)):
            return
    release(worktree, holder=current.holder, holder_session=current.holder_session)


def _checkpointed_task(worktree: Worktree, holder: str) -> Task | None:
    suffix = holder.removeprefix("task:")
    if suffix == holder or not suffix.isdigit() or worktree.occupied_at is None:
        return None
    task = Task.objects.filter(pk=int(suffix), status=Task.Status.PENDING).first()
    if task is None:
        return None
    attempt = task.attempts.filter(ended_at__gte=worktree.occupied_at).order_by("-pk").first()
    if attempt is None or not attempt.error.startswith(f"{LIMIT_PARKED_PREFIX}deploy checkpoint:"):
        return None
    return task


def _finish_task_checkout(worktree: Worktree, *, holder: str, holder_session: str) -> bool:
    task_pk = terminal_holder_task_pk(holder)
    task = (
        Task.objects.filter(pk=task_pk, claimed_by_session=holder_session).first()
        if task_pk is not None
        else _checkpointed_task(worktree, holder)
    )
    if task is None:
        return False
    task_pk = task.pk
    table = read_process_table()
    path = Path(worktree.worktree_path)
    if table.usable and table.holds(path):
        time.sleep(0.25)
        table = read_process_table()
    if not table.usable or table.holds(path):
        logger.warning(
            "Keeping checkout %s occupied after task %s: process evidence is incomplete or live", path, task_pk
        )
        try:
            page_surviving_checkout(task, path, holder_session=holder_session)
        except Exception:
            logger.exception("Could not page owner for surviving checkout process on task %s", task_pk)
        return True
    state = None
    if writes_checkout(task):
        try:
            state = checkout_state(path)
        except (OSError, RuntimeError, ValueError):
            logger.exception("Keeping checkout %s occupied after task %s: state could not be attested", path, task_pk)
            return True
    with transaction.atomic():
        row = Worktree.objects.select_for_update().get(pk=worktree.pk)
        if row.occupied_by != holder or row.occupied_by_session != holder_session:
            return True
        extra = dict(row.extra or {})
        if state is not None:
            extra["attested_state"] = state.digest
        Worktree.objects.filter(pk=row.pk, occupied_by=holder, occupied_by_session=holder_session).update(
            extra=extra,
            occupied_by="",
            occupied_by_session="",
            occupied_at=None,
            occupancy_expires_at=None,
        )
        Task.objects.filter(pk=task_pk, claimed_by_session=holder_session).update(**RELEASED_CLAIM)
    return True


def finish_recorded_task_checkout(task: Task) -> None:
    path = dispatch_worktree_path(task.ticket)
    worktree = _worktree_at(task.ticket, path) if path else None
    if worktree is not None:
        _finish_task_checkout(worktree, holder=task_holder_id(task), holder_session=task.claimed_by_session)


@contextmanager
def occupy_ticket_checkout(
    ticket: "Ticket",
    *,
    holder: str,
    holder_session: str = "",
    lease_seconds: int | None = None,
) -> Iterator[str]:
    """Hold *ticket*'s dispatch checkout for the duration of the block.

    Yields the on-disk path an agent should run in, and settles the claim on exit.
    An unreadable process table or a surviving writer keeps it reserved for review.

    Yields ``""`` and claims nothing when the ticket has no materialised checkout:
    there is no shared resource yet, so there is nothing to contend on and a
    pre-provision dispatch behaves exactly as it does today.
    """
    path = dispatch_worktree_path(ticket)
    worktree = _worktree_at(ticket, path) if path else None
    if worktree is None:
        yield path
        return

    _release_if_finished_task(worktree)
    acquire(worktree, holder=holder, holder_session=holder_session, lease_seconds=lease_seconds)
    try:
        yield path
    finally:
        if not _finish_task_checkout(worktree, holder=holder, holder_session=holder_session):
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

    Renews nothing when the ticket has no materialised checkout.
    """
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
