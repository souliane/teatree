from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from teatree.core.modelkit.task_failure_taxonomy import HALT_STAMP
from teatree.core.models import Session, Task, Ticket
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.loop.scanners.parked_ticket_question import ParkedTicketQuestionScanner
from teatree.loops.housekeeping.loop import MINI_LOOP


class ParkedTicketQuestionTest(TestCase):
    def _parked(self) -> tuple[Ticket, Task]:
        ticket = Ticket.objects.create(
            issue_url=f"https://example.com/issues/{Ticket.objects.count() + 1}", state=Ticket.State.WORK_STARTED
        )
        session = Session.objects.create(overlay="test", ticket=ticket)
        task = Task.objects.create(
            ticket=ticket, session=session, status=Task.Status.FAILED, execution_reason=HALT_STAMP
        )
        Task.objects.filter(pk=task.pk).update(created_at=timezone.now() - timedelta(days=15))
        return ticket, task

    def _answer(self, answer: str, *, channel: str = DeferredQuestion.ResolvedVia.SLACK) -> None:
        DeferredQuestion.objects.update(answered_at=timezone.now(), answer_text=answer, resolved_via=channel)

    def test_registered_job_asks_once_and_has_no_ticket_identifiers_in_card(self) -> None:
        self._parked()
        jobs = [job for job in MINI_LOOP.build_jobs() if job.scanner.name == "parked_ticket_question"]
        assert len(jobs) == 1
        jobs[0].scanner.scan()
        jobs[0].scanner.scan()
        row = DeferredQuestion.objects.get()
        assert row.evidence["decision"] == "irreversible"
        assert "Retire them" in row.options_json
        assert "issues/" not in str(row.evidence)

    def test_owner_retires_only_unchanged_tasks_listed_in_card(self) -> None:
        retained, _ = self._parked()
        changed, task = self._parked()
        scanner = ParkedTicketQuestionScanner()
        scanner.scan()
        Task.objects.create(ticket=changed, session=task.session, status=Task.Status.PENDING)
        self._answer("Retire them")
        scanner.scan()
        retained.refresh_from_db()
        changed.refresh_from_db()
        assert retained.state == Ticket.State.IGNORED
        assert changed.state == Ticket.State.WORK_STARTED
        scanner.scan()
        assert DeferredQuestion.objects.count() == 1

    def test_other_answer_and_local_channel_leave_tickets_parked(self) -> None:
        ticket, _ = self._parked()
        scanner = ParkedTicketQuestionScanner()
        scanner.scan()
        self._answer("Keep them parked")
        scanner.scan()
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.WORK_STARTED
        DeferredQuestion.objects.all().delete()
        scanner.scan()
        self._answer("Retire them", channel=DeferredQuestion.ResolvedVia.LOCAL)
        scanner.scan()
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.WORK_STARTED
