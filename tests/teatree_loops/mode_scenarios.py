"""The two ways a mode governs WITHOUT a manual override — shared #4196 setup.

Every #4185 test activated its mode through ``ModeOverride.objects.set_override``. That
is the ONE layer the preset resolver and the merged mode resolver read identically, and
the one the presence upgrade is documented never to touch — so membership and per-fire
admission agreed by construction, and a fully green suite sat on top of a membership set
that disagreed with the tick every weeknight.

These build the two configurations that actually separate the resolvers:

*   a SCHEDULE slot naming a presence-sensitive away-class mode, plus a fresh keystroke —
    the mode resolver upgrades to the present-class mode, the preset resolver stops at the
    slot's mode;
*   NO schedule and no override at all — the mode resolver falls through to the L0
    ``default_mode`` row, the preset resolver returns ``None`` and collapses to
    ``Loop.enabled``.
"""

import datetime as dt
import tempfile
from pathlib import Path
from unittest import mock

import django.test
from django.utils import timezone

from teatree.core import mode_resolution
from teatree.core.models import ConfigSetting, Loop, Mode, ModeOverride, ModeSchedule, ModeScheduleSlot
from teatree.live_presence import PresenceHeartbeat
from teatree.loop.preset_resolution import ACTIVE_SCHEDULE_SETTING

#: A registered, live-tick loop whose ``Loop.enabled`` column is OFF, so only a mode
#: opinion can admit it — the exact shape of the six loops #4196 found starved.
LOOP = "inbox"
AWAY_MODE = "away-4196"
PRESENT_MODE = "present-4196"
SCHEDULE = "calendar-4196"


class ModeWithoutOverrideMixin(django.test.TestCase):
    """A column-disabled loop plus the modes, with no ``ModeOverride`` in play."""

    def setUp(self) -> None:
        super().setUp()
        tmp = Path(tempfile.mkdtemp())
        patcher = mock.patch.object(mode_resolution, "PRESENCE", PresenceHeartbeat(lambda: tmp / "presence"))
        patcher.start()
        self.addCleanup(patcher.stop)

        Loop.objects.all().delete()
        Mode.objects.all().delete()
        ModeOverride.objects.all().delete()
        ModeSchedule.objects.all().delete()
        ConfigSetting.objects.set_value("loop_runner_enabled", value=True)
        ConfigSetting.objects.set_value(mode_resolution.PRESENCE_UPGRADE_SETTING, PRESENT_MODE)

        self.loop = Loop.objects.create(
            name=LOOP, script=f"src/teatree/loops/{LOOP}/loop.py", delay_seconds=60, enabled=False
        )
        Mode.objects.create(name=AWAY_MODE, entries={LOOP: False}, defers_questions=True, presence_sensitive=True)
        Mode.objects.create(name=PRESENT_MODE, entries={LOOP: True}, defers_questions=False)

    def activate_away_schedule_slot(self) -> None:
        """Point ``active_loop_schedule`` at an all-hours slot naming the away mode."""
        schedule = ModeSchedule.objects.create(name=SCHEDULE, timezone="UTC")
        ModeScheduleSlot.objects.create(
            schedule=schedule, days=[0, 1, 2, 3, 4, 5, 6], start_time=dt.time(0, 0), preset_name=AWAY_MODE
        )
        ConfigSetting.objects.set_value(ACTIVE_SCHEDULE_SETTING, SCHEDULE)

    def use_l0_default_mode(self) -> None:
        """No schedule and no override — the L0 ``default_mode`` row is the only opinion."""
        ConfigSetting.objects.set_value(ACTIVE_SCHEDULE_SETTING, "")
        ConfigSetting.objects.set_value(mode_resolution.DEFAULT_MODE_SETTING, PRESENT_MODE)

    def record_fresh_keystroke(self, now: dt.datetime | None = None) -> None:
        mode_resolution.PRESENCE.record(session_id="s", now=now or timezone.now())
