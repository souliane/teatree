"""A self-review HOLD loop is bounded: past the iteration cap the owner is asked once instead."""

from unittest.mock import patch

from django.test import TestCase

from teatree.core.models import DeferredQuestion, Task, Ticket
from teatree.core.repair_loop import max_phase_iterations
from tests.teatree_core._self_review_helpers import author_ticket, completed_self_review


def _apply(task: Task) -> bool:
    with patch.object(Ticket, "has_shippable_diff", return_value=True):
        return task._apply_phase_transition()


class TestTheHoldReworkLoopIsBounded(TestCase):
    def test_at_the_cap_the_next_hold_asks_the_owner_once_and_queues_no_rework(self) -> None:
        ticket = author_ticket()
        for _ in range(max_phase_iterations()):
            completed_self_review(ticket, "hold")
        latest = completed_self_review(ticket, "hold")

        assert _apply(latest) is False
        assert _apply(latest) is False

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.TESTED
        assert not Task.objects.filter(ticket=ticket, phase="coding").exists()
        [question] = DeferredQuestion.objects.all()
        assert question.dedupe_marker == f"self-review-hold-cap:{ticket.pk}"
        assert f"reviewing task {latest.pk}" in question.question

    def test_below_the_cap_the_hold_still_queues_its_rework(self) -> None:
        ticket = author_ticket()
        for _ in range(max_phase_iterations() - 1):
            completed_self_review(ticket, "hold")
        latest = completed_self_review(ticket, "hold")

        _apply(latest)

        assert Task.objects.filter(ticket=ticket, phase="coding", parent_task=latest).exists()
        assert not DeferredQuestion.objects.exists()
