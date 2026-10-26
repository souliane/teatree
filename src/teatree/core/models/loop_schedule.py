"""Weekly loop-preset schedule — the L2 calendar layer (#3159).

A :class:`ModeSchedule` is a named weekly calendar; each
:class:`ModeScheduleSlot` is a **start point** (a set of weekdays at a local
wall-clock time) that names the preset governing from that instant until the
next slot start. Slots partition the week by coverage, not by cron spans: the
governing slot at instant *t* is simply the latest slot-start ≤ *t* (searching
back across midnight / week wrap), so there are no gaps, no overlaps, and no
span-vs-fire ambiguity. Multiple named schedules may exist; the active one is the
``active_loop_schedule`` ``ConfigSetting`` (absent ⇒ no L2 layer). Slot resolution
itself lives in :mod:`teatree.loop.preset_resolution`; this module owns the
durable shape, active-schedule lookup, and owner timezone.
"""

import datetime as dt
import logging
import zoneinfo
from typing import TYPE_CHECKING, ClassVar

from django.db import models
from django.utils import timezone as django_timezone

from teatree.core.modelkit.notify_policy import ACTIVE_SCHEDULE_SETTING, OWNER_FALLBACK_ZONE
from teatree.core.models.config_setting import ConfigSetting

logger = logging.getLogger(__name__)


class ModeSchedule(models.Model):
    """A named weekly calendar whose slots pick the active preset by day and time."""

    if TYPE_CHECKING:
        # Reverse accessor Django synthesises at class-prep time from the
        # ``related_name`` on ``ModeScheduleSlot.schedule`` — invisible to a static checker.
        # Annotation-only; never evaluated at runtime.
        slots: "models.Manager[ModeScheduleSlot]"

    name = models.SlugField(max_length=64, unique=True)
    description = models.TextField(blank=True, default="")
    timezone = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "teatree_loop_schedule"
        ordering: ClassVar = ["name"]

    def __str__(self) -> str:
        return f"loop-schedule<{self.name} tz={self.timezone_label}>"

    @property
    def timezone_label(self) -> str:
        """Display form of ``timezone``.

        An unset value resolves to the *project* zone, not the operator's local
        one — naming it "local" reads as "your wall clock" and hides that slots
        seeded without a timezone fire in ``settings.TIME_ZONE``.
        """
        return self.timezone or f"{django_timezone.get_current_timezone_name()} (default)"


def active_schedule_name() -> str:
    raw = ConfigSetting.objects.get_effective("active_loop_schedule")
    return raw.strip() if isinstance(raw, str) and raw.strip() else ""


def schedule_by_name(name: str) -> ModeSchedule | None:
    schedule = ModeSchedule.objects.filter(name=name).first()
    if schedule is None:
        logger.warning("%s names unknown schedule %r — failing open to base config", ACTIVE_SCHEDULE_SETTING, name)
    return schedule


def active_schedule() -> ModeSchedule | None:
    name = active_schedule_name()
    return schedule_by_name(name) if name else None


def owner_zone() -> dt.tzinfo:
    schedule = active_schedule()
    if schedule is not None:
        try:
            return zoneinfo.ZoneInfo(schedule.timezone)
        except (zoneinfo.ZoneInfoNotFoundError, ValueError):
            logger.warning(
                "schedule %r timezone %r invalid — using %s", schedule.name, schedule.timezone, OWNER_FALLBACK_ZONE
            )
    return zoneinfo.ZoneInfo(OWNER_FALLBACK_ZONE)


_MAX_WEEKDAY = 6  # Python weekday(): Sunday


class ModeScheduleSlot(models.Model):
    """One weekly start point: weekdays at a local start time, the preset it activates.

    ``days`` are Python ``weekday()`` ints (Mon=0 .. Sun=6); ``start_time`` is a
    local wall-clock time in the owning schedule's ``timezone``.
    """

    if TYPE_CHECKING:
        # Django synthesises the ``<fk>_id`` shadow at class-prep time — invisible to a
        # static checker. Annotation-only; never evaluated at runtime.
        schedule_id: int

    schedule = models.ForeignKey(ModeSchedule, on_delete=models.CASCADE, related_name="slots")
    days = models.JSONField()
    start_time = models.TimeField()
    preset_name = models.CharField(max_length=64)

    class Meta:
        db_table = "teatree_loop_schedule_slot"
        ordering: ClassVar = ["start_time"]

    def __str__(self) -> str:
        return f"loop-schedule-slot<{self.schedule_id} {self.days}@{self.start_time} -> {self.preset_name}>"

    @property
    def weekdays(self) -> set[int]:
        """The valid Mon=0..Sun=6 weekday ints this slot starts on (malformed entries dropped)."""
        raw = self.days
        if not isinstance(raw, list):
            return set()
        return {day for day in raw if isinstance(day, int) and 0 <= day <= _MAX_WEEKDAY}
