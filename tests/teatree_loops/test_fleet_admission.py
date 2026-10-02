"""The fleet-admission reader IS the stop condition — there is no kill-switch (C1).

``loop_runner_enabled`` was an extra surface that never stopped the fleet: the reactive
queue drain ran while it was off and it never reached an in-flight sub-agent. What stops
the fleet is the active preset admitting zero loops, and this reader answers that with the
same three-valued shape the switch had — a read FAILURE is UNREADABLE, never silently NONE.
"""

from unittest.mock import patch

import django.test

from teatree.core.models import Loop, Mode, ModeOverride
from teatree.loop.loop_state_db import ControlPlanesUnreadableError
from teatree.loops import enable_verdict
from teatree.loops.enable_verdict import FleetAdmission, fleet_admits_work, read_fleet_admission

_LOOP = "inbox"
_RUNS = "runs-everything"
_STOPS = "stops-everything"


class _FleetFixture(django.test.TestCase):
    def setUp(self) -> None:
        super().setUp()
        Loop.objects.all().delete()
        Mode.objects.all().delete()
        ModeOverride.objects.all().delete()
        Loop.objects.create(name=_LOOP, script=f"src/teatree/loops/{_LOOP}/loop.py", delay_seconds=60)
        Mode.objects.create(name=_RUNS, entries={_LOOP: True})
        Mode.objects.create(name=_STOPS, entries={_LOOP: False})

    def activate(self, preset: str) -> None:
        ModeOverride.objects.set_override(preset, reason="pinned by the test")


class TestReadFleetAdmission(_FleetFixture):
    def test_admits_when_the_active_preset_runs_a_loop(self) -> None:
        self.activate(_RUNS)
        assert read_fleet_admission() is FleetAdmission.ADMITS

    def test_none_when_the_active_preset_runs_nothing(self) -> None:
        self.activate(_STOPS)
        assert read_fleet_admission() is FleetAdmission.NONE

    def test_unreadable_when_a_control_plane_cannot_be_read(self) -> None:
        self.activate(_RUNS)
        with patch.object(enable_verdict, "membership_loop_names", side_effect=ControlPlanesUnreadableError("hold")):
            assert read_fleet_admission() is FleetAdmission.UNREADABLE

    def test_read_failure_is_loud(self) -> None:
        self.activate(_RUNS)
        with (
            patch.object(enable_verdict, "membership_loop_names", side_effect=RuntimeError("db blip")),
            self.assertLogs(enable_verdict.logger.name, level="WARNING") as captured,
        ):
            read_fleet_admission()
        assert any("admits" in line for line in captured.output)


class TestFleetAdmitsWorkFailsSafe(_FleetFixture):
    def test_true_only_when_a_loop_is_admitted(self) -> None:
        self.activate(_RUNS)
        assert fleet_admits_work() is True

    def test_unreadable_maps_to_false_so_no_chain_is_perpetuated(self) -> None:
        self.activate(_RUNS)
        with patch.object(enable_verdict, "membership_loop_names", side_effect=ControlPlanesUnreadableError("hold")):
            assert fleet_admits_work() is False
