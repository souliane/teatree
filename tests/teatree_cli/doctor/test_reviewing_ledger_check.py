"""``check_reviewing_ledger`` — a completed review with no attempt must be visible (#4308).

The condition was only ever found by hand-querying the control DB, which is why six
zero-attempt reviewing tasks accumulated unnoticed while the PRs they "reviewed" stayed
held.
"""

from datetime import timedelta

import django.test
import pytest
from django.utils import timezone

from teatree.cli.doctor.checks_reviewing_ledger import check_reviewing_ledger
from teatree.core.models import Session, Task, TaskAttempt, Ticket


class ReviewingLedgerDoctorCheckTestCase(django.test.TestCase):
    @pytest.fixture(autouse=True)
    def _inject_fixtures(self, capsys: pytest.CaptureFixture[str]) -> None:
        self._capsys = capsys

    @staticmethod
    def _completed_task(*, phase: str = "reviewing", pr_id: int = 4308) -> Task:
        ticket = Ticket.objects.create(
            issue_url=f"https://github.com/souliane/teatree/pull/{pr_id}",
            overlay="acme",
            role=Ticket.Role.REVIEWER,
        )
        session = Session.objects.create(ticket=ticket, agent_id="external-review")
        return Task.objects.create(
            ticket=ticket,
            session=session,
            phase=phase,
            status=Task.Status.COMPLETED,
        )

    def test_an_empty_ledger_passes(self) -> None:
        assert check_reviewing_ledger() is True

    def test_a_completed_review_carrying_an_attempt_passes(self) -> None:
        task = self._completed_task()
        TaskAttempt.objects.create(task=task, ended_at=timezone.now(), exit_code=0, result={"summary": "reviewed"})

        assert check_reviewing_ledger() is True

    def test_a_completed_review_with_no_attempt_fails_loud(self) -> None:
        task = self._completed_task()

        assert check_reviewing_ledger() is False

        out = self._capsys.readouterr().out
        assert "FAIL" in out
        assert str(task.pk) in out
        assert "souliane/teatree#4308" in out

    def test_a_completed_task_in_another_phase_is_not_a_finding(self) -> None:
        self._completed_task(phase="coding")

        assert check_reviewing_ledger() is True

    def test_a_review_older_than_the_window_is_not_re_reported_forever(self) -> None:
        task = self._completed_task()
        Task.objects.filter(pk=task.pk).update(created_at=timezone.now() - timedelta(days=90))

        assert check_reviewing_ledger() is True
