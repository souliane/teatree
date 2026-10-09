"""An owner question whose subject has already finished is never asked."""

from unittest.mock import MagicMock

from django.test import TestCase

from teatree.core.models import PullRequest, Session, Task, Ticket
from teatree.core.models.deferred_question import DeferredQuestion, DeferredQuestionAudit
from teatree.core.models.question_subject import finished_subject_reason
from teatree.core.notify_question_drains import drain_unmirrored_deferred_questions
from tests._owner_channel import OWNER_DECISION


def _task_on(state: str) -> Task:
    ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, state=state)
    session = Session.objects.create(ticket=ticket, agent_id="coding")
    return Task.objects.create(ticket=ticket, session=session, phase="coding", status=Task.Status.COMPLETED)


def _pull_request(ticket: Ticket, *, state: str, iid: str) -> None:
    PullRequest.objects.create(
        ticket=ticket, url=f"https://github.com/acme/repo/pull/{iid}", repo="acme/repo", iid=iid, state=state
    )


class TestRecordTimeSubjectCheck(TestCase):
    def test_owner_question_on_merged_ticket_is_recorded_dismissed_and_never_mirrored(self) -> None:
        question = DeferredQuestion.record(
            "May I post the review request?", parked_task=_task_on(Ticket.State.MERGED), **OWNER_DECISION
        )
        backend = MagicMock()

        mirrored = drain_unmirrored_deferred_questions(user_id="U_ME", backend=backend)

        question.refresh_from_db()
        assert question.status == DeferredQuestion.STATUS_DISMISSED
        assert question.resolved_via == DeferredQuestion.ResolvedVia.STALE
        assert DeferredQuestionAudit.objects.get(question=question).resolver_id == "record_subject_settled"
        assert mirrored == (0, 0)
        backend.post_message.assert_not_called()

    def test_owner_question_on_pr_opened_ticket_stays_pending(self) -> None:
        task = _task_on(Ticket.State.PR_OPENED)
        _pull_request(task.ticket, state=PullRequest.State.OPEN, iid="1")

        question = DeferredQuestion.record("May I post the review request?", parked_task=task, **OWNER_DECISION)

        question.refresh_from_db()
        assert question.status == DeferredQuestion.STATUS_PENDING

    def test_internal_question_on_merged_ticket_is_left_to_the_sweep(self) -> None:
        question = DeferredQuestion.record("Why did the lane stop?", parked_task=_task_on(Ticket.State.MERGED))

        question.refresh_from_db()
        assert question.status == DeferredQuestion.STATUS_PENDING


class TestFinishedSubjectReason(TestCase):
    def test_retro_recorded_counts_as_finished(self) -> None:
        question = DeferredQuestion.record("q", parked_task=_task_on(Ticket.State.RETRO_RECORDED))

        assert finished_subject_reason(question) == "the subject ticket is retro_recorded"

    def test_every_pr_settled_counts_as_finished(self) -> None:
        task = _task_on(Ticket.State.PR_OPENED)
        _pull_request(task.ticket, state=PullRequest.State.MERGED, iid="1")
        _pull_request(task.ticket, state=PullRequest.State.CLOSED, iid="2")

        question = DeferredQuestion.record("q", task_session=task.session)

        assert finished_subject_reason(question) == "every pull request of the subject is merged or closed"

    def test_one_live_pr_keeps_the_subject_open(self) -> None:
        task = _task_on(Ticket.State.PR_OPENED)
        _pull_request(task.ticket, state=PullRequest.State.MERGED, iid="1")
        _pull_request(task.ticket, state=PullRequest.State.REVIEW_REQUESTED, iid="2")

        assert finished_subject_reason(DeferredQuestion.record("q", parked_task=task)) is None

    def test_a_subject_with_no_pull_request_is_not_finished(self) -> None:
        question = DeferredQuestion.record("q", parked_task=_task_on(Ticket.State.PR_OPENED))

        assert finished_subject_reason(question) is None

    def test_a_question_with_no_subject_has_no_reason(self) -> None:
        assert finished_subject_reason(DeferredQuestion.record("q", session_id="harness-uuid")) is None
