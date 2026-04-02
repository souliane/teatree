from django.db import models
from django.db.models import Q
from django.utils import timezone


class _OverlayFilterMixin:
    def for_overlay(self, overlay: str | None = None) -> models.QuerySet:
        if overlay:
            # Include tickets with empty overlay (created before multi-overlay)
            return self.filter(Q(overlay=overlay) | Q(overlay=""))  # type: ignore[attr-defined]
        return self.all()  # type: ignore[attr-defined]


class TicketQuerySet(_OverlayFilterMixin, models.QuerySet):
    def in_flight(self, overlay: str | None = None) -> models.QuerySet:
        from teatree.core.models.ticket import Ticket  # noqa: PLC0415

        return self.for_overlay(overlay).exclude(state=Ticket.State.DELIVERED).order_by("pk")


class WorktreeQuerySet(_OverlayFilterMixin, models.QuerySet):
    def active(self, overlay: str | None = None) -> models.QuerySet:
        from teatree.core.models.worktree import Worktree  # noqa: PLC0415

        return self.for_overlay(overlay).exclude(state=Worktree.State.CREATED).order_by("pk")


class SessionQuerySet(_OverlayFilterMixin, models.QuerySet):
    def for_agent(self, agent_id: str) -> models.QuerySet:
        return self.filter(agent_id=agent_id).order_by("pk")


class TaskQuerySet(models.QuerySet):
    def claimable_for_headless(self, overlay: str | None = None) -> models.QuerySet:
        from teatree.core.models.task import Task  # noqa: PLC0415

        return self._claimable_for_target(Task.ExecutionTarget.HEADLESS, overlay)

    def claimable_for_interactive(self, overlay: str | None = None) -> models.QuerySet:
        from teatree.core.models.task import Task  # noqa: PLC0415

        return self._claimable_for_target(Task.ExecutionTarget.INTERACTIVE, overlay)

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
