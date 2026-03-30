from django.db import models
from django.db.models import Q
from django.utils import timezone


class _OverlayFilterMixin:
    def for_overlay(self, overlay: str | None = None) -> models.QuerySet:
        if overlay:
            return self.filter(overlay=overlay)  # type: ignore[attr-defined]
        return self.all()  # type: ignore[attr-defined]


class TicketQuerySet(_OverlayFilterMixin, models.QuerySet):
    def in_flight(self, overlay: str | None = None) -> models.QuerySet:
        return self.for_overlay(overlay).exclude(state="delivered").order_by("pk")


class WorktreeQuerySet(_OverlayFilterMixin, models.QuerySet):
    def active(self, overlay: str | None = None) -> models.QuerySet:
        return self.for_overlay(overlay).exclude(state="created").order_by("pk")


class SessionQuerySet(_OverlayFilterMixin, models.QuerySet):
    def for_agent(self, agent_id: str) -> models.QuerySet:
        return self.filter(agent_id=agent_id).order_by("pk")


class TaskQuerySet(models.QuerySet):
    def claimable_for_headless(self, overlay: str | None = None) -> models.QuerySet:
        return self._claimable_for_target("headless", overlay)

    def claimable_for_interactive(self, overlay: str | None = None) -> models.QuerySet:
        return self._claimable_for_target("interactive", overlay)

    def _claimable_for_target(self, target: str, overlay: str | None = None) -> models.QuerySet:
        # String values mirror Task.ExecutionTarget / Task.Status enum values.
        # Direct import from models.py is not possible (circular).
        now = timezone.now()
        qs = (
            self.filter(execution_target=target, status__in=["pending", "claimed"])
            .filter(Q(lease_expires_at__isnull=True) | Q(lease_expires_at__lte=now))
            .order_by("pk")
        )
        if overlay:
            qs = qs.filter(Q(ticket__overlay=overlay) | Q(session__overlay=overlay))
        return qs


TicketManager = models.Manager.from_queryset(TicketQuerySet)
WorktreeManager = models.Manager.from_queryset(WorktreeQuerySet)
SessionManager = models.Manager.from_queryset(SessionQuerySet)
TaskManager = models.Manager.from_queryset(TaskQuerySet)
