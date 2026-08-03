"""A dispatch never joins an agent already in the checkout (souliane/teatree#3952).

The Task-seam dedupe (#3903) cannot see an agent that already HOLDS a checkout, so
before this guard the loop's own agent and an operator-dispatched one both ran in
one working tree. These pin the two halves at the dispatch seam: an occupied
checkout is REFUSED with the incumbent named, and a free one is held for the whole
run so a rival cannot take it mid-flight.

``_run_headless_agent`` is patched out throughout — the assertion is about which
runs are ALLOWED to start, and driving a real harness would prove nothing about
that while costing a model call.
"""

import tempfile
from pathlib import Path
from unittest import mock

import pytest
from django.test import TestCase

from teatree.agents import headless
from teatree.core.models import Task, TaskAttempt, Worktree
from teatree.core.worktree.occupancy import acquire, occupancy_holder, task_holder_id
from tests.factories import SessionFactory, TicketFactory, WorktreeFactory


class _DispatchCase(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.ticket = TicketFactory()
        self.checkout = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: self.checkout.exists() and self.checkout.rmdir())
        self.worktree = WorktreeFactory(ticket=self.ticket, extra={"worktree_path": str(self.checkout)})
        session = SessionFactory(ticket=self.ticket)
        self.task = Task.objects.create(
            ticket=self.ticket,
            session=session,
            phase="coding",
            status=Task.Status.CLAIMED,
            claimed_by="worker-A",
            claimed_by_session="session-A",
        )

    def dispatch(self, *, driver: object | None = None) -> TaskAttempt:
        run = driver or mock.Mock(return_value=mock.Mock(spec=TaskAttempt))
        with mock.patch.object(headless, "_run_headless_agent", run):
            return headless.run_headless(self.task, phase="coding", overlay_skill_metadata=mock.Mock())

    def fresh(self) -> Worktree:
        return Worktree.objects.get(pk=self.worktree.pk)


class OccupiedCheckoutTests(_DispatchCase):
    def test_a_dispatch_into_an_occupied_checkout_never_runs(self) -> None:
        acquire(self.worktree, holder="task:999", holder_session="operator-lane")
        driver = mock.Mock()

        self.dispatch(driver=driver)

        driver.assert_not_called()

    def test_the_refusal_is_recorded_as_a_failed_attempt_naming_the_holder(self) -> None:
        acquire(self.worktree, holder="task:999", holder_session="operator-lane")

        self.dispatch()

        self.task.refresh_from_db()
        attempt = TaskAttempt.objects.filter(task=self.task).latest("pk")
        assert self.task.status == Task.Status.FAILED
        assert "task:999" in attempt.error
        assert "operator-lane" in attempt.error
        assert str(self.checkout) in attempt.error

    def test_the_incumbent_keeps_the_checkout_after_the_refusal(self) -> None:
        acquire(self.worktree, holder="task:999", holder_session="operator-lane")

        self.dispatch()

        held = occupancy_holder(self.fresh())
        assert held is not None
        assert held.holder == "task:999"


class HeldForTheRunTests(_DispatchCase):
    def test_the_checkout_is_held_while_the_agent_runs(self) -> None:
        seen: list[str] = []

        def _driver(task: Task, **_: object) -> TaskAttempt:
            held = occupancy_holder(Worktree.objects.get(pk=self.worktree.pk))
            seen.append(held.holder if held else "")
            return mock.Mock(spec=TaskAttempt)

        self.dispatch(driver=_driver)

        assert seen == [task_holder_id(self.task)]

    def test_the_claim_is_handed_back_when_the_run_ends(self) -> None:
        self.dispatch()

        assert occupancy_holder(self.fresh()) is None

    def test_the_claim_is_handed_back_when_the_run_raises(self) -> None:
        driver = mock.Mock(side_effect=RuntimeError("boom"))

        with pytest.raises(RuntimeError, match="boom"):
            self.dispatch(driver=driver)

        assert occupancy_holder(self.fresh()) is None


class HeartbeatRenewalTests(_DispatchCase):
    def test_the_heartbeat_extends_this_runs_claim(self) -> None:
        acquire(
            self.worktree,
            holder=task_holder_id(self.task),
            holder_session="session-A",
            lease_seconds=60,
        )
        before = self.fresh().occupancy_expires_at

        headless._renew_lease_closing_connection(self.task)

        after = self.fresh().occupancy_expires_at
        assert before is not None
        assert after is not None
        assert after > before

    def test_a_checkout_taken_by_a_rival_aborts_the_run(self) -> None:
        acquire(self.worktree, holder="task:999", holder_session="operator-lane")

        with pytest.raises(headless.LeaseLostError, match="already occupied by task:999"):
            headless._renew_lease_closing_connection(self.task)

    def test_an_unprovisioned_ticket_renews_no_claim(self) -> None:
        Worktree.objects.filter(pk=self.worktree.pk).delete()

        headless._renew_lease_closing_connection(self.task)

        assert not Worktree.objects.filter(pk=self.worktree.pk).exists()

    def test_a_ticketless_task_still_renews_its_lease(self) -> None:
        # The LEASE renewal is the load-bearing half; the occupancy add-on must never
        # be what breaks it. A ticketless Task raises on the ``ticket`` descriptor.
        renew_lease = mock.Mock()
        with mock.patch.object(Task, "renew_lease", renew_lease):
            headless._renew_lease_closing_connection(Task())

        renew_lease.assert_called_once()
