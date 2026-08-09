"""A coding attempt that emitted no tool call could not act, so it is not a success.

The false-success class this pins: a toolless, single-turn coding run answers with
one sentence of prose, lands no commit, and is recorded ``outcome=success`` — with a
``files_modified`` list the #3263 salvage synthesized from the whole branch diff.
On a long-lived branch (a bootstrap branch thousands of commits ahead of its base)
``worktree_has_commits_ahead`` is permanently true, so the landing gate cannot fire
and the salvage manufactures evidence for a run that did nothing.

The separator is the tool stream, not the diff: a coding task with genuinely nothing
to change still READS before concluding that, so it emits tool calls. Zero measured
tool calls is positive evidence the agent could not act. UNMEASURED (``None``) is
never a verdict.
"""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, ToolUseBlock
from django.test import TestCase
from pydantic_ai.models.test import TestModel

import teatree.agents.headless as headless_mod
from teatree.agents.action_verification import action_verification_error
from teatree.agents.attempt_recorder import AttemptUsage, record_result_envelope
from teatree.agents.harness import PydanticAiHarness
from teatree.agents.headless import TaskUsage, _collect, run_agent
from teatree.core.models import Session, Task, Ticket, Worktree
from tests.teatree_core.models._shared import _init_repo_with_branch

#: The opening sentence a toolless run stops on — attempt 6534's whole output.
_OPENING_PROSE = "I'll start by reading the issue and understanding the current state."


class TestActionVerificationError(TestCase):
    def test_measured_zero_tool_calls_on_coding_is_refused(self) -> None:
        error = action_verification_error("coding", tool_calls=0)
        assert error.startswith("action_unverified:")
        assert "tool call" in error

    def test_measured_zero_tool_calls_on_debugging_is_refused(self) -> None:
        assert action_verification_error("debugging", tool_calls=0).startswith("action_unverified:")

    def test_one_tool_call_is_accepted(self) -> None:
        assert action_verification_error("coding", tool_calls=1) == ""

    def test_unmeasured_is_never_a_verdict(self) -> None:
        assert action_verification_error("coding", tool_calls=None) == ""

    def test_non_acting_phases_are_untouched(self) -> None:
        for phase in ("reviewing", "planning", "scoping", "answering", "shipping"):
            assert action_verification_error(phase, tool_calls=0) == ""


class _FakeSession:
    """A harness session yielding a fixed message stream, in the shared vocabulary."""

    def __init__(self, *messages: object) -> None:
        self._messages = messages

    async def query(self, _prompt: str) -> None:
        return None

    async def receive_response(self) -> AsyncIterator[Any]:
        for message in self._messages:
            yield message


def _result_message() -> ResultMessage:
    return ResultMessage(
        subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1, session_id="s1"
    )


class TestRunnerMeasuresToolCalls(TestCase):
    """The guard is only real if the driver measures — an unmeasured count never fires it."""

    def test_prose_only_run_measures_zero(self) -> None:
        session = _FakeSession(
            AssistantMessage(content=[TextBlock(text=_OPENING_PROSE)], model="m"),
            _result_message(),
        )
        outcome = asyncio.run(_collect(session, "go"))
        assert outcome.tool_calls == 0

    def test_tool_use_blocks_are_counted(self) -> None:
        session = _FakeSession(
            AssistantMessage(content=[ToolUseBlock(id="1", name="Read", input={})], model="m"),
            AssistantMessage(
                content=[ToolUseBlock(id="2", name="Edit", input={}), ToolUseBlock(id="3", name="Bash", input={})],
                model="m",
            ),
            AssistantMessage(content=[TextBlock(text="done")], model="m"),
            _result_message(),
        )
        outcome = asyncio.run(_collect(session, "go"))
        assert outcome.tool_calls == 3


class TestRunHeadlessRefusesAToollessCodingRun(TestCase):
    """End-to-end: the wire from the driver's measurement to the recorder's refusal.

    Pins the seam a future edit could silently unplug — dropping ``tool_calls`` on
    the way to ``AttemptUsage`` degrades every run to UNMEASURED, which the gate
    treats as no verdict, restoring the false success with every unit test green.
    """

    @classmethod
    def setUpTestData(cls) -> None:
        cls.ticket = Ticket.objects.create()

    def test_prose_only_coding_run_is_refused_by_the_action_gate(self) -> None:
        session = Session.objects.create(ticket=self.ticket, agent_id="agent-1")
        task = Task.objects.create(ticket=self.ticket, session=session, phase="coding")

        fake_harness = PydanticAiHarness(model=TestModel(custom_output_text=_OPENING_PROSE))
        with (
            patch.object(headless_mod, "resolve_harness", return_value=fake_harness),
            patch.object(headless_mod.TaskUsage, "for_task", classmethod(lambda cls, task: TaskUsage(0, 0.0))),
        ):
            attempt = run_agent(task, phase="coding", overlay_skill_metadata={})

        assert attempt.error.startswith("action_unverified:"), (
            f"a toolless coding run must be refused by the action gate; got: {attempt.error!r}"
        )
        task.refresh_from_db()
        assert task.status == Task.Status.FAILED
        session.refresh_from_db()
        assert "coding" not in (session.visited_phases or [])


class TestToollessCodingAttemptIsNotASuccess(TestCase):
    @pytest.fixture(autouse=True)
    def _inject_tmp_path(self, tmp_path: Path) -> None:
        self._tmp_path = tmp_path

    def _task(self, *, phase: str = "coding") -> Task:
        ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, state=Ticket.State.STARTED)
        session = Session.objects.create(ticket=ticket, agent_id=phase)
        return Task.objects.create(ticket=ticket, session=session, phase=phase)

    def _attach_long_lived_branch(self, ticket: Ticket, *, commits_ahead: int) -> Path:
        repo_dir = self._tmp_path / f"repo-{ticket.pk}"
        branch = f"feature-{ticket.pk}"
        _init_repo_with_branch(repo_dir, branch=branch, commits_ahead=commits_ahead)
        Worktree.objects.create(
            ticket=ticket,
            repo_path=str(repo_dir),
            branch=branch,
            extra={"worktree_path": str(repo_dir)},
        )
        return repo_dir

    def test_prose_only_zero_tool_call_run_fails_naming_the_cause(self) -> None:
        task = self._task()
        self._attach_long_lived_branch(task.ticket, commits_ahead=3)

        attempt = record_result_envelope(
            task,
            {"summary": _OPENING_PROSE},
            phase="coding",
            usage=AttemptUsage(num_turns=1, tool_calls=0),
            envelope_parsed=False,
        )

        assert attempt.error.startswith("action_unverified:")
        assert "tool call" in attempt.error
        task.refresh_from_db()
        assert task.status == Task.Status.FAILED

    def test_salvage_never_manufactures_evidence_for_a_toolless_run(self) -> None:
        task = self._task()
        self._attach_long_lived_branch(task.ticket, commits_ahead=3)

        attempt = record_result_envelope(
            task,
            {"summary": _OPENING_PROSE},
            phase="coding",
            usage=AttemptUsage(num_turns=1, tool_calls=0),
            envelope_parsed=False,
        )

        assert "files_modified" not in attempt.result

    def test_a_run_that_acted_still_completes(self) -> None:
        task = self._task()
        self._attach_long_lived_branch(task.ticket, commits_ahead=3)

        attempt = record_result_envelope(
            task,
            {"summary": "fixed it", "files_modified": [{"path": "f0.txt", "action": "modified"}]},
            phase="coding",
            usage=AttemptUsage(num_turns=9, tool_calls=12),
        )

        assert attempt.error == ""
        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED

    def test_an_unmeasured_in_session_handover_still_completes(self) -> None:
        task = self._task()
        self._attach_long_lived_branch(task.ticket, commits_ahead=3)

        attempt = record_result_envelope(
            task,
            {"summary": "fixed it", "files_modified": [{"path": "f0.txt", "action": "modified"}]},
            phase="coding",
            usage=AttemptUsage(num_turns=9),
        )

        assert attempt.error == ""
