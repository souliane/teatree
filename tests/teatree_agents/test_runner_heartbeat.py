"""An in-flight run checkpoints at its next heartbeat once a deploy drain closes admission (#5089).

The drain used to wait for every in-flight run to finish (up to its whole grace) and then
SIGKILL whatever was still going, recording no attempt and losing the conversation. The
heartbeat now reads the drain gate on every beat and interrupts the run so it parks with its
session id — deferring at most ``CHECKPOINT_MAX_DEFER_BEATS`` beats while a tool call is open.
"""

import asyncio
import sqlite3
import threading
from collections.abc import Callable
from unittest.mock import patch

import pytest
from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ToolUseBlock
from django.test import TestCase

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
from tests.teatree_agents._sdk_fake import InterruptibleSession, OneSessionHarness

_DRAIN = "this worker is quiescing for a rolling deploy"
_SESSION = "0f1e2d3c-4b5a-4968-8776-655443322110"


def _drain_from_beat(first: int, calls: list[str]) -> Callable[[], str]:
    def drain_reason() -> str:
        calls.append("beat")
        return _DRAIN if len(calls) >= first else ""

    return drain_reason


def _no_lease_renewal(_task: Task) -> None:
    pass


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

    outcome = _drive(session, drain_reason=_drain_from_beat(10**9, calls), max_runtime_seconds=0.1)

    assert len(calls) > 1, "the heartbeat must keep reading the gate on every beat"
    assert outcome.checkpointed is False
    assert outcome.stuck_reason is not None
    assert "runtime" in outcome.stuck_reason


def test_an_open_tool_call_defers_the_checkpoint_to_the_beat_cap() -> None:
    session = InterruptibleSession([_open_tool_call()], session_id=_SESSION)
    calls: list[str] = []

    outcome = _drive(session, drain_reason=_drain_from_beat(1, calls))

    assert outcome.checkpointed is True
    assert session.interrupts == 1
    assert len(calls) == CHECKPOINT_MAX_DEFER_BEATS + 1


def test_a_lost_lease_is_still_a_lost_lease_not_a_checkpoint() -> None:
    def lease_lost(_task: Task) -> None:
        msg = "lease lost for task 1: re-claimed by a competing worker"
        raise LeaseLostError(msg)

    session = InterruptibleSession([], session_id=_SESSION)

    outcome = _drive(session, drain_reason=_drain_from_beat(1, []), renew_lease=lease_lost)

    assert outcome.lease_lost is True
    assert outcome.checkpointed is False
    assert session.interrupts == 1


def test_an_unreadable_gate_keeps_the_run_going() -> None:
    def unreadable() -> str:
        msg = "database is locked"
        raise RuntimeError(msg)

    session = InterruptibleSession([], session_id=_SESSION)

    with patch.object(runner_heartbeat, "logger") as logger:
        outcome = _drive(session, drain_reason=unreadable, max_runtime_seconds=0.1)

    assert outcome.checkpointed is False
    assert logger.warning.call_count >= 1


class TestTheProductionDrainRead(TestCase):
    def test_it_reports_the_quiescing_gate(self) -> None:
        set_worker_quiescing(value=True)

        assert drain_reason_closing_connection() == _DRAIN

    def test_it_closes_its_worker_threads_raw_handle(self) -> None:
        raws: list[sqlite3.Connection] = []
        errors: list[BaseException] = []

        def _touch_the_orm() -> str:
            from django.db import connection  # noqa: PLC0415 — the WORKER thread's connection

            connection.ensure_connection()
            raws.append(connection.connection)
            return ""

        def _read_on_worker() -> None:
            try:
                drain_reason_closing_connection()
            except BaseException as exc:  # noqa: BLE001 — surfaced to the parent as an assertion
                errors.append(exc)

        with patch.object(runner_heartbeat, "drain_block_reason", _touch_the_orm):
            thread = threading.Thread(target=_read_on_worker)
            thread.start()
            thread.join()

        assert not errors, errors
        assert raws, "the drain read never opened the worker thread's connection"
        with pytest.raises(sqlite3.ProgrammingError):
            raws[0].execute("SELECT 1")
