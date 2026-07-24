"""The ``pydantic_ai`` session must be able to report a FAILED turn (souliane/teatree#3157 Unit A).

:class:`~teatree.agents.pydantic_ai_session.PydanticAiHarnessSession` is the single
point translating pydantic_ai reality into the ``claude_agent_sdk`` message vocabulary
the driver consumes, and the driver's whole failure taxonomy
(:func:`~teatree.agents.headless._limit_match` -> ``park_or_rotate_on_limit``,
:func:`~teatree.agents.headless._error_result_reason` -> FAILED) keys on
``ResultMessage.is_error``. A terminal message that is unconditionally ``success``
therefore makes a bad run indistinguishable from a good one on this lane: a 429
escapes as a raw exception and lands as a ``sdk_error`` traceback instead of a park,
``num_turns`` under-counts the watchdog's turn ceiling, and ``agent_session_id`` is
empty.

Hermetic throughout — pydantic_ai's own ``FunctionModel`` / ``TestModel`` doubles, no
network, no credential, zero tokens.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from claude_agent_sdk import ResultMessage
from django.test import TestCase
from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel
from pydantic_ai.models.test import TestModel

import teatree.agents.headless as headless_mod
from teatree.agents.harness import PydanticAiHarness, PydanticAiHarnessSession
from teatree.agents.headless import TaskUsage, run_headless
from teatree.agents.pydantic_ai_config import OpenAICompatibleLaneConfig, PydanticAiModelConfig
from teatree.agents.pydantic_ai_session import _turns_made
from teatree.core.models import ConfigSetting, Session, Task, TaskAttempt, Ticket

_MODEL = "claude-opus-4-8"

#: A phase-complete envelope — ``files_modified`` is the ``coding`` phase-evidence
#: gate's required key, unrelated to the failure mapping under test.
_RESULT_JSON = json.dumps({"summary": "done", "files_modified": ["a.py"]})

_DROPPED_TRANSPORT = "connection reset by peer"
_HARNESS_BUG = "a genuine bug in the harness"


def _api_error_model(*, status_code: int, error_type: str, message: str) -> FunctionModel:
    """A model double whose request is refused with a REAL-shaped Anthropic error body.

    The body mirrors the documented Messages-API error envelope
    (``{"type": "error", "error": {"type": <code>, "message": ...}}``), so the error
    code the classifier has to recognise reaches it exactly as the wire delivers it.
    """

    async def stream_fn(_messages: object, _info: AgentInfo) -> AsyncIterator[str]:
        await asyncio.sleep(0)
        raise ModelHTTPError(
            status_code=status_code,
            model_name=_MODEL,
            body={"type": "error", "error": {"type": error_type, "message": message}},
        )
        yield ""  # unreachable — the ``yield`` is what makes this an async GENERATOR

    return FunctionModel(stream_function=stream_fn)


def _dropped_mid_stream_model(*, after_requests: int = 1) -> FunctionModel:
    """A model double that streams part of a turn, then loses the transport.

    *after_requests* burns that many model requests first (each an unanswerable tool
    call pydantic_ai retries), so the drop lands with a stream already in flight and a
    request count above one — the case a hardcoded turn count cannot distinguish.
    """
    turns = {"n": 0}

    async def stream_fn(_messages: object, _info: AgentInfo) -> AsyncIterator[object]:
        await asyncio.sleep(0)
        turns["n"] += 1
        if turns["n"] < after_requests:
            yield {0: DeltaToolCall(name="ghost_tool", json_args="{}")}
            return
        yield "partial "
        raise ModelAPIError(_MODEL, _DROPPED_TRANSPORT)

    return FunctionModel(stream_function=stream_fn)


def _two_request_model(final_text: str = _RESULT_JSON) -> FunctionModel:
    """A model double that burns TWO model requests before finishing.

    The first request calls a tool the agent does not carry, which pydantic_ai answers
    with a retry prompt — a genuine second model request, not a hand-counted one.
    """
    turns = {"n": 0}

    async def stream_fn(_messages: object, _info: AgentInfo) -> AsyncIterator[object]:
        await asyncio.sleep(0)
        turns["n"] += 1
        if turns["n"] == 1:
            yield {0: DeltaToolCall(name="ghost_tool", json_args="{}")}
        else:
            yield final_text

    return FunctionModel(stream_function=stream_fn)


def _drive(session: PydanticAiHarnessSession, prompt: str = "go") -> list[object]:
    async def turn() -> list[object]:
        await session.query(prompt)
        return [message async for message in session.receive_response()]

    return asyncio.run(turn())


def _terminal(messages: list[object]) -> ResultMessage:
    results = [message for message in messages if isinstance(message, ResultMessage)]
    assert len(results) == 1, "a turn yields exactly one terminal ResultMessage"
    return results[0]


class TestTerminalResultReportsProviderFailure:
    """A provider/run failure becomes an ERROR ``ResultMessage``, never a raw exception."""

    def test_a_refused_request_reports_the_status_and_the_full_provider_text(self) -> None:
        session = PydanticAiHarnessSession(
            Agent(
                _api_error_model(
                    status_code=429,
                    error_type="rate_limit_error",
                    message="Number of requests has exceeded your rate limit",
                )
            ),
            model_name=_MODEL,
        )

        terminal = _terminal(_drive(session))

        assert terminal.is_error is True
        assert terminal.subtype == "error_during_execution"
        assert terminal.api_error_status == 429
        # The session stays DUMB: the provider's FULL text passes through in ``result``
        # so the SHARED classifier — not this seam — owns the phrase vocabulary.
        assert "rate_limit_error" in (terminal.result or "")

    def test_a_transport_drop_without_a_status_still_reports_the_failure(self) -> None:
        session = PydanticAiHarnessSession(Agent(_dropped_mid_stream_model()), model_name=_MODEL)

        terminal = _terminal(_drive(session))

        assert terminal.is_error is True
        assert terminal.subtype == "error_during_execution"
        assert terminal.api_error_status is None
        assert _DROPPED_TRANSPORT in (terminal.result or "")

    def test_a_usage_limit_is_a_max_turns_failure_the_classifier_cannot_claim(self) -> None:
        # The run hit its OWN step cap — a genuine FAILED, never a limit park. Its
        # message must therefore name no limit phrase, or a real failure would be
        # laundered into an infinitely re-parked task.
        from teatree.llm.anthropic_limits import classify_limit  # noqa: PLC0415 — test-local assertion

        session = PydanticAiHarnessSession(Agent(_two_request_model()), model_name=_MODEL, request_limit=1)

        terminal = _terminal(_drive(session))

        assert terminal.is_error is True
        assert terminal.subtype == "error_max_turns"
        assert classify_limit(terminal.result or "") is None

    def test_a_programming_error_still_propagates_to_the_durable_failure_path(self) -> None:
        # The handler is NARROW on purpose: a defect in teatree's own code must keep
        # landing as the driver's durable ``sdk_error`` FAILED-with-traceback, never
        # be laundered into a transport failure the recovery chain would retry.
        async def stream_fn(_messages: object, _info: AgentInfo) -> AsyncIterator[str]:
            await asyncio.sleep(0)
            raise TypeError(_HARNESS_BUG)
            yield ""  # unreachable — the ``yield`` is what makes this an async GENERATOR

        session = PydanticAiHarnessSession(Agent(FunctionModel(stream_function=stream_fn)), model_name=_MODEL)

        with pytest.raises(TypeError, match=_HARNESS_BUG):
            _drive(session)

    def test_a_healthy_turn_is_still_a_success_result(self) -> None:
        session = PydanticAiHarnessSession(Agent(TestModel(custom_output_text="all good")), model_name=_MODEL)

        terminal = _terminal(_drive(session))

        assert terminal.is_error is False
        assert terminal.subtype == "success"
        assert terminal.result == "all good"


class TestTerminalResultCarriesTheRealRunIdentity:
    """``num_turns`` and ``session_id`` describe the run that actually happened."""

    def test_num_turns_counts_the_model_requests_the_run_actually_made(self) -> None:
        session = PydanticAiHarnessSession(Agent(_two_request_model("done")), model_name=_MODEL)

        assert _terminal(_drive(session)).num_turns == 2

    def test_a_refused_first_request_still_records_one_attempted_turn(self) -> None:
        session = PydanticAiHarnessSession(
            Agent(_api_error_model(status_code=429, error_type="rate_limit_error", message="slow down")),
            model_name=_MODEL,
        )

        assert _terminal(_drive(session)).num_turns == 1

    def test_a_drop_mid_stream_records_the_turns_the_run_had_reached(self) -> None:
        # The stream is already in flight, so the count comes from the run itself —
        # not the flat "one attempt" the refused-first-request case falls back to.
        session = PydanticAiHarnessSession(Agent(_dropped_mid_stream_model(after_requests=2)), model_name=_MODEL)

        assert _terminal(_drive(session)).num_turns == 2

    def test_the_session_id_is_non_empty_and_stable_across_the_session_turns(self) -> None:
        session = PydanticAiHarnessSession(Agent(TestModel(custom_output_text="hi")), model_name=_MODEL)

        first = _terminal(_drive(session)).session_id
        second = _terminal(_drive(session)).session_id

        assert first, "an empty session_id leaves resume/audit with no independent handle"
        assert first == second == session.session_id

    def test_each_session_mints_its_own_id(self) -> None:
        one = PydanticAiHarnessSession(Agent(TestModel(custom_output_text="hi")), model_name=_MODEL)
        other = PydanticAiHarnessSession(Agent(TestModel(custom_output_text="hi")), model_name=_MODEL)

        assert one.session_id != other.session_id


class TestRunHeadlessFoldsProviderFailuresIntoTheTaxonomy(TestCase):
    """End-to-end: the driver's own park/fail taxonomy fires on this lane, untouched.

    The driver never special-cases the transport — these prove the session yields the
    same truthful shapes the ``claude_sdk`` lane yields, so ``park_or_rotate_on_limit``
    and ``_record_failure`` reach the right verdict with no driver change at all.
    """

    def setUp(self) -> None:
        self.ticket = Ticket.objects.create()
        self.session = Session.objects.create(ticket=self.ticket, agent_id="agent-1")
        self.task = Task.objects.create(ticket=self.ticket, session=self.session, phase="coding")
        ConfigSetting.objects.set_value("agent_harness", "pydantic_ai")

    def _dispatch(self, harness: PydanticAiHarness) -> TaskAttempt:
        with (
            patch.object(headless_mod, "resolve_harness", return_value=harness),
            patch.object(headless_mod.TaskUsage, "for_task", classmethod(lambda cls, task: TaskUsage(0, 0.0))),
        ):
            return run_headless(self.task, phase="coding", overlay_skill_metadata={})

    def _dispatch_api_error(self, *, status_code: int, error_type: str, message: str) -> TaskAttempt:
        return self._dispatch(
            PydanticAiHarness(model=_api_error_model(status_code=status_code, error_type=error_type, message=message))
        )

    def test_a_rate_limited_run_parks_for_auto_recovery_instead_of_crashing(self) -> None:
        from teatree.core.models import UsageWindowState  # noqa: PLC0415 — test-local

        ConfigSetting.objects.set_value("limit_autorecovery_enabled", value=True)

        attempt = self._dispatch_api_error(
            status_code=429,
            error_type="rate_limit_error",
            message="Number of requests has exceeded your rate limit",
        )

        self.task.refresh_from_db()
        assert self.task.status == Task.Status.PENDING, "PARKED for auto-resume, NOT a terminal FAILED"
        assert attempt.error.startswith("limit_parked: ")
        assert UsageWindowState.objects.active_for_lane(TaskAttempt.Lane.METERED) is not None

    def test_a_rate_limited_run_with_auto_recovery_off_fails_naming_the_cause(self) -> None:
        ConfigSetting.objects.set_value("limit_autorecovery_enabled", value=False)

        attempt = self._dispatch_api_error(
            status_code=429, error_type="rate_limit_error", message="Number of requests has exceeded your rate limit"
        )

        self.task.refresh_from_db()
        assert self.task.status == Task.Status.FAILED
        assert attempt.error.startswith("rate_limit: "), "a cause-marked reason, never an sdk_error traceback"
        assert "Traceback" not in attempt.error

    def test_an_overloaded_server_is_classified_transient_like_a_rate_limit(self) -> None:
        ConfigSetting.objects.set_value("limit_autorecovery_enabled", value=False)

        attempt = self._dispatch_api_error(status_code=529, error_type="overloaded_error", message="Overloaded")

        self.task.refresh_from_db()
        assert self.task.status == Task.Status.FAILED
        assert attempt.error.startswith("rate_limit: ")

    def test_a_credit_exhausted_key_fails_and_is_never_parked(self) -> None:
        # API-credit exhaustion has no timed window, so auto-recovery ON must STILL
        # land a terminal FAILED — nothing re-arms until the operator adds credits.
        ConfigSetting.objects.set_value("limit_autorecovery_enabled", value=True)

        attempt = self._dispatch_api_error(
            status_code=400,
            error_type="invalid_request_error",
            message="Your credit balance is too low to access the Anthropic API.",
        )

        self.task.refresh_from_db()
        assert self.task.status == Task.Status.FAILED, "a $0 balance has no timed reset — never park it"
        assert attempt.error.startswith("api_credit: ")

    def test_a_run_that_hits_its_own_step_cap_fails_and_is_never_parked(self) -> None:
        ConfigSetting.objects.set_value("limit_autorecovery_enabled", value=True)
        harness = PydanticAiHarness(
            model=_two_request_model(),
            config=PydanticAiModelConfig(backend=OpenAICompatibleLaneConfig(request_limit=1)),
        )

        attempt = self._dispatch(harness)

        self.task.refresh_from_db()
        assert self.task.status == Task.Status.FAILED, "the run's OWN cap is a real failure, not a limit park"
        assert "error_max_turns" in attempt.error
        assert "Traceback" not in attempt.error

    def test_a_successful_run_stamps_the_real_turns_and_session_id_on_the_attempt(self) -> None:
        attempt = self._dispatch(PydanticAiHarness(model=_two_request_model()))

        self.task.refresh_from_db()
        assert self.task.status == Task.Status.COMPLETED
        assert attempt.num_turns == 2, "the watchdog's turn ceiling reads this — a hardcoded 1 never bounds a run"
        assert attempt.agent_session_id, "resume/audit needs an independent handle on the run"


def test_turns_made_counts_requests_and_never_returns_zero() -> None:
    # a stream that made 3 requests reports 3
    stream = SimpleNamespace(usage=SimpleNamespace(requests=3))
    assert _turns_made(stream) == 3
    # a stream that recorded 0 requests still counts as one attempted turn
    assert _turns_made(SimpleNamespace(usage=SimpleNamespace(requests=0))) == 1
    # no stream at all (provider refused the first request) is still one attempted turn
    assert _turns_made(None) == 1
