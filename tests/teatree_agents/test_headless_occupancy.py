"""A dispatch never joins an agent already in the checkout (souliane/teatree#3952).

The Task-seam dedupe (#3903) cannot see an agent that already HOLDS a checkout, so
before this guard the loop's own agent and an operator-dispatched one both ran in
one working tree. These pin the two halves at the dispatch seam: an occupied
checkout is REFUSED with the incumbent named, and a free one is held for the whole
run so a rival cannot take it mid-flight.

``_run_agent`` is patched out throughout — the assertion is about which
runs are ALLOWED to start, and driving a real harness would prove nothing about
that while costing a model call.
"""

import asyncio
import tempfile
import threading
from collections.abc import AsyncIterator
from pathlib import Path
from unittest import mock

import pytest
from django.test import TestCase
from django.utils import timezone

from teatree.agents import runner
from teatree.agents.live_control import LiveTask
from teatree.agents.live_registry import shared_registry
from teatree.agents.runner_stream import StreamCapture
from teatree.core.models import LeaseLostError, Task, TaskAttempt, Worktree
from teatree.core.worktree.occupancy import (
    OccupancyHolder,
    WorktreeOccupiedError,
    acquire,
    occupancy_holder,
    task_holder_id,
)
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
        with mock.patch.object(runner, "_run_agent", run):
            return runner.run_agent(self.task, phase="coding", overlay_skill_metadata=mock.Mock())

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


class DeferredOccupancyRaceTests(_DispatchCase):
    """The residual TOCTOU race the self-heal cannot close: park, never fail (#4867).

    ``occupy_ticket_checkout``'s self-heal already handles the common case (a stale
    claim named by a finished Task); this is the narrow window where a genuine new
    holder wins the acquire between that release and this requester's own —
    exercised directly at ``run_agent``'s except handler rather than by racing two
    real threads, since the window itself is inherently timing-dependent.
    """

    def _refusal(self, *, holder_task: Task) -> mock.MagicMock:
        holder = OccupancyHolder(
            holder=task_holder_id(holder_task),
            holder_session="rival-session",
            since=None,
            expires_at=timezone.now(),
        )
        cm = mock.MagicMock()
        cm.__enter__.side_effect = WorktreeOccupiedError("occupied", holder=holder)
        return cm

    def test_a_refusal_naming_a_finished_holder_is_parked_not_failed(self) -> None:
        rival_ticket = TicketFactory()
        finished = Task.objects.create(
            ticket=rival_ticket, session=SessionFactory(ticket=rival_ticket), status=Task.Status.COMPLETED
        )
        driver = mock.Mock()

        with mock.patch.object(runner, "occupy_ticket_checkout", return_value=self._refusal(holder_task=finished)):
            self.dispatch(driver=driver)

        driver.assert_not_called()
        self.task.refresh_from_db()
        assert self.task.status == Task.Status.PENDING
        assert self.task.not_before is not None
        attempt = TaskAttempt.objects.filter(task=self.task).latest("pk")
        assert attempt.exit_code == 0
        assert "occupancy_deferred:" in str(attempt.result.get("summary", ""))

    def test_a_refusal_naming_a_terminal_but_not_confirmed_dead_holder_is_parked(self) -> None:
        """#4880: a third-party fail (#4872) keeps the holder's owner fields — still parks.

        ``Task.fail(by_holder=False)`` on a live holder lands the row FAILED (terminal)
        but deliberately leaves ``owner_pid`` intact when it cannot prove the owner
        dead. A successor dispatch racing that holder must still PARK, never HALT —
        the old run's own heartbeat unwinds the occupancy on its own (#4867). Reusing
        the self-heal's liveness-gated ``terminal_task_pk`` for this decision instead
        of the lenient ``terminal_holder_task_pk`` regressed this to a HALT.
        """
        rival_ticket = TicketFactory()
        rival_session = SessionFactory(ticket=rival_ticket)
        cancelled = Task.objects.create(ticket=rival_ticket, session=rival_session, phase="coding")
        cancelled.claim(claimed_by="worker-B", claimed_by_session="session-B")
        with mock.patch("teatree.utils.singleton.pid_alive", return_value=True):
            Task.objects.get(pk=cancelled.pk).fail(reason="test: cancelled by an operator", by_holder=False)
        driver = mock.Mock()

        with mock.patch.object(runner, "occupy_ticket_checkout", return_value=self._refusal(holder_task=cancelled)):
            self.dispatch(driver=driver)

        driver.assert_not_called()
        self.task.refresh_from_db()
        assert self.task.status == Task.Status.PENDING
        assert self.task.not_before is not None
        attempt = TaskAttempt.objects.filter(task=self.task).latest("pk")
        assert "occupancy_deferred:" in str(attempt.result.get("summary", ""))

    def test_a_refusal_naming_a_live_holder_still_fails(self) -> None:
        rival_ticket = TicketFactory()
        live = Task.objects.create(
            ticket=rival_ticket, session=SessionFactory(ticket=rival_ticket), status=Task.Status.PENDING
        )

        with mock.patch.object(runner, "occupy_ticket_checkout", return_value=self._refusal(holder_task=live)):
            self.dispatch()

        self.task.refresh_from_db()
        assert self.task.status == Task.Status.FAILED


class HeartbeatRenewalTests(_DispatchCase):
    def test_the_heartbeat_extends_this_runs_claim(self) -> None:
        acquire(
            self.worktree,
            holder=task_holder_id(self.task),
            holder_session="session-A",
            lease_seconds=60,
        )
        before = self.fresh().occupancy_expires_at

        runner._renew_lease_closing_connection(self.task)

        after = self.fresh().occupancy_expires_at
        assert before is not None
        assert after is not None
        assert after > before

    def test_a_checkout_taken_by_a_rival_aborts_the_run(self) -> None:
        acquire(self.worktree, holder="task:999", holder_session="operator-lane")

        with pytest.raises(LeaseLostError, match="already occupied by task:999"):
            runner._renew_lease_closing_connection(self.task)

    def test_an_unprovisioned_ticket_renews_no_claim(self) -> None:
        Worktree.objects.filter(pk=self.worktree.pk).delete()

        runner._renew_lease_closing_connection(self.task)

        assert not Worktree.objects.filter(pk=self.worktree.pk).exists()

    def test_a_ticketless_task_still_renews_its_lease(self) -> None:
        # The LEASE renewal is the load-bearing half; the occupancy add-on must never
        # be what breaks it. A ticketless Task raises on the ``ticket`` descriptor.
        renew_lease = mock.Mock()
        with mock.patch.object(Task, "renew_lease", renew_lease):
            runner._renew_lease_closing_connection(Task())

        renew_lease.assert_called_once()


class _SteerableStub:
    async def query(self, prompt: str) -> None: ...

    async def receive_response(self) -> AsyncIterator[object]:
        return
        yield

    async def interrupt(self) -> None: ...

    async def steer(self, text: str, *, input_id: str, wait: float) -> None: ...


class LiveSessionKeepsOneWriterTests(_DispatchCase):
    """A live, steerable session is still the checkout's one writer; steering never admits a second."""

    def test_a_second_dispatch_is_refused_while_the_first_session_is_live_and_steerable(self) -> None:
        rival = Task.objects.create(ticket=self.ticket, session=SessionFactory(ticket=self.ticket), phase="coding")
        release = threading.Event()
        attached = threading.Event()
        seen: list[list[dict[str, object]]] = []

        async def hold_live_session() -> None:
            live_task = LiveTask(pk=self.task.pk, ticket=self.ticket.pk, phase="coding", harness="claude_sdk", model="")
            async with shared_registry().attach(live_task, StreamCapture()) as controller:
                controller.bind(_SteerableStub())
                attached.set()
                await asyncio.to_thread(release.wait)

        def first_run(task: Task, **_: object) -> TaskAttempt:
            holder = threading.Thread(target=asyncio.run, args=(hold_live_session(),))
            holder.start()
            attached.wait(timeout=10)
            with mock.patch.object(runner, "_run_agent") as second_driver:
                runner.run_agent(rival, phase="coding", overlay_skill_metadata=mock.Mock())
            seen.append([dict(facts) for facts in shared_registry().sessions()])
            release.set()
            holder.join(timeout=10)
            second_driver.assert_not_called()
            return mock.Mock(spec=TaskAttempt)

        self.dispatch(driver=first_run)

        rival.refresh_from_db()
        assert rival.status == Task.Status.FAILED
        assert task_holder_id(self.task) in TaskAttempt.objects.filter(task=rival).latest("pk").error
        assert [(row["task"], row["steerable"]) for row in seen[0]] == [(self.task.pk, True)]
