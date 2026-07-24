"""``Task.claim`` refuses a window-parked task (F5).

A usage-limit-parked task is PENDING with a future ``not_before``. The claim CAS ANDs
``_claimable_now_q`` into its ``<claimable>`` predicate, so a parked row cannot be claimed
until its window re-arms — the same gate ``claim_next_pending`` already honours, closing
the drain→claim→re-park churn.
"""

import datetime as dt

import pytest
from django.test import TestCase
from django.utils import timezone

from teatree.core.models import Session, Task, Ticket
from teatree.core.models.errors import InvalidTransitionError


class TestClaimHonoursNotBefore(TestCase):
    def setUp(self) -> None:
        self.ticket = Ticket.objects.create(overlay="test")
        self.session = Session.objects.create(ticket=self.ticket, overlay="test")

    def _pending(self, *, not_before: dt.datetime | None) -> Task:
        return Task.objects.create(
            ticket=self.ticket,
            session=self.session,
            execution_target=Task.ExecutionTarget.HEADLESS,
            status=Task.Status.PENDING,
            phase="architectural_review",
            not_before=not_before,
        )

    def test_parked_task_refuses_claim(self) -> None:
        parked = self._pending(not_before=timezone.now() + dt.timedelta(hours=4))
        with pytest.raises(InvalidTransitionError, match="parked"):
            parked.claim(claimed_by="headless-worker")
        parked.refresh_from_db()
        assert parked.status == Task.Status.PENDING  # untouched — no CLAIMED, no fresh lease

    def test_elapsed_not_before_task_claims(self) -> None:
        # Control: an elapsed park window claims normally, proving the gate keys on a
        # FUTURE not_before, not on the field's presence.
        ready = self._pending(not_before=timezone.now() - dt.timedelta(minutes=1))
        ready.claim(claimed_by="headless-worker")
        ready.refresh_from_db()
        assert ready.status == Task.Status.CLAIMED

    def test_unparked_task_claims(self) -> None:
        task = self._pending(not_before=None)
        task.claim(claimed_by="headless-worker")
        task.refresh_from_db()
        assert task.status == Task.Status.CLAIMED
