"""A stop outside a misfit row's closed benign form records its owner card, on routed and unrouted runs (#5687)."""

from unittest.mock import patch

from django.test import TestCase

from teatree.agents import attempt_recorder
from teatree.agents.attempt_recorder import AttemptUsage, record_result_envelope
from teatree.core.models import DeferredQuestion, Session, Task, Ticket
from tests._owner_channel import (
    OWNER_KEPT_LOOKALIKES,
    UNLISTED_WORDING,
    assert_a_plain_card,
    owner_stop,
    stop_with_wording,
)
from tests.teatree_agents.test_attempt_recorder import _routed


class TestLookalikeStopsRecordACard(TestCase):
    def test_recorder_classifies_owner_stop_once(self) -> None:
        task = self._claimed(phase="scoping")
        stop = owner_stop("Is the deploy plan ready?", "product_scope") | {"summary": "waiting"}

        with patch.object(attempt_recorder, "owner_stop_misfit", wraps=attempt_recorder.owner_stop_misfit) as classify:
            record_result_envelope(task, stop)

        assert classify.call_count == 1

    def _claimed(self, *, phase: str) -> Task:
        ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, state=Ticket.State.WORK_STARTED)
        session = Session.objects.create(ticket=ticket, agent_id=phase)
        task = Task.objects.create(ticket=ticket, session=session, phase=phase)
        task.claim(claimed_by="loop-slot")
        return task

    def _assert_card_on_both_runs(self, kind: str, stop: dict[str, object]) -> None:
        for usage in (_routed(), AttemptUsage()):
            with self.subTest(kind=kind, routed=usage.route_candidate_index is not None):
                task = self._claimed(phase="scoping")

                attempt = record_result_envelope(task, stop | {"summary": "waiting"}, usage=usage)

                assert attempt.error == ""
                card = DeferredQuestion.objects.get(parked_task=task)
                assert card.evidence["decision"] == kind
                assert_a_plain_card(card)

    def test_every_lookalike_records_a_card_on_routed_and_unrouted_runs(self) -> None:
        for kind, question, facts in OWNER_KEPT_LOOKALIKES:
            self._assert_card_on_both_runs(kind, owner_stop(question, kind, *facts))

    def test_one_unlisted_wording_in_any_field_records_a_card_on_routed_and_unrouted_runs(self) -> None:
        for kind, field, wording in UNLISTED_WORDING:
            self._assert_card_on_both_runs(kind, stop_with_wording(kind, field, wording))
