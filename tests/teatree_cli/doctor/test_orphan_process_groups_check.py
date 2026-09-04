"""``_check_orphaned_process_groups`` — the load nobody owns (#4580).

The state nothing reported: 37 shells from a dead fuzz run held one leaderless group for
9 days 10 hours, removing ~58% of admitted capacity through the load average while every
per-process metric read healthy. ``_check_box_occupancy`` saw the surplus and could not
explain it; this check is the explanation, so it sits beside it.

Advisory by design: a leaderless group is a fact about the machine, not a fault in the
factory, so the verdict is always True and the run never reddens on it.
"""

from unittest.mock import patch

import django.test

import teatree.cli.doctor.run_checks as doctor_runner
from teatree.cli.doctor.checks_admission_pressure import _check_orphaned_process_groups
from teatree.core.cleanup.orphan_process_groups import GroupMember, OrphanGroup, OrphanSurvey

_SURVEY = "teatree.core.cleanup.orphan_process_groups.survey_orphan_groups"


def _group(*, pgid: int = 4076652, signalable: bool = True) -> OrphanGroup:
    return OrphanGroup(
        pgid=pgid,
        members=(GroupMember(pid=pgid + 1, comm="bash", state="R", argv=("/bin/bash", "-c", "while :")),),
        age_seconds=9.4 * 24 * 3600,
        cpu_seconds=5.4 * 24 * 3600,
        signalable=signalable,
        source="/proc" if signalable else "/host-proc",
    )


def _report(survey: OrphanSurvey) -> str:
    with patch(_SURVEY, return_value=survey), patch("typer.echo") as echo:
        assert _check_orphaned_process_groups() is True
    return "\n".join(str(call.args[0]) for call in echo.call_args_list)


class TestNamesTheGroupAndItsRemedy(django.test.TestCase):
    def test_a_signalable_group_is_warned_with_the_reap_command(self) -> None:
        report = _report(OrphanSurvey(groups=(_group(),), gaps=()))

        assert report.startswith("WARN")
        assert "4076652" in report
        assert "t3 tool reap-orphan-groups --pgid 4076652 --apply" in report

    def test_an_unsignalable_group_names_the_host_command_instead(self) -> None:
        report = _report(OrphanSurvey(groups=(_group(signalable=False),), gaps=()))

        assert "kill -TERM -4076652" in report
        assert "reap-orphan-groups" not in report

    def test_the_admission_cost_is_named_so_the_cpu_reading_does_not_dismiss_it(self) -> None:
        # The whole trap: ~0.0% instantaneous CPU reads harmless, and the cost is load.
        assert "load" in _report(OrphanSurvey(groups=(_group(),), gaps=())).lower()


class TestSilentWhenThereIsNothingToSay(django.test.TestCase):
    def test_a_clean_box_prints_nothing(self) -> None:
        assert _report(OrphanSurvey(groups=(), gaps=())) == ""


class TestAnUnreadableTableIsNeverAnEmptyPass(django.test.TestCase):
    def test_a_gap_is_reported_rather_than_read_as_no_orphans(self) -> None:
        report = _report(OrphanSurvey(groups=(), gaps=("no host process table at /host-proc",)))

        assert "WARN" in report
        assert "/host-proc" in report


class TestWiredIntoTheRun(django.test.TestCase):
    def test_the_doctor_run_actually_calls_it(self) -> None:
        # An advisory check returns True either way, so no aggregate verdict can prove it
        # runs — and a check no orchestration list evaluates prints for nobody.
        with patch.object(doctor_runner, "_check_orphaned_process_groups", return_value=True) as called:
            doctor_runner._run_loop_intent_gates()
        assert called.called


class TestNeverRedensTheRun(django.test.TestCase):
    def test_a_crashed_read_degrades_to_ok(self) -> None:
        with patch(_SURVEY, side_effect=RuntimeError("no /proc")), patch("typer.echo") as echo:
            assert _check_orphaned_process_groups() is True
        assert "Orphaned-process-group check crashed" in str(echo.call_args_list[0].args[0])
