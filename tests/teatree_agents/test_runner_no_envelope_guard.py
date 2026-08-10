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
import teatree.agents.runner as runner_mod
from teatree.agents.attempt_recorder import record_result_envelope
from teatree.agents.envelope_refusal import NO_ENVELOPE_ERROR, is_envelope_refusal
from teatree.agents.harness import PydanticAiHarness
from teatree.agents.runner import TaskUsage, run_agent
from teatree.core.models import Session, Task, TaskAttempt, Ticket

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
        patch.object(runner_mod.shutil, "which", return_value="/usr/bin/claude"),
        patch.object(harness_mod, "ClaudeSDKClient", _make_client),
        patch.object(runner_mod.TaskUsage, "for_task", classmethod(lambda cls, task: snapshot)),
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
            attempt = run_agent(task, phase="debugging", overlay_skill_metadata={})

        self._assert_refused(attempt, task, session)

    def test_pydantic_ai_lane_prose_only_on_nonexempt_phase_is_refused(self) -> None:
        # pydantic-ai lane (TestModel double): same prose-only run, same phase →
        # same refusal. Proves the guard is genuinely shared, not lane-conditional.
        session = Session.objects.create(ticket=self.ticket, agent_id="agent-1")
        task = Task.objects.create(ticket=self.ticket, session=session, phase="debugging")

        fake_harness = PydanticAiHarness(model=TestModel(custom_output_text=_PROSE))
        with (
            patch.object(runner_mod, "resolve_harness", return_value=fake_harness),
            patch.object(runner_mod.TaskUsage, "for_task", classmethod(lambda cls, task: TaskUsage(0, 0.0))),
        ):
            attempt = run_agent(task, phase="debugging", overlay_skill_metadata={})

        self._assert_refused(attempt, task, session)

    def test_recorded_refusal_is_classified_by_the_correcting_sweep(self) -> None:
        # Producer/consumer parity. The refusal this runner WRITES must be the
        # refusal ``loop.transient_requeue`` READS when it decides whether to spend
        # the one bounded corrective retry. The two strings were hand-typed in two
        # modules and drifted, so ``no_result_envelope`` — the most literal
        # omitted-envelope failure there is — was the single envelope refusal that
        # never earned the retry, and the first prose-only run paged a human.
        session = Session.objects.create(ticket=self.ticket, agent_id="agent-1")
        task = Task.objects.create(ticket=self.ticket, session=session, phase="debugging")

        with _fake_sdk(_PROSE):
            attempt = run_agent(task, phase="debugging", overlay_skill_metadata={})

        assert is_envelope_refusal(attempt.error), (
            f"the correcting sweep must classify the runner's own refusal; got: {attempt.error!r}"
        )
        # Control: the classifier can say NO — it is not a rubber stamp.
        assert not is_envelope_refusal("AssertionError: expected 3 got 4")

    def test_evidence_phase_with_no_json_is_diagnosed_as_no_envelope(self) -> None:
        # #3905. `testing` carries an evidence requirement, so an envelope-less run
        # is handed on to the recorder (so the salvage and the per-field diagnosis
        # keep their turn) — and the recorder then refused it with "result must
        # include one of [tests_run | tests_passed]". That reads as "the agent
        # emitted an envelope and omitted a key". It emitted nothing at all, and
        # the misdiagnosis sent the investigation of two halted tickets after the
        # wrong hypothesis.
        session = Session.objects.create(ticket=self.ticket, agent_id="agent-1")
        task = Task.objects.create(ticket=self.ticket, session=session, phase="testing")

        with _fake_sdk(_PROSE):
            attempt = run_agent(task, phase="testing", overlay_skill_metadata={})

        task.refresh_from_db()
        assert task.status == Task.Status.FAILED
        assert attempt.error == NO_ENVELOPE_ERROR, (
            f"a run that produced NO JSON must be diagnosed as such; got: {attempt.error!r}"
        )
        assert "missing required evidence" not in attempt.error
        assert attempt.result["summary"] == _PROSE[:1000], "the prose must stay on the attempt for diagnosis"

    def test_a_parsed_envelope_missing_its_evidence_key_keeps_the_per_field_diagnosis(self) -> None:
        # Behaviour preservation, and the control for the test above: when an
        # envelope WAS parsed, "you omitted the key" is the true diagnosis and
        # must survive untouched.
        session = Session.objects.create(ticket=self.ticket, agent_id="agent-1")
        task = Task.objects.create(ticket=self.ticket, session=session, phase="testing")

        with _fake_sdk('{"summary": "ran them, honest"}'):
            attempt = run_agent(task, phase="testing", overlay_skill_metadata={})

        assert "missing required evidence" in attempt.error
        assert attempt.error != NO_ENVELOPE_ERROR

    def test_both_diagnoses_stay_classified_as_envelope_refusals(self) -> None:
        # The consumer contract: `transient_requeue` routes on
        # `is_envelope_refusal`, so re-diagnosing must not drop either path out
        # of the corrective lane.
        session = Session.objects.create(ticket=self.ticket, agent_id="agent-1")
        no_json = Task.objects.create(ticket=self.ticket, session=session, phase="testing")
        parsed = Task.objects.create(ticket=self.ticket, session=session, phase="testing")

        with _fake_sdk(_PROSE):
            no_json_attempt = run_agent(no_json, phase="testing", overlay_skill_metadata={})
        with _fake_sdk('{"summary": "ran them, honest"}'):
            parsed_attempt = run_agent(parsed, phase="testing", overlay_skill_metadata={})

        assert is_envelope_refusal(no_json_attempt.error)
        assert is_envelope_refusal(parsed_attempt.error)
        assert not is_envelope_refusal("AssertionError: expected 3 got 4")

    def test_the_halt_fingerprint_is_stable_across_two_no_envelope_runs(self) -> None:
        # `task_repair` halts on two consecutive IDENTICAL fingerprints, so a
        # diagnosis carrying run-specific prose would make every no-envelope run
        # look fresh and defeat the stall check.
        session = Session.objects.create(ticket=self.ticket, agent_id="agent-1")
        first = Task.objects.create(ticket=self.ticket, session=session, phase="testing")
        second = Task.objects.create(ticket=self.ticket, session=session, phase="testing")

        with _fake_sdk(_PROSE):
            run_agent(first, phase="testing", overlay_skill_metadata={})
        with _fake_sdk("A completely different prose ending, still no envelope."):
            run_agent(second, phase="testing", overlay_skill_metadata={})

        fingerprints = {
            TaskAttempt.objects.get(task=first).error_fingerprint,
            TaskAttempt.objects.get(task=second).error_fingerprint,
        }
        assert len(fingerprints) == 1, f"the fingerprint must not vary with the agent's prose; got {fingerprints}"
        assert fingerprints != {""}

    def test_the_in_session_recorder_path_keeps_the_per_field_diagnosis(self) -> None:
        # `manage.py task record-attempt` hands over an envelope it already
        # parsed, so its evidence refusal is genuinely "you omitted the key".
        session = Session.objects.create(ticket=self.ticket, agent_id="agent-1")
        task = Task.objects.create(ticket=self.ticket, session=session, phase="testing")

        attempt = record_result_envelope(task, {"summary": "handed back in-session"}, phase="testing")

        assert "missing required evidence" in attempt.error

    def test_exempt_phase_keeps_prose_fallback(self) -> None:
        # Behaviour preservation: an exempt phase (``retro``) still records the
        # prose summary fallback and COMPLETES — byte-identical to before.
        session = Session.objects.create(ticket=self.ticket, agent_id="agent-1")
        task = Task.objects.create(ticket=self.ticket, session=session, phase="retro")

        with _fake_sdk(_PROSE):
            attempt = run_agent(task, phase="retro", overlay_skill_metadata={})

        task.refresh_from_db()
        assert attempt.exit_code == 0
        assert attempt.result["summary"] == _PROSE[:1000]
        assert task.status == Task.Status.COMPLETED
