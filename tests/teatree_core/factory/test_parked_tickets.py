from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from teatree.core.factory.operational_health import collect_signals
from teatree.core.factory.parked_ticket_decision import KEEP_ANSWER, ask_owner_about_parked
from teatree.core.factory.parked_tickets import aged_parked_tasks, silent_freeze_tasks
from teatree.core.modelkit.task_failure_taxonomy import HALT_STAMP, FailureKind
from teatree.core.models import PullRequest, Session, Task, Ticket
from teatree.core.models.deferred_question import DeferredQuestion


class ParkedTicketSelectionTest(TestCase):
    def _failed(self, *, reason: str = "", kind: str = "", age_days: int = 15) -> Task:
        ticket = Ticket.objects.create(
            issue_url=f"https://example.com/issues/{Ticket.objects.count() + 1}", state=Ticket.State.WORK_STARTED
        )
        session = Session.objects.create(overlay="test", ticket=ticket)
        task = Task.objects.create(
            ticket=ticket, session=session, status=Task.Status.FAILED, execution_reason=reason, failure_kind=kind
        )
        Task.objects.filter(pk=task.pk).update(created_at=timezone.now() - timedelta(days=age_days))
        task.refresh_from_db()
        return task

    def test_halted_and_cancelled_are_parked(self) -> None:
        halted = self._failed(reason=HALT_STAMP)
        cancelled = self._failed(kind=FailureKind.CANCELLED)
        silent = self._failed()
        assert {task.pk for task in aged_parked_tasks()} == {halted.pk, cancelled.pk}
        assert [task.pk for task in silent_freeze_tasks()] == [silent.pk]

    def test_newer_task_and_recent_park_do_not_qualify(self) -> None:
        superseded = self._failed(reason=HALT_STAMP)
        self._failed(reason=HALT_STAMP, age_days=1)
        Task.objects.create(ticket=superseded.ticket, session=superseded.session, status=Task.Status.PENDING)
        assert aged_parked_tasks() == []

    def test_a_ticket_in_review_is_not_parked(self) -> None:
        task = self._failed(reason=HALT_STAMP)
        Ticket.objects.filter(pk=task.ticket_id).update(state=Ticket.State.REVIEW_REQUESTED)
        assert aged_parked_tasks() == []

    def test_a_ticket_with_an_open_pull_request_is_not_parked(self) -> None:
        task = self._failed(reason=HALT_STAMP)
        PullRequest.objects.create(ticket=task.ticket, url="https://example.com/pull/1", repo="o/r", iid="1")
        assert aged_parked_tasks() == []

    def test_a_failed_ticket_in_review_stays_a_silent_freeze(self) -> None:
        task = self._failed()
        Ticket.objects.filter(pk=task.ticket_id).update(state=Ticket.State.REVIEW_REQUESTED)
        assert [row.pk for row in silent_freeze_tasks()] == [task.pk]

    def test_a_ticket_whose_pull_request_closed_is_parked_again(self) -> None:
        task = self._failed(reason=HALT_STAMP)
        PullRequest.objects.create(
            ticket=task.ticket, url="https://example.com/pull/1", repo="o/r", iid="1", state=PullRequest.State.CLOSED
        )
        assert [row.pk for row in aged_parked_tasks()] == [task.pk]

    def test_synthetic_ticket_is_not_deliverable_work(self) -> None:
        task = self._failed(reason=HALT_STAMP)
        Ticket.objects.filter(pk=task.ticket_id).update(issue_url="loop://test")
        assert aged_parked_tasks() == []

    def _parked_signals(self) -> list[str]:
        return [row.summary for row in collect_signals().signals if row.kind == "parked_tickets"]

    def test_aged_parks_raise_one_owner_decision_signal(self) -> None:
        self._failed(reason=HALT_STAMP)
        self._failed(kind=FailureKind.CANCELLED)
        (summary,) = self._parked_signals()
        assert summary.startswith("2 tickets have ")
        assert "created over 14 days ago" in summary
        assert "await your decision" not in summary

    def test_a_keep_answer_clears_the_signal_for_the_tickets_it_ruled_on(self) -> None:
        self._failed(reason=HALT_STAMP)
        ask_owner_about_parked(aged_parked_tasks())
        DeferredQuestion.objects.update(
            answered_at=timezone.now(), answer_text=KEEP_ANSWER, resolved_via=DeferredQuestion.ResolvedVia.SLACK
        )
        assert self._parked_signals() == []
        self._failed(reason=HALT_STAMP)
        (summary,) = self._parked_signals()
        assert summary.startswith("1 ticket has ")
