"""A factory run cut off short of a clean result keeps the work it finished, and never runs compacted.

A Claude-lane run can end three ways before its terminal result: the per-run turn ceiling, the
runtime ceiling, and a full context window — the normal end of a long run once compaction is
switched off. Each discarded an envelope the run had already written. These drive the real
dispatch through a scripted SDK stream that can also fire the hooks the dispatch registered.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import patch

from claude_agent_sdk import ResultMessage
from claude_agent_sdk.types import HookJSONOutput, PreCompactHookInput
from django.test import TestCase

import teatree.agents.harness as harness_mod
import teatree.agents.runner as runner_mod
import teatree.agents.skill_assurance as skill_assurance_mod
import teatree.agents.skill_injection as skill_injection_mod
from teatree.agents.runner import run_agent
from teatree.core.modelkit.task_failure_taxonomy import RecoveryStrategy, classify_failure, recovery_strategy
from teatree.core.models import ConfigSetting, PullRequest, Session, Task, TaskAttempt, Ticket
from teatree.core.models.task_handoff import schedule_resume
from teatree.loop.transient_requeue import requeue_transient_failed
from tests.factories import planned_ticket
from tests.teatree_agents._sdk_fake import FakeHarnessSession, assistant_text, assistant_tool_use, result_message

_CODED = json.dumps({"summary": "implemented", "files_modified": [{"path": "src/x.py", "action": "modified"}]})

_RUN_SESSION = "33333333-3333-4333-8333-333333333333"
_PARKED_SESSION = "44444444-4444-4444-8444-444444444444"


@dataclass(frozen=True)
class _Sleep:
    seconds: float


@dataclass(frozen=True)
class _PreCompact:
    trigger: str


class _ScriptedSession(FakeHarnessSession):
    def __init__(self, script: list[object], options: Any) -> None:
        super().__init__([])
        self.options = options
        self.hook_outputs: list[HookJSONOutput] = []
        self._script = script

    async def receive_response(self) -> AsyncIterator[Any]:
        for step in self._script:
            # The CLI answers an interrupt with its terminal result, which is how a stopped run's
            # spend — and the session it spent — reach its attempt at all.
            if self.interrupted and not isinstance(step, ResultMessage):
                return
            if isinstance(step, _Sleep):
                await asyncio.sleep(step.seconds)
            elif isinstance(step, _PreCompact):
                await self._fire_pre_compact(step.trigger)
            else:
                yield step

    async def _fire_pre_compact(self, trigger: str) -> None:
        payload = PreCompactHookInput(
            hook_event_name="PreCompact",
            trigger="auto" if trigger == "auto" else "manual",
            custom_instructions=None,
            session_id="s1",
            transcript_path="",
            cwd="",
        )
        for matcher in (self.options.hooks or {}).get("PreCompact", []):
            for hook in matcher.hooks:
                self.hook_outputs.append(await hook(payload, None, {"signal": None}))
        await asyncio.sleep(0.01)


def _max_turns() -> object:
    return result_message(subtype="error_max_turns", is_error=True, num_turns=3000)


def _context_full() -> object:
    return result_message(is_error=True, result="Prompt is too long", api_error_status=400, session_id=_RUN_SESSION)


def _errored() -> object:
    """A terminal error result naming the session it spent — an attempt records one from nowhere else."""
    return result_message(
        subtype="error_during_execution", is_error=True, result="the tool call failed", session_id=_RUN_SESSION
    )


class _Dispatch(TestCase):
    def _task(self, *, phase: str = "coding", **ticket_fields: object) -> Task:
        ticket = planned_ticket(**ticket_fields)
        session = Session.objects.create(ticket=ticket, agent_id=phase)
        return Task.objects.create(ticket=ticket, session=session, phase=phase)

    def _dispatch(self, task: Task, script: list[object], *, moved_to: str = "") -> _ScriptedSession:
        sessions: list[_ScriptedSession] = []
        effort = runner_mod.resolve_spawn_effort

        def _effort_after_the_stream(phase: str) -> str | None:
            if moved_to:
                Ticket.objects.filter(pk=task.ticket_id).update(state=moved_to)
            return effort(phase)

        def _open(*, options: Any = None, **_: object) -> _ScriptedSession:
            sessions.append(_ScriptedSession(script, options))
            return sessions[-1]

        idle = runner_mod.TaskUsage(turns=0, cost_usd=0.0)
        with (
            patch.object(runner_mod.shutil, "which", return_value="/usr/bin/claude"),
            patch.object(harness_mod, "ClaudeSDKClient", _open),
            patch.object(runner_mod.TaskUsage, "for_task", classmethod(lambda cls, task: idle)),
            patch("teatree.agents.runner_outcomes.alert_owner_max_turns_truncation"),
            patch.object(runner_mod, "resolve_spawn_effort", side_effect=_effort_after_the_stream),
        ):
            run_agent(task, phase=task.phase, overlay_skill_metadata={})
        task.refresh_from_db()
        [session] = sessions
        return session

    @staticmethod
    def _last_attempt(task: Task) -> TaskAttempt:
        attempt = task.attempts.order_by("-pk").first()
        assert attempt is not None
        return attempt


class TestTheFactoryRunsUncompacted(_Dispatch):
    def test_the_claude_child_is_spawned_with_compaction_switched_off(self) -> None:
        session = self._dispatch(self._task(), [assistant_tool_use(), assistant_text(_CODED), result_message()])

        assert session.options.env.get("DISABLE_COMPACT") == "1"

    def test_an_automatic_compaction_is_blocked_and_ends_the_run(self) -> None:
        task = self._task()

        session = self._dispatch(
            task, [assistant_tool_use(), _PreCompact("auto"), assistant_text(_CODED), result_message()]
        )

        [output] = session.hook_outputs
        assert output.get("decision") == "block"
        assert output.get("continue_") is False
        assert session.interrupted
        assert task.status == Task.Status.FAILED

    def test_a_stopped_run_is_recorded_as_a_compaction_and_re_dispatched_fresh(self) -> None:
        task = self._task()

        self._dispatch(task, [assistant_tool_use(), _PreCompact("auto"), result_message()])

        error = self._last_attempt(task).error
        assert "compaction" in error
        assert recovery_strategy(classify_failure(error)) is RecoveryStrategy.RETRY

    def test_work_finished_before_the_compaction_attempt_is_kept(self) -> None:
        task = self._task()

        self._dispatch(task, [assistant_tool_use(), assistant_text(_CODED), _PreCompact("auto"), result_message()])

        assert task.status == Task.Status.COMPLETED


class TestATurnCeilingKeepsFinishedWork(_Dispatch):
    def test_an_envelope_written_before_the_ceiling_is_recorded(self) -> None:
        task = self._task()

        self._dispatch(task, [assistant_tool_use(), assistant_text(_CODED), _max_turns()])

        assert task.status == Task.Status.COMPLETED
        assert self._last_attempt(task).result["summary"] == "implemented"

    def test_a_partial_envelope_stays_the_ceiling_failure(self) -> None:
        task = self._task()

        self._dispatch(
            task, [assistant_tool_use(), assistant_text('{"summary": "implemented", "files_mod'), _max_turns()]
        )

        assert task.status == Task.Status.FAILED
        assert "turn ceiling" in self._last_attempt(task).error

    def test_an_envelope_without_its_phase_evidence_stays_the_ceiling_failure(self) -> None:
        task = self._task()

        self._dispatch(task, [assistant_tool_use(), assistant_text(json.dumps({"summary": "halfway"})), _max_turns()])

        assert task.status == Task.Status.FAILED
        assert "turn ceiling" in self._last_attempt(task).error

    def test_a_phase_that_landed_before_the_run_is_not_credited_to_it(self) -> None:
        task = self._task(role=Ticket.Role.AUTHOR, state=Ticket.State.IN_REVIEW)

        self._dispatch(task, [assistant_tool_use(), _max_turns()])

        assert task.status == Task.Status.FAILED
        assert "turn ceiling" in self._last_attempt(task).error

    def test_another_actor_advancing_the_ticket_mid_run_is_not_credited_to_it(self) -> None:
        task = self._task(role=Ticket.Role.AUTHOR)

        self._dispatch(task, [assistant_tool_use(), _max_turns()], moved_to=Ticket.State.IN_REVIEW)

        assert task.status == Task.Status.FAILED

    def test_a_stale_cached_ticket_is_not_credited(self) -> None:
        task = self._task(role=Ticket.Role.AUTHOR, state=Ticket.State.IN_REVIEW)

        self._dispatch(task, [assistant_tool_use(), _max_turns()], moved_to=Ticket.State.STARTED)

        assert task.status == Task.Status.FAILED

    def test_a_review_with_no_recorded_verdict_is_not_credited(self) -> None:
        task = self._task(phase="reviewing", role=Ticket.Role.AUTHOR, state=Ticket.State.REVIEWED)

        with TemporaryDirectory() as directory:
            for name in (
                "code-review",
                "interactive",
                "internals",
                "slack-formatting",
                "rules",
                "platforms",
                "review",
                "code",
            ):
                skill_file = Path(directory) / name / "SKILL.md"
                skill_file.parent.mkdir()
                skill_file.write_text(f"# {name}\nFollow the test instructions.\n", encoding="utf-8")
            skill_dirs = [Path(directory)]
            with (
                patch.object(skill_injection_mod, "harness_skills_dirs", return_value=skill_dirs),
                patch.object(skill_assurance_mod, "harness_skills_dirs", return_value=skill_dirs),
            ):
                self._dispatch(task, [assistant_tool_use(), _max_turns()])

        assert task.status == Task.Status.FAILED

    def test_the_text_a_capped_run_wrote_is_kept_on_its_failure(self) -> None:
        task = self._task()

        self._dispatch(task, [assistant_tool_use(), assistant_text("half the migration is written"), _max_turns()])

        attempt = self._last_attempt(task)
        assert task.status == Task.Status.FAILED
        assert "half the migration is written" in str(attempt.result.get("summary", ""))

    def test_a_side_effect_without_the_phase_landing_is_still_a_failure(self) -> None:
        task = self._task(phase="shipping", role=Ticket.Role.AUTHOR, state=Ticket.State.REVIEWED)
        PullRequest.objects.create(
            ticket=task.ticket, url="https://github.com/o/r/pull/7", repo="o/r", iid="7", state=PullRequest.State.OPEN
        )

        self._dispatch(task, [assistant_tool_use(), _max_turns()])

        assert task.status == Task.Status.FAILED


class TestARuntimeCeilingKeepsFinishedWork(_Dispatch):
    def setUp(self) -> None:
        ConfigSetting.objects.set_value("watchdog_max_runtime_seconds", 1, scope="")

    def test_an_envelope_written_before_the_runtime_ceiling_is_recorded(self) -> None:
        task = self._task()

        self._dispatch(task, [assistant_tool_use(), assistant_text(_CODED), _Sleep(30)])

        assert task.status == Task.Status.COMPLETED

    def test_a_run_that_finished_nothing_stays_the_runtime_failure_and_keeps_its_text(self) -> None:
        task = self._task()

        self._dispatch(task, [assistant_tool_use(), assistant_text("still reading the fixtures"), _Sleep(30)])

        attempt = self._last_attempt(task)
        assert task.status == Task.Status.FAILED
        assert "runtime ceiling" in attempt.error
        assert "still reading the fixtures" in str(attempt.result.get("summary", ""))


class TestAFullContextWindowKeepsFinishedWork(_Dispatch):
    def test_an_envelope_written_before_the_context_filled_is_recorded(self) -> None:
        task = self._task()

        self._dispatch(task, [assistant_tool_use(), assistant_text(_CODED), _context_full()])

        assert task.status == Task.Status.COMPLETED

    def test_an_unfinished_run_is_named_and_re_dispatched_fresh_with_its_text(self) -> None:
        task = self._task()

        self._dispatch(task, [assistant_tool_use(), assistant_text("three of five files done"), _context_full()])

        attempt = self._last_attempt(task)
        assert task.status == Task.Status.FAILED
        assert "context_exhausted" in attempt.error
        assert recovery_strategy(classify_failure(attempt.error)) is RecoveryStrategy.RETRY
        assert "three of five files done" in str(attempt.result.get("summary", ""))


class TestARetryNeverResumesAConversationThatRanOut(_Dispatch):
    """A requeued dispatch continues a conversation that can still take a turn, and never one that ran out.

    A blocked compaction and a full context window both end a run whose history no longer fits, so
    resuming it re-pays that history straight back into the same wall. Every failing run below records
    the same session, and the compaction script differs from the ordinary-failure one by a single
    scripted step, so what separates a fresh retry from a resumed one is only the conversation running out.
    """

    def _retry_resume(self, script: list[object]) -> str | None:
        task = self._task()
        self._dispatch(task, script)
        assert requeue_transient_failed() == 1, "the fixture must reach the sweep's reopen branch"
        task.refresh_from_db()
        return self._dispatch(task, [assistant_tool_use(), assistant_text(_CODED), result_message()]).options.resume

    def test_a_run_stopped_by_a_blocked_compaction_is_re_dispatched_fresh(self) -> None:
        assert self._retry_resume([assistant_tool_use(), _PreCompact("auto"), _errored()]) is None

    def test_a_run_that_filled_its_context_window_is_re_dispatched_fresh(self) -> None:
        assert self._retry_resume([assistant_tool_use(), _context_full()]) is None

    def test_a_run_that_failed_with_room_left_is_retried_on_its_own_conversation(self) -> None:
        assert self._retry_resume([assistant_tool_use(), _errored()]) == _RUN_SESSION

    def test_an_answered_question_still_resumes_the_parked_conversation(self) -> None:
        parked = self._task()
        TaskAttempt.objects.create(
            task=parked,
            agent_session_id=_PARKED_SESSION,
            result={"needs_user_input": True, "user_input_reason": "Which DB?"},
        )
        resume = schedule_resume(parked, answer="postgres-1")

        session = self._dispatch(resume, [assistant_tool_use(), assistant_text(_CODED), result_message()])

        assert session.options.resume == _PARKED_SESSION
