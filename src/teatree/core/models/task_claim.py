"""Atomic claim + lease-renewal over ``Task`` — the #786 compare-and-swap shape.

The per-instance claim/lease half of the task lifecycle: :func:`claim` takes an
existing row (a fresh PENDING task or a reclaimable expired-lease orphan) and
:func:`renew_lease` heartbeats this worker's live claim. Both are backend-agnostic
conditional ``UPDATE`` compare-and-swaps — never a read-then-write — so they stay
correct on the production SQLite backend where ``select_for_update`` is a silent
no-op. Split out of ``task.py`` (which is at its module-health LOC cap) — the thin
``Task`` methods delegate here. The functions take a ``Task`` and reach the model
class through the instance, so this module needs no runtime import of ``Task`` and
stays cycle-free (task.py imports it at module level).
"""

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from django.db.models import Q
from django.utils import timezone

from teatree.core.models.errors import InvalidTransitionError, LeaseLostError

if TYPE_CHECKING:
    from teatree.core.models.task import Task


def window_parked(task: "Task", now: datetime | None = None) -> bool:
    """Whether *task* sits behind an unelapsed usage-window park gate (Directive #3).

    The single-row twin of :func:`~teatree.core.managers_task_claim._claimable_now_q`, for
    the seams that hold ONE instance rather than a queryset (the ``post_save`` headless
    auto-enqueue, the lost-claim diagnosis below). Kept as one expression both spellings
    share so "is there work" and "may this be re-dispatched" can never disagree.
    """
    return task.not_before is not None and task.not_before > (now or timezone.now())


def claim(task: "Task", *, claimed_by: str, claimed_by_session: str = "", lease_seconds: int = 300) -> None:
    """Atomically claim *task* — exactly one concurrent claimer wins (#786 shape).

    A single guarded conditional ``UPDATE ... WHERE pk=task AND <claimable>``
    whose affected-row count is the compare-and-swap token — NOT a
    read-then-write. The previous shape (``select_for_update().get()`` then
    an unconditional ``save()``) raced on the production SQLite backend:
    ``has_select_for_update`` is ``False`` there, so ``select_for_update``
    is a silent no-op (the #786 B1 lesson the sibling ``claim_next_pending``
    / ``reap_stale_claims`` / ``LoopLease.acquire`` paths already heed). Two
    concurrent sessions both passed the in-Python guard on the same stale
    view and both wrote, each believing it owned the task — so two sessions
    worked the same unit. The conditional UPDATE re-evaluates ``<claimable>``
    at write time and is atomic on SQLite AND Postgres: exactly one writer
    matches one row, the loser updates zero.

    ``<claimable>`` is PENDING, or CLAIMED with an absent/expired lease (a
    dead owner's orphan — reclaimable). A CLAIMED task whose lease is still
    live is NOT claimable, so a healthy owner's claim is never stolen; a
    terminal task is never re-claimed. On a lost claim the current row is
    read back ONLY to raise the matching typed error — the claim *decision*
    is the atomic UPDATE's row count, never the read-back.

    ``claimed_by_session`` rides the SET clause only — never the
    ``<claimable>`` CAS predicate — exactly as ``claim_next_pending`` does,
    so the claim semantics are byte-identical with or without a session.
    Writing it here (rather than leaving it untouched) is what keeps
    ``renew_lease``'s claim-generation CAS truthful: a re-claim of an
    expired-lease orphan overwrites the dead owner's stale session instead
    of leaving it to falsely satisfy the heartbeat predicate.

    A window-parked task (PENDING with ``not_before`` in the future,
    Directive #3) is NOT claimable until its window re-arms — the same
    ``_claimable_now_q`` predicate ``claim_next_pending`` honours, ANDed into
    the CAS here so the two claim paths can never disagree on "is there work".
    Without it a parked task claimed at entry would be pre-flight re-parked
    every drain, churning junk park attempts (F5); with it the claim itself
    refuses a parked row and the drain never re-surfaces one.
    """
    from teatree.core.managers import _claimable_now_q  # noqa: PLC0415 — deferred: single-source park predicate

    status = task.Status
    now = timezone.now()
    claimable = (
        Q(status=status.PENDING)
        | (Q(status=status.CLAIMED) & (Q(lease_expires_at__isnull=True) | Q(lease_expires_at__lte=now)))
    ) & _claimable_now_q(now)
    won = (
        type(task)
        .objects.filter(pk=task.pk)
        .filter(claimable)
        .update(
            status=status.CLAIMED,
            claimed_by=claimed_by,
            claimed_by_session=claimed_by_session,
            claimed_at=now,
            heartbeat_at=now,
            lease_expires_at=now + timedelta(seconds=lease_seconds),
        )
    )
    if won != 1:
        task.refresh_from_db()
        if task.status in status.terminal():
            msg = "Task already finished"
            raise InvalidTransitionError(msg)
        if task.status == status.PENDING and window_parked(task, now):
            msg = f"Task parked until {task.not_before.isoformat()}"
            raise InvalidTransitionError(msg)
        msg = "Task already claimed"
        raise InvalidTransitionError(msg)
    task.refresh_from_db()


def renew_lease(task: "Task", *, lease_seconds: int = 300) -> None:
    """Heartbeat this worker's claim on *task* — a compare-and-swap, not a blind write (#786 shape).

    The renewal is guarded by the CLAIM GENERATION — ``status=CLAIMED`` AND
    the ``(claimed_by, claimed_by_session, claimed_at)`` this worker took the
    task under. ``claimed_at`` is re-stamped on every (re)claim, so once the
    lease lapsed and another worker reclaimed the row (``reclaim_orphaned_claims``
    → PENDING → a fresh ``claim`` with a new ``claimed_at``), this worker's
    predicate matches ZERO rows and it must NOT re-stamp the lease. The
    previous unconditional ``save(update_fields=…)`` re-stamped
    ``lease_expires_at`` with no WHERE predicate, resurrecting an expired
    claim after a rival had already taken over — two workers then drove the
    same unit (double-spend, racing ``complete()``). Zero rows → raise
    :class:`LeaseLostError` so the heartbeating worker aborts.
    """
    now = timezone.now()
    expires = now + timedelta(seconds=lease_seconds)
    renewed = (
        type(task)
        .objects.filter(pk=task.pk, status=task.Status.CLAIMED)
        .filter(
            claimed_by=task.claimed_by,
            claimed_by_session=task.claimed_by_session,
            claimed_at=task.claimed_at,
        )
        .update(heartbeat_at=now, lease_expires_at=expires)
    )
    if renewed != 1:
        msg = f"lease lost for task {task.pk}: claim generation moved on (re-claimed or terminal)"
        raise LeaseLostError(msg)
    task.heartbeat_at = now
    task.lease_expires_at = expires


def describe_lease_loss(task: "Task") -> str:
    """Name what actually took *task*'s claim, read back from the row (#3982).

    A single worker driving every loop still loses leases to ITSELF: an event-loop-starved
    heartbeat lets the lease lapse, this same process's ``reclaim_orphaned_claims`` sweep
    requeues the row, and the still-running agent's next renewal finds the claim generation
    moved on. Reporting that as "re-claimed by another worker" sends the operator hunting
    for a second worker that does not exist, so the reclaimer is read rather than assumed.

    Every reason keeps the ``lease lost for task <pk>:`` opening, which is what
    :func:`~teatree.core.modelkit.task_failure_taxonomy.classify_failure` keys
    ``FailureKind.LEASE_LOST`` on once the caller prefixes ``stuck_loop: ``.
    """
    opening = f"lease lost for task {task.pk}:"
    current = type(task).objects.filter(pk=task.pk).values("status", "claimed_by").first()
    if current is None:
        return f"{opening} the row no longer exists"
    status, owner = str(current["status"]), current["claimed_by"]
    if status in task.Status.terminal():
        return f"{opening} the row is already {status} — the attempt has nothing left to hand over"
    if status == task.Status.PENDING:
        return f"{opening} the lease lapsed and the row was requeued in-process — no competing worker holds it"
    if owner == task.claimed_by:
        return f"{opening} re-claimed in-process by this same worker ({owner!r}) — no competing worker holds it"
    return f"{opening} re-claimed by a competing worker ({owner!r})"
