from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from django.db import models, transaction
from django.db.models import Q
from django.utils import timezone

from teatree.config import load_config
from teatree.core.models.errors import RedisSlotsExhaustedError

if TYPE_CHECKING:
    from teatree.core.models.task import Task
    from teatree.core.models.ticket import Ticket


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
        """Pick the lowest free Redis DB index for the ticket.

        Idempotent: returns the existing index if the ticket already has one.
        Raises RedisSlotsExhaustedError when every slot is in use.
        """
        if ticket.redis_db_index is not None:
            return int(ticket.redis_db_index)
        count = load_config().user.redis_db_count
        taken = set(self.filter(redis_db_index__isnull=False).values_list("redis_db_index", flat=True))
        for index in range(count):
            if index not in taken:
                ticket.redis_db_index = index
                ticket.save(update_fields=["redis_db_index"])
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

    def claim_next_pending(self, *, claimed_by: str, lease_seconds: int = 300) -> "Task | None":
        """Atomically select and claim the oldest PENDING task (#786, N4).

        Selects ``FOR UPDATE SKIP LOCKED`` so two concurrent loop ticks each
        get a *distinct* task (or ``None``) — never the same one. The claim
        is the dispatch boundary: callers spawn the sub-agent for the
        returned task only, so a second tick cannot double-dispatch a task
        the first already took (the spawn-then-claim race this replaces).
        Returns the claimed task, or ``None`` when nothing is claimable.
        """
        from teatree.core.models.task import Task  # noqa: PLC0415

        now = timezone.now()
        with transaction.atomic():
            task = self.select_for_update(skip_locked=True).filter(status=Task.Status.PENDING).order_by("pk").first()
            if task is None:
                return None
            task.status = Task.Status.CLAIMED
            task.claimed_by = claimed_by
            task.claimed_at = now
            task.heartbeat_at = now
            task.lease_expires_at = now + timedelta(seconds=lease_seconds)
            task.save(
                update_fields=[
                    "status",
                    "claimed_by",
                    "claimed_at",
                    "heartbeat_at",
                    "lease_expires_at",
                ],
            )
            return task

    def reap_stale_claims(self) -> int:
        """Fail CLAIMED tasks whose lease has expired. Returns number of reaped tasks."""
        from teatree.core.models.task import Task  # noqa: PLC0415

        now = timezone.now()
        stale = self.filter(status=Task.Status.CLAIMED, lease_expires_at__lt=now)
        count = 0
        for task in stale:
            task.fail()
            count += 1
        return count

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
