"""The driver holds the round ceiling on every runtime and hands the task off at the boundary.

A runtime whose harness takes hooks gets the refusal hook; one that does not (the codex
app-server, the metered lane) is interrupted as a round starts past the ceiling, and the
run is recorded as a ``needs_user_input`` hand-off that parks the task behind a question.
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import patch

from claude_agent_sdk import AssistantMessage, SystemMessage, UserMessage
from django.test import TestCase

import teatree.agents.runner as runner_mod
from teatree.agents.codex_app_server_messages import tool_blocks
from teatree.agents.harness_registry import HarnessCapabilities
from teatree.agents.round_ceiling import PUSHED_ROUND_CEILING, ROUND_STARTED, ROUND_TOOL_MATCHER, RoundCeiling
from teatree.agents.runner import LoopWatchdog, TaskUsage, _build_options, _drive_with_heartbeat
from teatree.agents.runner_outcomes import outcome_failure
from teatree.agents.runner_stream import HarnessOutcome
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
    async def open(self, options: Any) -> AsyncIterator[FakeHarnessSession]:
        self.opened_options = options
        self.session = FakeHarnessSession(self._messages)
        yield self.session


def _task() -> Task:
    ticket = planned_ticket(role=Ticket.Role.AUTHOR, state=Ticket.State.WORK_STARTED)
    session = Session.objects.create(ticket=ticket, agent_id="agent-1")
    task = Task.objects.create(ticket=ticket, session=session, phase="shipping")
    task.renew_lease = lambda **_kw: None
    return task


def _drive(task: Task, harness: _Harness) -> HarnessOutcome:
    options = _build_options(task, "ctx", phase="shipping", skills=[])
    watchdog = LoopWatchdog(max_runtime_seconds=0, max_turns=0, max_cost_usd=0.0)
    with patch.object(runner_mod.TaskUsage, "for_task", classmethod(lambda cls, t: TaskUsage(0, 0.0))):
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

    def test_a_hook_runtime_gets_the_refusal_hook_and_is_never_interrupted(self) -> None:
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


class TestTheHandoffParksTheTask(TestCase):
    def test_the_interrupted_run_is_recorded_as_needs_user_input(self) -> None:
        task = _task()
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
        assert attempt.result["needs_user_input"] is True
        assert task.status == Task.Status.COMPLETED
        question = DeferredQuestion.objects.get(parked_task=task)
        assert "3 rounds" in question.question
        assert "may have partially run" in question.question
        assert "stopped before" not in question.question
