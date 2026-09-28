"""How a finished drive is recorded when the model that served it is not the one it asked for (#4874)."""

import json

from claude_agent_sdk import SystemMessage
from django.test import TestCase

from teatree.agents.runner import run_agent
from teatree.core.modelkit.task_failure_taxonomy import FailureKind, exhausted_the_conversation
from teatree.core.models import Session, Task, TaskAttempt
from tests.factories import planned_ticket
from tests.teatree_agents._sdk_fake import assistant_text, assistant_tool_use, fake_sdk, result_message

_ENVELOPE = {"summary": "Done", "files_modified": [{"path": "src/x.py", "action": "modified"}]}
_PARKED_SESSION = "33333333-3333-4333-8333-333333333333"
_ACTION_UNVERIFIED = "action_unverified: "

_SERVED_BY_SONNET = {
    "claude-haiku-4-5-20251001": {"inputTokens": 900, "outputTokens": 40, "contextWindow": 200_000},
    "claude-sonnet-5": {"inputTokens": 20, "cacheReadInputTokens": 400_000, "contextWindow": 1_000_000},
}


def _fallback(content: str) -> SystemMessage:
    return SystemMessage(
        subtype="model_fallback",
        data={
            "type": "system",
            "subtype": "model_fallback",
            "original_model": "claude-opus-5-5",
            "fallback_model": "claude-sonnet-5",
            "content": content,
        },
    )


_CLI_TOO_OLD = _fallback(
    "Switched to Sonnet 5 because claude-opus-5-5 returned an error that could not be retried (400 "
    '{"type":"error","error":{"details":{"error_code":"claude_code_version_too_old"}}})'
)
_OVERLOADED = _fallback("Switched to Sonnet 5 because claude-opus-5-5 is overloaded (529)")


class _Drive(TestCase):
    def setUp(self) -> None:
        self.ticket = planned_ticket()
        self.task = Task.objects.create(ticket=self.ticket, session=Session.objects.create(ticket=self.ticket))

    def _drive(self, stream: list[object]) -> TaskAttempt:
        with fake_sdk(stream):
            attempt = run_agent(self.task, phase="coding", overlay_skill_metadata={})
        self.task.refresh_from_db()
        return attempt

    def _successful_stream(self, *head: object) -> list[object]:
        return [
            *head,
            assistant_tool_use(),
            assistant_text(json.dumps(_ENVELOPE)),
            result_message(model_usage=_SERVED_BY_SONNET, num_turns=4),
        ]


class TestADowngradeForcedByAnOutdatedCliFailsLoud(_Drive):
    def test_a_run_that_reported_success_is_recorded_failed_under_its_own_kind(self) -> None:
        attempt = self._drive(self._successful_stream(_CLI_TOO_OLD))

        assert attempt.exit_code == 1
        assert attempt.failure_kind == FailureKind.CLI_TOO_OLD_FOR_MODEL
        assert self.task.status == Task.Status.FAILED
        assert self.task.attempts.count() == 1

    def test_the_attempt_names_the_model_that_actually_served_it(self) -> None:
        attempt = self._drive(self._successful_stream(_CLI_TOO_OLD))

        assert attempt.model == "claude-sonnet-5"
        assert attempt.model_fell_back

    def test_a_run_parked_on_a_limit_after_the_fallback_is_requeued_fresh(self) -> None:
        """Tasks 4973/4975: parked on a usage window, then resumed as Opus 5.5 into a Sonnet-grown conversation."""
        limit = result_message(
            subtype="error_during_execution",
            is_error=True,
            result="Claude usage limit reached for this session.",
            session_id=_PARKED_SESSION,
            num_turns=162,
        )

        self._drive([_CLI_TOO_OLD, assistant_text("working"), limit])

        assert self.task.status == Task.Status.PENDING
        assert self.task.session_continuation == Task.SessionContinuation.FRESH

    def test_a_capacity_fallback_still_completes(self) -> None:
        attempt = self._drive(self._successful_stream(_OVERLOADED))

        assert attempt.exit_code == 0
        assert self.task.status == Task.Status.COMPLETED
        assert attempt.model == "claude-sonnet-5"

    def test_a_genuine_failure_keeps_its_own_name(self) -> None:
        attempt = self._drive([_CLI_TOO_OLD, result_message(is_error=True, result="boom", num_turns=2)])

        assert attempt.failure_kind == FailureKind.RESULT_ERROR
        assert self.task.attempts.count() == 1


class TestAResumeTheCliDeclinedStartsTheRetryFresh(_Drive):
    """Task 4973: the declined resume was filed ``no_result_envelope`` and resumed into the same wall."""

    _DECLINED = result_message(num_turns=0, result="", session_id=_PARKED_SESSION)

    def _park_a_conversation(self) -> None:
        TaskAttempt.objects.create(task=self.task, agent_session_id=_PARKED_SESSION, exit_code=1, num_turns=40)
        Task.objects.filter(pk=self.task.pk).update(session_continuation=Task.SessionContinuation.SELF)
        self.task.refresh_from_db()

    def test_a_declined_resume_is_an_exhausted_conversation(self) -> None:
        self._park_a_conversation()

        attempt = self._drive([self._DECLINED])

        assert exhausted_the_conversation(attempt.error)

    def test_its_retry_does_not_resume_the_same_conversation(self) -> None:
        self._park_a_conversation()
        self._drive([self._DECLINED])

        self.task.reopen()

        assert self.task.session_continuation == Task.SessionContinuation.FRESH

    def test_a_fresh_run_that_took_no_turn_keeps_its_own_refusal(self) -> None:
        attempt = self._drive([self._DECLINED])

        assert attempt.error.startswith(_ACTION_UNVERIFIED)
