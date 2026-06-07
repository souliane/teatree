import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from django.db import IntegrityError, models, transaction
from django.db.models import Q
from django.utils import timezone

from teatree.config import load_config
from teatree.core.loop_lease_manager import LoopLeaseManager, LoopLeaseQuerySet, OwnershipStatus
from teatree.core.models.errors import RedisSlotsExhaustedError
from teatree.core.session_handover_manager import SessionHandoverManager, SessionHandoverQuerySet

if TYPE_CHECKING:
    from teatree.core.models.task import Task
    from teatree.core.models.ticket import Ticket

__all__ = [
    "IncomingEventManager",
    "LoopLeaseManager",
    "LoopLeaseQuerySet",
    "OwnershipStatus",
    "ReplyDispatchManager",
    "SessionHandoverManager",
    "SessionHandoverQuerySet",
    "SessionManager",
    "TaskManager",
    "TicketManager",
    "WorktreeManager",
]


logger = logging.getLogger(__name__)


class _OverlayFilterMixin:
    def for_overlay(self, overlay: str | None = None) -> models.QuerySet:
        if overlay:
            # Include tickets with empty overlay (created before multi-overlay)
            return self.filter(Q(overlay=overlay) | Q(overlay=""))  # type: ignore[attr-defined]
        return self.all()  # type: ignore[attr-defined]


class TicketQuerySet(_OverlayFilterMixin, models.QuerySet):
    def resolve(self, ref: str) -> "Ticket":
        """Resolve a ticket from a numeric pk, an issue number, or an issue URL.

        Accepts a numeric pk (``"314"`` — direct DB lookup), a full issue URL
        (``"https://github.com/owner/repo/issues/466"`` — exact match on
        ``issue_url``), or a bare issue number when no pk exists (``"466"`` —
        matches an ``issue_url`` ending in ``/466`` *or* one stored as the
        bare string ``"466"``, #707). Shared by
        ``pr create`` and ``lifecycle visit-phase`` so both accept the same
        identifier set (#694) — callers naturally pass the forge issue number
        and must not silently hit ``DoesNotExist``.
        """
        from teatree.core.models.ticket import Ticket  # noqa: PLC0415

        if ref.isdigit():
            try:
                return self.get(pk=int(ref))
            except Ticket.DoesNotExist:
                # No such pk — fall back to issue_url. Match either a forge
                # URL ending in /<ref> or a bare-number issue_url stored as
                # just the issue number (#707), keeping the match exact.
                ticket = self.filter(Q(issue_url__endswith=f"/{ref}") | Q(issue_url=ref)).first()
                if ticket is not None:
                    return ticket
                raise
        ticket = self.filter(issue_url=ref).first()
        if ticket is None:
            msg = f"No ticket matching {ref!r} (looked up by pk and issue_url)"
            raise Ticket.DoesNotExist(msg)
        return ticket

    def in_flight(self, overlay: str | None = None) -> models.QuerySet:
        from teatree.core.models.ticket import Ticket  # noqa: PLC0415

        return (
            self.for_overlay(overlay)
            .exclude(state__in=[Ticket.State.DELIVERED, Ticket.State.IGNORED])
            .filter(Q(extra__tracker_status__isnull=True) | ~Q(extra__tracker_status="Done"))
            .order_by("pk")
        )

    def allocate_redis_slot(self, ticket: "Ticket") -> int:
        """Claim the lowest free Redis DB index for the ticket, atomically.

        Idempotent: returns the existing index if the ticket already has one.
        Raises RedisSlotsExhaustedError when every slot is in use.

        The claim is the contention boundary: ``redis_db_index`` carries a
        unique constraint, so the bare read-taken-set → pick-lowest-free →
        ``save()`` shape let two concurrent worktree provisions both read the
        same taken-set, both pick the same free index, and the loser's
        ``save()`` raise an uncaught ``IntegrityError`` (the #804/#786 RMW
        class on the production SQLite backend, where ``select_for_update`` is
        a no-op). This mirrors the ``claim_next_pending`` / ``LoopLease.acquire``
        compare-and-swap doctrine in its caught-collision form: each candidate
        slot is claimed inside its own ``transaction.atomic`` and the unique
        constraint is the CAS token, so a colliding loser catches the
        ``IntegrityError`` and reselects the next free slot instead of
        crashing. Exactly one writer wins each slot.
        """
        if ticket.redis_db_index is not None:
            return int(ticket.redis_db_index)
        count = load_config().user.redis_db_count
        for _ in range(count):
            taken = set(self.filter(redis_db_index__isnull=False).values_list("redis_db_index", flat=True))
            index = next((i for i in range(count) if i not in taken), None)
            if index is None:
                break
            try:
                with transaction.atomic(using=self.db):
                    ticket.redis_db_index = index
                    ticket.save(update_fields=["redis_db_index"], using=self.db)
            except IntegrityError:
                ticket.redis_db_index = None
                continue
            return index
        msg = f"All {count} Redis DB slots are in use — release a ticket's slot first"
        raise RedisSlotsExhaustedError(msg)


class WorktreeQuerySet(_OverlayFilterMixin, models.QuerySet):
    def active(self, overlay: str | None = None) -> models.QuerySet:
        """Worktrees whose ticket is still in flight (not delivered or ignored).

        Matches the worktrees panel one-to-one so the KPI count and table size agree.
        """
        from teatree.core.models.ticket import Ticket  # noqa: PLC0415

        return (
            self.for_overlay(overlay)
            .exclude(ticket__state__in=[Ticket.State.DELIVERED, Ticket.State.IGNORED])
            .order_by("pk")
        )


class SessionQuerySet(_OverlayFilterMixin, models.QuerySet):
    def for_agent(self, agent_id: str) -> models.QuerySet:
        return self.filter(agent_id=agent_id).order_by("pk")


class IncomingEventQuerySet(models.QuerySet):
    def unprocessed(self) -> models.QuerySet:
        return self.filter(processed_at__isnull=True)

    def active_dm_thread(self, *, channel: str) -> str:
        from teatree.core.models.incoming_event import IncomingEvent  # noqa: PLC0415

        if not channel:
            return ""
        latest = (
            self.filter(source=IncomingEvent.Source.SLACK, channel_ref=channel)
            .order_by("-received_at", "-pk")
            .values_list("thread_ref", flat=True)
            .first()
        )
        return latest or ""


class ReplyDispatchQuerySet(models.QuerySet):
    def due_for_retry(self, now: datetime | None = None) -> models.QuerySet:
        from django.utils import timezone  # noqa: PLC0415

        from teatree.core.models.reply_dispatch import ReplyDispatch  # noqa: PLC0415

        moment = now or timezone.now()
        return (
            self.filter(status=ReplyDispatch.Status.FAILED)
            .exclude(action_name="dead_letter_alert")
            .filter(models.Q(next_retry_at__isnull=True) | models.Q(next_retry_at__lte=moment))
            .order_by("next_retry_at", "pk")
        )


class TaskQuerySet(models.QuerySet):
    def for_claude_session(self, claude_session_id: str) -> models.QuerySet:
        """Tasks whose session is the given Claude session, newest first.

        Scopes the task list to the work persisted under one Claude session:
        ``Session.agent_id`` holds the Claude session UUID (set by Claude Code),
        so the join is ``task.session.agent_id == claude_session_id``. An empty
        id matches nothing — an anonymous caller has no session-scoped list.
        """
        if not claude_session_id:
            return self.none()
        return self.filter(session__agent_id=claude_session_id).order_by("-pk")

    def completed_in_phase(self, phase: str) -> models.QuerySet:
        """Completed tasks whose phase normalizes to ``phase`` (#757).

        Matches any accepted spelling (short verb or gerund) — the FSM
        ``review()`` / ``mark_reviewed_externally()`` conditions must see
        a short-verb ``review`` task the same as a canonical
        ``reviewing`` one, mirroring the ``normalize_phase`` contract the
        rest of the system honours.
        """
        from teatree.core.models.task import Task  # noqa: PLC0415
        from teatree.core.phases import phase_spellings  # noqa: PLC0415

        return self.filter(phase__in=phase_spellings(phase), status=Task.Status.COMPLETED)

    def pending_in_phase(self, phase: str) -> models.QuerySet:
        """Non-terminal tasks whose phase normalizes to ``phase`` (#769).

        The consume-side mirror of ``completed_in_phase`` (#757):
        ``_consume_pending_phase_tasks`` must match a short-verb
        ``review`` task the same as a canonical ``reviewing`` one, so a
        direct-CLI path does not orphan a short-verb PENDING/CLAIMED task
        as a zombie session. Same SSOT (``phase_spellings``), opposite
        status set.
        """
        from teatree.core.models.task import Task  # noqa: PLC0415
        from teatree.core.phases import phase_spellings  # noqa: PLC0415

        return self.filter(
            phase__in=phase_spellings(phase),
            status__in=[Task.Status.PENDING, Task.Status.CLAIMED],
        )

    def claimable_for_headless(self, overlay: str | None = None) -> models.QuerySet:
        from teatree.core.models.task import Task  # noqa: PLC0415

        return self._claimable_for_target(Task.ExecutionTarget.HEADLESS, overlay)

    def claimable_for_interactive(self, overlay: str | None = None) -> models.QuerySet:
        from teatree.core.models.task import Task  # noqa: PLC0415

        return self._claimable_for_target(Task.ExecutionTarget.INTERACTIVE, overlay)

    def claim_next_pending(
        self,
        *,
        claimed_by: str,
        lease_seconds: int = 300,
        extra_filter: "Q | None" = None,
    ) -> "Task | None":
        """Atomically claim the oldest PENDING task — backend-agnostic (#786, N4).

        The claim is the dispatch boundary: callers spawn a sub-agent only
        for the returned task, so a second concurrent loop tick cannot
        double-dispatch a task the first already took (the spawn-then-claim
        race this replaces).

        Atomicity does NOT rely on ``select_for_update(skip_locked=True)``:
        teatree's production DB is SQLite, where
        ``has_select_for_update_skip_locked`` is ``False`` and Django
        silently drops the clause, so two ticks would both SELECT the same
        row. Instead this is a single conditional ``UPDATE ... WHERE
        status='pending' AND pk=<oldest>``: the row's status is the
        compare-and-swap token. Exactly one writer's UPDATE matches
        (``rowcount == 1``); the loser's ``WHERE status='pending'`` no
        longer holds so it updates 0 rows and returns ``None``. Correct on
        SQLite AND Postgres. ``extra_filter`` (a ``Q``) narrows the
        candidate set (e.g. dispatchable-only) so the command and the
        manager share ONE audited claim path.
        Returns the claimed task, or ``None`` when nothing is claimable.
        """
        from teatree.core.models.task import Task  # noqa: PLC0415

        now = timezone.now()
        candidates = self.filter(status=Task.Status.PENDING)
        if extra_filter is not None:
            candidates = candidates.filter(extra_filter)
        with transaction.atomic():
            oldest_pk = candidates.order_by("pk").values_list("pk", flat=True).first()
            if oldest_pk is None:
                return None
            # Compare-and-swap on status: only the writer that still sees
            # the row PENDING wins; a concurrent tick updates 0 rows.
            claimed_count = self.filter(pk=oldest_pk, status=Task.Status.PENDING).update(
                status=Task.Status.CLAIMED,
                claimed_by=claimed_by,
                claimed_at=now,
                heartbeat_at=now,
                lease_expires_at=now + timedelta(seconds=lease_seconds),
            )
            if claimed_count != 1:
                return None
        return self.get(pk=oldest_pk)

    def reclaim_orphaned_claims(self) -> int:
        """Return expired-lease CLAIMED tasks to PENDING. Returns the count (#652).

        When the Claude session driving the loop exits mid-task — terminal
        closed, ``/exit``, crash — its CLAIMED ``Task`` stops heartbeating
        and the lease expires. ``reap_stale_claims`` would transition that
        row CLAIMED→FAILED, which needs a manual ``reopen()`` before any
        other open session can resume it, so the loop silently stalls
        until the user notices. This instead returns the orphan to PENDING
        so the next ``PendingTasksScanner`` tick — in *any* still-open
        session — re-surfaces it and the loop continues on its own ("the
        fastest open session takes over").

        Same backend-agnostic compare-and-swap as ``claim_next_pending`` /
        ``reap_stale_claims``: a single conditional ``UPDATE ... WHERE
        status=CLAIMED AND lease_expires_at < now`` where the expiry
        predicate is the CAS token, re-evaluated atomically at write time.
        A lease renewed by a still-live owner between any read and the
        write moves ``lease_expires_at`` past ``now``, the ``WHERE`` no
        longer matches, and the healthy claim is left with its owner —
        never yanked away. Correct on the production SQLite backend (where
        ``select_for_update(skip_locked=True)`` is a silent no-op — the
        #786 B1 lesson): exactly one of N concurrent ticks updates the row
        and the losers update 0 rows. Runs *before* ``reap_stale_claims``
        in the tick so a recoverable orphan is taken over, not failed.
        """
        from teatree.core.models.task import Task  # noqa: PLC0415

        now = timezone.now()
        with transaction.atomic():
            return self.filter(status=Task.Status.CLAIMED, lease_expires_at__lt=now).update(
                status=Task.Status.PENDING,
                claimed_at=None,
                claimed_by="",
                lease_expires_at=None,
                heartbeat_at=None,
            )

    def replay_orphaned_transitions(self) -> int:
        """Replay FSM transitions a mid-transition crash dropped. Returns the count (#883).

        ``Task.complete`` does the task ``save()`` then the FSM transition
        in ``_advance_ticket``. ``complete`` is now one
        ``transaction.atomic`` so that window is closed going forward —
        but a row that completed *before* the atomic fix shipped (or via
        any future un-wrapped seam) can be left COMPLETED while its ticket
        is still on the old state. Lease expiry can't rescue it: the task
        is COMPLETED, not CLAIMED, so neither ``reclaim_orphaned_claims``
        nor ``reap_stale_claims`` ever sees it and the loop silently
        stalls forever on the half-advanced ticket.

        This is the boot/tick recovery sweep — sibling of
        ``reclaim_orphaned_claims``, run from the same hook. For each
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
        from django_fsm import TransitionNotAllowed  # noqa: PLC0415

        from teatree.core.models.task import Task  # noqa: PLC0415

        replayed = 0
        seen: set[int] = set()
        for task in self.filter(status=Task.Status.COMPLETED).select_related("ticket").order_by("-pk"):
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

    def reap_stale_claims(self) -> int:
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
        """
        from teatree.core.models.task import Task  # noqa: PLC0415

        now = timezone.now()
        with transaction.atomic():
            return self.filter(status=Task.Status.CLAIMED, lease_expires_at__lt=now).update(
                status=Task.Status.FAILED,
                claimed_at=None,
                claimed_by="",
                lease_expires_at=None,
                heartbeat_at=None,
            )

    def in_flight_claimed_count(self, dispatchable_filter: "Q") -> int:
        """Count CLAIMED tasks that match the dispatchable phase/role filter.

        The pipelined WIP cap subtracts this from the raw overlay budget so
        the standing total of CLAIMED dispatchable tasks can never exceed the
        cap, regardless of which tick admitted them. A CLAIMED task whose
        lease has expired is excluded — the reaper will reclaim it and it is
        not truly in flight.
        """
        from teatree.core.models.task import Task  # noqa: PLC0415

        now = timezone.now()
        return self.filter(status=Task.Status.CLAIMED, lease_expires_at__gt=now).filter(dispatchable_filter).count()

    def active_claim_exists(self) -> bool:
        """True iff some task is CLAIMED with a still-live lease.

        A live CLAIMED lease means a worker / sub-agent is actively driving
        a unit of loop work right now — the deferred-reinstall drain reads
        this to DEFER re-anchoring the running interpreter until no unit is
        in flight (never mutate the code out from under an active agent).
        An expired lease is not in-flight (the worker is gone; the reaper /
        reclaimer will sweep it).
        """
        from teatree.core.models.task import Task  # noqa: PLC0415

        now = timezone.now()
        return self.filter(status=Task.Status.CLAIMED, lease_expires_at__gt=now).exists()

    def _claimable_for_target(self, target: str, overlay: str | None = None) -> models.QuerySet:
        from teatree.core.models.task import Task  # noqa: PLC0415

        now = timezone.now()
        qs = (
            self.filter(
                execution_target=target,
                status__in=[Task.Status.PENDING, Task.Status.CLAIMED],
            )
            .filter(Q(lease_expires_at__isnull=True) | Q(lease_expires_at__lte=now))
            .order_by("pk")
        )
        if overlay:
            qs = qs.filter(
                Q(ticket__overlay=overlay)
                | Q(session__overlay=overlay)
                | Q(ticket__overlay="")
                | Q(session__overlay="")
            )
        return qs


TicketManager = models.Manager.from_queryset(TicketQuerySet)
WorktreeManager = models.Manager.from_queryset(WorktreeQuerySet)
SessionManager = models.Manager.from_queryset(SessionQuerySet)
TaskManager = models.Manager.from_queryset(TaskQuerySet)
IncomingEventManager = models.Manager.from_queryset(IncomingEventQuerySet)
ReplyDispatchManager = models.Manager.from_queryset(ReplyDispatchQuerySet)
