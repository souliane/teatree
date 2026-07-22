"""Regression tests for the no-result-envelope guard in ``_record_success``.

The generic task prompt (``prompt.py``) demands a final JSON result object from
every phase, so prose-only output is a contract violation. Before this guard,
``_record_success`` manufactured ``{"summary": prose[:1000]}`` for ANY phase
absent from ``PHASE_REQUIRED_EVIDENCE`` — laundering a no-envelope run into a
false SUCCESS (task COMPLETED, FSM advanced) on both the claude-SDK and
pydantic-ai lanes.

The guard is lane-agnostic: both lanes funnel through the single shared
``_record_success`` chokepoint. A no-envelope run on a non-exempt phase now
records a FAILED attempt with a ``no_result_envelope:`` diagnostic and does NOT
advance the FSM. The exempt phases (``scoping``, ``retro``) keep the prose
fallback unchanged — pinned by ``test_headless``'s untouched scoping test.
"""

import contextlib
from collections.abc import AsyncIterator, Iterator
from typing import Any, Self
from unittest.mock import patch

from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock
from django.test import TestCase
from pydantic_ai.models.test import TestModel

import teatree.agents.harness as harness_mod
import teatree.agents.headless as headless_mod
from teatree.agents.harness import PydanticAiHarness
from teatree.agents.headless import TaskUsage, run_headless
from teatree.core.models import Session, Task, Ticket

_PROSE = "I finished the work but forgot to emit the JSON result envelope."


class _FakeSdkClient:
    """Async-context SDK stand-in yielding fixed assistant text + a success result."""

    def __init__(self, agent_text: str) -> None:
        self._agent_text = agent_text

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def query(self, _prompt: str) -> None:
        return None

    async def receive_response(self) -> AsyncIterator[Any]:
        yield AssistantMessage(content=[TextBlock(text=self._agent_text)], model="claude-opus-4-8[1m]")
        yield ResultMessage(
            subtype="success",
            duration_ms=10,
            duration_api_ms=8,
            is_error=False,
            num_turns=1,
            session_id="s1",
        )

    async def interrupt(self) -> None:
        return None


@contextlib.contextmanager
def _fake_sdk(agent_text: str) -> Iterator[None]:
    def _make_client(**_: object) -> _FakeSdkClient:
        return _FakeSdkClient(agent_text)

    snapshot = TaskUsage(turns=0, cost_usd=0.0)
    with (
        patch.object(headless_mod.shutil, "which", return_value="/usr/bin/claude"),
        patch.object(harness_mod, "ClaudeSDKClient", _make_client),
        patch.object(headless_mod.TaskUsage, "for_task", classmethod(lambda cls, task: snapshot)),
    ):
        yield


class TestNoEnvelopeGuardIsLaneAgnostic(TestCase):
    @classmethod
    def setUpTestData(cls) -> None:
        cls.ticket = Ticket.objects.create()

    def _assert_refused(self, attempt: Any, task: Task, session: Session) -> None:
        task.refresh_from_db()
        session.refresh_from_db()
        assert task.status == Task.Status.FAILED, (
            f"a no-envelope run on a non-exempt phase must FAIL; got status={task.status}"
        )
        assert attempt.error.startswith("no_result_envelope:"), (
            f"the refusal must carry the greppable diagnostic prefix; got: {attempt.error!r}"
        )
        assert "debugging" not in (session.visited_phases or []), (
            f"the FSM must NOT advance on a refused no-envelope run; visited={session.visited_phases}"
        )

    def test_claude_lane_prose_only_on_nonexempt_phase_is_refused(self) -> None:
        # Claude-SDK lane (fake-SDK scaffold): pure prose, no JSON envelope, on a
        # non-exempt phase (``debugging``) → refused, FSM not advanced.
        session = Session.objects.create(ticket=self.ticket, agent_id="agent-1")
        task = Task.objects.create(ticket=self.ticket, session=session, phase="debugging")

        with _fake_sdk(_PROSE):
            attempt = run_headless(task, phase="debugging", overlay_skill_metadata={})

        self._assert_refused(attempt, task, session)

    def test_pydantic_ai_lane_prose_only_on_nonexempt_phase_is_refused(self) -> None:
        # pydantic-ai lane (TestModel double): same prose-only run, same phase →
        # same refusal. Proves the guard is genuinely shared, not lane-conditional.
        session = Session.objects.create(ticket=self.ticket, agent_id="agent-1")
        task = Task.objects.create(ticket=self.ticket, session=session, phase="debugging")

        fake_harness = PydanticAiHarness(model=TestModel(custom_output_text=_PROSE))
        with (
            patch.object(headless_mod, "resolve_harness", return_value=fake_harness),
            patch.object(headless_mod.TaskUsage, "for_task", classmethod(lambda cls, task: TaskUsage(0, 0.0))),
        ):
            attempt = run_headless(task, phase="debugging", overlay_skill_metadata={})

        self._assert_refused(attempt, task, session)

    def test_exempt_phase_keeps_prose_fallback(self) -> None:
        # Behaviour preservation: an exempt phase (``retro``) still records the
        # prose summary fallback and COMPLETES — byte-identical to before.
        session = Session.objects.create(ticket=self.ticket, agent_id="agent-1")
        task = Task.objects.create(ticket=self.ticket, session=session, phase="retro")

        with _fake_sdk(_PROSE):
            attempt = run_headless(task, phase="retro", overlay_skill_metadata={})

        task.refresh_from_db()
        assert attempt.exit_code == 0
        assert attempt.result["summary"] == _PROSE[:1000]
        assert task.status == Task.Status.COMPLETED
