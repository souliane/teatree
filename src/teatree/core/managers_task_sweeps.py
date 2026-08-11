"""Orphan- and stale-claim recovery sweeps — the boot/tick ``run_boot_sweeps`` family.

Split out of :mod:`teatree.core.managers` (which holds the ticket / worktree / task
lifecycle queries) because these three are one concern with one ordering contract:
what to do about work whose worker vanished. They run as a sequence, rescue before
fail — :func:`reclaim_orphaned_claims` returns a recoverable orphan to the queue,
:func:`replay_orphaned_transitions` re-fires a transition a mid-transition crash
dropped, and :func:`reap_stale_claims` terminally fails only what the first could not
rescue. Reading them together is what makes that ordering legible; the surrounding
"where is this unit of work" queries do not participate in it.

Each takes the ``TaskQuerySet`` as its first argument and
:class:`~teatree.core.managers.TaskQuerySet` delegates to it, so every call site and
the public queryset API are unchanged.
"""

import logging
from typing import TYPE_CHECKING, cast

from django.apps import apps
from django.db import transaction
from django.utils import timezone

from teatree.core.claim_liveness import (
    OWNER_COLUMNS,
    RELEASED_CLAIM,
    ClaimOwner,
    executing_owner_reason,
    owner_is_executing,
)
from teatree.core.modelkit.task_failure_taxonomy import LEASE_EXPIRED_PREFIX, FailureKind
from teatree.core.repair_loop import IterationStalled, MaxIterationsExceeded

if TYPE_CHECKING:
    from datetime import datetime

    from django.db import models

    from teatree.core.models.task import Task
    from teatree.core.models.task_attempt import TaskAttempt
    from teatree.core.models.ticket import Ticket

__all__ = ["reap_stale_claims", "reclaim_orphaned_claims", "replay_orphaned_transitions"]

logger = logging.getLogger(__name__)


def _lease_expired_reason(claimed_by: str) -> str:
    """The named reason a lease-reaped task records (#3957)."""
    holder = claimed_by or "an unidentified worker"
    return f"{LEASE_EXPIRED_PREFIX}lease held by {holder} expired without a heartbeat and was reaped"


def reclaim_orphaned_claims(qs: "models.QuerySet") -> int:
    """Return expired-lease CLAIMED tasks to PENDING. Returns the count (#652).

    When the Claude session driving the loop exits mid-task — terminal
    closed, ``/exit``, crash — its CLAIMED ``Task`` stops heartbeating
    and the lease expires. :func:`reap_stale_claims` would transition that
    row CLAIMED→FAILED, which needs a manual ``reopen()`` before any
    other open session can resume it, so the loop silently stalls
    until the user notices. This instead returns the orphan to PENDING
    so the next ``PendingTasksScanner`` tick — in *any* still-open
    session — re-surfaces it and the loop continues on its own ("the
    fastest open session takes over").

    Same backend-agnostic compare-and-swap as ``claim_next_pending`` /
    :func:`reap_stale_claims`: a single conditional ``UPDATE ... WHERE
    status=CLAIMED AND lease_expires_at < now`` where the expiry
    predicate is the CAS token, re-evaluated atomically at write time.
    A lease renewed by a still-live owner between any read and the
    write moves ``lease_expires_at`` past ``now``, the ``WHERE`` no
    longer matches, and the healthy claim is left with its owner —
    never yanked away. Correct on the production SQLite backend (where
    ``select_for_update(skip_locked=True)`` is a silent no-op — the
    #786 B1 lesson): exactly one of N concurrent ticks updates the row
    and the losers update 0 rows. Runs *before* :func:`reap_stale_claims`
    in the tick so a recoverable orphan is taken over, not failed.

    #2009: the re-queue is the repair-loop's retry chokepoint, so the
    per-phase iteration budget and stall detector are enforced here. A row
    whose ticket-phase has hit the configured iteration cap, or has stalled
    on two consecutive identical failures (which also escalates to the user),
    is dropped from the re-queue set and held CLAIMED — so a doomed phase
    neither re-runs nor burns more attempts on the identical failure.

    #4164: a lapsed lease is evidence about the LEASE, not about the process. A row whose
    owner process is still executing it (a memory-thrashed event loop that stalled past its
    900s lease) is held with its owner instead — re-queuing it strands a run that is still
    producing work and hands a second agent the same worktree.
    """
    task_model = cast("type[Task]", apps.get_model("core", "Task"))

    now = timezone.now()
    with transaction.atomic():
        candidate_pks = list(
            qs.filter(status=task_model.Status.CLAIMED, lease_expires_at__lt=now).values_list("pk", flat=True)
        )
        requeueable = _requeueable_within_budget(qs, candidate_pks, now=now)
        if not requeueable:
            return 0
        return qs.filter(
            pk__in=requeueable,
            status=task_model.Status.CLAIMED,
            lease_expires_at__lt=now,
        ).update(status=task_model.Status.PENDING, **RELEASED_CLAIM)


def _requeueable_within_budget(qs: "models.QuerySet", candidate_pks: list[int], *, now: "datetime") -> list[int]:
    """Filter *candidate_pks* to those whose ticket-phase may still re-queue (#2009, #4164).

    Consults the repair-loop budget per row: a phase at its iteration cap
    (:class:`~teatree.core.repair_loop.MaxIterationsExceeded`) or stalled on
    two identical failures (:class:`~teatree.core.repair_loop.IterationStalled`,
    which also escalates to the user) is dropped from the re-queue set. So is a
    row whose owner process is still executing it (#4164) — the only evidence
    that WITHHOLDS the sweep, never one that widens it.
    """
    allowed: list[int] = []
    for task in qs.filter(pk__in=candidate_pks).select_related("ticket", "session"):
        owner = ClaimOwner.of(task)
        if owner_is_executing(owner, task.pk, now=now):
            logger.info("reclaim skip task=%s ticket=%s: %s", task.pk, task.ticket_id, executing_owner_reason(owner))
            continue
        try:
            task.check_requeue_allowed()
        except (MaxIterationsExceeded, IterationStalled) as exc:
            logger.warning("reclaim skip task=%s ticket=%s %s: %s", task.pk, task.ticket_id, type(exc).__name__, exc)
            continue
        allowed.append(task.pk)
    return allowed


def replay_orphaned_transitions(qs: "models.QuerySet") -> int:
    """Replay FSM transitions a mid-transition crash dropped. Returns the count (#883).

    ``Task.complete`` does the task ``save()`` then the FSM transition
    in ``_advance_ticket``. ``complete`` is now one
    ``transaction.atomic`` so that window is closed going forward —
    but a row that completed *before* the atomic fix shipped (or via
    any future un-wrapped seam) can be left COMPLETED while its ticket
    is still on the old state. Lease expiry can't rescue it: the task
    is COMPLETED, not CLAIMED, so neither :func:`reclaim_orphaned_claims`
    nor :func:`reap_stale_claims` ever sees it and the loop silently
    stalls forever on the half-advanced ticket.

    This is the boot/tick recovery sweep — sibling of
    :func:`reclaim_orphaned_claims`, run from the same hook. For each
    ticket it takes that ticket's latest COMPLETED task and replays
    the *same* idempotent ``Task._apply_phase_transition`` the live
    ``complete`` path uses — there is no parallel transition
    mechanism. Idempotency and gate-integrity come for free from that
    shared path: every transition is guarded by both the phase *and*
    the required ``ticket.state``, so an already-advanced ticket
    no-ops and a ticket can never be teleported past a lifecycle gate
    it did not earn (a COMPLETED ``shipping`` task on a ``started``
    ticket finds no matching guard). The shared path also enforces
    the needs-user-input hold (#927): a task the agent could not
    finish (its last attempt returned ``needs_user_input``) was held
    by ``_advance_ticket`` with an interactive followup scheduled —
    the sweep must not force-advance it past that phase, and does not,
    because ``_apply_phase_transition`` itself no-ops for a held task.
    Returns the number of tickets a transition actually fired for.
    """
    # Latest COMPLETED task per ticket: iterate newest-first and keep
    # the first one seen for each ticket. ``distinct("ticket_id")`` is
    # Postgres-only; teatree's production DB is SQLite (the #786 B1
    # backend-agnostic lesson), so this stays a plain ordered scan.
    #
    # A terminal ticket is excluded (#3879): no branch of the shared path
    # advances one, so a crash can have dropped nothing. Every branch guards
    # on a source that is not its own target EXCEPT ``mark_reviewed_externally``,
    # which accepts REVIEW_POSTED so a re-review at a moved head SHA can
    # re-stamp — leaning on the guard therefore re-fired that self-loop for
    # every closed review on every tick, minting a teardown job each time.
    from django_fsm import TransitionNotAllowed  # noqa: PLC0415 — deferred: heavy/optional dep at call site

    task_model = cast("type[Task]", apps.get_model("core", "Task"))
    ticket_model = cast("type[Ticket]", apps.get_model("core", "Ticket"))
    advanceable = qs.filter(status=task_model.Status.COMPLETED).exclude(
        ticket__state__in=ticket_model.marker_release_states()
    )

    replayed = 0
    seen: set[int] = set()
    for task in advanceable.select_related("ticket").order_by("-pk"):
        if task.ticket_id in seen:
            continue
        seen.add(task.ticket_id)
        try:
            if task._apply_phase_transition():  # noqa: SLF001  # the shared single transition path (#883)
                replayed += 1
        except (TransitionNotAllowed, ValueError) as exc:
            # Per-ticket isolation: one un-gated ticket must not abort the sweep.
            logger.warning("replay skip task=%s ticket=%s %s: %s", task.pk, task.ticket_id, type(exc).__name__, exc)
    return replayed


def reap_stale_claims(qs: "models.QuerySet") -> int:
    """Fail CLAIMED tasks whose lease is *still* expired. Returns the count.

    #800 N5: the previous shape scanned ``lease_expires_at < now``
    then called ``task.fail()`` per row with no re-check under a
    lock. A concurrent ``Task.renew_lease`` (a live worker
    heartbeating its still-valid claim) extends ``lease_expires_at``
    after the scan but before the unconditional ``fail()`` — the
    healthy task is spuriously failed. This is now the #804
    backend-agnostic conditional-UPDATE compare-and-swap: a single
    ``UPDATE ... WHERE status=CLAIMED AND lease_expires_at < now``
    where the expiry predicate is the CAS token, re-evaluated
    atomically at write time. A lease renewed between any scan and
    the write moves ``lease_expires_at`` past ``now``, the ``WHERE``
    no longer matches that row, and it is not reaped. Correct on the
    production SQLite backend (where ``select_for_update`` is a
    no-op) because the conditional UPDATE is itself atomic — the
    same shape as ``claim_next_pending`` / ``LoopLease.acquire``.

    #4164: the CAS closes the window between the scan and the write, but not the case
    where the lease genuinely lapsed while the owner PROCESS kept running — a stalled
    event loop renews nothing, so the CAS matches and a live agent's row is failed under
    it. A row whose owner is still executing is skipped, so the two sweeps agree: what
    :func:`reclaim_orphaned_claims` declines to re-queue this does not terminally fail.
    """
    task_model = cast("type[Task]", apps.get_model("core", "Task"))
    attempt_model = cast("type[TaskAttempt]", apps.get_model("core", "TaskAttempt"))

    now = timezone.now()
    reaped = 0
    with transaction.atomic():
        # Per-row rather than one bulk UPDATE (#3957): a bulk UPDATE recorded NO
        # attempt and no reason, so a lease reap was indistinguishable on the board
        # from a genuine failure of the work — the single largest cause-less failure
        # path. The CAS is unchanged and still the write-time predicate; it is simply
        # evaluated per row, so only the rows this call actually reaped get a reason.
        # Volume is bounded by however many leases lapsed since the last sweep.
        candidates = list(
            qs.filter(status=task_model.Status.CLAIMED, lease_expires_at__lt=now).values_list(
                "pk",
                "claimed_by",
                *OWNER_COLUMNS,
            ),
        )
        for pk, claimed_by, owner_pid, owner_pid_namespace, owner_driving_since in candidates:
            owner = ClaimOwner(owner_pid, owner_pid_namespace or "", owner_driving_since)
            if owner_is_executing(owner, pk, now=now):
                logger.info("stale-claim reap skip task=%s: %s", pk, executing_owner_reason(owner))
                continue
            claimed = qs.filter(pk=pk, status=task_model.Status.CLAIMED, lease_expires_at__lt=now).update(
                status=task_model.Status.FAILED,
                failure_reason=_lease_expired_reason(claimed_by),
                failure_kind=FailureKind.LEASE_EXPIRED,
                **RELEASED_CLAIM,
            )
            if not claimed:
                # The lease was renewed between the scan and this write — a live
                # worker still holds it, so it was not reaped and records nothing.
                continue
            reaped += 1
            attempt_model.objects.create(
                task_id=pk,
                ended_at=now,
                exit_code=1,
                error=_lease_expired_reason(claimed_by),
            )
    return reaped
