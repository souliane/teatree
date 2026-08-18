"""``check_reviewing_ledger`` — a completed review with no attempt must be visible (#4308).

The condition was only ever found by hand-querying the control DB, which is why six
zero-attempt reviewing tasks accumulated unnoticed while the PRs they "reviewed" stayed
held.
"""

import re
from datetime import timedelta

import django.test
import pytest
from django.core.management import call_command
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
        self._completed_task()

        assert check_reviewing_ledger() is False

        out = self._capsys.readouterr().out
        assert "FAIL" in out
        assert "souliane/teatree#4308" in out

    def test_many_empty_rows_report_one_finding_not_one_per_row(self) -> None:
        """A backlog is one condition, not N incidents.

        The operator surface that consumes doctor output batches red findings into
        notifications, so a line per row turns a single condition into dozens of
        messages. The count belongs in the finding; the enumeration belongs behind a
        command.
        """
        for offset in range(25):
            self._completed_task(pr_id=5000 + offset)

        assert check_reviewing_ledger() is False

        out = self._capsys.readouterr().out
        assert out.count("FAIL") == 1, f"expected one aggregate finding, got {out.count('FAIL')}"
        assert "25 completed review task(s)" in out

    def test_a_completed_task_in_another_phase_is_not_a_finding(self) -> None:
        self._completed_task(phase="coding")

        assert check_reviewing_ledger() is True

    def test_a_review_older_than_the_window_is_not_re_reported_forever(self) -> None:
        task = self._completed_task()
        Task.objects.filter(pk=task.pk).update(created_at=timezone.now() - timedelta(days=90))

        assert check_reviewing_ledger() is True


class PrescribedCommandIsRealTestCase(django.test.TestCase):
    """A finding that prescribes a remedy must prescribe one that RUNS.

    The first cut of the aggregate finding told the operator to run
    `tasks list --phase reviewing`. There is no `--phase` option — the command exits
    `No such option: '--phase'` — and the same change had removed the per-task ids, so the
    rows it reports became unenumerable by any route. A wrong remedy is worse than a bare
    count: it costs the reader a round trip before they learn the finding cannot be acted on.

    Nothing else catches this. The repo's CLI-literal resolver keys on `t3 <lowercase>`, so
    every `t3 <overlay> ...` literal — the form every overlay-scoped command takes — is
    invisible to it.
    """

    @pytest.fixture(autouse=True)
    def _inject_fixtures(self, capsys: pytest.CaptureFixture[str]) -> None:
        self._capsys = capsys

    def _finding_text(self) -> str:
        ticket = Ticket.objects.create(
            issue_url="https://github.com/souliane/teatree/pull/4308",
            overlay="acme",
            role=Ticket.Role.REVIEWER,
        )
        session = Session.objects.create(ticket=ticket, agent_id="external-review")
        Task.objects.create(ticket=ticket, session=session, phase="reviewing", status=Task.Status.COMPLETED)
        check_reviewing_ledger()
        return self._capsys.readouterr().out

    def test_the_prescribed_command_actually_runs(self) -> None:
        """Execute the remedy the finding prints. A flag the command rejects raises here.

        Introspecting typer's option metadata was tried first and reported nothing for any
        parameter, so it would have passed over the very defect it was written to catch.
        Running the command is the only check that cannot be green for the wrong reason.
        """
        text = self._finding_text()
        invocation = re.search(r"tasks list ([^`|]*)", text)
        assert invocation, f"the finding no longer prescribes a `tasks list` command:\n{text}"

        argv = invocation.group(1).split()
        call_command("tasks", "list", *argv)

    def test_the_finding_stays_enumerable(self) -> None:
        """A count with no route back to the rows is a dead end for whoever must act on it."""
        assert "tasks list" in self._finding_text()
