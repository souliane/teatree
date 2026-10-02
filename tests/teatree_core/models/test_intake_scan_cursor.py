"""The intake resume point and pass-health ledger (#4466)."""

from django.test import TestCase

from teatree.core.models import IntakeScanCursor

OVERLAY = "acme"
ISSUE = "https://github.com/souliane/teatree/issues/4466"


class IntakeScanCursorTests(TestCase):
    def test_no_row_resumes_at_the_oldest_candidate(self) -> None:
        assert IntakeScanCursor.objects.resume_after(OVERLAY) == ""

    def test_a_recorded_pass_supplies_the_resume_point(self) -> None:
        IntakeScanCursor.objects.record_pass(OVERLAY, last_issue_url=ISSUE, complete=False)

        assert IntakeScanCursor.objects.resume_after(OVERLAY) == ISSUE

    def test_each_overlay_keeps_its_own_resume_point(self) -> None:
        IntakeScanCursor.objects.record_pass(OVERLAY, last_issue_url=ISSUE, complete=True)

        assert IntakeScanCursor.objects.resume_after("other") == ""

    def test_an_incomplete_pass_stamps_its_moment(self) -> None:
        row = IntakeScanCursor.objects.record_pass(OVERLAY, last_issue_url=ISSUE, complete=False)

        assert row.last_incomplete_at is not None
        assert row.last_complete_at is None

    def test_a_complete_pass_stamps_its_moment(self) -> None:
        row = IntakeScanCursor.objects.record_pass(OVERLAY, last_issue_url=ISSUE, complete=True)

        assert row.last_complete_at is not None

    def test_stalled_reads_nothing_below_the_threshold(self) -> None:
        IntakeScanCursor.objects.record_pass(OVERLAY, last_issue_url=ISSUE, complete=False)

        assert not list(IntakeScanCursor.objects.stalled(threshold=2))

    def test_stalled_reads_the_overlay_at_the_threshold(self) -> None:
        for _ in range(2):
            IntakeScanCursor.objects.record_pass(OVERLAY, last_issue_url=ISSUE, complete=False)

        assert [row.overlay for row in IntakeScanCursor.objects.stalled(threshold=2)] == [OVERLAY]

    def test_str_names_the_overlay_and_the_resume_point(self) -> None:
        row = IntakeScanCursor.objects.record_pass(OVERLAY, last_issue_url=ISSUE, complete=True)

        assert str(row) == f"{OVERLAY} @ {ISSUE}"

    def test_str_of_a_global_row_at_the_start(self) -> None:
        row = IntakeScanCursor.objects.record_pass("", last_issue_url="", complete=True)

        assert str(row) == "(global) @ (start)"
