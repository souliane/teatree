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

from datetime import timedelta
from typing import TYPE_CHECKING

from django.db.models import Q
from django.utils import timezone

from teatree.core.models.errors import InvalidTransitionError, LeaseLostError

if TYPE_CHECKING:
    from teatree.core.models.task import Task


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
        if task.status == status.PENDING and task.not_before is not None and task.not_before > now:
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
