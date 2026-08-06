"""``_check_loop_schedule_liveness`` — the `t3 doctor` stopped-chain FAIL (#4140).

The scheduledness reading no cadence surface carries: ``t3 loop list`` reported
``issue_implementer`` as ``last 7m04s … in 22m55s`` throughout a 61-minute outage,
because a manual ``t3 loops tick`` moves the anchor without restoring the chain. Its
verdict IS returned for the doctor's pass/fail aggregation, and a crashed read degrades
to OK so a doctor run never reddens on the alarm's own failure.
"""

from unittest.mock import patch

import django.test

from teatree.cli.doctor.checks_loop import _check_loop_schedule_liveness
from teatree.loops.schedule_liveness import UnscheduledLoop

_TARGET = "teatree.loops.schedule_liveness.unscheduled_loops"


class TestScheduleLivenessDoctorCheck(django.test.TestCase):
    def test_a_fully_scheduled_fleet_passes(self) -> None:
        with patch(_TARGET, return_value=()):
            assert _check_loop_schedule_liveness() is True

    def test_a_stopped_chain_fails(self) -> None:
        with patch(_TARGET, return_value=(UnscheduledLoop(name="issue_implementer", corpse_timers=1),)):
            assert _check_loop_schedule_liveness() is False

    def test_a_crashed_read_degrades_to_ok(self) -> None:
        with patch(_TARGET, side_effect=RuntimeError("db gone")):
            assert _check_loop_schedule_liveness() is True

    def test_the_reason_distinguishes_a_corpse_from_no_row_at_all(self) -> None:
        assert "past their tick deadline" in UnscheduledLoop(name="x", corpse_timers=2).reason
        assert "no loop_timer row at all" in UnscheduledLoop(name="x", corpse_timers=0).reason
