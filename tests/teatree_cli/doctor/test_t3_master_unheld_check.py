"""``_check_t3_master_unheld_while_loops_tick`` — the unheld-owner-lease FAIL (#4253).

The state nothing reported: two reactive cycles no-oping every beat on an unheld
``t3-master`` lease while the worker drove every tick. ``t3 worker status`` exited 0,
the cadence surfaces read healthy, and the only notice was a log line — so the degraded
state ran for an hour and was fixed by chance. This check is the surface that reports it.

Its verdict is returned for the doctor's pass/fail aggregation, and a crashed read
degrades to OK so a doctor run never reddens on the alarm's own failure.
"""

from unittest.mock import patch

import django.test

from teatree.cli.doctor.app import _run_loop_intent_gates
from teatree.cli.doctor.checks_loop import _check_t3_master_unheld_while_loops_tick
from teatree.loops.master_lease_contradiction import UnheldMasterLease

_TARGET = "teatree.loops.master_lease_contradiction.unheld_master_lease_with_live_ticks"

_FINDING = UnheldMasterLease(ticking_loops=("review", "ship"), freshest_tick_seconds=3.0)


class TestT3MasterUnheldDoctorCheck(django.test.TestCase):
    def test_a_held_lease_passes(self) -> None:
        with patch(_TARGET, return_value=None):
            assert _check_t3_master_unheld_while_loops_tick() is True

    def test_an_unheld_lease_with_live_ticks_fails(self) -> None:
        with patch(_TARGET, return_value=_FINDING):
            assert _check_t3_master_unheld_while_loops_tick() is False

    def test_a_crashed_read_degrades_to_ok(self) -> None:
        with patch(_TARGET, side_effect=RuntimeError("db gone")):
            assert _check_t3_master_unheld_while_loops_tick() is True

    def test_the_fail_reaches_the_doctor_run_verdict(self) -> None:
        # Wired into the aggregation, not merely defined: a check that no orchestration
        # list evaluates is dead authority, and its FAIL would never reach an operator.
        with patch(_TARGET, return_value=_FINDING):
            assert _run_loop_intent_gates() is False


class TestFailMessage(django.test.TestCase):
    def _message(self) -> str:
        with patch(_TARGET, return_value=_FINDING), patch("typer.echo") as echo:
            _check_t3_master_unheld_while_loops_tick()
        return "\n".join(str(call.args[0]) for call in echo.call_args_list)

    def test_it_quotes_the_evidence_it_decided_on(self) -> None:
        message = self._message()

        assert "2 loop(s) are ticking on cadence (review, ship)" in message
        assert "freshest 3s ago" in message

    def test_it_names_the_lease_rather_than_asserting_the_factory_is_idle(self) -> None:
        # The whole point of the ticket: "nothing is driving the loops" was false while
        # every loop ticked. This surface reports BOTH facts and claims neither alone.
        message = self._message()

        assert "`t3-master` owner lease is unheld" in message
        assert "nothing is driving the loops" not in message

    def test_it_does_not_tell_an_operator_to_start_a_second_worker(self) -> None:
        # A running worker already holds the flock; "start `t3 worker`" would run a
        # second generation against one control DB — worse than the state it claims to fix.
        assert "Start `t3 worker`" not in self._message()
