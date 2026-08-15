"""An intake pass that never reaches the frontier is a doctor finding, not a log line (#4466).

Twenty-one abandoned passes in three hours left five filed issues unadmitted while every
health surface read green — the only witness was a WARN in the worker log.
"""

from django.test import TestCase

from teatree.cli.doctor.checks_loop import _check_intake_pass_incomplete
from teatree.core.models import INCOMPLETE_PASS_ALARM, IntakeScanCursor

OVERLAY = "acme"
ISSUE = "https://github.com/souliane/teatree/issues/4466"


class IntakePassIncompleteCheckTests(TestCase):
    def _record(self, times: int, *, complete: bool = False) -> None:
        for _ in range(times):
            IntakeScanCursor.objects.record_pass(OVERLAY, last_issue_url=ISSUE, complete=complete)

    def test_a_run_of_incomplete_passes_fails(self) -> None:
        self._record(INCOMPLETE_PASS_ALARM)

        assert _check_intake_pass_incomplete() is False

    def test_one_short_pass_is_not_an_alarm(self) -> None:
        self._record(1)

        assert _check_intake_pass_incomplete() is True

    def test_a_completed_pass_clears_the_alarm(self) -> None:
        self._record(INCOMPLETE_PASS_ALARM)

        self._record(1, complete=True)

        assert _check_intake_pass_incomplete() is True

    def test_no_cursor_at_all_is_quiet(self) -> None:
        assert _check_intake_pass_incomplete() is True

    def test_the_finding_names_the_overlay_and_the_resume_point(self) -> None:
        self._record(INCOMPLETE_PASS_ALARM)

        report = IntakeScanCursor.objects.get(overlay=OVERLAY).report()

        assert OVERLAY in report
        assert ISSUE in report
