from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from teatree.core.models import NOOP_OVER_COMPLETED_MARKER, Session, Task, TaskAttempt, Ticket
from teatree.core.models.deferred_question import DeferredQuestion, DeferredQuestionAudit
from teatree.core.models.usage_window_state import CHECKOUT_PARKED_PREFIX
from teatree.loop.question_drain import Verdict, drain_pending_questions, question_reachability
from teatree.loop.transient_requeue import requeue_transient_failed


class TestRecoveredRepairHalts(TestCase):
    def setUp(self) -> None:
        self.recovered = self._halted_ticket()
        self.other = self._halted_ticket()
        self.question = DeferredQuestion.objects.get(dedupe_marker__startswith="repair-halt:")
        assert DeferredQuestion.objects.filter(dedupe_marker__startswith="repair-halt:").count() == 1

    def _halted_ticket(self, *, error: str = "result_error: no terminal ResultMessage") -> Ticket:
        ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, state=Ticket.State.WORK_STARTED)
        session = Session.objects.create(ticket=ticket, agent_id="coding")
        failed = Task.objects.create(ticket=ticket, session=session, phase="coding", status=Task.Status.FAILED)
        for _ in range(2):
            TaskAttempt.objects.create(task=failed, ended_at=timezone.now(), exit_code=1, error=error)
        assert requeue_transient_failed() == 0
        return ticket

    def _attempt(
        self,
        *,
        phase: str = "coding",
        status: str = Task.Status.COMPLETED,
        result: object | None = None,
        error: str = "",
        parent_task: Task | None = None,
    ) -> tuple[Task, TaskAttempt]:
        session = Session.objects.create(ticket=self.recovered, agent_id=phase)
        task = Task.objects.create(
            ticket=self.recovered, session=session, phase=phase, status=status, parent_task=parent_task
        )
        attempt = TaskAttempt.objects.create(
            task=task, ended_at=timezone.now(), exit_code=0, error=error, result=result if result is not None else {}
        )
        return task, attempt

    def _assert_pending(self) -> None:
        assert drain_pending_questions().drained == 0
        self.question.refresh_from_db()
        assert self.question.is_pending

    def test_recovered_and_settled_subjects_drain_with_audit_and_reachability(self) -> None:
        self._attempt()
        Ticket.objects.filter(pk=self.other.pk).update(state=Ticket.State.MERGED)

        reach = next(reach for reach in question_reachability() if reach.question_id == self.question.pk)
        assert reach.decisions["repair_halt_recovered"] == Verdict.DRAIN
        assert drain_pending_questions().drained == 1
        self.question.refresh_from_db()
        assert self.question.resolved_via == DeferredQuestion.ResolvedVia.STALE
        assert DeferredQuestionAudit.objects.get(question=self.question).resolver_id == "repair_halt_recovered"

    def test_success_in_another_phase_keeps_the_row(self) -> None:
        self._attempt(phase="testing")
        Ticket.objects.filter(pk=self.other.pk).update(state=Ticket.State.MERGED)
        self._assert_pending()

    def test_failure_after_success_keeps_the_row(self) -> None:
        task, _ = self._attempt()
        TaskAttempt.objects.create(task=task, ended_at=timezone.now(), exit_code=1, error="later failure")
        Ticket.objects.filter(pk=self.other.pk).update(state=Ticket.State.MERGED)
        self._assert_pending()

    def test_needs_user_input_stop_is_not_a_pass(self) -> None:
        self._attempt(result={"needs_user_input": True})
        Ticket.objects.filter(pk=self.other.pk).update(state=Ticket.State.MERGED)
        self._assert_pending()

    def test_uncompleted_task_success_is_not_a_pass(self) -> None:
        self._attempt(status=Task.Status.PENDING)
        Ticket.objects.filter(pk=self.other.pk).update(state=Ticket.State.MERGED)
        self._assert_pending()

    def test_success_parked_on_a_pending_question_is_not_a_pass(self) -> None:
        task, _ = self._attempt()
        DeferredQuestion.record("Continue this task?", parked_task=task)
        Ticket.objects.filter(pk=self.other.pk).update(state=Ticket.State.MERGED)
        self._assert_pending()

    def test_resumed_task_success_after_answer_counts(self) -> None:
        parked, _ = self._attempt()
        question = DeferredQuestion.record("Continue this task?", parked_task=parked)
        DeferredQuestion.objects.filter(pk=question.pk).update(answered_at=timezone.now())
        self._attempt(parent_task=parked)
        Ticket.objects.filter(pk=self.other.pk).update(state=Ticket.State.MERGED)
        assert drain_pending_questions().drained == 1

    def test_exit_zero_interruption_is_not_a_pass(self) -> None:
        self._attempt(result={"summary": f"interrupted — {NOOP_OVER_COMPLETED_MARKER}stop"})
        Ticket.objects.filter(pk=self.other.pk).update(state=Ticket.State.MERGED)
        self._assert_pending()

    def test_checkout_park_is_a_refusal_not_a_pass(self) -> None:
        _task, attempt = self._attempt(error=f"{CHECKOUT_PARKED_PREFIX}occupied")
        assert attempt.outcome == TaskAttempt.Outcome.REFUSAL
        Ticket.objects.filter(pk=self.other.pk).update(state=Ticket.State.MERGED)
        self._assert_pending()

    def test_live_unrecovered_subject_keeps_the_row(self) -> None:
        self._attempt()
        self._assert_pending()

    def test_all_settled_without_recovery_uses_terminal_resolver(self) -> None:
        Ticket.objects.filter(pk__in=[self.recovered.pk, self.other.pk]).update(state=Ticket.State.MERGED)
        assert drain_pending_questions().drained == 1
        assert DeferredQuestionAudit.objects.get(question=self.question).resolver_id == "subject_terminal"

    def test_phase_alias_counts(self) -> None:
        self._attempt(phase="code")
        Ticket.objects.filter(pk=self.other.pk).update(state=Ticket.State.MERGED)
        assert drain_pending_questions().drained == 1

    def test_success_with_a_non_mapping_result_counts(self) -> None:
        self._attempt(result=[])
        Ticket.objects.filter(pk=self.other.pk).update(state=Ticket.State.MERGED)
        assert drain_pending_questions().drained == 1

    def test_repeated_halts_on_one_ticket_count_once(self) -> None:
        session = Session.objects.create(ticket=self.recovered, agent_id="coding")
        failed = Task.objects.create(ticket=self.recovered, session=session, phase="coding", status=Task.Status.FAILED)
        for _ in range(2):
            TaskAttempt.objects.create(
                task=failed, ended_at=timezone.now(), exit_code=1, error="result_error: no terminal ResultMessage"
            )
        assert requeue_transient_failed() == 0
        self._attempt()
        Ticket.objects.filter(pk=self.other.pk).update(state=Ticket.State.MERGED)

        assert drain_pending_questions().drained == 1
        audit = DeferredQuestionAudit.objects.get(question=self.question)
        assert "(1 recovered)" in audit.dismissed_reason

    def test_owner_audience_row_is_not_recovered_automatically(self) -> None:
        DeferredQuestion.objects.filter(pk=self.question.pk).update(audience=DeferredQuestion.Audience.OWNER_QUESTION)
        self._attempt()
        Ticket.objects.filter(pk=self.other.pk).update(state=Ticket.State.MERGED)
        self._assert_pending()

    def test_missing_marker_subject_keeps_the_row(self) -> None:
        Task.objects.filter(ticket_id__in=[self.recovered.pk, self.other.pk], status=Task.Status.FAILED).delete()
        self._attempt()
        self._assert_pending()

    def test_query_count_is_constant_across_repair_halt_rows(self) -> None:
        extra = self._halted_ticket(error="result_error: another fingerprint")
        second = DeferredQuestion.objects.exclude(pk=self.question.pk).get(dedupe_marker__startswith="repair-halt:")
        assert extra.pk != self.recovered.pk
        self._attempt()
        Ticket.objects.filter(pk=self.other.pk).update(state=Ticket.State.MERGED)
        DeferredQuestion.objects.filter(pk=second.pk).update(answered_at=timezone.now())
        with CaptureQueriesContext(connection) as one:
            question_reachability()
        DeferredQuestion.objects.filter(pk=second.pk).update(answered_at=None)
        with CaptureQueriesContext(connection) as two:
            question_reachability()
        assert len(two) == len(one)
