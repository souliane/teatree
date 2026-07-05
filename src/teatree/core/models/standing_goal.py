"""Registered standing verified-green goals (PR-25, M8).

A ``StandingGoal`` is an operator-registered "drive X to green" mission: a named
shell ``check_command`` whose zero exit means the goal is met. The Stop gate
``handle_standing_goal_stop`` re-runs the check at turn-end and, while an active
goal is unmet, denies a stop-as-if-done — a status report is a checkpoint, not
the deliverable (``/t3:rules`` § "Lead a Completion Report With the Assigned-Work
Status"). A passing check auto-retires the goal (``active`` → False).

The shape mirrors the guarded-factory convention of
:class:`teatree.core.models.waiting_item.WaitingItem`: a manager that refuses an
empty name/command at ``set`` time, an ``active`` predicate the gate and CLI
share, and single-use retire/clear.
"""

from typing import ClassVar

from django.db import models
from django.utils import timezone


class StandingGoalError(ValueError):
    """A :class:`StandingGoal` was rejected at ``set`` time — empty name or command."""


class StandingGoalManager(models.Manager["StandingGoal"]):
    """Register / retire / clear / list surface for standing verified-green goals."""

    def active_goals(self) -> models.QuerySet["StandingGoal"]:
        """Every active (still-driving) goal, oldest first — the set the gate re-checks."""
        return self.filter(active=True).order_by("created_at")

    def set_goal(self, name: str, check_command: str) -> "StandingGoal":
        """Register or update a goal by name (single canonical row); refuse empties.

        Upsert: a re-``set`` of an existing name updates its command and re-arms
        it (``active`` → True), so a retired goal is trivially re-engaged.
        """
        clean_name = name.strip()
        clean_command = check_command.strip()
        if not clean_name:
            msg = "a standing goal requires a non-empty name (PR-25)"
            raise StandingGoalError(msg)
        if not clean_command:
            msg = "a standing goal requires a non-empty --check command (PR-25)"
            raise StandingGoalError(msg)
        goal, _created = self.update_or_create(
            name=clean_name,
            defaults={"check_command": clean_command, "active": True, "updated_at": timezone.now()},
        )
        return goal

    def retire(self, name: str) -> bool:
        """Mark an active goal met (``active`` → False) single-use; ``False`` when absent/retired."""
        updated = self.filter(name=name, active=True).update(active=False, updated_at=timezone.now())
        return updated > 0

    def clear(self, name: str | None = None) -> int:
        """Delete the named goal, or ALL goals when *name* is None; return the count deleted."""
        queryset = self.all() if name is None else self.filter(name=name)
        deleted, _ = queryset.delete()
        return deleted


class StandingGoal(models.Model):
    """One registered standing verified-green goal (name + green ``check_command``)."""

    name = models.CharField(max_length=200, unique=True)
    check_command = models.TextField()
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)

    objects: ClassVar[StandingGoalManager] = StandingGoalManager()

    class Meta:
        db_table = "teatree_standing_goal"
        ordering: ClassVar = ["created_at"]

    def __str__(self) -> str:
        return f"standing-goal<{self.name}:{'active' if self.active else 'retired'}>"
