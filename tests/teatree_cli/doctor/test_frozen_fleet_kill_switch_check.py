"""The doctor FAIL for a fleet the kill-switch stopped and nobody turned back on.

Every sibling detector in ``self_heal`` is gated on ``loop_runner_enabled`` being ON, and
``_check_stale_loop_timer`` structurally cannot see this class: the reconciler re-heads
every chain the halted timers drain, so the READY timers stay fresh while no loop has
ticked for weeks. A fleet frozen BY the kill-switch therefore raised no doctor FAIL at
all.

The detector closes that gap on the CONJUNCTION only. An OFF switch is a sanctioned
operator action — a gate that reddens the moment it is flipped is one people learn to
ignore — so it fires only once the fleet the switch stopped is provably frozen.
"""

import datetime as dt
import io
from collections.abc import Callable
from contextlib import redirect_stdout
from unittest import mock

import django.test
from django.utils import timezone

from teatree.cli.doctor import self_heal, self_heal_frozen_fleet
from teatree.cli.doctor.self_heal_frozen_fleet import check_frozen_fleet_under_kill_switch
from teatree.core.models import ConfigSetting, Loop, Prompt
from teatree.loops.base import MiniLoop
from teatree.loops.loop_staleness import Admission, LoopHealth, StaleLoop

_MOD = "teatree.cli.doctor.self_heal_frozen_fleet"
_REGISTRY_SEAM = "teatree.loops.registry.iter_loops"
_ADMITTED_SEAM = "teatree.loops.loop_table.admitted_loop_names"


def _echoes(check: Callable[[], bool]) -> tuple[bool, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        ok = check()
    return ok, buf.getvalue()


def _health(*, runner_enabled: bool, frozen: bool) -> LoopHealth:
    stale = (StaleLoop(name="tickets", cadence_seconds=300, age_seconds=1228800, ever_ran=True, suppressed=False),)
    return LoopHealth(
        admission=Admission(mode="engaged", source="default", admitted=("tickets",), admitted_total=1),
        stale=stale if frozen else (),
        considered=1,
        runner_enabled=runner_enabled,
    )


class FrozenFleetKillSwitchCheckTest(django.test.SimpleTestCase):
    def test_an_off_switch_over_a_frozen_fleet_fails_and_names_the_switch(self) -> None:
        with mock.patch(f"{_MOD}._loop_health", return_value=_health(runner_enabled=False, frozen=True)):
            ok, out = _echoes(check_frozen_fleet_under_kill_switch)
        assert ok is False
        assert "FAIL" in out
        assert "loop_runner_enabled" in out

    def test_an_off_switch_over_a_ticking_fleet_is_silent(self) -> None:
        # The operator's maintenance window: flipped OFF minutes ago, nothing dead yet.
        with mock.patch(f"{_MOD}._loop_health", return_value=_health(runner_enabled=False, frozen=False)):
            ok, out = _echoes(check_frozen_fleet_under_kill_switch)
        assert ok is True
        assert out == ""

    def test_a_frozen_fleet_with_the_switch_on_is_left_to_its_own_cause(self) -> None:
        # A different cause (a mode mask, a wedged worker) owns that shape and the
        # sibling detectors already report it — this one must not double-report it
        # under a remedy that would not help.
        with mock.patch(f"{_MOD}._loop_health", return_value=_health(runner_enabled=True, frozen=True)):
            ok, out = _echoes(check_frozen_fleet_under_kill_switch)
        assert ok is True
        assert out == ""

    def test_an_unreadable_reading_degrades_to_a_pass(self) -> None:
        with mock.patch(f"{_MOD}._loop_health", side_effect=OSError("control db gone")):
            ok, out = _echoes(check_frozen_fleet_under_kill_switch)
        assert ok is True
        assert "WARN" in out


class FrozenFleetKillSwitchWiringTest(django.test.TestCase):
    """The detector is reached by the doctor run, over real rows and the stored switch."""

    def setUp(self) -> None:
        super().setUp()
        Loop.objects.all().delete()
        prompt, _ = Prompt.objects.get_or_create(name="demo-prompt", defaults={"body": "do x"})
        for name in ("tickets", "dispatch"):
            Loop.objects.create(
                name=name,
                prompt=prompt,
                enabled=True,
                delay_seconds=300,
                last_run_at=timezone.now() - dt.timedelta(days=14),
            )
        self.registry = tuple(
            MiniLoop(name=name, default_cadence_seconds=300, build_jobs=lambda **_: [])
            for name in ("tickets", "dispatch")
        )

    def _run(self) -> tuple[bool, str]:
        with (
            mock.patch(_REGISTRY_SEAM, return_value=self.registry),
            mock.patch(_ADMITTED_SEAM, return_value=["tickets", "dispatch"]),
        ):
            return _echoes(check_frozen_fleet_under_kill_switch)

    def test_a_stored_off_switch_over_a_fortnight_dead_fleet_fails(self) -> None:
        ConfigSetting.objects.set_value("loop_runner_enabled", value=False)
        ok, out = self._run()
        assert ok is False
        assert "loop_runner_enabled" in out

    def test_the_same_dead_fleet_with_the_switch_stored_on_does_not_fire_here(self) -> None:
        ConfigSetting.objects.set_value("loop_runner_enabled", value=True)
        ok, out = self._run()
        assert ok is True
        assert out == ""

    def test_the_detector_is_reached_by_the_doctor_self_heal_run(self) -> None:
        # An unwired detector is a check that never runs — the same vacuity class this
        # detector exists to catch, so the wiring itself needs a control.
        assert self_heal.run_self_heal_checks() is True
        with (
            mock.patch.object(self_heal, "check_frozen_fleet_under_kill_switch", return_value=False),
            redirect_stdout(io.StringIO()),
        ):
            assert self_heal.run_self_heal_checks() is False
        assert self_heal.check_frozen_fleet_under_kill_switch is (
            self_heal_frozen_fleet.check_frozen_fleet_under_kill_switch
        )
