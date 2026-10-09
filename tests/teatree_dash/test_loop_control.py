"""Loop-control read model: effective verdict + deciding layer, and the action dispatch (#3162)."""

import datetime as dt
from unittest.mock import patch

from django.test import TestCase, override_settings

from teatree.core.models import ConfigSetting, Mode, ModeOverride, ModeSchedule, ModeScheduleSlot
from teatree.core.models.loop import Loop
from teatree.core.models.loop_state import LoopState
from teatree.dash import loop_control
from teatree.loop.preset_resolution import ACTIVE_SCHEDULE_SETTING
from teatree.loops.enable_verdict import LoopVerdict


def _make_loop(name: str = "dashloop") -> Loop:
    return Loop.objects.create(name=name, script="teatree.loops.review", delay_seconds=60)


class LoopRowsTestCase(TestCase):
    def test_an_un_overridden_loop_is_decided_by_the_preset(self) -> None:
        _make_loop()
        row = next(r for r in loop_control.build_loop_rows() if r.name == "dashloop")
        assert row.effective is True
        assert row.deciding_layer == "preset (default)"

    def test_paused_loop_held_at_l4(self) -> None:
        _make_loop()
        LoopState.objects.pause("dashloop")
        row = next(r for r in loop_control.build_loop_rows() if r.name == "dashloop")
        assert row.effective is False
        assert "paused" in row.deciding_layer

    def test_a_manual_override_decides_and_names_its_reason(self) -> None:
        _make_loop()
        Loop.objects.set_manual_override("dashloop", runs=False, reason="incident 42")
        row = next(r for r in loop_control.build_loop_rows() if r.name == "dashloop")
        assert row.effective is False
        assert row.deciding_layer == "manual override — incident 42"


class LoopRowTagsTestCase(TestCase):
    """The row's tags come from the ``MiniLoop`` declaration, never from the DB row."""

    def test_registered_loop_carries_its_declared_tags(self) -> None:
        row = next(r for r in loop_control.build_loop_rows() if r.name == "review")
        assert row.tags == ("ingress", "egress", "colleague", "ai")

    def test_local_only_loop_carries_determinism_alone(self) -> None:
        row = next(r for r in loop_control.build_loop_rows() if r.name == "db_backup")
        assert row.tags == ("deterministic",)

    def test_row_with_no_registered_loop_carries_no_tags(self) -> None:
        _make_loop("dash-unregistered")
        row = next(r for r in loop_control.build_loop_rows() if r.name == "dash-unregistered")
        assert row.tags == ()


class LoopRowsPresetMaskTestCase(TestCase):
    """The dashboard verdict honours the #3159 preset mask, not just enabled+held."""

    def _activate(self, preset_name: str, entries: dict[str, bool]) -> None:
        Mode.objects.create(name=preset_name, entries=entries)
        ModeOverride.objects.set_override(preset_name, reason="test override")

    def test_preset_masked_off_loop_is_not_effective(self) -> None:
        _make_loop()
        self._activate("dash-away", {"dashloop": False})
        row = next(r for r in loop_control.build_loop_rows() if r.name == "dashloop")
        assert row.effective is False
        assert row.deciding_layer == "preset (pinned)"

    def test_a_preset_admitted_loop_is_effective(self) -> None:
        _make_loop()
        self._activate("dash-present", {"dashloop": True})
        row = next(r for r in loop_control.build_loop_rows() if r.name == "dashloop")
        assert row.effective is True
        assert row.deciding_layer == "preset (pinned)"

    def test_hold_still_wins_over_a_force_on_preset(self) -> None:
        _make_loop()
        LoopState.objects.pause("dashloop")
        self._activate("dash-present", {"dashloop": True})
        row = next(r for r in loop_control.build_loop_rows() if r.name == "dashloop")
        assert row.effective is False
        assert "paused" in row.deciding_layer

    def test_disabled_via_loopstate_reads_a_hold(self) -> None:
        _make_loop()
        LoopState.objects.disable("dashloop")
        row = next(r for r in loop_control.build_loop_rows() if r.name == "dashloop")
        assert row.effective is False
        assert row.deciding_layer == "hold — disabled"

    @override_settings(USE_TZ=True, TIME_ZONE="UTC")
    def test_active_schedule_slot_decides_at_l2(self) -> None:
        _make_loop()
        Mode.objects.create(name="dash-away", entries={"dashloop": False})
        schedule = ModeSchedule.objects.create(name="standard", timezone="UTC")
        # An all-day, every-weekday slot always governs "now".
        ModeScheduleSlot.objects.create(
            schedule=schedule, days=[0, 1, 2, 3, 4, 5, 6], start_time=dt.time(0, 0), preset_name="dash-away"
        )
        ConfigSetting.objects.set_value(ACTIVE_SCHEDULE_SETTING, "standard")
        row = next(r for r in loop_control.build_loop_rows() if r.name == "dashloop")
        assert row.effective is False
        assert row.deciding_layer == "preset (schedule)"


class LoopRowsRaceSafetyTestCase(TestCase):
    """A verdict whose ``Loop`` row vanished between the two reads is skipped, not a KeyError."""

    def test_verdict_without_a_loop_row_is_skipped(self) -> None:
        _make_loop("dash-present")
        phantom = LoopVerdict(name="ghost-loop", admitted=True, layer="base", detail="Loop.enabled")
        real = LoopVerdict(name="dash-present", admitted=True, layer="base", detail="Loop.enabled")
        with patch("teatree.dash.loop_control.effective_verdicts", return_value=[phantom, real]):
            names = {row.name for row in loop_control.build_loop_rows()}
        assert names == {"dash-present"}


class BuildLoopControlTestCase(TestCase):
    def test_view_carries_rows_and_header_state(self) -> None:
        _make_loop()
        # The switcher renders one button per Mode row, so the header needs real rows.
        Mode.objects.all().delete()
        Mode.objects.create(name="present", entries={})
        view = loop_control.build_loop_control()
        assert any(r.name == "dashloop" for r in view.loops)
        assert view.mode_name
        assert view.mode_names == ("present",)
        assert view.gate_fail_open is False

    def test_view_survives_broken_gate_read(self) -> None:
        # The loop-control page previously read danger_gate_fail_open unguarded
        # and 500'd on a broken read; the shared guarded helper now fails closed
        # to False so the page renders (#3313).
        _make_loop()
        real = ConfigSetting.objects.get_effective

        def _raise_on_gate(key, *args, **kwargs):
            if key == "danger_gate_fail_open":
                msg = "db down"
                raise RuntimeError(msg)
            return real(key, *args, **kwargs)

        with patch.object(ConfigSetting.objects, "get_effective", side_effect=_raise_on_gate):
            view = loop_control.build_loop_control()
        assert view.gate_fail_open is False
