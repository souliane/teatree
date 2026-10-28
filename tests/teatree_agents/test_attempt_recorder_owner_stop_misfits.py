"""A stop outside a misfit row's closed benign form records its owner card, on routed and unrouted runs (#5687)."""

from django.test import TestCase

from teatree.agents.attempt_recorder import AttemptUsage, record_result_envelope
from teatree.core.models import DeferredQuestion, Session, Task, Ticket
from tests._owner_channel import OWNER_KEPT_LOOKALIKES, assert_a_plain_card, owner_stop
from tests.teatree_agents.test_attempt_recorder import _routed


class TestLookalikeStopsRecordACard(TestCase):
    def _claimed(self, *, phase: str) -> Task:
        ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, state=Ticket.State.WORK_STARTED)
        session = Session.objects.create(ticket=ticket, agent_id=phase)
        task = Task.objects.create(ticket=ticket, session=session, phase=phase)
        task.claim(claimed_by="loop-slot")
        return task

    def test_every_lookalike_records_a_card_on_routed_and_unrouted_runs(self) -> None:
        for kind, question, facts in OWNER_KEPT_LOOKALIKES:
            for usage in (_routed(), AttemptUsage()):
                with self.subTest(kind=kind, question=question, routed=usage.route_candidate_index is not None):
                    task = self._claimed(phase="scoping")

                    attempt = record_result_envelope(
                        task, owner_stop(question, kind, *facts) | {"summary": "waiting"}, usage=usage
                    )

                    assert attempt.error == ""
                    card = DeferredQuestion.objects.get(parked_task=task)
                    assert card.evidence["decision"] == kind
                    assert_a_plain_card(card)
