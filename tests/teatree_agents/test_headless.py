import asyncio
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from claude_agent_sdk.types import RateLimitType
from django.test import TestCase, override_settings

import teatree.agents.harness as harness_mod
import teatree.agents.headless as headless_mod
from teatree.agents.harness import ClaudeSdkHarness
from teatree.agents.headless import (
    LoopWatchdog,
    TaskUsage,
    TicketBudget,
    _drive_with_heartbeat,
    _get_resume_session_id,
    _limit_match,
    _parse_result,
    _runtime_child_env,
    _validate_result,
    get_result_json_schema,
    run_headless,
)
from teatree.agents.headless_usage import _safe_float, _safe_int
from teatree.agents.model_tiering import TIER_EFFORT, TIER_MODELS
from teatree.config import AgentRuntime
from teatree.core.models import ConfigSetting, Session, Task, TaskAttempt, Ticket
from teatree.llm.anthropic_limits import LimitCause
from tests.teatree_agents._sdk_fake import assistant_text as _assistant_text
from tests.teatree_agents._sdk_fake import fake_sdk as _fake_sdk
from tests.teatree_agents._sdk_fake import rate_limit_event as _rate_limit_event
from tests.teatree_agents._sdk_fake import result_message as _result_message
from tests.teatree_agents._sdk_fake import success_stream as _success_stream


class TestRunHeadless(TestCase):
    @classmethod
    def setUpTestData(cls) -> None:
        cls.ticket = Ticket.objects.create()

    def test_captures_structured_result(self) -> None:
        result = {
            "summary": "Done",
            "files_modified": [{"path": "src/x.py", "action": "modified"}],
            "tests_passed": 5,
            "tests_failed": 0,
        }
        with _fake_sdk(_success_stream(result)):
            session = Session.objects.create(ticket=self.ticket, agent_id="agent-1")
            task = Task.objects.create(ticket=self.ticket, session=session)

            attempt = run_headless(task, phase="coding", overlay_skill_metadata={})

        task.refresh_from_db()
        assert attempt.exit_code == 0
        assert attempt.result["summary"] == "Done"
        assert attempt.result["tests_passed"] == 5
        assert task.status == Task.Status.COMPLETED

    def test_completed_result_stamps_attempt_usage(self) -> None:
        result = {"summary": "Done", "files_modified": [{"path": "src/x.py", "action": "modified"}]}
        stream = _success_stream(
            result,
            session_id="sess-xyz",
            num_turns=4,
            total_cost_usd=0.5,
            usage={
                "input_tokens": 1000,
                "output_tokens": 200,
                "cache_creation_input_tokens": 50,
                "cache_read_input_tokens": 9000,
            },
            model_usage={"claude-opus-4-8[1m]": {"costUSD": 0.5}},
        )
        with _fake_sdk(stream):
            session = Session.objects.create(ticket=self.ticket)
            task = Task.objects.create(ticket=self.ticket, session=session)
            attempt = run_headless(task, phase="coding", overlay_skill_metadata={})

        attempt.refresh_from_db()
        assert attempt.exit_code == 0
        assert attempt.agent_session_id == "sess-xyz"
        assert attempt.cost_usd == pytest.approx(0.5)
        assert attempt.input_tokens == 1000
        assert attempt.output_tokens == 200
        assert attempt.cache_write_tokens == 50
        assert attempt.cache_read_tokens == 9000
        assert attempt.model == "claude-opus-4-8[1m]"
        assert attempt.num_turns == 4

    def test_estimates_cost_from_price_table_when_sdk_cost_absent(self) -> None:
        result = {"summary": "Done", "files_modified": [{"path": "src/x.py", "action": "modified"}]}
        stream = _success_stream(
            result,
            session_id="sess-noc",
            usage={"input_tokens": 1_000_000, "output_tokens": 0},
            model_usage={"claude-sonnet-4-6": {}},
        )
        with _fake_sdk(stream):
            session = Session.objects.create(ticket=self.ticket)
            task = Task.objects.create(ticket=self.ticket, session=session)
            attempt = run_headless(task, phase="coding", overlay_skill_metadata={})

        attempt.refresh_from_db()
        assert attempt.exit_code == 0
        # 1M input tokens at the Sonnet $3/MTok input rate.
        assert attempt.cost_usd == pytest.approx(3.0)

    def test_fails_when_binary_not_found(self) -> None:
        with patch.object(headless_mod.shutil, "which", return_value=None):
            session = Session.objects.create(ticket=self.ticket)
            task = Task.objects.create(ticket=self.ticket, session=session)

            attempt = run_headless(task, phase="coding", overlay_skill_metadata={})

        task.refresh_from_db()
        assert attempt.exit_code == 1
        assert "not installed" in attempt.error
        assert task.status == Task.Status.FAILED

    def test_summary_fallback_on_no_json_for_phase_without_evidence_requirement(self) -> None:
        with _fake_sdk([_assistant_text("no structured output"), _result_message()]):
            session = Session.objects.create(ticket=self.ticket)
            task = Task.objects.create(ticket=self.ticket, session=session)

            attempt = run_headless(task, phase="scoping", overlay_skill_metadata={})

        task.refresh_from_db()
        assert attempt.exit_code == 0
        assert "no structured output" in attempt.result["summary"]
        assert task.status == Task.Status.COMPLETED

    def test_no_json_fails_phase_with_evidence_requirement(self) -> None:
        with _fake_sdk([_assistant_text("no structured output"), _result_message()]):
            session = Session.objects.create(ticket=self.ticket)
            task = Task.objects.create(ticket=self.ticket, session=session)

            attempt = run_headless(task, phase="coding", overlay_skill_metadata={})

        task.refresh_from_db()
        assert attempt.exit_code == 0
        assert "files_modified" in attempt.error
        assert task.status == Task.Status.FAILED

    def test_fails_when_result_violates_schema(self) -> None:
        bad = {"summary": "OK", "rogue_field": True}
        with _fake_sdk([_assistant_text(json.dumps(bad)), _result_message()]):
            session = Session.objects.create(ticket=self.ticket)
            task = Task.objects.create(ticket=self.ticket, session=session)

            attempt = run_headless(task, phase="coding", overlay_skill_metadata={})

        task.refresh_from_db()
        assert "unexpected keys" in attempt.error
        assert "rogue_field" in attempt.error
        assert task.status == Task.Status.FAILED

    def test_routes_to_interactive_when_needs_user_input(self) -> None:
        result = {
            "summary": "Blocked on design",
            "needs_user_input": True,
            "user_input_reason": "Need design decision",
        }
        with _fake_sdk(_success_stream(result)):
            session = Session.objects.create(ticket=self.ticket, agent_id="agent-1")
            task = Task.objects.create(
                ticket=self.ticket,
                session=session,
                execution_target=Task.ExecutionTarget.HEADLESS,
            )

            attempt = run_headless(task, phase="coding", overlay_skill_metadata={})

        task.refresh_from_db()
        assert attempt.exit_code == 0
        assert attempt.result["needs_user_input"] is True
        assert task.status == Task.Status.COMPLETED
        followup = Task.objects.filter(
            ticket=self.ticket,
            execution_target=Task.ExecutionTarget.INTERACTIVE,
            status=Task.Status.PENDING,
        ).first()
        assert followup is not None
        assert "Need design decision" in followup.execution_reason


class TestRunHeadlessRoutingRefusal(TestCase):
    """The loop-dispatch billing guard refuses a registered phase before any SDK call.

    ``run_headless`` is reached only through ``core.tasks.execute_headless_task`` /
    the ``work-next-sdk`` CLI, which both consult ``loop_dispatch_refusal`` and
    record a ``routing_error`` *before* invoking the runner. This pins that
    seam: a loop-dispatched phase never instantiates ``ClaudeSDKClient``.
    """

    def _make_headless_task(self, *, phase: str) -> Task:
        # Empty overlay → dispatchable (the #1959 poison-pill guard passes), so
        # execution reaches the loop-dispatch billing guard rather than failing
        # earlier on an unknown overlay.
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket, agent_id="agent-1")
        task = Task.objects.create(ticket=ticket, session=session, phase=phase)
        task.route_to_headless(reason="forced headless for the test")
        return task

    def test_registered_phase_refused_before_sdk_client_built(self) -> None:
        from teatree.core.tasks import execute_headless_task  # noqa: PLC0415

        ConfigSetting.objects.set_value("agent_runtime", "interactive")
        task = self._make_headless_task(phase="answering")
        with patch.object(
            harness_mod,
            "ClaudeSDKClient",
            side_effect=AssertionError("SDK client must not be built for a refused phase"),
        ):
            result = execute_headless_task.func(task.pk, "answering")

        task.refresh_from_db()
        assert result["exit_code"] == 1
        assert "answering" in result["routing_error"]
        assert task.status == Task.Status.FAILED


class TestRunHeadlessUsageLimit(TestCase):
    """A usage/weekly-limit terminal result is surfaced as a clear limit failure."""

    @classmethod
    def setUpTestData(cls) -> None:
        cls.ticket = Ticket.objects.create()

    def test_weekly_limit_error_recorded_as_usage_limit_not_generic(self) -> None:
        # is_error result whose text names a weekly limit must NOT be a silent
        # success and must NOT be a generic crash — it is a clear limit signal.
        limit_message = _result_message(
            subtype="error_during_execution",
            is_error=True,
            result="You've hit your weekly limit. It resets on Jun 14.",
        )
        with _fake_sdk([_assistant_text("starting"), limit_message]):
            session = Session.objects.create(ticket=self.ticket)
            task = Task.objects.create(ticket=self.ticket, session=session)
            attempt = run_headless(task, phase="coding", overlay_skill_metadata={})

        task.refresh_from_db()
        assert attempt.exit_code != 0
        assert "subscription_weekly" in attempt.error
        assert "weekly limit" in attempt.error
        assert "credit" not in attempt.error.casefold()
        assert task.status == Task.Status.FAILED

    def test_usage_limit_phrase_recorded(self) -> None:
        limit_message = _result_message(
            subtype="error_during_execution",
            is_error=True,
            result="Claude usage limit reached for this session.",
        )
        with _fake_sdk([limit_message]):
            session = Session.objects.create(ticket=self.ticket)
            task = Task.objects.create(ticket=self.ticket, session=session)
            attempt = run_headless(task, phase="coding", overlay_skill_metadata={})

        task.refresh_from_db()
        assert "subscription_session" in attempt.error
        assert "usage limit" in attempt.error
        assert task.status == Task.Status.FAILED

    def test_credit_balance_too_low_is_api_credit_not_subscription(self) -> None:
        # The billed ANTHROPIC_API_KEY at $0 surfaces HTTP 400 "credit balance
        # is too low" — an API-CREDIT exhaustion, NOT a subscription quota. The
        # operator fix is to add credits at console.anthropic.com.
        credit_message = _result_message(
            subtype="error_during_execution",
            is_error=True,
            api_error_status=400,
            result="Your credit balance is too low to access the Anthropic API.",
        )
        with _fake_sdk([_assistant_text("starting"), credit_message]):
            session = Session.objects.create(ticket=self.ticket)
            task = Task.objects.create(ticket=self.ticket, session=session)
            attempt = run_headless(task, phase="coding", overlay_skill_metadata={})

        task.refresh_from_db()
        assert attempt.exit_code != 0
        assert "api_credit" in attempt.error
        assert "console.anthropic.com" in attempt.error
        assert "subscription" not in attempt.error.casefold()
        assert "weekly" not in attempt.error.casefold()
        assert task.status == Task.Status.FAILED

    def test_out_of_credits_is_api_credit_not_subscription(self) -> None:
        # "out of credits" is an API-CREDIT signal — it must NOT be laundered into
        # the generic "subscription quota exhausted" message (the original bug).
        credit_message = _result_message(
            subtype="error_during_execution",
            is_error=True,
            result="The request failed: you are out of credits.",
        )
        with _fake_sdk([credit_message]):
            session = Session.objects.create(ticket=self.ticket)
            task = Task.objects.create(ticket=self.ticket, session=session)
            attempt = run_headless(task, phase="coding", overlay_skill_metadata={})

        task.refresh_from_db()
        assert "api_credit" in attempt.error
        assert "console.anthropic.com" in attempt.error
        assert "subscription" not in attempt.error.casefold()
        assert task.status == Task.Status.FAILED

    def test_session_limit_classified_distinctly_from_weekly(self) -> None:
        # The ~5h rolling SESSION limit resets same-day — it must report a session
        # cause, never the weekly one and never a credit one.
        session_message = _result_message(
            subtype="error_during_execution",
            is_error=True,
            result="Claude 5-hour limit reached for this session.",
        )
        with _fake_sdk([session_message]):
            session = Session.objects.create(ticket=self.ticket)
            task = Task.objects.create(ticket=self.ticket, session=session)
            attempt = run_headless(task, phase="coding", overlay_skill_metadata={})

        task.refresh_from_db()
        assert "subscription_session" in attempt.error
        assert "weekly" not in attempt.error.casefold()
        assert "credit" not in attempt.error.casefold()
        assert task.status == Task.Status.FAILED

    def test_non_error_result_mentioning_limit_is_not_a_limit_failure(self) -> None:
        # A healthy result whose prose merely discusses a usage limit must not
        # be flagged — the classifier keys on ``is_error``.
        result = {
            "summary": "Added handling for the weekly usage limit edge case",
            "files_modified": [{"path": "src/x.py", "action": "modified"}],
        }
        with _fake_sdk(_success_stream(result)):
            session = Session.objects.create(ticket=self.ticket)
            task = Task.objects.create(ticket=self.ticket, session=session)
            attempt = run_headless(task, phase="coding", overlay_skill_metadata={})

        task.refresh_from_db()
        assert attempt.exit_code == 0
        assert "usage_limit" not in (attempt.error or "")
        assert task.status == Task.Status.COMPLETED

    def test_non_limit_error_result_is_a_failure_not_a_completion(self) -> None:
        # A ``ResultMessage(is_error=True)`` whose text is NOT a usage-limit
        # message is a genuine FAILED run (#1764 class) — it must record a failed
        # attempt and leave the task FAILED, never be laundered into a completion
        # that advances the ticket FSM over a failed run.
        error_message = _result_message(
            subtype="error_during_execution",
            is_error=True,
            result="The agent crashed with an unhandled exception.",
        )
        with _fake_sdk([_assistant_text('{"summary": "partial"}'), error_message]):
            session = Session.objects.create(ticket=self.ticket)
            task = Task.objects.create(ticket=self.ticket, session=session)
            attempt = run_headless(task, phase="coding", overlay_skill_metadata={})

        task.refresh_from_db()
        assert attempt.exit_code != 0
        assert task.status == Task.Status.FAILED

    def test_missing_terminal_result_is_a_failure_not_a_completion(self) -> None:
        # No terminal ``ResultMessage`` at all (the stream ended before the CLI
        # emitted one) is also a failed run, not a silent completion.
        with _fake_sdk([_assistant_text('{"summary": "partial"}')]):
            session = Session.objects.create(ticket=self.ticket)
            task = Task.objects.create(ticket=self.ticket, session=session)
            attempt = run_headless(task, phase="coding", overlay_skill_metadata={})

        task.refresh_from_db()
        assert attempt.exit_code != 0
        assert task.status == Task.Status.FAILED


class TestRunHeadlessTypedRateLimitWindow(TestCase):
    """A rejected ``RateLimitEvent`` classifies the failure from its TYPED window.

    The per-model 7-day windows (``seven_day_opus`` / ``seven_day_sonnet``)
    matched NO phrase signature, so a weekly cap would land as a generic crash.
    With the typed field wired through ``_collect`` they classify WEEKLY — and the
    result text here names NO limit phrase, so the ONLY path to a weekly verdict is
    the typed window (anti-vacuous: the test fails without the typed wiring).
    """

    @classmethod
    def setUpTestData(cls) -> None:
        cls.ticket = Ticket.objects.create()

    def _run_with_window(self, window: RateLimitType) -> TaskAttempt:
        event = _rate_limit_event(window)
        terminal = _result_message(
            subtype="error_during_execution", is_error=True, result="The run could not complete."
        )
        with _fake_sdk([_assistant_text("working"), event, terminal]):
            session = Session.objects.create(ticket=self.ticket, agent_id="agent-typed")
            task = Task.objects.create(ticket=self.ticket, session=session)
            attempt = run_headless(task, phase="coding", overlay_skill_metadata={})
        task.refresh_from_db()
        assert task.status == Task.Status.FAILED
        return attempt

    def test_seven_day_opus_window_recorded_as_subscription_weekly(self) -> None:
        attempt = self._run_with_window("seven_day_opus")
        assert "subscription_weekly" in attempt.error
        assert "seven_day_opus" in attempt.error
        assert "credit" not in attempt.error.casefold()

    def test_seven_day_sonnet_window_recorded_as_subscription_weekly(self) -> None:
        attempt = self._run_with_window("seven_day_sonnet")
        assert "subscription_weekly" in attempt.error
        assert "seven_day_sonnet" in attempt.error


def test_limit_match_prefers_the_typed_window_over_the_result_text() -> None:
    # The result text names no limit phrase; the rejected typed window is the only
    # signal, so _limit_match classifies WEEKLY from it (the fallback would be None).
    msg = _result_message(is_error=True, result="The run could not complete.")
    info = _rate_limit_event("seven_day_sonnet").rate_limit_info
    match = _limit_match(msg, info)
    assert match is not None
    assert match.cause is LimitCause.SUBSCRIPTION_WEEKLY
    assert match.phrase == "seven_day_sonnet"


def test_limit_match_typed_window_ignored_when_result_is_not_an_error() -> None:
    # The is_error gate still governs: a healthy run is never failed on a stray
    # rejected window event.
    msg = _result_message(is_error=False, result="done")
    info = _rate_limit_event("seven_day_opus").rate_limit_info
    assert _limit_match(msg, info) is None


def test_limit_match_classifies_weekly_distinctly() -> None:
    msg = _result_message(is_error=True, result="You've hit your weekly limit.")
    match = _limit_match(msg)
    assert match is not None
    assert match.cause is LimitCause.SUBSCRIPTION_WEEKLY
    assert match.phrase == "weekly limit"


def test_limit_match_classifies_credit_as_api_credit_not_subscription() -> None:
    msg = _result_message(is_error=True, result="Your credit balance is too low.")
    match = _limit_match(msg)
    assert match is not None
    assert match.cause is LimitCause.API_CREDIT
    assert "console.anthropic.com" in match.remediation
    assert "subscription" not in match.as_reason().casefold()


def test_limit_match_ignores_non_error_results() -> None:
    msg = _result_message(is_error=False, result="weekly limit discussed in passing")
    assert _limit_match(msg) is None


def test_limit_match_ignores_unrelated_error() -> None:
    msg = _result_message(is_error=True, result="some other failure")
    assert _limit_match(msg) is None


def test_limit_match_handles_missing_message() -> None:
    assert _limit_match(None) is None


# --- Pure function tests (no DB) ---


def test_validate_result_accepts_valid_keys() -> None:
    assert _validate_result({"summary": "OK", "tests_passed": 5}) == ""


def test_validate_result_rejects_unknown_keys() -> None:
    error = _validate_result({"summary": "OK", "bogus": True})
    assert "bogus" in error


def test_parse_result_extracts_last_json_line() -> None:
    stdout = "Loading skills...\nRunning task...\n" + json.dumps({"summary": "OK"}) + "\n"
    assert _parse_result(stdout) == {"summary": "OK"}


def test_parse_result_returns_empty_dict_for_no_json() -> None:
    assert _parse_result("no json here\n") == {}


def test_parse_result_skips_malformed_json() -> None:
    assert _parse_result("{bad json\n") == {}


def test_get_result_json_schema_returns_valid_schema() -> None:
    schema = get_result_json_schema()
    assert schema["type"] == "object"
    properties = schema["properties"]
    assert isinstance(properties, dict)
    assert "summary" in properties


# --- Session resume tests ---

FAKE_SESSION_UUID = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"


class TestGetResumeSessionId(TestCase):
    @classmethod
    def setUpTestData(cls) -> None:
        cls.ticket = Ticket.objects.create()

    def test_from_parent_attempt(self) -> None:
        """Parent task's attempt has an agent_session_id — headless should resume it."""
        parent_session = Session.objects.create(ticket=self.ticket, agent_id="interactive-followup")
        parent_task = Task.objects.create(ticket=self.ticket, session=parent_session)
        TaskAttempt.objects.create(task=parent_task, agent_session_id=FAKE_SESSION_UUID)

        child_session = Session.objects.create(ticket=self.ticket, agent_id="coding")
        child_task = Task.objects.create(ticket=self.ticket, session=child_session, parent_task=parent_task)

        assert _get_resume_session_id(child_task) == FAKE_SESSION_UUID

    def test_from_parent_session_agent_id(self) -> None:
        """Parent task's session.agent_id is a UUID — headless should resume it."""
        parent_session = Session.objects.create(ticket=self.ticket, agent_id=FAKE_SESSION_UUID)
        parent_task = Task.objects.create(ticket=self.ticket, session=parent_session)

        child_session = Session.objects.create(ticket=self.ticket, agent_id="review")
        child_task = Task.objects.create(ticket=self.ticket, session=child_session, parent_task=parent_task)

        assert _get_resume_session_id(child_task) == FAKE_SESSION_UUID

    def test_returns_empty_without_parent(self) -> None:
        """No parent task — nothing to resume."""
        session = Session.objects.create(ticket=self.ticket)
        task = Task.objects.create(ticket=self.ticket, session=session)

        assert _get_resume_session_id(task) == ""

    def test_skips_non_uuid_agent_ids(self) -> None:
        """Parent exists but agent_id is not a UUID — don't resume."""
        parent_session = Session.objects.create(ticket=self.ticket, agent_id="not-a-uuid")
        parent_task = Task.objects.create(ticket=self.ticket, session=parent_session)

        child_session = Session.objects.create(ticket=self.ticket, agent_id="coding")
        child_task = Task.objects.create(ticket=self.ticket, session=child_session, parent_task=parent_task)

        assert _get_resume_session_id(child_task) == ""


class TestBuildOptionsFailLoudGate(TestCase):
    """A headless run must HARD-deny AskUserQuestion (it is attended-only).

    There is no human at the harness in the SDK/headless lane, so the
    structured ``needs_user_input`` return is the only sanctioned ask-path.
    AskUserQuestion is disallowed on the SDK options so the agent cannot
    silently stall on an unrendered question.
    """

    @classmethod
    def setUpTestData(cls) -> None:
        cls.ticket = Ticket.objects.create()

    def test_askuserquestion_is_disallowed(self) -> None:
        session = Session.objects.create(ticket=self.ticket, agent_id="agent-1")
        task = Task.objects.create(ticket=self.ticket, session=session)
        options = headless_mod._build_options(task, "ctx", phase="coding", skills=[])
        assert "AskUserQuestion" in (options.disallowed_tools or [])


class TestRunHeadlessResumesParentSession(TestCase):
    def test_resume_session_id_passed_to_sdk_options(self) -> None:
        """A child of a session-carrying parent gets ``resume=<id>`` on the SDK options."""
        result = {"summary": "Continued work", "files_modified": [{"path": "src/x.py", "action": "modified"}]}
        with _fake_sdk(_success_stream(result)) as client_cls:
            ticket = Ticket.objects.create()
            parent_session = Session.objects.create(ticket=ticket, agent_id=FAKE_SESSION_UUID)
            parent_task = Task.objects.create(ticket=ticket, session=parent_session)

            child_session = Session.objects.create(ticket=ticket, agent_id="coding")
            child_task = Task.objects.create(ticket=ticket, session=child_session, parent_task=parent_task)

            run_headless(child_task, phase="coding", overlay_skill_metadata={})

        assert client_cls.last_options is not None
        assert client_cls.last_options.resume == FAKE_SESSION_UUID

    def test_no_parent_session_leaves_resume_unset(self) -> None:
        result = {"summary": "Fresh", "files_modified": [{"path": "src/x.py", "action": "modified"}]}
        with _fake_sdk(_success_stream(result)) as client_cls:
            ticket = Ticket.objects.create()
            session = Session.objects.create(ticket=ticket, agent_id="coding")
            task = Task.objects.create(ticket=ticket, session=session)

            run_headless(task, phase="coding", overlay_skill_metadata={})

        assert client_cls.last_options is not None
        assert client_cls.last_options.resume is None


class TestResolveTaskCwd(TestCase):
    def test_worktree_with_real_repo_path_is_returned(self) -> None:
        import tempfile  # noqa: PLC0415

        from teatree.agents.headless import _resolve_task_cwd  # noqa: PLC0415
        from teatree.core.models.worktree import Worktree  # noqa: PLC0415

        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket)
        task = Task.objects.create(ticket=ticket, session=session)
        with tempfile.TemporaryDirectory() as repo_dir:
            Worktree.objects.create(ticket=ticket, repo_path=repo_dir)
            assert _resolve_task_cwd(task) == repo_dir

    def test_worktree_with_missing_repo_path_returns_none(self) -> None:
        from teatree.agents.headless import _resolve_task_cwd  # noqa: PLC0415
        from teatree.core.models.worktree import Worktree  # noqa: PLC0415

        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket)
        task = Task.objects.create(ticket=ticket, session=session)
        Worktree.objects.create(ticket=ticket, repo_path="/nonexistent/repo/path")
        assert _resolve_task_cwd(task) is None


def test_collect_ignores_messages_that_are_neither_assistant_nor_result() -> None:
    from teatree.agents.headless import _collect  # noqa: PLC0415

    class _Other:
        pass

    messages = [_Other(), _assistant_text("hi"), _result_message(session_id="s1"), _Other()]

    class _Client:
        async def query(self, _prompt: str) -> None:
            return None

        async def receive_response(self) -> Any:
            for message in messages:
                yield message

    outcome = asyncio.run(_collect(_Client(), "p"))
    assert outcome.agent_text == "hi"
    assert outcome.result_message is not None
    assert outcome.result_message.session_id == "s1"


def test_attempt_usage_for_missing_message_is_empty() -> None:
    from teatree.agents.attempt_recorder import AttemptUsage  # noqa: PLC0415
    from teatree.agents.headless_usage import _attempt_usage  # noqa: PLC0415

    assert _attempt_usage(None) == AttemptUsage()


# --- _safe_int / _safe_float ---


def test_safe_int_converts_string() -> None:
    assert _safe_int("42") == 42
    assert _safe_int("3.7") == 3  # truncates


def test_safe_int_returns_none_for_invalid() -> None:
    assert _safe_int(None) is None
    assert _safe_int("abc") is None


def test_safe_float_converts_string() -> None:
    assert _safe_float("0.042") == pytest.approx(0.042)


def test_safe_float_returns_none_for_invalid() -> None:
    assert _safe_float(None) is None
    assert _safe_float("abc") is None


# --- Stuck-loop / cost-spike watchdog (#882) ---


class TestLoopWatchdog(TestCase):
    """Watchdog evaluation against real Task / TaskAttempt rows."""

    def setUp(self) -> None:
        self.ticket = Ticket.objects.create()
        self.session = Session.objects.create(ticket=self.ticket)
        self.task = Task.objects.create(ticket=self.ticket, session=self.session)

    def test_disabled_watchdog_never_terminates(self) -> None:
        watchdog = LoopWatchdog(max_runtime_seconds=0, max_turns=0, max_cost_usd=0.0)
        assert watchdog.breach_reason(self.task, elapsed_seconds=99999) is None

    def test_runtime_ceiling_breach(self) -> None:
        watchdog = LoopWatchdog(max_runtime_seconds=30, max_turns=0, max_cost_usd=0.0)
        assert watchdog.breach_reason(self.task, elapsed_seconds=10) is None
        reason = watchdog.breach_reason(self.task, elapsed_seconds=31)
        assert reason is not None
        assert "runtime" in reason
        assert "31" in reason

    def test_turn_count_breach_from_accumulated_attempts(self) -> None:
        TaskAttempt.objects.create(task=self.task, num_turns=120)
        TaskAttempt.objects.create(task=self.task, num_turns=140)
        watchdog = LoopWatchdog(max_runtime_seconds=0, max_turns=200, max_cost_usd=0.0)
        reason = watchdog.breach_reason(self.task, elapsed_seconds=5)
        assert reason is not None
        assert "turns" in reason
        assert "260" in reason

    def test_cost_breach_from_accumulated_attempts(self) -> None:
        TaskAttempt.objects.create(task=self.task, cost_usd=4.0)
        TaskAttempt.objects.create(task=self.task, cost_usd=3.5)
        watchdog = LoopWatchdog(max_runtime_seconds=0, max_turns=0, max_cost_usd=5.0)
        reason = watchdog.breach_reason(self.task, elapsed_seconds=5)
        assert reason is not None
        assert "cost" in reason
        assert "7.5" in reason

    def test_under_all_thresholds_no_breach(self) -> None:
        TaskAttempt.objects.create(task=self.task, num_turns=10, cost_usd=0.5)
        watchdog = LoopWatchdog(max_runtime_seconds=600, max_turns=200, max_cost_usd=5.0)
        assert watchdog.breach_reason(self.task, elapsed_seconds=60) is None

    def test_from_settings_reads_defaults(self) -> None:
        with override_settings(
            TEATREE_LOOP_WATCHDOG={"max_runtime_seconds": 42, "max_turns": 7, "max_cost_usd": 1.5},
        ):
            watchdog = LoopWatchdog.from_settings()
        assert watchdog.max_runtime_seconds == 42
        assert watchdog.max_turns == 7
        assert watchdog.max_cost_usd == pytest.approx(1.5)

    def test_from_settings_falls_back_to_conservative_default(self) -> None:
        with override_settings():
            from django.conf import settings  # noqa: PLC0415

            if hasattr(settings, "TEATREE_LOOP_WATCHDOG"):
                del settings.TEATREE_LOOP_WATCHDOG
            watchdog = LoopWatchdog.from_settings()
        assert watchdog.max_runtime_seconds > 0
        assert watchdog.max_turns == 0
        assert watchdog.max_cost_usd == pytest.approx(0.0)


class TestDriveWithHeartbeat(TestCase):
    """The SDK driver renews the lease and honours the watchdog (#882, #997)."""

    def setUp(self) -> None:
        self.ticket = Ticket.objects.create()
        self.session = Session.objects.create(ticket=self.ticket)
        self.task = Task.objects.create(ticket=self.ticket, session=self.session)
        # Threaded ORM access under TestCase's wrapping transaction is a
        # harness artifact, not production behaviour — stub the lease renewal.
        self.task.renew_lease = lambda **_kw: None

    def _options(self) -> Any:
        return headless_mod._build_options(self.task, "ctx", phase="coding", skills=[])

    def test_collects_text_and_terminal_result(self) -> None:
        messages = [_assistant_text("hello"), _result_message(session_id="s1", num_turns=2)]
        watchdog = LoopWatchdog(max_runtime_seconds=0, max_turns=0, max_cost_usd=0.0)
        with _fake_sdk(messages):
            outcome = asyncio.run(
                _drive_with_heartbeat(self.task, "p", self._options(), ClaudeSdkHarness(), watchdog=watchdog)
            )

        assert outcome.stuck_reason is None
        assert outcome.agent_text == "hello"
        assert outcome.result_message is not None
        assert outcome.result_message.session_id == "s1"

    def test_renews_lease_during_run(self) -> None:
        renew_count = 0

        def counting_renew(**_kwargs: object) -> None:
            nonlocal renew_count
            renew_count += 1

        self.task.renew_lease = counting_renew
        messages = [_assistant_text("hello"), _result_message()]
        watchdog = LoopWatchdog(max_runtime_seconds=0, max_turns=0, max_cost_usd=0.0)
        with _fake_sdk(messages, delay=0.05), patch.object(headless_mod, "_HEARTBEAT_INTERVAL", 0.02):
            outcome = asyncio.run(
                _drive_with_heartbeat(self.task, "p", self._options(), ClaudeSdkHarness(), watchdog=watchdog)
            )

        assert outcome.stuck_reason is None
        assert renew_count >= 1

    def test_runtime_ceiling_interrupts_and_reports_stuck(self) -> None:
        # A stream that never terminates within the runtime ceiling is
        # interrupted and reported as a runtime breach.
        messages = [_assistant_text("step") for _ in range(1000)]
        watchdog = LoopWatchdog(max_runtime_seconds=0.2, max_turns=0, max_cost_usd=0.0)
        start = time.monotonic()
        with _fake_sdk(messages, delay=0.05), patch.object(headless_mod, "_HEARTBEAT_INTERVAL", 0.02):
            outcome = asyncio.run(
                _drive_with_heartbeat(self.task, "p", self._options(), ClaudeSdkHarness(), watchdog=watchdog)
            )
        elapsed = time.monotonic() - start

        assert elapsed < 10
        assert outcome.stuck_reason is not None
        assert "runtime" in outcome.stuck_reason

    def test_survives_renew_lease_failure(self) -> None:
        def failing_renew(**_kwargs: object) -> None:
            msg = "DB connection lost"
            raise RuntimeError(msg)

        self.task.renew_lease = failing_renew
        messages = [_assistant_text("ok"), _result_message()]
        watchdog = LoopWatchdog(max_runtime_seconds=0, max_turns=0, max_cost_usd=0.0)
        with (
            _fake_sdk(messages, delay=0.05),
            patch.object(headless_mod, "_HEARTBEAT_INTERVAL", 0.02),
            patch.object(headless_mod, "logger") as mock_logger,
        ):
            outcome = asyncio.run(
                _drive_with_heartbeat(self.task, "p", self._options(), ClaudeSdkHarness(), watchdog=watchdog)
            )

        assert outcome.stuck_reason is None
        assert mock_logger.warning.call_count >= 1


class TestUsageSampleClosesWorkerConnection(TestCase):
    """The pre-run usage sample must close its worker-thread DB connection.

    ``_drive_with_heartbeat`` samples ``TaskUsage.for_task`` in an
    ``asyncio.to_thread`` worker. That worker opens its OWN Django connection
    (a thread-local), which — pre-fix — was never closed, so a file-backed
    sqlite connection (the production config) surfaced as a
    ``ResourceWarning: unclosed database`` when the thread was GC'd — the
    order-dependent ``test_tasks.py`` flake. The in-memory test DB cannot
    reproduce the GC warning (its connection is deliberately persistent), so
    the guard asserts the fix's contract directly: the sampler closes the
    thread-local connection it used. RED before the fix: ``close`` is never
    called.
    """

    def test_sampler_closes_its_thread_connection(self) -> None:
        from django.db import connection  # noqa: PLC0415

        from teatree.agents.headless import _sample_usage_closing_connection  # noqa: PLC0415

        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket, agent_id="agent-1")
        task = Task.objects.create(ticket=ticket, session=session)
        TaskAttempt.objects.create(task=task, num_turns=3, cost_usd=1.0)

        with patch.object(connection, "close", wraps=connection.close) as close_spy:
            usage = _sample_usage_closing_connection(task)

        assert usage.turns == 3
        close_spy.assert_called_once()


class TestRunHeadlessRecordsStuckLoop(TestCase):
    """run_headless records a stuck_loop TaskAttempt failure when the watchdog fires."""

    def test_records_stuck_loop_failure_with_observed_deltas(self) -> None:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket, agent_id="agent-1")
        task = Task.objects.create(ticket=ticket, session=session)
        TaskAttempt.objects.create(task=task, num_turns=500)
        task.renew_lease = lambda **_kw: None

        watchdog = LoopWatchdog(max_runtime_seconds=0, max_turns=200, max_cost_usd=0.0)
        # A long never-terminating stream: the watchdog must interrupt it on the
        # turns breach (500 > 200) at the first heartbeat tick, NOT drain the
        # whole list. The interrupt cuts the stream short, so the run finishes in
        # ~one heartbeat — proving the watchdog stops a runaway rather than
        # waiting it out.
        messages = [_assistant_text("step") for _ in range(1000)]
        start = time.monotonic()
        with (
            _fake_sdk(messages, delay=0.05, task_usage=TaskUsage(turns=500, cost_usd=0.0)),
            patch.object(headless_mod.LoopWatchdog, "from_settings", return_value=watchdog),
            patch.object(headless_mod, "_HEARTBEAT_INTERVAL", 0.02),
        ):
            attempt = run_headless(task, phase="coding", overlay_skill_metadata={})
        elapsed = time.monotonic() - start

        task.refresh_from_db()
        assert attempt.exit_code != 0
        assert "stuck_loop" in attempt.error
        assert "turns" in attempt.error
        assert "500" in attempt.error
        assert task.status == Task.Status.FAILED
        # The interrupt cut the 1000-message stream short instead of streaming
        # all 50s of it (1000 * 0.05s delay) — a generous bound that still fails
        # loudly if the watchdog stops interrupting the stream.
        assert elapsed < 10, f"watchdog did not cut the runaway stream short: {elapsed:.1f}s"


class TestTicketBudget(TestCase):
    """Per-ticket cumulative cost-cap consumer (#885 / #398-4)."""

    def setUp(self) -> None:
        self.ticket = Ticket.objects.create()
        self.session = Session.objects.create(ticket=self.ticket)
        self.task = Task.objects.create(ticket=self.ticket, session=self.session)

    def test_disabled_budget_never_refuses(self) -> None:
        TaskAttempt.objects.create(task=self.task, cost_usd=9999.0)
        budget = TicketBudget(max_cost_usd=0.0)
        assert budget.breach_reason(self.ticket) is None

    def test_under_cap_does_not_refuse(self) -> None:
        TaskAttempt.objects.create(task=self.task, cost_usd=2.0)
        budget = TicketBudget(max_cost_usd=5.0)
        assert budget.breach_reason(self.ticket) is None

    def test_over_cap_refuses_with_observed_total(self) -> None:
        TaskAttempt.objects.create(task=self.task, cost_usd=4.0)
        TaskAttempt.objects.create(task=self.task, cost_usd=3.5)
        budget = TicketBudget(max_cost_usd=5.0)
        reason = budget.breach_reason(self.ticket)
        assert reason is not None
        assert "budget" in reason
        assert "7.50" in reason
        assert "5.00" in reason

    def test_sums_across_all_tasks_of_the_ticket(self) -> None:
        other_session = Session.objects.create(ticket=self.ticket)
        other_task = Task.objects.create(ticket=self.ticket, session=other_session)
        TaskAttempt.objects.create(task=self.task, cost_usd=3.0)
        TaskAttempt.objects.create(task=other_task, cost_usd=3.0)
        budget = TicketBudget(max_cost_usd=5.0)
        reason = budget.breach_reason(self.ticket)
        assert reason is not None
        assert "6.00" in reason

    def test_ignores_other_tickets(self) -> None:
        other_ticket = Ticket.objects.create()
        other_session = Session.objects.create(ticket=other_ticket)
        other_task = Task.objects.create(ticket=other_ticket, session=other_session)
        TaskAttempt.objects.create(task=other_task, cost_usd=99.0)
        TaskAttempt.objects.create(task=self.task, cost_usd=1.0)
        budget = TicketBudget(max_cost_usd=5.0)
        assert budget.breach_reason(self.ticket) is None

    def test_from_settings_reads_configured_cap(self) -> None:
        with override_settings(TEATREE_TICKET_BUDGET={"max_cost_usd": 12.5}):
            budget = TicketBudget.from_settings()
        assert budget.max_cost_usd == pytest.approx(12.5)

    def test_from_settings_defaults_to_disabled(self) -> None:
        with override_settings():
            from django.conf import settings  # noqa: PLC0415

            if hasattr(settings, "TEATREE_TICKET_BUDGET"):
                del settings.TEATREE_TICKET_BUDGET
            budget = TicketBudget.from_settings()
        assert budget.max_cost_usd == pytest.approx(0.0)


class TestRunHeadlessRefusesOverBudgetTicket(TestCase):
    """run_headless refuses dispatch and records a budget_exceeded failure."""

    def setUp(self) -> None:
        self.ticket = Ticket.objects.create()
        self.session = Session.objects.create(ticket=self.ticket, agent_id="agent-1")

    def test_over_budget_ticket_is_not_dispatched(self) -> None:
        spent = Task.objects.create(ticket=self.ticket, session=self.session)
        TaskAttempt.objects.create(task=spent, cost_usd=8.0)
        task = Task.objects.create(ticket=self.ticket, session=self.session)

        with (
            override_settings(TEATREE_TICKET_BUDGET={"max_cost_usd": 5.0}),
            patch.object(headless_mod.shutil, "which", return_value="/usr/bin/claude"),
            patch.object(
                harness_mod,
                "ClaudeSDKClient",
                side_effect=AssertionError("SDK client must not be built over budget"),
            ),
        ):
            attempt = run_headless(task, phase="coding", overlay_skill_metadata={})

        task.refresh_from_db()
        assert attempt.exit_code != 0
        assert "budget_exceeded" in attempt.error
        assert "8.00" in attempt.error
        assert task.status == Task.Status.FAILED

    def test_under_budget_ticket_proceeds(self) -> None:
        spent = Task.objects.create(ticket=self.ticket, session=self.session)
        TaskAttempt.objects.create(task=spent, cost_usd=1.0)
        task = Task.objects.create(ticket=self.ticket, session=self.session)
        result = {"summary": "Done", "files_modified": [{"path": "src/x.py", "action": "modified"}]}

        with (
            override_settings(TEATREE_TICKET_BUDGET={"max_cost_usd": 5.0}),
            _fake_sdk(_success_stream(result)),
        ):
            attempt = run_headless(task, phase="coding", overlay_skill_metadata={})

        task.refresh_from_db()
        assert attempt.exit_code == 0
        assert task.status == Task.Status.COMPLETED


# --- SDK options / model tiering (#880) ---


class TestBuildOptions(TestCase):
    """``_build_options`` carries the model, permission mode, and resume id."""

    @classmethod
    def setUpTestData(cls) -> None:
        cls.ticket = Ticket.objects.create()

    def _options_for_phase(self, phase: str) -> Any:
        session = Session.objects.create(ticket=self.ticket)
        task = Task.objects.create(ticket=self.ticket, session=session)
        absent = Path(tempfile.mkdtemp()) / "nope.toml"
        with (
            patch("teatree.agents.model_tiering.CONFIG_PATH", absent),
            patch("teatree.config_agent.CONFIG_PATH", absent),
        ):
            return headless_mod._build_options(task, "ctx", phase=phase, skills=[])

    def test_retrospecting_runs_on_frontier(self) -> None:
        options = self._options_for_phase("retrospecting")
        assert options.model == TIER_MODELS["frontier"]

    def test_reviewing_runs_on_frontier(self) -> None:
        options = self._options_for_phase("reviewing")
        assert options.model == TIER_MODELS["frontier"]

    def test_coding_runs_on_frontier(self) -> None:
        # The redesign maps coding to the frontier tier (was inherit/None before).
        options = self._options_for_phase("coding")
        assert options.model == TIER_MODELS["frontier"]

    def test_requesting_review_runs_on_cheap(self) -> None:
        options = self._options_for_phase("requesting_review")
        assert options.model == TIER_MODELS["cheap"]

    def test_testing_runs_on_balanced(self) -> None:
        options = self._options_for_phase("testing")
        assert options.model == TIER_MODELS["balanced"]

    def test_permission_mode_bypasses_prompts(self) -> None:
        options = self._options_for_phase("coding")
        assert options.permission_mode == "bypassPermissions"

    def test_frontier_phase_pins_adaptive_thinking_and_xhigh_effort(self) -> None:
        # Opus 4.8 omits thinking by default; a frontier reasoning phase pins
        # adaptive thinking explicitly AND the frontier-tier effort (xhigh).
        options = self._options_for_phase("coding")
        assert options.thinking == {"type": "adaptive"}
        assert options.effort == TIER_EFFORT["frontier"]

    def test_balanced_phase_pins_adaptive_thinking_and_high_effort(self) -> None:
        # A balanced (Sonnet) phase supports thinking and carries the balanced-tier
        # effort (high).
        options = self._options_for_phase("testing")
        assert options.thinking == {"type": "adaptive"}
        assert options.effort == TIER_EFFORT["balanced"]

    def test_cheap_phase_leaves_thinking_and_effort_unset(self) -> None:
        # requesting_review resolves to the Haiku tier, which rejects both levers —
        # neither thinking nor effort is pinned, so the SDK defaults apply.
        options = self._options_for_phase("requesting_review")
        assert options.thinking is None
        assert options.effort is None

    def test_system_prompt_appends_claude_code_preset(self) -> None:
        # A plain-str system_prompt REPLACES the claude_code preset (the SDK maps
        # it to --system-prompt); a headless run must APPEND to the preset (the
        # deleted ``claude -p`` path used --append-system-prompt), or every
        # production run loses the Claude Code preset.
        session = Session.objects.create(ticket=self.ticket)
        task = Task.objects.create(ticket=self.ticket, session=session)
        options = headless_mod._build_options(task, "my context", phase="coding", skills=[])
        assert options.system_prompt == {
            "type": "preset",
            "preset": "claude_code",
            "append": "my context",
        }


class TestBuildOptionsSpawnModelFloor(TestCase):
    """``_build_options`` routes the SDK model through ``resolve_spawn_model``.

    The model is the most-capable-wins floor merge of the per-phase tier and the
    per-skill MODEL floors of the loaded skills. The per-skill floor is MODEL only:
    effort is per-abstract-TIER (via ``resolve_spawn_effort``), and the separate
    ``session_effort`` interactive pin never leaks into a headless SDK run.
    """

    @classmethod
    def setUpTestData(cls) -> None:
        cls.ticket = Ticket.objects.create()

    def _options(self, phase: str, *, skills: list[str], config_body: str) -> Any:
        cfg = Path(tempfile.mkdtemp()) / ".teatree.toml"
        cfg.write_text(config_body, encoding="utf-8")
        session = Session.objects.create(ticket=self.ticket)
        task = Task.objects.create(ticket=self.ticket, session=session)
        with (
            patch("teatree.agents.model_tiering.CONFIG_PATH", cfg),
            patch("teatree.config_agent.CONFIG_PATH", cfg),
        ):
            return headless_mod._build_options(task, "ctx", phase=phase, skills=skills)

    def test_skill_floor_raises_the_headless_model(self) -> None:
        options = self._options(
            "coding",
            skills=["architecture-design"],
            config_body='[agent.skill_models]\narchitecture-design = "fable"\n',
        )
        assert options.model == "fable"

    def test_sentinel_skill_floor_keeps_phase_model(self) -> None:
        options = self._options(
            "requesting_review",
            skills=["code-review"],
            config_body='[agent.skill_models]\ncode-review = "inherit"\n',
        )
        # requesting_review's cheap phase default stands; the inherit floor is a no-op.
        assert options.model == TIER_MODELS["cheap"]

    def test_session_effort_does_not_leak_into_headless(self) -> None:
        # A headless SDK run's effort comes from the per-TIER map, never from the
        # interactive ``session_effort`` pin. A cheap phase carries no tier effort,
        # so it stays unset even with session_effort configured — proving the
        # interactive pin does not leak into the sub-agent spawn.
        options = self._options(
            "requesting_review",
            skills=[],
            config_body='[agent]\nsession_effort = "xhigh"\nsession_model = "fable"\n',
        )
        assert options.effort is None

    def test_per_tier_effort_reaches_headless_spawn(self) -> None:
        # The counterpart: a frontier phase DOES carry the per-tier effort onto the
        # headless spawn (the axis session_effort must not be confused with).
        options = self._options(
            "coding",
            skills=[],
            config_body='[agent.tier_effort]\nfrontier = "max"\n',
        )
        assert options.effort == "max"


class TestRuntimeChildEnv(TestCase):
    """``_runtime_child_env`` pins the credential for the chosen headless runtime.

    DB access: the credential is now built through the config-aware factory
    (``teatree.credential_config``), which reads the ``ConfigSetting`` routing list.
    The empty table yields no override, so the child-env assertions are unchanged —
    ``TestCase`` just provides the DB the (no-op) config read needs.
    """

    def test_sdk_oauth_pins_subscription_and_strips_api_key(self) -> None:
        with patch.dict(os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": "oauth-x", "ANTHROPIC_API_KEY": "key-y"}):
            env = _runtime_child_env(AgentRuntime.SDK_OAUTH)

        assert env is not None
        assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth-x"
        assert "ANTHROPIC_API_KEY" not in env

    def test_sdk_apikey_pins_key_and_strips_oauth(self) -> None:
        with patch.dict(os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": "oauth-x", "ANTHROPIC_API_KEY": "key-y"}):
            env = _runtime_child_env(AgentRuntime.SDK_APIKEY)

        assert env is not None
        assert env["ANTHROPIC_API_KEY"] == "key-y"
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in env

    def test_interactive_and_api_use_ambient_env(self) -> None:
        assert _runtime_child_env(AgentRuntime.INTERACTIVE) is None
        assert _runtime_child_env(AgentRuntime.API) is None


class TestAgentRuntimeApiGuard(TestCase):
    """``run_headless`` refuses the not-yet-built raw-API runtime, loud and early."""

    def test_api_runtime_records_not_implemented_failure(self) -> None:
        ConfigSetting.objects.set_value("agent_runtime", "api")
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket, agent_id="agent-1")
        task = Task.objects.create(ticket=ticket, session=session, phase="coding")

        attempt = run_headless(task, phase="coding", overlay_skill_metadata={})

        task.refresh_from_db()
        assert attempt.exit_code == 1
        assert "not implemented" in attempt.error
        assert task.status == Task.Status.FAILED
