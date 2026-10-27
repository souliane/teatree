from django.test import TestCase

from teatree.core.models import Ticket
from teatree.core.models.round_metric import RoundMetric


class TestRoundMetric(TestCase):
    def test_review_rounds_are_unique_per_head_and_count_up(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://github.com/example/repo/issues/1", overlay="teatree")
        first = RoundMetric.objects.open(
            kind="review", ticket=ticket, subject="a" * 40, size=0, detail={"case": "full"}
        )
        repeated = RoundMetric.objects.open(kind="review", ticket=ticket, subject="a" * 40, size=99, detail={})
        second = RoundMetric.objects.open(
            kind="review", ticket=ticket, subject="b" * 40, size=3, detail={"case": "delta"}
        )
        third = RoundMetric.objects.open(
            kind="review", ticket=ticket, subject="c" * 40, size=1, detail={"case": "delta"}
        )
        RoundMetric.objects.settle(kind="review", ticket=ticket, subject="b" * 40, outcome="hold")

        assert repeated.pk == first.pk
        assert repeated.size == 0
        assert str(first) == f"round<review:{ticket.pk}:{'a' * 8} #1>"
        assert (first.round_number, second.round_number, third.round_number) == (1, 2, 3)
        assert RoundMetric.objects.get(pk=second.pk).outcome == "hold"
        assert RoundMetric.objects.rounds_per_ticket("review") == {ticket.pk: 3}
