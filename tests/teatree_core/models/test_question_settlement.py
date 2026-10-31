"""An owner card a closed-form rule already decides is answered by policy when recorded, never sent (#5096)."""

import io
from unittest.mock import MagicMock

from django.core.management import call_command
from django.test import TestCase

from teatree.agents.attempt_recorder import record_result_envelope
from teatree.core.modelkit.question_card import QuestionCard
from teatree.core.models import DeferredQuestion, Session, Task, Ticket
from teatree.core.notify_question_drains import (
    drain_deferred_questions,
    drain_unmirrored_deferred_questions,
    reask_escalated_questions,
)
from tests._owner_channel import BENIGN_STOPS, OWNER_KEPT_LOOKALIKES, UNLISTED_WORDING, owner_stop, stop_with_wording
from tests.factories import planned_ticket

RULE_NAMES = {
    "credentials": "retry_denied_start",
    "public_post": "push_own_finished_branch",
    "irreversible": "stop_own_process",
}


def _card_of(stop: dict[str, object]) -> QuestionCard:
    card = QuestionCard.from_envelope(stop)
    assert card is not None
    return card


def _backend() -> MagicMock:
    backend = MagicMock()
    backend.open_dm.return_value = "D0DEMOOWNER"
    backend.post_message.return_value = {"ok": True, "ts": "1800000000.000100"}
    backend.get_permalink.return_value = "https://acme.slack.example/archives/D0DEMOOWNER/p1800000000000100"
    return backend


class TestAClosedFormAskIsDecidedByItsRule(TestCase):
    def test_each_row_answers_the_card_by_policy_naming_its_rule_and_leaves_nothing_pending(self) -> None:
        for kind, (question, facts) in BENIGN_STOPS.items():
            with self.subTest(kind=kind):
                DeferredQuestion.objects.all().delete()

                row = DeferredQuestion.record(question, card=_card_of(owner_stop(question, kind, *facts)))

                row.refresh_from_db()
                assert not row.is_pending
                assert row.resolved_via == DeferredQuestion.ResolvedVia.POLICY
                assert RULE_NAMES[kind] in row.answer_text
                assert row.audits.get(action="answered").resolver_id == f"rule:{RULE_NAMES[kind]}"
                assert not DeferredQuestion.owner_pending().exists()

    def test_no_drain_posts_a_rule_decided_ask(self) -> None:
        for kind, (question, facts) in BENIGN_STOPS.items():
            DeferredQuestion.record(question, card=_card_of(owner_stop(question, kind, *facts)))
        backend = _backend()

        drain_unmirrored_deferred_questions(user_id="U0DEMOOWNER", backend=backend)
        drain_deferred_questions(user_id="U0DEMOOWNER", backend=backend)
        reask_escalated_questions(user_id="U0DEMOOWNER", backend=backend)

        backend.post_message.assert_not_called()


class TestANearMissWordingStillMakesACard(TestCase):
    def test_a_lookalike_ask_is_a_pending_owner_card(self) -> None:
        for kind, question, facts in OWNER_KEPT_LOOKALIKES:
            with self.subTest(kind=kind, question=question):
                DeferredQuestion.objects.all().delete()

                row = DeferredQuestion.record(question, card=_card_of(owner_stop(question, kind, *facts)))

                assert row.is_pending
                assert row.audience == DeferredQuestion.Audience.OWNER_QUESTION

    def test_one_unlisted_wording_in_any_field_is_a_pending_owner_card(self) -> None:
        for kind, field, wording in UNLISTED_WORDING:
            with self.subTest(kind=kind, field=field, wording=wording):
                DeferredQuestion.objects.all().delete()
                stop = stop_with_wording(kind, field, wording)

                row = DeferredQuestion.record(str(stop["user_input_reason"]), card=_card_of(stop))

                assert row.is_pending


class TestAParkedTaskResumesWithTheRulesAnswer(TestCase):
    def test_an_unrouted_stop_in_closed_form_resumes_its_task_with_the_ruling(self) -> None:
        ticket = planned_ticket(role=Ticket.Role.AUTHOR, state=Ticket.State.WORK_STARTED)
        session = Session.objects.create(ticket=ticket, agent_id="coding")
        task = Task.objects.create(ticket=ticket, session=session, phase="coding")
        task.claim(claimed_by="loop-slot")
        question, facts = BENIGN_STOPS["credentials"]

        record_result_envelope(task, owner_stop(question, "credentials", *facts) | {"summary": "waiting"})

        resumed = Task.objects.get(parent_task=task)
        assert RULE_NAMES["credentials"] in resumed.execution_reason
        assert not DeferredQuestion.owner_pending().exists()


class TestQuestionsRecordSaysWhichRuleDecided(TestCase):
    def test_the_output_names_the_rule_and_no_owner_card_is_left(self) -> None:
        question, _ = BENIGN_STOPS["public_post"]
        out = io.StringIO()

        call_command(
            "questions",
            "record",
            question,
            "--decision",
            "public_post",
            "--checked",
            "No standing answer covers it.",
            "--why",
            "The answer changes what the factory does next.",
            "--blocker",
            "Only the owner can choose.",
            "--options",
            '[{"label": "Yes", "description": "I go ahead.", "recommended": true},'
            ' {"label": "No", "description": "I leave it as it is."}]',
            stdout=out,
        )

        assert "not sent to the owner" in out.getvalue()
        assert RULE_NAMES["public_post"] in out.getvalue()
        assert not DeferredQuestion.owner_pending().exists()
