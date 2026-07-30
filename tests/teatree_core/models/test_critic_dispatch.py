"""CriticDispatch (SELFCATCH-5): the idempotent enqueue of the async headless critic.

Mirrors ``AutoReviewDispatch``: one row per ``(ticket, transition, head_sha)`` linking
the claimable headless ``Task(phase="reviewing")`` the loop self-pump dispatches. A
re-fire at the same delivered head returns ``None`` (no second critic); the row and its
task share one transaction.
"""

import datetime as dt

from django.test import TestCase
from django.utils import timezone

from teatree.core.models import CriticDispatch, CriticVerdict, Task, Ticket
from teatree.core.models.auto_review_dispatch import MAX_DISPATCH_ATTEMPTS
from teatree.core.models.critic_verdict import CriticItemVerdict

_FORTY_HEX = "a" * 40
HEAD = _FORTY_HEX


class TestCriticDispatchEnqueue(TestCase):
    def test_enqueue_creates_a_headless_reviewing_task(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.DELIVERED)
        row = CriticDispatch.enqueue(
            ticket=ticket, transition="mark_delivered", head_sha=_FORTY_HEX, contract="judge this delivery"
        )
        assert row is not None
        assert row.task is not None
        # Its OWN phase so the result is measured against the critic evidence contract,
        # not the reviewing one (the production dead-path fix). The execution lane is the
        # runtime's routing decision (Task.save), not ours.
        assert row.task.phase == "critic_reviewing"
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


class TestStrandedCriticDispatchIsReArmable(TestCase):
    """#3920: a dead critic must not leave the merge-quality gate unsatisfiable forever."""

    def setUp(self) -> None:
        self.ticket = Ticket.objects.create(issue_url="https://github.com/souliane/teatree/issues/3920")

    def _arm(self) -> CriticDispatch:
        row = CriticDispatch.enqueue(
            ticket=self.ticket, transition="merge", head_sha=HEAD, contract="grade the delivery"
        )
        assert row is not None
        return row

    @staticmethod
    def _expire(row: CriticDispatch) -> None:
        CriticDispatch.objects.filter(pk=row.pk).update(deadline=timezone.now() - dt.timedelta(minutes=1))

    def test_a_live_claim_still_dedups(self) -> None:
        self._arm()
        assert CriticDispatch.enqueue(ticket=self.ticket, transition="merge", head_sha=HEAD, contract="again") is None
        assert Task.objects.filter(phase="critic_reviewing").count() == 1

    def test_an_expired_claim_re_arms_once(self) -> None:
        first = self._arm()
        self._expire(first)

        again = CriticDispatch.enqueue(
            ticket=self.ticket, transition="merge", head_sha=HEAD, contract="grade the delivery"
        )

        assert again is not None
        assert again.pk == first.pk
        assert again.attempts == 2
        assert again.task is not None
        assert again.task.pk != first.task_id

    def test_an_expired_claim_with_budget_left_is_not_saturated(self) -> None:
        # Re-armable, so not the doctor's business: saturation is the END of the
        # retry, not any one dead attempt.
        row = self._arm()
        self._expire(row)

        assert row.attempts < MAX_DISPATCH_ATTEMPTS
        assert CriticDispatch.saturated().count() == 0

    def test_the_retry_budget_is_bounded_and_surfaces(self) -> None:
        row = self._arm()
        for _ in range(MAX_DISPATCH_ATTEMPTS - 1):
            self._expire(row)
            row = CriticDispatch.enqueue(
                ticket=self.ticket, transition="merge", head_sha=HEAD, contract="grade the delivery"
            )
            assert row is not None
        self._expire(row)

        assert CriticDispatch.enqueue(ticket=self.ticket, transition="merge", head_sha=HEAD, contract="grade") is None
        assert CriticDispatch.saturated().count() == 1

    def test_the_last_attempt_is_not_saturated_while_its_deadline_is_still_live(self) -> None:
        # The final critic is still running and may yet record a verdict, so a
        # spent budget alone is not "nothing will re-arm this head".
        row = self._arm()
        for _ in range(MAX_DISPATCH_ATTEMPTS - 1):
            self._expire(row)
            row = CriticDispatch.enqueue(
                ticket=self.ticket, transition="merge", head_sha=HEAD, contract="grade the delivery"
            )
            assert row is not None

        assert row.attempts == MAX_DISPATCH_ATTEMPTS
        assert row.deadline > timezone.now()
        assert CriticDispatch.saturated().count() == 0

    def test_a_recorded_verdict_is_terminal(self) -> None:
        row = self._arm()
        CriticVerdict.record(
            ticket=self.ticket,
            transition="merge",
            head_sha=HEAD,
            grader_identity="an-independent-critic",
            items=[CriticItemVerdict(slug="scope", status="pass", citation="src/x.py:1")],
        )
        self._expire(row)

        assert CriticDispatch.enqueue(ticket=self.ticket, transition="merge", head_sha=HEAD, contract="grade") is None
