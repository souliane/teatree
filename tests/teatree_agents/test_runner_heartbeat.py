"""An in-flight run checkpoints at its next heartbeat once a deploy drain closes admission (#5089).

The drain used to wait for every in-flight run to finish (up to its whole grace) and then
SIGKILL whatever was still going, recording no attempt and losing the conversation. The
heartbeat now reads the drain gate on every beat and interrupts the run so it parks with its
session id — deferring at most ``CHECKPOINT_MAX_DEFER_BEATS`` beats while a tool call is open.
"""

import asyncio
import contextlib
import sqlite3
import threading
from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from unittest.mock import patch

import pytest
from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ToolUseBlock
from django.test import TestCase

import teatree.agents.runner as runner_mod
from teatree.agents import runner_heartbeat
from teatree.agents.runner_heartbeat import (
    CHECKPOINT_MAX_DEFER_BEATS,
    HeartbeatRuntime,
    drain_reason_closing_connection,
    drive_with_heartbeat,
)
from teatree.agents.runner_stream import HarnessOutcome
from teatree.agents.runner_watchdog import LoopWatchdog, TaskUsage
from teatree.core.models import LeaseLostError, Task
from teatree.loop.drain import set_worker_quiescing
from tests.teatree_agents._sdk_fake import InterruptibleSession, OneSessionHarness, result_message

_DRAIN = "this worker is quiescing for a rolling deploy"
_SESSION = "0f1e2d3c-4b5a-4968-8776-655443322110"


def _drain_from_beat(first: int, calls: list[str]) -> Callable[[], str]:
    def drain_reason() -> str:
        calls.append("beat")
        return _DRAIN if len(calls) >= first else ""

    return drain_reason


def _no_lease_renewal(_task: Task) -> None:
    pass


def _lease_lost_on_beat(beat: int) -> Callable[[Task], None]:
    renewals: list[Task] = []

    def renew(task: Task) -> None:
        renewals.append(task)
        if len(renewals) >= beat:
            msg = f"lease lost for task {task.pk}: re-claimed by a competing worker"
            raise LeaseLostError(msg)

    return renew


def _drive(
    session: InterruptibleSession,
    *,
    drain_reason: Callable[[], str],
    renew_lease: Callable[[Task], None] = _no_lease_renewal,
    max_runtime_seconds: float = 5,
) -> HarnessOutcome:
    runtime = HeartbeatRuntime(
        watchdog=LoopWatchdog(max_runtime_seconds=max_runtime_seconds, max_turns=0, max_cost_usd=0.0),
        heartbeat_interval=0.005,
        sample_usage=lambda _task: TaskUsage(turns=0, cost_usd=0.0),
        renew_lease=renew_lease,
        drain_reason=drain_reason,
    )
    return asyncio.run(
        drive_with_heartbeat(
            Task(phase="coding"), "p", ClaudeAgentOptions(), OneSessionHarness(session), runtime=runtime
        )
    )


def _open_tool_call() -> AssistantMessage:
    return AssistantMessage(content=[ToolUseBlock(id="t1", name="Bash", input={})], model="m")


def test_a_drain_interrupts_the_run_at_its_next_heartbeat() -> None:
    session = InterruptibleSession([], session_id=_SESSION)

    outcome = _drive(session, drain_reason=_drain_from_beat(2, []))

    assert outcome.checkpointed is True
    assert outcome.stuck_reason == f"deploy checkpoint: {_DRAIN}"
    assert session.interrupts == 1
    assert outcome.result_message is not None
    assert outcome.result_message.session_id == _SESSION


def test_no_drain_never_interrupts_the_run() -> None:
    session = InterruptibleSession([], session_id=_SESSION)
    calls: list[str] = []

    outcome = _drive(session, drain_reason=_drain_from_beat(10**9, calls), renew_lease=_lease_lost_on_beat(3))

    assert len(calls) == 2, "the heartbeat must read the gate on every beat it survives"
    assert outcome.checkpointed is False
    assert outcome.lease_lost is True


def test_an_open_tool_call_defers_the_checkpoint_to_the_beat_cap() -> None:
    session = InterruptibleSession([_open_tool_call()], session_id=_SESSION)
    calls: list[str] = []

    outcome = _drive(session, drain_reason=_drain_from_beat(1, calls))

    assert outcome.checkpointed is True
    assert session.interrupts == 1
    assert len(calls) == CHECKPOINT_MAX_DEFER_BEATS + 1


def test_a_lost_lease_is_still_a_lost_lease_not_a_checkpoint() -> None:
    session = InterruptibleSession([], session_id=_SESSION)

    outcome = _drive(session, drain_reason=_drain_from_beat(1, []), renew_lease=_lease_lost_on_beat(1))

    assert outcome.lease_lost is True
    assert outcome.checkpointed is False
    assert session.interrupts == 1


def test_an_unreadable_gate_keeps_the_run_going() -> None:
    def unreadable() -> str:
        msg = "database is locked"
        raise RuntimeError(msg)

    session = InterruptibleSession([], session_id=_SESSION)

    with patch.object(runner_heartbeat, "logger") as logger:
        outcome = _drive(session, drain_reason=unreadable, renew_lease=_lease_lost_on_beat(3))

    assert outcome.checkpointed is False
    assert outcome.lease_lost is True, "the run kept going past both unreadable beats"
    assert logger.warning.call_count >= 2


class _SlowToStopSession(InterruptibleSession):
    """A session whose interrupt lands late: it works on until released, or until its Nth interrupt."""

    def __init__(self, *, stops_on_interrupt: int = 0) -> None:
        super().__init__([], session_id=_SESSION)
        self._stops_on_interrupt = stops_on_interrupt
        self._released = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None

    async def receive_response(self) -> AsyncIterator[Any]:
        self._loop = asyncio.get_running_loop()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._released.wait(), timeout=2)
        yield result_message(session_id=_SESSION, subtype="error_during_execution", is_error=True)

    async def interrupt(self) -> None:
        self.interrupts += 1
        if self.interrupts == self._stops_on_interrupt:
            self._released.set()

    def release_from_another_thread(self) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._released.set)


def test_the_beat_keeps_the_lease_alive_until_an_interrupted_run_ends() -> None:
    session = _SlowToStopSession()
    renewals_after_the_interrupt: list[int] = []

    def renew(_task: Task) -> None:
        if session.interrupts:
            renewals_after_the_interrupt.append(session.interrupts)
            if len(renewals_after_the_interrupt) == 2:
                session.release_from_another_thread()

    outcome = _drive(session, drain_reason=_drain_from_beat(1, []), renew_lease=renew)

    assert len(renewals_after_the_interrupt) >= 2, "an interrupted run still holds its claim until it ends"
    assert session.interrupts == 1, "one interrupt request, not one per beat"
    assert outcome.checkpointed is True


def test_a_lease_lost_after_the_checkpoint_request_stops_the_beat_and_stays_a_checkpoint() -> None:
    session = _SlowToStopSession(stops_on_interrupt=2)

    outcome = _drive(session, drain_reason=_drain_from_beat(1, []), renew_lease=_lease_lost_on_beat(2))

    assert session.interrupts == 2, "the run is asked to stop again once a rival holds its claim"
    assert outcome.checkpointed is True
    assert outcome.lease_lost is False, "the flags name what first interrupted the run"
    assert outcome.stuck_reason == f"deploy checkpoint: {_DRAIN}"


def test_the_runner_wires_the_gate_read_off_the_event_loop_into_every_run() -> None:
    # An ORM read on the event-loop thread is refused by Django, and the config tier then answers "open".
    read_on: list[threading.Thread] = []

    def gate() -> str:
        read_on.append(threading.current_thread())
        return _DRAIN

    task = Task(phase="coding")
    session = InterruptibleSession([], session_id=_SESSION)
    watchdog = LoopWatchdog(max_runtime_seconds=5, max_turns=0, max_cost_usd=0.0)

    with (
        patch.object(runner_mod, "_HEARTBEAT_INTERVAL", 0.005),
        patch.object(Task, "renew_lease"),
        patch.object(
            runner_mod.TaskUsage, "for_task", classmethod(lambda _cls, _task: TaskUsage(turns=0, cost_usd=0.0))
        ),
        patch.object(runner_heartbeat, "drain_block_reason", gate),
    ):
        outcome = asyncio.run(
            runner_mod._drive_with_heartbeat(
                task, "p", ClaudeAgentOptions(), OneSessionHarness(session), watchdog=watchdog
            )
        )

    assert outcome.checkpointed is True
    assert outcome.stuck_reason == f"deploy checkpoint: {_DRAIN}"
    assert read_on[0] is not threading.main_thread()


class TestTheProductionDrainRead(TestCase):
    def test_it_reports_the_quiescing_gate(self) -> None:
        set_worker_quiescing(value=True)

        assert drain_reason_closing_connection() == _DRAIN

    def test_it_closes_its_worker_threads_raw_handle(self) -> None:
        raws: list[sqlite3.Connection] = []

        def _touch_the_orm() -> str:
            from django.db import connection  # noqa: PLC0415 — the WORKER thread's connection

            connection.ensure_connection()
            raws.append(connection.connection)
            return ""

        with patch.object(runner_heartbeat, "drain_block_reason", _touch_the_orm), ThreadPoolExecutor(1) as worker:
            worker.submit(drain_reason_closing_connection).result()

        assert raws, "the drain read never opened the worker thread's connection"
        with pytest.raises(sqlite3.ProgrammingError):
            raws[0].execute("SELECT 1")
