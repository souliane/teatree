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
import shutil
import tempfile
import threading
import time
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import pytest
from claude_agent_sdk import ClaudeAgentOptions
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from teatree.agents import runner
from teatree.agents.live_control import ControlOutcome, LiveFence, LiveTask, RejectCode
from teatree.agents.live_registry import shared_registry
from teatree.agents.runner_heartbeat import HeartbeatRuntime, drive_with_heartbeat
from teatree.agents.runner_stream import StreamCapture
from teatree.agents.runner_watchdog import LoopWatchdog, TaskUsage
from teatree.core.cleanup.process_table import ProcessTable
from teatree.core.models import LeaseLostError, Task, TaskAttempt, Worktree
from teatree.core.worktree.checkout_fence import CheckoutFenceError
from teatree.core.worktree.checkout_state import checkout_state
from teatree.core.worktree.occupancy import (
    OccupancyHolder,
    WorktreeOccupiedError,
    acquire,
    occupancy_holder,
    release,
    task_holder_id,
)
from teatree.utils.thread_db import close_thread_db_connections
from tests._file_backed_db import file_backed_default_database
from tests.factories import SessionFactory, TicketFactory, WorktreeFactory
from tests.teatree_agents._sdk_fake import OneSessionHarness
from tests.teatree_agents.test_live_control_integration import _in_background
from tests.teatree_agents.test_runner_heartbeat import _TwoTurnSession
from tests.teatree_core.models._shared import _init_repo_with_branch


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
        with mock.patch.object(runner, "_run_agent", run), mock.patch.object(runner, "verify_task_checkout"):
            return runner.run_agent(self.task, phase="coding", overlay_skill_metadata=mock.Mock())

    def fresh(self) -> Worktree:
        return Worktree.objects.get(pk=self.worktree.pk)


class OccupiedCheckoutTests(_DispatchCase):
    def test_a_dispatch_into_an_occupied_checkout_never_runs(self) -> None:
        acquire(self.worktree, holder="task:999", holder_session="operator-lane")
        driver = mock.Mock()

        self.dispatch(driver=driver)

        driver.assert_not_called()

    def test_the_refusal_is_recorded_as_a_parked_attempt_naming_the_holder(self) -> None:
        acquire(self.worktree, holder="task:999", holder_session="operator-lane")

        self.dispatch()

        self.task.refresh_from_db()
        attempt = TaskAttempt.objects.filter(task=self.task).latest("pk")
        assert self.task.status == Task.Status.PENDING
        assert attempt.exit_code == 0
        assert "task:999" in str(attempt.result.get("summary", ""))
        assert "operator-lane" in str(attempt.result.get("summary", ""))
        assert str(self.checkout) in str(attempt.result.get("summary", ""))

    def test_the_holders_release_makes_the_parked_dispatch_due_at_once(self) -> None:
        held = acquire(self.worktree, holder="task:999", holder_session="operator-lane")

        self.dispatch()

        self.task.refresh_from_db()
        assert self.task.not_before is not None
        assert self.task.not_before > timezone.now()
        assert release(self.worktree, holder=held.holder, holder_session=held.holder_session)
        self.task.refresh_from_db()
        assert self.task.not_before <= timezone.now()

    def test_the_incumbent_keeps_the_checkout_after_the_refusal(self) -> None:
        acquire(self.worktree, holder="task:999", holder_session="operator-lane")

        self.dispatch()

        held = occupancy_holder(self.fresh())
        assert held is not None
        assert held.holder == "task:999"


class CheckoutFenceDispatchTests(_DispatchCase):
    def test_a_real_foreign_edit_parks_without_asking(self) -> None:
        _init_repo_with_branch(self.checkout, branch="feature", commits_ahead=0)
        self.addCleanup(shutil.rmtree, self.checkout)
        self.worktree.merge_extra(set_keys={"attested_state": checkout_state(self.checkout).digest})
        clear = ProcessTable(frozenset(), "/proc")
        with mock.patch("teatree.core.worktree.checkout_fence.read_process_table", return_value=clear):
            (self.checkout / "foreign.txt").write_text("foreign edit")
            attempt = runner.run_agent(self.task, phase="coding", overlay_skill_metadata=mock.Mock())

        assert attempt.error.startswith("checkout_parked:")
        assert not self.task.deferred_questions.exists()

    def test_a_refused_checkout_never_opens_the_harness(self) -> None:
        driver = mock.Mock()
        with (
            mock.patch.object(runner, "_run_agent", driver),
            mock.patch.object(runner, "verify_task_checkout", side_effect=CheckoutFenceError("foreign edit")),
        ):
            attempt = runner.run_agent(self.task, phase="coding", overlay_skill_metadata=mock.Mock())

        driver.assert_not_called()
        self.task.refresh_from_db()
        assert self.task.status == Task.Status.PENDING
        assert attempt.exit_code == 0
        assert "foreign edit" in str(attempt.result.get("summary", ""))


class HeldForTheRunTests(_DispatchCase):
    def test_a_crashed_run_releases_and_attests_its_own_checkout_edit(self) -> None:
        _init_repo_with_branch(self.checkout, branch="feature", commits_ahead=0)
        self.addCleanup(shutil.rmtree, self.checkout)
        self.worktree.merge_extra(set_keys={"attested_state": checkout_state(self.checkout).digest})
        clear = ProcessTable(frozenset(), "/proc")
        failure = RuntimeError("harness crashed")

        def crash(*_: object, **__: object) -> TaskAttempt:
            (self.checkout / "own.txt").write_text("agent edit")
            raise failure

        with (
            mock.patch.object(runner, "_run_agent", side_effect=crash),
            mock.patch("teatree.core.worktree.checkout_fence.read_process_table", return_value=clear),
            mock.patch("teatree.core.worktree.occupancy.read_process_table", return_value=clear),
            pytest.raises(RuntimeError, match="harness crashed"),
        ):
            runner.run_agent(self.task, phase="coding", overlay_skill_metadata=mock.Mock())

        settled = self.fresh()
        assert occupancy_holder(settled) is None
        assert settled.get_extra()["attested_state"] == checkout_state(self.checkout).digest

    def test_nested_run_attests_own_edit_with_host_namespace_gaps(self) -> None:
        self.worktree.delete()
        self.addCleanup(shutil.rmtree, self.checkout)
        host_gap = ProcessTable(
            frozenset(), "/proc", (), ("host namespace unavailable",), ("host namespace unavailable",)
        )

        def finish(task: Task, **_: object) -> TaskAttempt:
            _init_repo_with_branch(self.checkout, branch="feature", commits_ahead=0)
            self.worktree = WorktreeFactory(ticket=self.ticket, extra={"worktree_path": str(self.checkout)})
            (self.checkout / "own.txt").write_text("agent edit")
            task.fail_claimed(reason="agent failed after writing")
            return mock.Mock(spec=TaskAttempt)

        with (
            mock.patch.object(runner, "nested_pid_namespace", return_value="pid:[42]"),
            mock.patch.object(runner, "_run_agent", side_effect=finish),
            mock.patch("teatree.core.worktree.occupancy.read_process_table", return_value=host_gap),
        ):
            runner.run_agent(self.task, phase="coding", overlay_skill_metadata=mock.Mock())

        settled = self.fresh()
        assert occupancy_holder(settled) is None
        assert settled.get_extra()["attested_state"] == checkout_state(self.checkout).digest

    def test_process_settlement_runs_before_checkout_release(self) -> None:
        seen: list[bool] = []

        def settle(*_: object, **__: object) -> None:
            seen.append(occupancy_holder(self.fresh()) is not None)

        with mock.patch.object(runner, "settle_cancelled_checkout", side_effect=settle) as called:
            self.dispatch()

        called.assert_called_once()
        assert seen == [True]

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
        assert "checkout_parked:" in str(attempt.result.get("summary", ""))

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
        assert "checkout_parked:" in str(attempt.result.get("summary", ""))

    def test_a_refusal_naming_a_live_holder_parks(self) -> None:
        rival_ticket = TicketFactory()
        live = Task.objects.create(
            ticket=rival_ticket, session=SessionFactory(ticket=rival_ticket), status=Task.Status.PENDING
        )

        with mock.patch.object(runner, "occupy_ticket_checkout", return_value=self._refusal(holder_task=live)):
            self.dispatch()

        self.task.refresh_from_db()
        assert self.task.status == Task.Status.PENDING


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
        assert rival.status == Task.Status.PENDING
        summary = TaskAttempt.objects.filter(task=rival).latest("pk").result.get("summary", "")
        assert task_holder_id(self.task) in str(summary)
        assert [(row["task"], row["steerable"]) for row in seen[0]] == [(self.task.pk, True)]


class IdleWindowHoldsTheClaimTests(TransactionTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.enterContext(file_backed_default_database())
        self.ticket = TicketFactory()
        self.checkout = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.checkout, ignore_errors=True)
        self.worktree = WorktreeFactory(ticket=self.ticket, extra={"worktree_path": str(self.checkout)})
        self.task = Task.objects.create(
            ticket=self.ticket,
            session=SessionFactory(ticket=self.ticket),
            phase="coding",
            status=Task.Status.CLAIMED,
            claimed_by="worker-A",
            claimed_by_session="session-A",
            claimed_at=timezone.now(),
            claimed_generation="worker-A",
            lease_expires_at=timezone.now() + timedelta(minutes=5),
        )

    def holder(self) -> OccupancyHolder | None:
        return occupancy_holder(Worktree.objects.get(pk=self.worktree.pk))

    def test_the_task_lease_and_checkout_are_renewed_through_the_idle_window_and_released_after(self) -> None:
        minted = LiveFence.mint()

        def observe() -> tuple[str, datetime, datetime, datetime, datetime]:
            deadline = time.monotonic() + 10
            while not any(row["state"] == "idle" for row in shared_registry().sessions()):
                assert time.monotonic() < deadline, "no idle window opened"
                time.sleep(0.005)
            facts = shared_registry().facts(self.task.pk)
            assert facts is not None
            assert (facts["attempt"], facts["generation"]) == (minted.attempt, "worker-A")
            while Task.objects.get(pk=self.task.pk).lease_expires_at is None:
                assert time.monotonic() < deadline, "no lease renewal reached the task"
                time.sleep(0.005)
            held = self.holder()
            lease = Task.objects.get(pk=self.task.pk).lease_expires_at
            time.sleep(0.2)
            renewed = self.holder()
            renewed_lease = Task.objects.get(pk=self.task.pk).lease_expires_at
            close_thread_db_connections()
            assert held is not None
            assert renewed is not None
            assert lease is not None
            assert renewed_lease is not None
            return held.holder, held.expires_at, renewed.expires_at, lease, renewed_lease

        def driver(task: Task, **_: object) -> TaskAttempt:
            runtime = HeartbeatRuntime(
                watchdog=LoopWatchdog(max_runtime_seconds=30, max_turns=0, max_cost_usd=0.0),
                heartbeat_interval=0.02,
                sample_usage=lambda _task: TaskUsage(turns=0, cost_usd=0.0),
                renew_lease=runner._renew_lease_closing_connection,
                drain_reason=lambda: "",
                idle_window_seconds=1.0,
            )
            asyncio.run(
                drive_with_heartbeat(
                    task, "p", ClaudeAgentOptions(), OneSessionHarness(_TwoTurnSession()), runtime=runtime
                )
            )
            return mock.Mock(spec=TaskAttempt)

        observed = _in_background(observe)
        with (
            mock.patch.object(runner, "_run_agent", driver),
            mock.patch.object(runner, "verify_task_checkout"),
            mock.patch.object(LiveFence, "mint", return_value=minted),
        ):
            runner.run_agent(self.task, phase="coding", overlay_skill_metadata=mock.Mock())
        holder, held_until, renewed_until, lease, renewed_lease = observed.result(timeout=30)

        assert holder == task_holder_id(self.task)
        assert renewed_until > held_until
        assert renewed_lease > lease
        assert self.holder() is None

    def test_a_new_worker_generation_refuses_an_idle_follow_up_before_a_second_turn(self) -> None:
        session = _TwoTurnSession()

        def observe() -> tuple[ControlOutcome, RejectCode | None]:
            deadline = time.monotonic() + 10
            while not any(row["state"] == "idle" for row in shared_registry().sessions()):
                assert time.monotonic() < deadline, "no idle window opened"
                time.sleep(0.005)
            Task.objects.filter(pk=self.task.pk).update(claimed_generation="worker-B")
            facts = shared_registry().facts(self.task.pk)
            assert facts is not None
            fence = LiveFence(facts["attempt"], facts["generation"])
            receipt = asyncio.run(
                shared_registry().follow_up(self.task.pk, "stale", command_id="old", wait=1, fence=fence)
            )
            close_thread_db_connections()
            return receipt.outcome, receipt.code

        def driver(task: Task, **_: object) -> TaskAttempt:
            runtime = HeartbeatRuntime(
                watchdog=LoopWatchdog(max_runtime_seconds=30, max_turns=0, max_cost_usd=0.0),
                heartbeat_interval=0.5,
                sample_usage=lambda _task: TaskUsage(turns=0, cost_usd=0.0),
                renew_lease=runner._renew_lease_closing_connection,
                drain_reason=lambda: "",
                idle_window_seconds=1.0,
            )
            asyncio.run(
                drive_with_heartbeat(task, "p", ClaudeAgentOptions(), OneSessionHarness(session), runtime=runtime)
            )
            return mock.Mock(spec=TaskAttempt)

        observed = _in_background(observe)
        with mock.patch.object(runner, "_run_agent", driver), mock.patch.object(runner, "verify_task_checkout"):
            runner.run_agent(self.task, phase="coding", overlay_skill_metadata=mock.Mock())

        assert observed.result(timeout=30) == (ControlOutcome.REJECTED, RejectCode.STALE_COMMAND)
        assert session.prompts == []
