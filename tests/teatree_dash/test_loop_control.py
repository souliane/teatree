"""Loop-control read model: effective verdict + deciding layer, and the action dispatch (#3162)."""

import pytest
from django.test import TestCase

from teatree.core.models.loop import Loop
from teatree.core.models.loop_state import LoopState, LoopStatus
from teatree.dash import loop_control


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
