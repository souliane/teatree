"""Lane vetoes apply to waiting work without replacing terminal claim results."""

from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from teatree.core.models import Session, Task, Ticket
from teatree.core.task_claim_gate import pending_lane_denial


class TestPendingLaneDenial(TestCase):
    def test_seated_review_reads_the_brake(self) -> None:
        ticket = Ticket.objects.create()
        task = Task.objects.create(
            ticket=ticket,
            session=Session.objects.create(ticket=ticket),
            phase="architectural_review",
            admitted_at=timezone.now(),
        )
        with patch("teatree.core.task_claim_gate.agent_admission_verdict") as verdict:
            verdict.return_value.claim_denied_reason.return_value = "machine load HALT"
            assert pending_lane_denial(task) == "machine load HALT"
        verdict.return_value.claim_denied_reason.assert_called_once_with("architectural_review", seated=True)

    def test_finished_row_keeps_its_claim_result_without_a_lane_probe(self) -> None:
        ticket = Ticket.objects.create()
        task = Task.objects.create(
            ticket=ticket,
            session=Session.objects.create(ticket=ticket),
            phase="architectural_review",
            status=Task.Status.COMPLETED,
        )
        with patch("teatree.core.task_claim_gate.agent_admission_verdict") as verdict:
            assert pending_lane_denial(task) is None
        verdict.assert_not_called()
