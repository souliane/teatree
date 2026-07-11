"""teatree.loop.preset_resolution — the read-time preset mask (L3 override, L2 schedule).

Resolution order: a live ``LoopPresetOverride`` (L3) wins; else the active
schedule's governing slot (L2, the latest slot-start ≤ now); else no opinion. Every
layer fails open to ``None`` (base config) so a deleted preset / broken schedule /
unreadable DB can never brick the loop fleet. The empty-table no-op invariant lives
here too: no override + no active schedule ⇒ ``None`` for every loop.
"""

import datetime as dt
import zoneinfo

import django.test
from django.utils import timezone

from teatree.core.models import ConfigSetting, LoopPreset, LoopPresetOverride, LoopSchedule, LoopScheduleSlot
from teatree.loop.preset_resolution import (
    ACTIVE_SCHEDULE_SETTING,
    active_overlay_scope,
    next_boundary,
    resolve_active_preset,
    resolve_preset_state,
)


def _preset(name: str, entries: dict[str, bool], **kwargs: object) -> LoopPreset:
    return LoopPreset.objects.create(name=name, entries=entries, **kwargs)


def _activate_schedule(name: str) -> None:
    ConfigSetting.objects.set_value(ACTIVE_SCHEDULE_SETTING, name)


@django.test.override_settings(USE_TZ=True, TIME_ZONE="UTC")
class TestEmptyTableNoOp(django.test.TestCase):
    """No override, no active schedule, no preset ⇒ every loop resolves ``None`` (inherit base)."""

    def test_no_active_preset(self) -> None:
        assert resolve_active_preset() is None

    def test_every_loop_has_no_opinion(self) -> None:
        for name in ("inbox", "review", "dispatch", "ship", "dream"):
            assert resolve_preset_state(name) is None

    def test_a_preset_that_is_not_activated_has_no_effect(self) -> None:
        _preset("heads-down", {"review": False})
        assert resolve_active_preset() is None
        assert resolve_preset_state("review") is None

    def test_overlay_scope_empty_when_no_active_preset(self) -> None:
        assert active_overlay_scope() == []


@django.test.override_settings(USE_TZ=True, TIME_ZONE="UTC")
class TestManualOverride(django.test.TestCase):
    """L3: a live override selects the preset; a deleted/expired override fails open."""

    def test_override_selects_the_preset(self) -> None:
        _preset("heads-down", {"review": False, "dispatch": True})
        LoopPresetOverride.objects.set_override("heads-down")
        active = resolve_active_preset()
        assert active is not None
        assert active.layer == "override"
        assert resolve_preset_state("review") is False
        assert resolve_preset_state("dispatch") is True

    def test_absent_entry_is_inherit_not_off(self) -> None:
        _preset("heads-down", {"review": False})
        LoopPresetOverride.objects.set_override("heads-down")
        assert resolve_preset_state("issue_implementer") is None

    def test_expired_override_is_inert(self) -> None:
        _preset("off", {"review": False})
        past = timezone.now() - dt.timedelta(hours=1)
        LoopPresetOverride.objects.create(preset_name="off", until=past)
        assert resolve_active_preset() is None

    def test_override_naming_deleted_preset_fails_open(self) -> None:
        LoopPresetOverride.objects.set_override("ghost")
        assert resolve_active_preset() is None
        assert resolve_preset_state("review") is None

    def test_override_outranks_the_schedule(self) -> None:
        _preset("engaged", {"review": True})
        _preset("off", {"review": False})
        schedule = LoopSchedule.objects.create(name="standard", timezone="UTC")
        LoopScheduleSlot.objects.create(
            schedule=schedule, days=[0, 1, 2, 3, 4, 5, 6], start_time=dt.time(0, 0), preset_name="engaged"
        )
        _activate_schedule("standard")
        LoopPresetOverride.objects.set_override("off")
        assert resolve_preset_state("review") is False


@django.test.override_settings(USE_TZ=True, TIME_ZONE="UTC")
class TestScheduleSlots(django.test.TestCase):
    """L2: the governing slot is the latest start ≤ now, searching back across week wrap."""

    def setUp(self) -> None:
        _preset("engaged", {"review": True})
        _preset("maintenance", {"review": False, "dream": True})
        self.schedule = LoopSchedule.objects.create(name="standard", timezone="UTC")
        # Weekday day → engaged at 08:00, evening → maintenance at 19:00.
        LoopScheduleSlot.objects.create(
            schedule=self.schedule, days=[0, 1, 2, 3, 4], start_time=dt.time(8, 0), preset_name="engaged"
        )
        LoopScheduleSlot.objects.create(
            schedule=self.schedule, days=[0, 1, 2, 3, 4], start_time=dt.time(19, 0), preset_name="maintenance"
        )
        _activate_schedule("standard")

    def _monday(self, hour: int, minute: int = 0) -> dt.datetime:
        # 2026-07-13 is a Monday.
        return dt.datetime(2026, 7, 13, hour, minute, tzinfo=dt.UTC)

    def test_daytime_resolves_to_engaged(self) -> None:
        assert resolve_preset_state("review", now=self._monday(10)) is True

    def test_evening_resolves_to_maintenance(self) -> None:
        assert resolve_preset_state("review", now=self._monday(20)) is False
        assert resolve_preset_state("dream", now=self._monday(20)) is True

    def test_before_first_slot_wraps_back_to_prior_slot(self) -> None:
        # 06:00 Monday: the latest start ≤ now is the previous Friday 19:00 maintenance.
        assert resolve_preset_state("review", now=self._monday(6)) is False

    def test_next_boundary_is_the_upcoming_slot_start(self) -> None:
        boundary = next_boundary(now=self._monday(10))
        assert boundary == self._monday(19)

    def test_unknown_active_schedule_fails_open(self) -> None:
        _activate_schedule("nonexistent")
        assert resolve_active_preset(now=self._monday(10)) is None

    def test_slot_naming_deleted_preset_fails_open(self) -> None:
        LoopScheduleSlot.objects.create(
            schedule=self.schedule, days=[0, 1, 2, 3, 4], start_time=dt.time(9, 0), preset_name="ghost"
        )
        assert resolve_active_preset(now=self._monday(9, 30)) is None


@django.test.override_settings(USE_TZ=True, TIME_ZONE="UTC")
class TestScheduleTimezone(django.test.TestCase):
    """Slot starts are local wall-clock in the schedule's own zoneinfo, not project UTC."""

    def test_wall_clock_is_the_schedule_zone(self) -> None:
        _preset("engaged", {"review": True})
        _preset("maintenance", {"review": False})
        schedule = LoopSchedule.objects.create(name="tz", timezone="Europe/Zurich")
        LoopScheduleSlot.objects.create(
            schedule=schedule, days=[0, 1, 2, 3, 4], start_time=dt.time(8, 0), preset_name="engaged"
        )
        LoopScheduleSlot.objects.create(
            schedule=schedule, days=[0, 1, 2, 3, 4], start_time=dt.time(19, 0), preset_name="maintenance"
        )
        _activate_schedule("tz")
        zurich = zoneinfo.ZoneInfo("Europe/Zurich")
        # 09:00 Zurich local Monday = engaged; the same instant is 07:00 UTC.
        nine_zurich = dt.datetime(2026, 7, 13, 9, 0, tzinfo=zurich)
        assert resolve_preset_state("review", now=nine_zurich) is True
