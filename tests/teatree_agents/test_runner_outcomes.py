"""How a finished drive is recorded when the model that served it is not the one it asked for (#4874)."""

import json

from claude_agent_sdk import AssistantMessage, SystemMessage, ToolResultBlock, ToolUseBlock
from django.test import TestCase

from teatree.agents.runner import run_agent
from teatree.agents.runner_outcomes import Transport, _failure_usage
from teatree.agents.runner_stream import HarnessOutcome
from teatree.agents.runner_usage import DispatchProvenance
from teatree.core.modelkit.task_failure_taxonomy import FailureKind, exhausted_the_conversation
from teatree.core.models import Session, Task, TaskAttempt
from tests.factories import planned_ticket
from tests.teatree_agents._sdk_fake import assistant_text, assistant_tool_use, fake_sdk, result_message

_ENVELOPE = {"summary": "Done", "files_modified": [{"path": "src/x.py", "action": "modified"}]}
_PARKED_SESSION = "33333333-3333-4333-8333-333333333333"
_OTHER_SESSION = "44444444-4444-4444-8444-444444444444"
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


def test_failure_usage_keeps_requested_loads_bare_and_once() -> None:
    receipt = {
        "requested": ["code"],
        "found": ["code"],
        "injected": [],
        "explicit_load": ["code"],
        "missing": [],
        "evidence": [],
        "observed_loads": [],
        "status": "unverified",
    }
    outcome = HarnessOutcome(
        agent_text="",
        result_message=None,
        stuck_reason=None,
        observed_skill_loads=("code", "code", "other", "t3:code"),
    )
    transport = Transport(provenance=DispatchProvenance(skill_assurance=receipt))

    usage = _failure_usage(outcome, lane="", transport=transport)

    assert usage.skill_assurance is not None
    assert usage.skill_assurance["observed_loads"] == ["code"]


def test_failure_usage_without_a_receipt_stays_without_one() -> None:
    outcome = HarnessOutcome(agent_text="", result_message=None, stuck_reason=None, observed_skill_loads=("code",))

    assert _failure_usage(outcome, lane="", transport=Transport()).skill_assurance is None


class TestSkillLoadsAcrossAttempts(_Drive):
    def _completed(self, session_id: str) -> TaskAttempt:
        return self._drive(
            [
                assistant_tool_use(),
                assistant_text(json.dumps(_ENVELOPE)),
                result_message(session_id=session_id, num_turns=4),
            ]
        )

    def _previous(
        self, *, session_id: str = _PARKED_SESSION, task: Task | None = None, loads: list[str] | None = None
    ) -> None:
        TaskAttempt.objects.create(
            task=task or self.task,
            agent_session_id=session_id,
            exit_code=1,
            num_turns=3,
            result={"skill_assurance": {"observed_loads": ["code"] if loads is None else loads}},
        )

    def test_a_parked_run_records_skill_loads(self) -> None:
        skills = ("t3:code", "t3:code", "t3:review")
        skill_call = AssistantMessage(
            content=[
                ToolUseBlock(id=f"skill{i}", name="Skill", input={"skill": skill}) for i, skill in enumerate(skills)
            ],
            model="",
        )
        skill_result = AssistantMessage(
            content=[ToolResultBlock(tool_use_id=f"skill{i}", content="loaded") for i in range(len(skills))],
            model="",
        )
        limit = result_message(
            subtype="error_during_execution",
            is_error=True,
            result="Claude usage limit reached for this session.",
            session_id=_PARKED_SESSION,
            num_turns=3,
        )

        attempt = self._drive([skill_call, skill_result, limit])

        assert attempt.failure_kind == FailureKind.USAGE_LIMIT_PARKED
        assert attempt.result["skill_assurance"]["observed_loads"] == ["code"]

    def test_a_resumed_run_inherits_its_sessions_loads(self) -> None:
        self._previous()
        Task.objects.filter(pk=self.task.pk).update(session_continuation=Task.SessionContinuation.SELF)
        self.task.refresh_from_db()

        attempt = self._completed(_PARKED_SESSION)

        assert attempt.result["skill_assurance"]["observed_loads"] == ["code"]

    def test_a_different_session_does_not_inherit_loads(self) -> None:
        self._previous()
        Task.objects.filter(pk=self.task.pk).update(session_continuation=Task.SessionContinuation.SELF)
        self.task.refresh_from_db()

        attempt = self._completed(_OTHER_SESSION)

        assert attempt.result["skill_assurance"]["observed_loads"] == []

    def test_a_fresh_run_does_not_inherit_loads(self) -> None:
        self._previous()

        attempt = self._completed(_PARKED_SESSION)

        assert attempt.result["skill_assurance"]["observed_loads"] == []

    def test_an_empty_session_does_not_inherit_loads(self) -> None:
        self._previous(session_id="")
        assert self.task.attempts.skill_loads_in_session("") == []
        Task.objects.filter(pk=self.task.pk).update(session_continuation=Task.SessionContinuation.SELF)
        self.task.refresh_from_db()

        attempt = self._completed("")

        assert attempt.result["skill_assurance"]["observed_loads"] == []

    def test_another_tasks_loads_do_not_count(self) -> None:
        other_task = Task.objects.create(ticket=self.ticket, session=Session.objects.create(ticket=self.ticket))
        self._previous(loads=[])
        self._previous(task=other_task)
        Task.objects.filter(pk=self.task.pk).update(session_continuation=Task.SessionContinuation.SELF)
        self.task.refresh_from_db()

        attempt = self._completed(_PARKED_SESSION)

        assert attempt.result["skill_assurance"]["observed_loads"] == []
