from datetime import datetime
from typing import TYPE_CHECKING

from django.db import models
from django.db.models import Q
from django.utils import timezone

from teatree.config import load_config
from teatree.core.models.errors import RedisSlotsExhaustedError

if TYPE_CHECKING:
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
        falls back to ``issue_url`` ending in ``/466``). Shared by
        ``pr create`` and ``lifecycle visit-phase`` so both accept the same
        identifier set (#694) — callers naturally pass the forge issue number
        and must not silently hit ``DoesNotExist``.
        """
        from teatree.core.models.ticket import Ticket  # noqa: PLC0415

        if ref.isdigit():
            try:
                return self.get(pk=int(ref))
            except Ticket.DoesNotExist:
                ticket = self.filter(issue_url__endswith=f"/{ref}").first()
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
    def claimable_for_headless(self, overlay: str | None = None) -> models.QuerySet:
        from teatree.core.models.task import Task  # noqa: PLC0415

        return self._claimable_for_target(Task.ExecutionTarget.HEADLESS, overlay)

    def claimable_for_interactive(self, overlay: str | None = None) -> models.QuerySet:
        from teatree.core.models.task import Task  # noqa: PLC0415

        return self._claimable_for_target(Task.ExecutionTarget.INTERACTIVE, overlay)

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
