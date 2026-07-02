"""``t3 teatree questions answer`` resumes a parked headless task (#31).

The CLI ``answer`` path is the chat-only operator's parallel of the Slack
reply scanner (``askuserquestion_reply._apply_one``). The scanner re-queues a
headless continuation when the resolved ``DeferredQuestion`` carries a
``parked_task``; the CLI path historically resolved the answer + wrote the
audit but never scheduled the resume, so a headless run parked via the
away-mode hook stayed parked forever even though ``_resurface_text`` tells the
user to answer through this very command.

These tests pin parity with the scanner: a CLI-answered parked question
re-queues exactly one HEADLESS followup chained on the captured session, a
question with no ``parked_task`` queues none, and re-answering / a pre-existing
resume child never double-queues.
"""

import pytest
from django.core.management import call_command

from teatree.agents._headless_options import _get_resume_session_id
from teatree.core.models import ConfigSetting, Session, Task, TaskAttempt, Ticket
from teatree.core.models.deferred_question import DeferredQuestion

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

_RESUME_UUID = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"


def _parked_task() -> Task:
    ConfigSetting.objects.set_value("agent_runtime", "sdk_oauth")
    ticket = Ticket.objects.create()
    session = Session.objects.create(ticket=ticket, agent_id=_RESUME_UUID)
    parked = Task.objects.create(
        ticket=ticket,
        session=session,
        phase="coding",
        execution_target=Task.ExecutionTarget.HEADLESS,
    )
    TaskAttempt.objects.create(task=parked, agent_session_id=_RESUME_UUID)
    return parked


def _parked_question(parked: Task) -> DeferredQuestion:
    return DeferredQuestion.record(
        "Which DB host?",
        session_id=str(parked.session_id),
        parked_task=parked,
    )


class TestAnswerResumesParkedTask:
    def test_answer_queues_headless_resume_with_answer_and_resume_session(self) -> None:
        parked = _parked_task()
        question = _parked_question(parked)

        call_command("questions", "answer", question.pk, "use postgres-1")

        question.refresh_from_db()
        assert question.answer_text == "use postgres-1"
        assert question.resolved_via == DeferredQuestion.ResolvedVia.LOCAL
        resume = parked.child_tasks.get()
        assert resume.execution_target == Task.ExecutionTarget.HEADLESS
        assert resume.parent_task_id == parked.pk
        assert "use postgres-1" in resume.execution_reason
        assert _get_resume_session_id(resume) == _RESUME_UUID

    def test_answer_without_parked_task_queues_no_resume(self) -> None:
        question = DeferredQuestion.record("Chat-only question?")

        call_command("questions", "answer", question.pk, "yes")

        question.refresh_from_db()
        assert question.answer_text == "yes"
        assert not Task.objects.exists()

    def test_re_answer_does_not_double_queue_resume(self) -> None:
        parked = _parked_task()
        question = _parked_question(parked)

        call_command("questions", "answer", question.pk, "use postgres-1")
        with pytest.raises(SystemExit):
            call_command("questions", "answer", question.pk, "use postgres-2")

        assert parked.child_tasks.count() == 1

    def test_pre_existing_resume_child_is_not_double_queued(self) -> None:
        parked = _parked_task()
        question = _parked_question(parked)
        existing = Task.objects.create(
            ticket=parked.ticket,
            session=parked.session,
            phase=parked.phase,
            execution_target=Task.ExecutionTarget.HEADLESS,
            parent_task=parked,
        )

        call_command("questions", "answer", question.pk, "use postgres-1")

        assert list(parked.child_tasks.values_list("pk", flat=True)) == [existing.pk]
