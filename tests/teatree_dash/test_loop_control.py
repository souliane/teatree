"""Loop-control read model: effective verdict + deciding layer, and the action dispatch (#3162)."""

import datetime as dt
from unittest.mock import patch

import pytest
from django.test import TestCase, override_settings

from teatree.core.models import ConfigSetting, Mode, ModeOverride, ModeSchedule, ModeScheduleSlot
from teatree.core.models.loop import Loop
from teatree.core.models.loop_state import LoopState, LoopStatus
from teatree.dash import loop_control
from teatree.loop.preset_resolution import ACTIVE_SCHEDULE_SETTING
from teatree.loops.preset_status import LoopVerdict


def _make_loop(name: str = "dashloop") -> Loop:
    return Loop.objects.create(name=name, script="teatree.loops.review", delay_seconds=60)


class LoopRowsTestCase(TestCase):
    def test_enabled_loop_is_effective_at_l1(self) -> None:
        _make_loop()
        row = next(r for r in loop_control.build_loop_rows() if r.name == "dashloop")
        assert row.effective is True
        assert row.deciding_layer == "L1 — enabled"

    def test_paused_loop_held_at_l4(self) -> None:
        _make_loop()
        LoopState.objects.pause("dashloop")
        row = next(r for r in loop_control.build_loop_rows() if r.name == "dashloop")
        assert row.effective is False
        assert "paused" in row.deciding_layer

    def test_disabled_flag_decides_at_l1(self) -> None:
        _make_loop()
        Loop.objects.filter(name="dashloop").update(enabled=False)
        row = next(r for r in loop_control.build_loop_rows() if r.name == "dashloop")
        assert row.effective is False
        assert "Loop.enabled off" in row.deciding_layer


class LoopRowsPresetMaskTestCase(TestCase):
    """The dashboard verdict honours the #3159 preset mask, not just enabled+held."""

    def _activate(self, preset_name: str, entries: dict[str, bool]) -> None:
        Mode.objects.create(name=preset_name, entries=entries)
        ModeOverride.objects.set_override(preset_name)

    def test_preset_masked_off_loop_is_not_effective(self) -> None:
        _make_loop()
        self._activate("heads-down", {"dashloop": False})
        row = next(r for r in loop_control.build_loop_rows() if r.name == "dashloop")
        assert row.effective is False
        assert "override" in row.deciding_layer

    def test_preset_forced_on_masks_a_base_disabled_loop_on(self) -> None:
        _make_loop()
        Loop.objects.filter(name="dashloop").update(enabled=False)
        self._activate("engaged", {"dashloop": True})
        row = next(r for r in loop_control.build_loop_rows() if r.name == "dashloop")
        assert row.effective is True
        assert "override" in row.deciding_layer

    def test_hold_still_wins_over_a_force_on_preset(self) -> None:
        _make_loop()
        LoopState.objects.pause("dashloop")
        self._activate("engaged", {"dashloop": True})
        row = next(r for r in loop_control.build_loop_rows() if r.name == "dashloop")
        assert row.effective is False
        assert "paused" in row.deciding_layer

    def test_disabled_via_loopstate_reads_l4_hold_disabled(self) -> None:
        _make_loop()
        LoopState.objects.disable("dashloop")
        row = next(r for r in loop_control.build_loop_rows() if r.name == "dashloop")
        assert row.effective is False
        assert row.deciding_layer == "L4 hold — disabled"

    @override_settings(USE_TZ=True, TIME_ZONE="UTC")
    def test_active_schedule_slot_decides_at_l2(self) -> None:
        _make_loop()
        Mode.objects.create(name="heads-down", entries={"dashloop": False})
        schedule = ModeSchedule.objects.create(name="standard", timezone="UTC")
        # An all-day, every-weekday slot always governs "now".
        ModeScheduleSlot.objects.create(
            schedule=schedule, days=[0, 1, 2, 3, 4, 5, 6], start_time=dt.time(0, 0), preset_name="heads-down"
        )
        ConfigSetting.objects.set_value(ACTIVE_SCHEDULE_SETTING, "standard")
        row = next(r for r in loop_control.build_loop_rows() if r.name == "dashloop")
        assert row.effective is False
        assert "L2 schedule" in row.deciding_layer
        assert "masked" in row.deciding_layer


class LoopRowsRaceSafetyTestCase(TestCase):
    """A verdict whose ``Loop`` row vanished between the two reads is skipped, not a KeyError."""

    def test_verdict_without_a_loop_row_is_skipped(self) -> None:
        _make_loop("present")
        phantom = LoopVerdict(name="ghost-loop", admitted=True, layer="base", detail="Loop.enabled")
        real = LoopVerdict(name="present", admitted=True, layer="base", detail="Loop.enabled")
        with patch("teatree.dash.loop_control.effective_verdicts", return_value=[phantom, real]):
            names = {row.name for row in loop_control.build_loop_rows()}
        assert names == {"present"}


class ApplyLoopActionTestCase(TestCase):
    def setUp(self) -> None:
        _make_loop()

    def test_pause_is_reversible_hold(self) -> None:
        landed = loop_control.apply_loop_action("pause", "dashloop")
        assert landed == LoopStatus.PAUSED.value
        assert Loop.objects.get(name="dashloop").enabled is True

    def test_unknown_action_raises(self) -> None:
        with pytest.raises(loop_control.LoopActionError):
            loop_control.apply_loop_action("nuke", "dashloop")

    def test_unknown_loop_raises(self) -> None:
        with pytest.raises(loop_control.LoopActionError):
            loop_control.apply_loop_action("pause", "ghost")


class BuildLoopControlTestCase(TestCase):
    def test_view_carries_rows_and_header_state(self) -> None:
        _make_loop()
        view = loop_control.build_loop_control()
        assert any(r.name == "dashloop" for r in view.loops)
        assert view.availability_mode
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
