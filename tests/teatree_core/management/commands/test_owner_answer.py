"""``t3 teatree questions answer-as-owner`` answers a mirrored owner card from the owner's own session (#5096).

The owner typing an answer in their own session closes the posted card and resumes the parked task, exactly as a
Slack tap does, with provenance that says it was the operator's. A dispatched agent can never use it.
"""

import io
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.core import notify as notify_module
from teatree.core.models import DeferredQuestion, DeferredQuestionAudit, Session, Task, Ticket
from teatree.utils.agent_spawn_marker import DISPATCHED_AGENT_ENV
from tests._owner_channel import OWNER_DECISION
from tests.factories import planned_ticket


def _mirrored_card_on_a_parked_task() -> tuple[DeferredQuestion, Task]:
    ticket = planned_ticket(role=Ticket.Role.AUTHOR, state=Ticket.State.WORK_STARTED)
    parked = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase="coding")
    card = DeferredQuestion.record("Rotate the deploy token?", parked_task=parked, **OWNER_DECISION)
    DeferredQuestion.objects.filter(pk=card.pk).update(slack_channel="D0DEMOOWNER", slack_ts="1700000000.000100")
    card.refresh_from_db()
    return card, parked


def _backend() -> MagicMock:
    backend = MagicMock()
    backend.update_message.return_value = {"ok": True}
    return backend


def _answer_cards_as_owner(card: DeferredQuestion, text: str, backend: MagicMock) -> None:
    with (
        patch.object(notify_module, "resolve_owner_dm_backend", return_value=(backend, None)),
        patch.dict("os.environ", {}, clear=True),
    ):
        call_command("questions", "answer-as-owner", card.pk, text, stdout=io.StringIO(), stderr=io.StringIO())


class TestAnOwnerAnswerClosesTheCard(TestCase):
    def test_it_records_operator_provenance_edits_the_card_closed_and_resumes_the_parked_task(self) -> None:
        card, parked = _mirrored_card_on_a_parked_task()
        backend = _backend()

        _answer_cards_as_owner(card, "Yes", backend)

        card.refresh_from_db()
        assert card.answer_text == "Yes"
        assert card.resolved_via == DeferredQuestion.ResolvedVia.OPERATOR
        assert card.answered_on_owner_channel
        assert DeferredQuestionAudit.objects.get(question=card, action="answered").resolver_id.startswith("operator:")
        backend.update_message.assert_called_once()
        assert backend.update_message.call_args.kwargs["ts"] == "1700000000.000100"
        resumed = Task.objects.get(parent_task=parked)
        assert "Yes" in resumed.execution_reason

    def test_a_second_owner_answer_reports_already_answered_and_resumes_nothing_more(self) -> None:
        card, parked = _mirrored_card_on_a_parked_task()
        _answer_cards_as_owner(card, "Yes", _backend())
        stderr = io.StringIO()

        with (
            patch.object(notify_module, "resolve_owner_dm_backend", return_value=(_backend(), None)),
            patch.dict("os.environ", {}, clear=True),
            pytest.raises(SystemExit) as refused,
        ):
            call_command("questions", "answer-as-owner", card.pk, "No", stdout=io.StringIO(), stderr=stderr)

        assert refused.value.code == 1
        assert "already answered" in stderr.getvalue()
        assert Task.objects.filter(parent_task=parked).count() == 1


class TestAnOwnerAnswerIsNeverADispatchedAgents(TestCase):
    def test_a_dispatched_agent_is_refused_and_the_card_stays_pending(self) -> None:
        card, parked = _mirrored_card_on_a_parked_task()
        stderr = io.StringIO()

        with (
            patch.dict("os.environ", {DISPATCHED_AGENT_ENV: "1"}, clear=True),
            pytest.raises(SystemExit) as refused,
        ):
            call_command("questions", "answer-as-owner", card.pk, "Yes", stdout=io.StringIO(), stderr=stderr)

        assert refused.value.code == 2
        card.refresh_from_db()
        assert card.is_pending
        assert not Task.objects.filter(parent_task=parked).exists()
