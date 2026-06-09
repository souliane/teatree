"""Singleton cadence marker for the idle-stack reaper (souliane/teatree#2190).

The :class:`IdleStackReaperScanner` runs every loop tick but only acts once
per ``idle_stack_reaper_cadence_minutes`` — the cadence gate is carried across
tick boundaries by this durable singleton row (mirrors
:class:`ResourcePressureMarker`). Without it a sub-minute tick cadence would
re-scan + re-shell ``docker ps`` on every tick.
"""

from typing import ClassVar

from django.db import models
from django.utils import timezone


class LocalStackReaperMarker(models.Model):
    """One singleton row carrying the idle-reaper cadence gate."""

    singleton = models.BooleanField(default=True, unique=True)
    last_run_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "teatree_local_stack_reaper_marker"
        ordering: ClassVar = ["-last_run_at"]

    def __str__(self) -> str:
        return f"idle-stack-reaper<last_run={self.last_run_at}>"

    @classmethod
    def load(cls) -> "LocalStackReaperMarker":
        """Return the singleton row, creating it on first access."""
        marker, _ = cls.objects.get_or_create(singleton=True)
        return marker

    def stamp_run(self) -> None:
        self.last_run_at = timezone.now()
        self.save(update_fields=["last_run_at"])
