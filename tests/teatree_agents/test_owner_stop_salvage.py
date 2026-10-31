"""A refused owner stop never discards the finished work behind it, and never reaches the owner as refused (#5096)."""

from unittest.mock import MagicMock

from django.test import TestCase

from teatree.agents.attempt_recorder import record_result_envelope
from teatree.agents.prompt import build_task_prompt
from teatree.core.modelkit.task_failure_taxonomy import OWNER_STOP_REFUSAL_PREFIX
from teatree.core.models import DeferredQuestion, DeferredQuestionAudit, Session, Task, Ticket
from teatree.core.notify_question_drains import (
    drain_deferred_questions,
    drain_unmirrored_deferred_questions,
    reask_escalated_questions,
)
from tests._owner_channel import OWNER_CARD, owner_stop
from tests.factories import planned_ticket

SHORTHAND_QUESTION = "Should the factory rotate the deploy token for Ticket 5273?"
OVER_LONG_QUESTION = f"Should the factory {'really ' * 30}rotate the deploy token?"
FINISHED = {"summary": "fix committed", "files_modified": [{"path": "src/app.py", "action": "modified"}]}


def _backend() -> MagicMock:
    backend = MagicMock()
    backend.open_dm.return_value = "D0DEMOOWNER"
    backend.post_message.return_value = {"ok": True, "ts": "1800000000.000100"}
    backend.get_permalink.return_value = "https://acme.slack.example/archives/D0DEMOOWNER/p1800000000000100"
    return backend


def _every_drain(backend: MagicMock) -> None:
    drain_unmirrored_deferred_questions(user_id="U0DEMOOWNER", backend=backend)
    drain_deferred_questions(user_id="U0DEMOOWNER", backend=backend)
    reask_escalated_questions(user_id="U0DEMOOWNER", backend=backend)


class _CodingRun(TestCase):
    def setUp(self) -> None:
        self.ticket = planned_ticket(role=Ticket.Role.AUTHOR, state=Ticket.State.WORK_STARTED)
        session = Session.objects.create(ticket=self.ticket, agent_id="coding")
        self.task = Task.objects.create(ticket=self.ticket, session=session, phase="coding")
        self.task.claim(claimed_by="loop-slot")

    def _refused_stop_after_finished_work(self, question: str) -> object:
        return record_result_envelope(self.task, owner_stop(question) | FINISHED, phase="coding")


class TestARefusedStopKeepsTheFinishedWork(_CodingRun):
    def test_a_refused_stop_completes_the_task_and_queues_the_next_phase(self) -> None:
        for question in (SHORTHAND_QUESTION, OVER_LONG_QUESTION):
            with self.subTest(question=question[:30]):
                self.setUp()
                attempt = self._refused_stop_after_finished_work(question)

                self.task.refresh_from_db()
                assert attempt.error == ""
                assert self.task.status == Task.Status.COMPLETED
                assert self.task.failure_kind == ""
                assert Task.objects.filter(ticket=self.ticket, phase="testing", status=Task.Status.PENDING).exists()
                row = DeferredQuestion.objects.get(parked_task=self.task)
                assert row.audience == DeferredQuestion.Audience.INTERNAL
                assert DeferredQuestionAudit.objects.filter(question=row, action="withheld").exists()
                assert "owner_stop" not in str(attempt.result)

    def test_the_withheld_row_is_one_per_marker(self) -> None:
        self._refused_stop_after_finished_work(SHORTHAND_QUESTION)
        first = DeferredQuestion.objects.get(parked_task=self.task)
        self.task.refresh_from_db()
        Task.objects.filter(pk=self.task.pk).update(status=Task.Status.CLAIMED)
        self.task.refresh_from_db()

        self._refused_stop_after_finished_work(SHORTHAND_QUESTION)

        assert DeferredQuestion.objects.filter(dedupe_marker=first.dedupe_marker).count() == 1

    def test_a_refused_stop_without_finished_work_is_still_a_refusal(self) -> None:
        attempt = record_result_envelope(
            self.task, owner_stop(SHORTHAND_QUESTION) | {"summary": "not done yet"}, phase="coding"
        )

        self.task.refresh_from_db()
        assert attempt.error.startswith(OWNER_STOP_REFUSAL_PREFIX)
        assert self.task.status == Task.Status.FAILED
        assert not DeferredQuestion.objects.exists()

    def test_a_refused_stop_whose_landing_check_fails_is_still_a_refusal(self) -> None:
        attempt = record_result_envelope(
            self.task,
            owner_stop(SHORTHAND_QUESTION) | {"summary": "x", "files_modified": [], "fix_record": {}},
            phase="coding",
        )

        assert attempt.error.startswith(OWNER_STOP_REFUSAL_PREFIX)
        assert not DeferredQuestion.objects.exists()


class TestTheRefusedTextNeverReachesTheOwner(_CodingRun):
    def test_no_drain_posts_the_refused_question(self) -> None:
        self._refused_stop_after_finished_work(SHORTHAND_QUESTION)
        withheld = DeferredQuestion.objects.get(parked_task=self.task)
        backend = _backend()

        _every_drain(backend)

        backend.post_message.assert_not_called()
        assert withheld.is_pending

    def test_the_next_phase_prompt_names_the_withheld_stop_and_asks_for_a_plain_rewrite(self) -> None:
        self._refused_stop_after_finished_work(SHORTHAND_QUESTION)
        testing = Task.objects.get(ticket=self.ticket, phase="testing")

        prompt = build_task_prompt(testing)

        assert "was withheld" in prompt
        assert "internal shorthand" in prompt
        assert "questions record" in prompt

    def test_a_phase_that_did_not_follow_a_withheld_stop_carries_no_such_line(self) -> None:
        other = Task.objects.create(ticket=self.ticket, session=self.task.session, phase="testing")

        assert "was withheld" not in build_task_prompt(other)

    def test_a_valid_rewrite_under_the_same_marker_posts_once_with_only_the_rewritten_text(self) -> None:
        self._refused_stop_after_finished_work(SHORTHAND_QUESTION)
        withheld = DeferredQuestion.objects.get(parked_task=self.task)

        rewritten = DeferredQuestion.record(
            "Should the factory rotate the deploy token?",
            card=OWNER_CARD,
            dedupe_marker=withheld.dedupe_marker,
        )
        backend = _backend()

        drain_unmirrored_deferred_questions(user_id="U0DEMOOWNER", backend=backend)

        backend.post_message.assert_called_once()
        posted = backend.post_message.call_args.kwargs["text"]
        assert "rotate the deploy token" in posted
        assert "5273" not in posted
        rewritten.refresh_from_db()
        assert rewritten.parked_task_id == self.task.pk
        assert rewritten.audience == DeferredQuestion.Audience.OWNER_QUESTION
