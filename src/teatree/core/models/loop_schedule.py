"""Weekly loop-preset schedule — the L2 calendar layer (#3159).

A :class:`ModeSchedule` is a named weekly calendar; each
:class:`ModeScheduleSlot` is a **start point** (a set of weekdays at a local
wall-clock time) that names the preset governing from that instant until the
next slot start. Slots partition the week by coverage, not by cron spans: the
governing slot at instant *t* is simply the latest slot-start ≤ *t* (searching
back across midnight / week wrap), so there are no gaps, no overlaps, and no
span-vs-fire ambiguity. Multiple named schedules may exist; the active one is the
``active_loop_schedule`` ``ConfigSetting`` (absent ⇒ no L2 layer). Slot resolution
itself lives in :mod:`teatree.loop.preset_resolution`; this module is the durable
shape only, referencing presets **by name** so a deleted preset fails open.
"""

from typing import ClassVar

from django.db import models


class ModeSchedule(models.Model):
    """A named weekly calendar whose slots pick the active preset by day and time."""

    name = models.SlugField(max_length=64, unique=True)
    description = models.TextField(blank=True, default="")
    timezone = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "teatree_loop_schedule"
        ordering: ClassVar = ["name"]

    def __str__(self) -> str:
        return f"loop-schedule<{self.name} tz={self.timezone or 'local'}>"


_MAX_WEEKDAY = 6  # Python weekday(): Sunday


class ModeScheduleSlot(models.Model):
    """One weekly start point: weekdays at a local start time, the preset it activates.

    ``days`` are Python ``weekday()`` ints (Mon=0 .. Sun=6); ``start_time`` is a
    local wall-clock time in the owning schedule's ``timezone``.
    """

    schedule = models.ForeignKey(ModeSchedule, on_delete=models.CASCADE, related_name="slots")
    days = models.JSONField()
    start_time = models.TimeField()
    preset_name = models.CharField(max_length=64)

    class Meta:
        db_table = "teatree_loop_schedule_slot"
        ordering: ClassVar = ["start_time"]

    def __str__(self) -> str:
        return f"loop-schedule-slot<{self.schedule_id} {self.days}@{self.start_time} -> {self.preset_name}>"  # ty: ignore[unresolved-attribute]

    @property
    def weekdays(self) -> set[int]:
        """The valid Mon=0..Sun=6 weekday ints this slot starts on (malformed entries dropped)."""
        raw = self.days
        if not isinstance(raw, list):
            return set()
        return {day for day in raw if isinstance(day, int) and 0 <= day <= _MAX_WEEKDAY}
