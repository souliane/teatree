from django.db import models
from django.db.models import Q
from django.utils import timezone


class TicketQuerySet(models.QuerySet):
    def in_flight(self) -> models.QuerySet:
        return self.exclude(state="delivered").order_by("pk")


class WorktreeQuerySet(models.QuerySet):
    def active(self) -> models.QuerySet:
        return self.exclude(state="created").order_by("pk")


class SessionQuerySet(models.QuerySet):
    def for_agent(self, agent_id: str) -> models.QuerySet:
        return self.filter(agent_id=agent_id).order_by("pk")


class TaskQuerySet(models.QuerySet):
    def claimable_for_headless(self) -> models.QuerySet:
        return self._claimable_for_target("headless")

    def claimable_for_interactive(self) -> models.QuerySet:
        return self._claimable_for_target("interactive")

    def _claimable_for_target(self, target: str) -> models.QuerySet:
        # String values mirror Task.ExecutionTarget / Task.Status enum values.
        # Direct import from models.py is not possible (circular).
        now = timezone.now()
        return (
            self.filter(execution_target=target, status__in=["pending", "claimed"])
            .filter(Q(lease_expires_at__isnull=True) | Q(lease_expires_at__lte=now))
            .order_by("pk")
        )


TicketManager = models.Manager.from_queryset(TicketQuerySet)
WorktreeManager = models.Manager.from_queryset(WorktreeQuerySet)
SessionManager = models.Manager.from_queryset(SessionQuerySet)
TaskManager = models.Manager.from_queryset(TaskQuerySet)
