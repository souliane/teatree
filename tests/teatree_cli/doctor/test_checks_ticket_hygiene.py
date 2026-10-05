"""The ticket-sweep trend doctor check (#162 Rule 4).

The check is surfacing-only ON PURPOSE, so the tests that matter are the ones
pinning it CANNOT gate: a nonzero count, a crashed read and an unfinished run all
still return True. The inverse of "reports a rising count" is not "fails" — it is
"reports it and lets the operator judge".
"""

from unittest.mock import patch

import pytest
from django.db import DatabaseError
from django.test import TestCase

from teatree.cli.doctor.checks_ticket_hygiene import _check_ticket_sweep_trend
from teatree.core.models import TicketSweepRun

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

_A = "https://gitlab.com/acme/widgets/-/issues/1"


def _finished(*, changed: int) -> TicketSweepRun:
    run = TicketSweepRun.objects.begin(source="loop")
    for index in range(changed):
        TicketSweepRun.objects.record_change(run_id=run.run_id, issue_url=f"{_A}{index}")
    return TicketSweepRun.objects.finish(run_id=run.run_id, examined_count=10)


class TestTicketSweepTrendCheck(TestCase):
    def _run(self) -> tuple[bool, str]:
        with patch("teatree.cli.doctor.checks_ticket_hygiene.typer.echo") as echo:
            ok = _check_ticket_sweep_trend()
        return ok, "\n".join(str(call.args[0]) for call in echo.call_args_list)

    def test_no_runs_yet_says_so_and_still_passes(self) -> None:
        ok, output = self._run()
        assert ok is True
        assert "no finished run" in output

    def test_reports_the_latest_count_the_series_and_the_zero_streak(self) -> None:
        _finished(changed=3)
        _finished(changed=0)
        _finished(changed=0)
        ok, output = self._run()
        assert ok is True
        assert "latest changed 0" in output
        assert "3 0 0" in output, "the series reads oldest-first so the trend direction is visible"
        assert "2 consecutive zero-change run(s)" in output

    def test_a_rising_count_is_reported_but_never_gates(self) -> None:
        _finished(changed=7)
        ok, output = self._run()
        assert ok is True, "a threshold-free advisory must not redden the exit code"
        assert "latest changed 7" in output

    def test_an_unfinished_run_is_warned_about(self) -> None:
        _finished(changed=0)
        stale = TicketSweepRun.objects.begin(source="interactive")
        ok, output = self._run()
        assert ok is True
        assert "never finished" in output
        assert stale.run_id in output

    def test_a_crashed_read_warns_and_still_passes(self) -> None:
        with patch.object(TicketSweepRun.objects, "recent", side_effect=DatabaseError("db gone")):
            ok, output = self._run()
        assert ok is True
        assert "crashed" in output

    def test_a_non_database_error_propagates(self) -> None:
        with (
            patch.object(TicketSweepRun.objects, "recent", side_effect=RuntimeError("bad logic")),
            pytest.raises(RuntimeError, match="bad logic"),
        ):
            self._run()
