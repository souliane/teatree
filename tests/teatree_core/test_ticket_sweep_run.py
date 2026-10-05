"""Rule 4 of #162: a sweep's changed-ticket count is measured, not claimed.

"Each sweep tends to zero" is only a metric if a zero-change sweep leaves a row
behind — otherwise "no row" and "nothing changed" are indistinguishable and the
trend is unreadable. The tests below pin that, the unique-URL dedupe (one ticket
touched twice is one change), and the immutability of a finished run.
"""

import pytest
from django.test import TestCase

from teatree.core.models import TicketSweepRun

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

_A = "https://gitlab.com/acme/widgets/-/issues/1"
_B = "https://gitlab.com/acme/widgets/-/issues/2"


class TestBeginAndFinish(TestCase):
    def test_a_begun_run_is_incomplete_until_finished(self) -> None:
        run = TicketSweepRun.objects.begin(source="interactive")
        assert run.finished_at is None
        assert TicketSweepRun.objects.incomplete().count() == 1
        TicketSweepRun.objects.finish(run_id=run.run_id, examined_count=3)
        assert TicketSweepRun.objects.incomplete().count() == 0

    def test_a_zero_change_sweep_still_persists_a_finished_run(self) -> None:
        run = TicketSweepRun.objects.begin(source="loop")
        finished = TicketSweepRun.objects.finish(run_id=run.run_id, examined_count=12)
        assert finished.changed_count == 0
        assert finished.finished_at is not None
        assert list(TicketSweepRun.objects.recent(limit=1)) == [finished]

    def test_finishing_an_unknown_run_is_refused(self) -> None:
        with pytest.raises(TicketSweepRun.DoesNotExist):
            TicketSweepRun.objects.finish(run_id="never-begun", examined_count=1)

    def test_a_finished_run_cannot_be_finished_again(self) -> None:
        run = TicketSweepRun.objects.begin(source="interactive")
        TicketSweepRun.objects.finish(run_id=run.run_id, examined_count=1)
        with pytest.raises(ValueError, match="already finished"):
            TicketSweepRun.objects.finish(run_id=run.run_id, examined_count=99)

    def test_an_unknown_source_is_refused(self) -> None:
        with pytest.raises(ValueError, match="source"):
            TicketSweepRun.objects.begin(source="whatever")


class TestChangedCount(TestCase):
    def test_the_same_ticket_touched_twice_counts_once(self) -> None:
        run = TicketSweepRun.objects.begin(source="interactive")
        TicketSweepRun.objects.record_change(run_id=run.run_id, issue_url=_A)
        TicketSweepRun.objects.record_change(run_id=run.run_id, issue_url=_A)
        TicketSweepRun.objects.record_change(run_id=run.run_id, issue_url=_B)
        finished = TicketSweepRun.objects.finish(run_id=run.run_id, examined_count=5)
        assert finished.changed_count == 2
        assert sorted(finished.changed_urls) == [_A, _B]

    def test_a_finished_run_rejects_a_late_change(self) -> None:
        run = TicketSweepRun.objects.begin(source="interactive")
        TicketSweepRun.objects.finish(run_id=run.run_id, examined_count=1)
        with pytest.raises(ValueError, match="finished"):
            TicketSweepRun.objects.record_change(run_id=run.run_id, issue_url=_A)
        run.refresh_from_db()
        assert run.changed_count == 0

    def test_a_change_against_an_unknown_run_is_refused(self) -> None:
        with pytest.raises(TicketSweepRun.DoesNotExist):
            TicketSweepRun.objects.record_change(run_id="never-begun", issue_url=_A)


class TestTrend(TestCase):
    def test_recent_returns_finished_runs_newest_first(self) -> None:
        for index in range(3):
            run = TicketSweepRun.objects.begin(source="loop")
            if index:
                TicketSweepRun.objects.record_change(run_id=run.run_id, issue_url=f"{_A}#{index}")
            TicketSweepRun.objects.finish(run_id=run.run_id, examined_count=1)
        counts = [run.changed_count for run in TicketSweepRun.objects.recent(limit=3)]
        assert counts == [1, 1, 0]

    def test_recent_excludes_an_unfinished_run(self) -> None:
        run = TicketSweepRun.objects.begin(source="loop")
        TicketSweepRun.objects.finish(run_id=run.run_id, examined_count=1)
        TicketSweepRun.objects.begin(source="loop")
        assert TicketSweepRun.objects.recent(limit=5).count() == 1

    def test_zero_streak_counts_consecutive_zero_change_runs_from_the_latest(self) -> None:
        for changed in (True, False, False):
            run = TicketSweepRun.objects.begin(source="loop")
            if changed:
                TicketSweepRun.objects.record_change(run_id=run.run_id, issue_url=_A)
            TicketSweepRun.objects.finish(run_id=run.run_id, examined_count=1)
        assert TicketSweepRun.objects.zero_streak() == 2

    def test_zero_streak_is_zero_when_the_latest_run_changed_something(self) -> None:
        run = TicketSweepRun.objects.begin(source="loop")
        TicketSweepRun.objects.record_change(run_id=run.run_id, issue_url=_A)
        TicketSweepRun.objects.finish(run_id=run.run_id, examined_count=1)
        assert TicketSweepRun.objects.zero_streak() == 0
