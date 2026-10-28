"""The driver holds the round ceiling on every runtime and hands the task off at the boundary.

A runtime whose harness takes hooks gets the refusal hook; one that does not (the codex
app-server, the metered lane) is interrupted as a round starts past the ceiling, and the
run is parked for a bounded continuation.
"""

import asyncio
import contextlib
from collections.abc import AsyncGenerator
from typing import Any, cast
from unittest.mock import patch

from claude_agent_sdk import AssistantMessage, SystemMessage, UserMessage
from django.test import TestCase

import teatree.agents.runner as runner_mod
from teatree.agents.codex_app_server_messages import tool_blocks
from teatree.agents.harness_registry import HarnessCapabilities
from teatree.agents.round_ceiling import PUSHED_ROUND_CEILING, ROUND_STARTED, ROUND_TOOL_MATCHER, RoundCeiling
from teatree.agents.runner import LoopWatchdog, TaskUsage, _build_options, _drive_with_heartbeat
from teatree.agents.runner_outcomes import outcome_failure
from teatree.agents.runner_route_recording import fallback_reason_for_outcome
from teatree.agents.runner_stream import HarnessOutcome, StreamCapture, drain_turn
from teatree.core.models import DeferredQuestion, Session, Task, Ticket
from tests.factories import planned_ticket
from tests.teatree_agents._codex_command_shape import codex_wrapped
from tests.teatree_agents._sdk_fake import FakeHarnessSession, assistant_text, result_message


def _push(tool_id: str) -> list[Any]:
    """A completed push exactly as the codex harness translates it, in the shape codex reports."""
    command = codex_wrapped("git -C /work push origin feature")
    item = {"id": tool_id, "type": "commandExecution", "command": command, "status": "completed", "exitCode": 0}
    use, result = tool_blocks(item)
    return [
        SystemMessage(subtype=ROUND_STARTED, data={"command": command}),
        AssistantMessage(content=[use], model="m"),
        UserMessage(content=[result]),
    ]


class _Harness:
    def __init__(self, messages: list[Any], *, hooks: bool) -> None:
        self.capabilities = HarnessCapabilities(hooks=hooks)
        self._messages = messages
        self.opened_options: Any = None
        self.session: FakeHarnessSession | None = None

    @contextlib.asynccontextmanager
    async def open(self, options: Any) -> AsyncGenerator[FakeHarnessSession]:
        self.opened_options = options
        self.session = FakeHarnessSession(self._messages)
        yield self.session


def _task() -> Task:
    ticket = planned_ticket(role=Ticket.Role.AUTHOR, state=Ticket.State.WORK_STARTED)
    session = Session.objects.create(ticket=ticket, agent_id="agent-1")
    return Task.objects.create(ticket=ticket, session=session, phase="shipping")


def _drive(task: Task, harness: _Harness) -> HarnessOutcome:
    options = _build_options(task, "ctx", phase="shipping", skills=[])
    watchdog = LoopWatchdog(max_runtime_seconds=0, max_turns=0, max_cost_usd=0.0)
    with (
        patch.object(runner_mod.TaskUsage, "for_task", classmethod(lambda cls, t: TaskUsage(0, 0.0))),
        patch.object(task, "renew_lease", new=lambda **_kw: None),
    ):
        return asyncio.run(_drive_with_heartbeat(task, "p", options, harness, watchdog=watchdog))


def _four_rounds() -> list[Any]:
    rounds = [message for n in range(PUSHED_ROUND_CEILING) for message in _push(f"push-{n}")]
    return [*rounds, *_push("push-extra"), assistant_text("still going"), result_message()]


class TestTheDriverHoldsTheCeilingOnEveryRuntime(TestCase):
    def test_a_runtime_without_hooks_is_interrupted_as_the_extra_round_starts(self) -> None:
        harness = _Harness(_four_rounds(), hooks=False)

        outcome = _drive(_task(), harness)

        assert harness.session is not None
        assert harness.session.interrupted
        assert f"{PUSHED_ROUND_CEILING} rounds" in outcome.round_handoff
        assert f"at the start of round {PUSHED_ROUND_CEILING + 1}" in outcome.round_handoff
        assert "still going" not in outcome.agent_text

    def test_a_hook_runtime_gets_the_refusal_hook(self) -> None:
        harness = _Harness(_four_rounds(), hooks=True)

        outcome = _drive(_task(), harness)

        matchers = harness.opened_options.hooks["PreToolUse"]
        assert any(
            m.matcher == ROUND_TOOL_MATCHER and isinstance(getattr(m.hooks[0], "__self__", None), RoundCeiling)
            for m in matchers
        )
        assert harness.session is not None
        assert not harness.session.interrupted
        assert outcome.round_handoff == ""

    def test_a_hook_refusal_is_captured_and_interrupts_the_run(self) -> None:
        ceiling = RoundCeiling(enforce_by_observation=False)
        ceiling.pushes = PUSHED_ROUND_CEILING
        refusal = asyncio.run(
            ceiling.pre_tool_use(
                {
                    "session_id": "test",
                    "transcript_path": "",
                    "cwd": "/work",
                    "hook_event_name": "PreToolUse",
                    "tool_name": "Bash",
                    "tool_input": {"command": "git push origin feature"},
                    "tool_use_id": "push-1",
                },
                None,
                {"signal": None},
            )
        )
        assert cast("dict[str, Any]", refusal)["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert ceiling.handoff

        session = FakeHarnessSession([assistant_text("stopping now"), result_message()])
        outcome = asyncio.run(drain_turn(session, StreamCapture(round_ceiling=ceiling)))
        assert session.interrupted
        assert outcome.round_handoff == ceiling.handoff


class TestTheHandoffParksTheTask(TestCase):
    def test_driver_ended_outcomes_never_become_route_failures(self) -> None:
        terminal = result_message(
            subtype="error_during_execution",
            is_error=True,
            result="The process timed out while discussing authorization and credentials.",
        )
        outcomes = (
            HarnessOutcome("work done", terminal, None, round_handoff="round ceiling"),
            HarnessOutcome("work done", terminal, "deploy", checkpointed=True),
            HarnessOutcome("work done", terminal, "lease reclaimed", lease_lost=True),
            HarnessOutcome("work done", terminal, "operator pause", paused=True),
        )
        for outcome in outcomes:
            with self.subTest(outcome=outcome):
                assert fallback_reason_for_outcome(outcome, metered_transport=False) is None
        assert (
            fallback_reason_for_outcome(
                HarnessOutcome("work done", terminal, "watchdog timeout"), metered_transport=False
            )
            is not None
        )

    def test_the_interrupted_run_queues_a_bounded_continuation(self) -> None:
        task = _task()
        task.claim(claimed_by="test-worker", claimed_by_session="session-1")
        outcome = HarnessOutcome(
            agent_text="fixed two tests, pipeline still red on lint",
            result_message=None,
            stuck_reason=None,
            tool_calls=4,
            round_handoff="pushed 3 rounds (ceiling 3 rounds); interrupted at the start of round 4",
        )

        attempt = outcome_failure(task, outcome, phase="shipping")

        task.refresh_from_db()
        assert attempt is not None
        assert attempt.error.startswith("limit_parked: round ceiling")
        assert task.status == Task.Status.PENDING
        assert task.not_before is not None
        assert "check the branch and its pipeline" in task.execution_reason
        assert not DeferredQuestion.objects.filter(parked_task=task).exists()

    def test_fourth_ceiling_stop_fails_instead_of_asking(self) -> None:
        task = _task()
        outcome = HarnessOutcome("still working", None, None, round_handoff="round ceiling")
        for index in range(3):
            task.claim(claimed_by="test-worker", claimed_by_session="session-1")
            attempt = outcome_failure(task, outcome, phase="shipping")
            assert attempt is not None
            assert attempt.error.startswith("limit_parked: round ceiling")
            parks = task.attempts.filter(error__startswith="limit_parked: round ceiling")
            assert sum(1 + repeats for repeats in parks.values_list("park_repeats", flat=True)) == index + 1
            task.refresh_from_db()
            assert task.status == Task.Status.PENDING

        task.claim(claimed_by="test-worker", claimed_by_session="session-1")
        attempt = outcome_failure(task, outcome, phase="shipping")
        assert attempt is not None
        assert attempt.error.startswith("round_ceiling_exhausted:")
        task.refresh_from_db()
        assert task.status == Task.Status.FAILED
        assert not DeferredQuestion.objects.filter(parked_task=task).exists()
