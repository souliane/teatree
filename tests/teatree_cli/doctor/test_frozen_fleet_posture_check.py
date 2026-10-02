"""The doctor FAIL for a fleet a posture stopped and nobody restarted.

Every sibling detector in ``self_heal`` is gated on the fleet admitting work, and
``_check_stale_loop_timer`` structurally cannot see this class: the reconciler re-heads
every chain the halted timers drain, so the READY timers stay fresh while no loop has
ticked for weeks. A fleet frozen BY its posture therefore raised no doctor FAIL at all.

The detector closes that gap on the CONJUNCTION only. A stopping posture is a sanctioned
operator action — a gate that reddens the moment one is picked is one people learn to
ignore — so it fires only once the fleet that posture stopped is provably frozen.
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
from teatree.core.models import Loop, Mode, ModeOverride, Prompt
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


def _health(*, fleet_admits: bool, frozen: bool) -> LoopHealth:
    stale = (StaleLoop(name="tickets", cadence_seconds=300, age_seconds=1228800, ever_ran=True, suppressed=False),)
    return LoopHealth(
        admission=Admission(mode="engaged", source="default", admitted=("tickets",), admitted_total=1),
        stale=stale if frozen else (),
        considered=1,
        fleet_admits=fleet_admits,
    )


class FrozenFleetPostureCheckTest(django.test.SimpleTestCase):
    def test_a_stopping_posture_over_a_frozen_fleet_fails_and_names_the_preset(self) -> None:
        with mock.patch(f"{_MOD}._loop_health", return_value=_health(fleet_admits=False, frozen=True)):
            ok, out = _echoes(check_frozen_fleet_under_kill_switch)
        assert ok is False
        assert "FAIL" in out
        assert "admits ZERO loops" in out
        assert "'engaged'" in out

    def test_a_stopping_posture_over_a_ticking_fleet_is_silent(self) -> None:
        # The operator's maintenance window: switched minutes ago, nothing dead yet.
        with mock.patch(f"{_MOD}._loop_health", return_value=_health(fleet_admits=False, frozen=False)):
            ok, out = _echoes(check_frozen_fleet_under_kill_switch)
        assert ok is True
        assert out == ""

    def test_a_frozen_fleet_that_admits_work_is_left_to_its_own_cause(self) -> None:
        # A different cause (a mode mask, a wedged worker) owns that shape and the
        # sibling detectors already report it — this one must not double-report it
        # under a remedy that would not help.
        with mock.patch(f"{_MOD}._loop_health", return_value=_health(fleet_admits=True, frozen=True)):
            ok, out = _echoes(check_frozen_fleet_under_kill_switch)
        assert ok is True
        assert out == ""

    def test_an_unreadable_reading_degrades_to_a_pass(self) -> None:
        with mock.patch(f"{_MOD}._loop_health", side_effect=OSError("control db gone")):
            ok, out = _echoes(check_frozen_fleet_under_kill_switch)
        assert ok is True
        assert "WARN" in out


class FrozenFleetPostureWiringTest(django.test.TestCase):
    """The detector is reached by the doctor run, over real rows and the live posture."""

    def setUp(self) -> None:
        super().setUp()
        Loop.objects.all().delete()
        prompt, _ = Prompt.objects.get_or_create(name="demo-prompt", defaults={"body": "do x"})
        for name in ("tickets", "dispatch"):
            # No manual override: the preset alone decides, which is what the posture
            # arm under test reads.
            Loop.objects.create(
                name=name,
                prompt=prompt,
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

    def test_a_stopping_posture_over_a_fortnight_dead_fleet_fails(self) -> None:
        Mode.objects.all().delete()
        ModeOverride.objects.all().delete()
        Mode.objects.create(name="stopped", entries=dict.fromkeys(("tickets", "dispatch"), False))
        ModeOverride.objects.set_override("stopped", reason="pinned by the test")
        ok, out = self._run()
        assert ok is False
        assert "admits ZERO loops" in out

    def test_the_same_dead_fleet_under_an_admitting_posture_does_not_fire_here(self) -> None:
        Mode.objects.all().delete()
        ModeOverride.objects.all().delete()
        Mode.objects.create(name="running", entries=dict.fromkeys(("tickets", "dispatch"), True))
        ModeOverride.objects.set_override("running", reason="pinned by the test")
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
