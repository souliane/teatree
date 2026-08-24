"""``_check_box_occupancy`` — the factory's own count beside the whole box's load (#4407).

The state nothing reported: a parallel fan-out took the box from load 14 to 53 and 16 GB
to 1 GB free while the factory correctly held intake at 3. Every factory surface counts
only what the factory started, so all of them read green at once and throughput went to
zero for over an hour. This check is the surface that reports the difference.

Advisory by design: foreign load is a fact about the machine, not a fault in the factory,
so the verdict is always True and the run never reddens on it.
"""

from unittest.mock import patch

import django.test

import teatree.cli.doctor.run_checks as doctor_runner
from teatree.cli.doctor.checks_admission_pressure import _check_box_occupancy
from teatree.core.admission_governor import BRAKE_LOAD_PER_CORE, MachineSignal, QuotaSignal
from teatree.core.models import Task

_CORES = 8
_WATERMARK = BRAKE_LOAD_PER_CORE * _CORES
_MACHINE = "teatree.core.admission_governor.read_machine_signal"


_QUOTA = "teatree.core.admission_governor.read_quota_signal"
_WEEK = 7 * 24 * 3600


def _quota(weekly: float = 0.0) -> QuotaSignal:
    return QuotaSignal(
        fresh=True,
        all_accounts_exhausted=False,
        weekly_utilization=weekly,
        short_utilization=0.0,
        seconds_to_weekly_reset=_WEEK * 0.02,
    )


def _report(load1: float, *, agents: int = 3, weekly: float = 0.0) -> str:
    """The check's own output for a stated box, with its verdict asserted advisory."""
    machine = MachineSignal(cores=_CORES, load1=load1, ram_available_gb=20.0)
    with (
        patch(_MACHINE, return_value=machine),
        patch(_QUOTA, return_value=_quota(weekly)),
        patch.object(Task.objects, "claimed_agent_count", return_value=agents),
        patch("typer.echo") as echo,
    ):
        assert _check_box_occupancy() is True
    return "\n".join(str(call.args[0]) for call in echo.call_args_list)


class TestReportsThePressureScalar(django.test.TestCase):
    """#4508 — the operator's read of the one number the decisions consult.

    Load alone cannot show why admission is refusing: the two dimensions that halt a
    factory most often are quota-shaped and leave the load average looking healthy.
    """

    def test_a_quiet_box_names_its_band(self) -> None:
        assert "pressure 0." in _report(1.0)
        assert "FULL" in _report(1.0)

    def test_a_spent_window_names_the_cause_a_load_reading_cannot_show(self) -> None:
        report = _report(1.0, weekly=0.92)
        assert "SHED" in report
        assert "weekly-quota" in report

    def test_an_unreadable_probe_degrades_to_the_load_line(self) -> None:
        machine = MachineSignal(cores=_CORES, load1=1.0, ram_available_gb=20.0)
        with (
            patch(_MACHINE, return_value=machine),
            patch(_QUOTA, side_effect=RuntimeError("boom")),
            patch.object(Task.objects, "claimed_agent_count", return_value=1),
            patch("typer.echo") as echo,
        ):
            assert _check_box_occupancy() is True
        assert "box load" in "\n".join(str(call.args[0]) for call in echo.call_args_list)


class TestAlwaysPrintsBothNumbers(django.test.TestCase):
    """Both numbers, on every run.

    The actionable thing is the DIFFERENCE between the two counts, which a check that
    speaks only when it is unhappy can never show.
    """

    def test_a_quiet_box_still_reports_the_pair(self) -> None:
        report = _report(2.0)

        assert report.startswith("OK    Box occupancy")
        assert "factory agents in flight: 3" in report
        assert "box load 2.0 on 8 core(s)" in report

    def test_a_saturated_box_warns_and_names_both_sides(self) -> None:
        report = _report(53.0)

        assert report.startswith("WARN  Box saturated")
        assert "factory agents in flight: 3" in report
        assert "box load 53.0 on 8 core(s)" in report

    def test_the_warn_fires_at_the_same_watermark_the_governor_brakes_on(self) -> None:
        assert _report(_WATERMARK).startswith("WARN")
        assert _report(_WATERMARK - 0.1).startswith("OK")

    def test_an_idle_factory_on_a_thrashing_box_is_still_reported(self) -> None:
        # The recorded shape: nothing the factory owns is running, and the box is melting.
        assert _report(53.0, agents=0).startswith("WARN")


class TestWiredIntoTheRun(django.test.TestCase):
    def test_the_doctor_run_actually_calls_it(self) -> None:
        # An advisory check returns True either way, so no aggregate verdict can prove it
        # runs — and a check no orchestration list evaluates prints for nobody.
        with patch.object(doctor_runner, "_check_box_occupancy", return_value=True) as called:
            doctor_runner._run_loop_intent_gates()
        assert called.called


class TestNeverRedensTheRun(django.test.TestCase):
    def test_saturation_is_a_warn_not_a_fail(self) -> None:
        # A red run the operator can do nothing about from inside the factory trains
        # them to ignore red runs — `_report` asserts the True verdict.
        assert _report(999.0, agents=0)

    def test_a_crashed_read_degrades_to_ok(self) -> None:
        with patch(_MACHINE, side_effect=RuntimeError("no /proc")), patch("typer.echo") as echo:
            assert _check_box_occupancy() is True
        assert "Box-occupancy check crashed" in str(echo.call_args_list[0].args[0])
