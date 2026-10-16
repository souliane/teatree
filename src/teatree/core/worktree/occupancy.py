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
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from django.db import transaction
from django.utils import timezone

from teatree.core.claim_liveness import RELEASED_CLAIM
from teatree.core.cleanup.process_table import read_process_table
from teatree.core.modelkit.task_failure_taxonomy import FailureKind
from teatree.core.models import Task, Worktree
from teatree.core.models.checkout_exit import CheckoutExit, CheckoutExitScope, checkout_exit_scope
from teatree.core.models.ticket_worktree_checks import dispatch_worktree_path
from teatree.core.models.worktree_occupancy import (
    OccupancyHolder,
    TenureOptions,
    WorktreeOccupancyLostError,
    WorktreeOccupiedError,
    _occupied_error,
    _worktree_at,
    acquire,
    held_dispatch_checkout,
    occupancy_holder,
    release,
    release_task_occupancy,
    task_holder_id,
    terminal_holder_task_pk,
    terminal_task_pk,
)
from teatree.core.worktree.cancel_processes import wait_for_owned_checkout_processes
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


def _settle_checkout_exit(task: Task, worktree: Worktree, *, attest: bool, wait: bool = True) -> CheckoutExit:
    """Wait for the run's own processes, then say whether its checkout may be released and what it attests."""
    path = Path(worktree.worktree_path)
    if wait and wait_for_owned_checkout_processes(path):
        return _defer_checkout_exit(task, worktree, attest=attest, cause="own_processes")
    table = read_process_table(exclude_ancestors=True)
    if table.usable and table.holds(path):
        return _defer_checkout_exit(task, worktree, attest=attest, cause="process_table")
    if not (attest and table.usable and writes_checkout(task)):
        return CheckoutExit(release=True)
    return CheckoutExit(release=True, attested_state=_exit_digest(task, path))


def _exit_digest(task: Task, path: Path) -> str | None:
    try:
        return checkout_state(path, ticket=task.ticket).digest
    except (OSError, RuntimeError, ValueError):
        logger.exception("Checkout %s after task %s remains unattested: its state could not be read", path, task.pk)
        return None


def _defer_checkout_exit(
    task: Task, worktree: Worktree, *, attest: bool, cause: Literal["own_processes", "process_table"]
) -> CheckoutExit:
    if attest and writes_checkout(task) and (digest := _exit_digest(task, Path(worktree.worktree_path))) is not None:
        worktree.merge_extra(
            set_keys={
                "deferred_exit": {
                    "holder": worktree.occupied_by,
                    "session": worktree.occupied_by_session,
                    "digest": digest,
                    "cause": cause,
                    "at": timezone.now().isoformat(),
                }
            }
        )
    return CheckoutExit(release=False)


def _prepare_task_checkout_exit(
    task: Task, ticket: "Ticket", *, holder: str, holder_session: str
) -> CheckoutExit | None:
    if (holder, holder_session) != (task_holder_id(task), task.claimed_by_session):
        return None
    row = held_dispatch_checkout(ticket, holder=holder, holder_session=holder_session)
    return None if row is None else _settle_checkout_exit(task, row, attest=True)


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
        table = read_process_table(exclude_ancestors=True)
        if not table.usable or table.holds(Path(worktree.worktree_path)):
            return
    release(worktree, holder=current.holder, holder_session=current.holder_session)


def _finish_task_checkout(ticket: "Ticket", *, holder: str, holder_session: str, scope: CheckoutExitScope) -> None:
    row = held_dispatch_checkout(ticket, holder=holder, holder_session=holder_session)
    if row is None:
        return
    pk = holder.removeprefix("task:")
    task = Task.objects.filter(pk=int(pk)).first() if pk.isdigit() else None
    parked = task is not None and task.status == Task.Status.PENDING
    if task is None or task.status == Task.Status.CLAIMED or (parked and not scope.park_attests):
        release(row, holder=holder, holder_session=holder_session)
        return
    cancelled = task.status == Task.Status.FAILED and task.failure_kind == FailureKind.CANCELLED
    settled = scope.settled or _settle_checkout_exit(task, row, attest=parked, wait=not cancelled)
    if not settled.release:
        logger.warning(
            "Keeping checkout %s occupied after task %s: its processes are still live", row.worktree_path, task.pk
        )
        try:
            page_surviving_checkout(task, Path(row.worktree_path), holder_session=holder_session)
        except Exception:
            logger.exception("Could not page owner for surviving checkout process on task %s", task.pk)
        return
    with transaction.atomic():
        if release(row, holder=holder, holder_session=holder_session, attested_state=settled.attested_state):
            Task.objects.filter(pk=task.pk, claimed_by_session=holder_session).update(**RELEASED_CLAIM)


@contextmanager
def _held_checkout(ticket: "Ticket", *, holder: str, holder_session: str) -> Generator[None]:
    with checkout_exit_scope(
        lambda task: _prepare_task_checkout_exit(task, ticket, holder=holder, holder_session=holder_session)
    ) as scope:
        try:
            yield
        finally:
            _finish_task_checkout(ticket, holder=holder, holder_session=holder_session, scope=scope)


@contextmanager
def recording_task_checkout(task: Task) -> Generator[bool]:
    """Settle a ``/loop`` record's checkout like a worker exit; yields whether the task held it."""
    holder, session = task_holder_id(task), task.claimed_by_session
    if held_dispatch_checkout(task.ticket, holder=holder, holder_session=session) is None:
        yield False
        return
    with _held_checkout(task.ticket, holder=holder, holder_session=session):
        yield True


@contextmanager
def occupy_ticket_checkout(
    ticket: "Ticket",
    *,
    holder: str,
    holder_session: str = "",
    lease_seconds: int | None = None,
    namespace: str = "",
) -> Generator[str]:
    """Hold *ticket*'s dispatch checkout for the duration of the block.

    Yields the on-disk path an agent should run in, and settles the claim on exit:
    a surviving process of the run keeps it reserved and pages the owner.

    Yields ``""`` and claims nothing when the ticket has no materialised checkout yet.
    A checkout the run creates is taken by :func:`renew_ticket_checkout` and settled
    by this exit like one held from the start.
    """
    path = dispatch_worktree_path(ticket)
    worktree = _worktree_at(ticket, path) if path else None
    if worktree is not None:
        _release_if_finished_task(worktree)
        acquire(
            worktree,
            holder=holder,
            holder_session=holder_session,
            lease_seconds=lease_seconds,
            tenure=TenureOptions(namespace=namespace),
        )
    with _held_checkout(ticket, holder=holder, holder_session=holder_session):
        yield path


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

    Renews nothing when the ticket has no materialised checkout; takes over one the run created.
    """
    path = dispatch_worktree_path(ticket)
    worktree = _worktree_at(ticket, path) if path else None
    if worktree is None:
        return
    try:
        acquire(
            worktree,
            holder=holder,
            holder_session=holder_session,
            lease_seconds=lease_seconds,
            tenure=TenureOptions(renewal=True),
        )
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
