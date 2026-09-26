"""A mode governing WITHOUT a manual override — shared #4196 setup.

Every #4185 test activated its mode through ``ModeOverride.objects.set_override``. That
is the ONE layer the preset resolver and the merged mode resolver read identically, so
membership and per-fire admission agreed by construction and a fully green suite sat on
top of a membership set that disagreed with the tick every weeknight.

Two configurations actually separate the resolvers:

*   a SCHEDULE slot naming a mode that masks the loop off;
*   NO schedule and no override at all — the mode resolver falls through to the
    ``default_mode`` row while the preset resolver returns ``None``.
"""

import datetime as dt

import django.test

from teatree.core import mode_resolution
from teatree.core.models import ConfigSetting, Loop, Mode, ModeOverride, ModeSchedule, ModeScheduleSlot
from teatree.loop.preset_resolution import ACTIVE_SCHEDULE_SETTING

#: A registered, live-tick loop carrying no manual override, so only a mode opinion can
#: admit it — the exact shape of the six loops #4196 found starved.
LOOP = "inbox"
AWAY_MODE = "afk-4196"
PRESENT_MODE = "present-4196"
SCHEDULE = "calendar-4196"


class ModeWithoutOverrideMixin(django.test.TestCase):
    """An un-overridden loop plus the modes, with no ``ModeOverride`` in play."""

    def setUp(self) -> None:
        super().setUp()
        Loop.objects.all().delete()
        Mode.objects.all().delete()
        ModeOverride.objects.all().delete()
        ModeSchedule.objects.all().delete()

        self.loop = Loop.objects.create(name=LOOP, script=f"src/teatree/loops/{LOOP}/loop.py", delay_seconds=60)
        Mode.objects.create(name=AWAY_MODE, entries={LOOP: False})
        Mode.objects.create(name=PRESENT_MODE, entries={LOOP: True})

    def activate_away_schedule_slot(self) -> None:
        """Point ``active_loop_schedule`` at an all-hours slot naming the masking mode."""
        schedule = ModeSchedule.objects.create(name=SCHEDULE, timezone="UTC")
        ModeScheduleSlot.objects.create(
            schedule=schedule, days=[0, 1, 2, 3, 4, 5, 6], start_time=dt.time(0, 0), preset_name=AWAY_MODE
        )
        ConfigSetting.objects.set_value(ACTIVE_SCHEDULE_SETTING, SCHEDULE)

    def use_l0_default_mode(self) -> None:
        """No schedule and no override — the ``default_mode`` row is the only opinion."""
        ConfigSetting.objects.set_value(ACTIVE_SCHEDULE_SETTING, "")
        ConfigSetting.objects.set_value(mode_resolution.DEFAULT_MODE_SETTING, PRESENT_MODE)
