"""CriticDispatch (SELFCATCH-5): the idempotent enqueue of the async headless critic.

Mirrors ``AutoReviewDispatch``: one row per ``(ticket, transition, head_sha)`` linking
the claimable headless ``Task(phase="reviewing")`` the loop self-pump dispatches. A
re-fire at the same delivered head returns ``None`` (no second critic); the row and its
task share one transaction.
"""

from django.test import TestCase

from teatree.core.models import CriticDispatch, Ticket

_FORTY_HEX = "a" * 40


class TestCriticDispatchEnqueue(TestCase):
    def test_enqueue_creates_a_headless_reviewing_task(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.DELIVERED)
        row = CriticDispatch.enqueue(
            ticket=ticket, transition="mark_delivered", head_sha=_FORTY_HEX, contract="judge this delivery"
        )
        assert row is not None
        assert row.task is not None
        # phase="reviewing" is what the loop self-pump dispatches; the actual
        # execution lane is the runtime's routing decision (Task.save), not ours.
        assert row.task.phase == "reviewing"
        assert "judge this delivery" in row.task.execution_reason
        assert row.head_sha == _FORTY_HEX

    def test_enqueue_is_idempotent_per_head(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.DELIVERED)
        first = CriticDispatch.enqueue(ticket=ticket, transition="mark_delivered", head_sha=_FORTY_HEX, contract="c")
        second = CriticDispatch.enqueue(ticket=ticket, transition="mark_delivered", head_sha=_FORTY_HEX, contract="c")
        assert first is not None
        assert second is None  # a second enqueue for the same head arms no new critic
        assert CriticDispatch.objects.filter(ticket=ticket).count() == 1

    def test_a_new_head_arms_a_fresh_critic(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.DELIVERED)
        CriticDispatch.enqueue(ticket=ticket, transition="mark_delivered", head_sha=_FORTY_HEX, contract="c")
        fresh = CriticDispatch.enqueue(ticket=ticket, transition="mark_delivered", head_sha="b" * 40, contract="c")
        assert fresh is not None
        assert CriticDispatch.objects.filter(ticket=ticket).count() == 2
