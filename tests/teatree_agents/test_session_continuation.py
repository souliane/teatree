"""Which conversation a dispatch carries is a STORED decision, never inferred from phase equality.

Phase equality is neither necessary nor sufficient. ``Task.spawn_child_tasks`` mints
ordinary same-phase parallel children that must each start fresh, and
``transient_requeue`` retries by reopening the SAME row — whose ``parent_task`` is
unchanged, so no parent-chain walk can reach the attempt it must continue.

Every case is pinned on BOTH lanes, because they resolve the discriminator through
different stores: ``claude_sdk`` through the SDK ``resume=`` option
(``session_lineage.resume_session_id``) and ``pydantic_ai`` through the durable thread
store (``pydantic_ai_resume.rehydrate_thread_for_resume``).
"""

import asyncio
import json
import shutil
import tempfile
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, TextPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls, FunctionModel
from pydantic_ai.models.test import TestModel

import teatree.agents.harness as harness_mod
import teatree.agents.runner as runner_mod
from teatree.agents.envelope_refusal import NO_ENVELOPE_ERROR
from teatree.agents.lane_b import toolsets as lane_b_toolsets
from teatree.agents.lane_b.tool_names import TOOL_READ
from teatree.agents.pydantic_ai_resume import persist_parked_thread, rehydrate_thread_for_resume
from teatree.agents.runner import TaskUsage, run_agent
from teatree.agents.runner_failure_taxonomy import RESULT_ERROR_PREFIX
from teatree.agents.session_lineage import resume_session_id
from teatree.core.modelkit.task_failure_taxonomy import CONTEXT_EXHAUSTED_MARKER
from teatree.core.models import ConfigSetting, Session, Task, TaskAttempt, Ticket, Worktree
from teatree.core.models.task_handoff import RESUME_CONTINUATION_CLAUSE, dispatch_reason, schedule_resume
from teatree.loop.transient_requeue import requeue_transient_failed
from tests.factories import planned_ticket

_PARENT_SESSION = "11111111-1111-4111-8111-111111111111"
_OWN_SESSION = "22222222-2222-4222-8222-222222222222"

#: The error text ``classify_failure`` routes to ``RecoveryStrategy.RETRY``, so the real
#: sweep reaches ``_reopen`` rather than the corrective or escalation branches.
_TRANSIENT = "outage_death: connection refused"

#: A run whose own conversation filled up, stamped exactly as ``runner_failure_taxonomy`` writes it.
#: Classifies ``result_error`` like any other failed result, so the same sweep reopens it.
_EXHAUSTED = f"{RESULT_ERROR_PREFIX}{CONTEXT_EXHAUSTED_MARKER} — subtype=error — prompt is too long"


def _history(output: str) -> list[ModelMessage]:
    return asyncio.run(Agent(TestModel(custom_output_text=output)).run("hello")).all_messages()


class _Lanes(TestCase):
    def setUp(self) -> None:
        self.ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, state=Ticket.State.STARTED)

    def _task(self, phase: str = "coding", *, parent: Task | None = None, **kwargs: object) -> Task:
        session = Session.objects.create(ticket=self.ticket, agent_id=phase)
        return Task.objects.create(ticket=self.ticket, session=session, phase=phase, parent_task=parent, **kwargs)

    def _parked(self, phase: str = "coding", *, session_id: str = _PARENT_SESSION) -> tuple[Task, list[ModelMessage]]:
        """A task that ran, asked, and STOPPED — its conversation durable on both lanes."""
        task = self._task(phase)
        TaskAttempt.objects.create(
            task=task,
            agent_session_id=session_id,
            result={"needs_user_input": True, "user_input_reason": "Which DB?"},
        )
        history = _history(session_id)
        persist_parked_thread(task, history)
        return task, history

    @staticmethod
    def _fail(task: Task, *, session_id: str = "", error: str = _TRANSIENT) -> None:
        TaskAttempt.objects.create(
            task=task, ended_at=timezone.now(), exit_code=1, error=error, agent_session_id=session_id
        )
        Task.objects.filter(pk=task.pk).update(status=Task.Status.FAILED)

    @staticmethod
    def _reopened(task: Task) -> Task:
        assert requeue_transient_failed() == 1, "the sweep must take the reopen branch for this fixture"
        task.refresh_from_db()
        assert task.status == Task.Status.PENDING
        return task


class TestAnOrdinarySamePhaseChildStartsFresh(_Lanes):
    """``spawn_child_tasks`` parallel children share their parent's phase and nothing else."""

    def _child(self) -> Task:
        parent, _ = self._parked()
        return parent.spawn_child_tasks(["repo-a"])[0]

    def test_the_claude_lane_does_not_resume_the_parents_session(self) -> None:
        assert resume_session_id(self._child()) == ""

    def test_the_pydantic_lane_does_not_adopt_the_parents_thread(self) -> None:
        child = self._child()

        assert rehydrate_thread_for_resume(child) is None

        self.ticket.refresh_from_db()
        assert self.ticket.extra["pydantic_ai_threads"], "the parent's own continuation still needs its thread"


class TestATaskReopenedInPlaceResumesItself(_Lanes):
    """``transient_requeue`` retries the SAME row, so its own last attempt is what continues."""

    def test_the_claude_lane_resumes_its_own_attempt_not_its_parents(self) -> None:
        parent, _ = self._parked()
        retried = self._task(parent=parent)
        self._fail(retried, session_id=_OWN_SESSION)

        assert resume_session_id(self._reopened(retried)) == _OWN_SESSION

    def test_a_non_claude_harness_never_receives_the_legacy_session_agent_id(self) -> None:
        task = self._task()
        task.session.agent_id = _OWN_SESSION
        task.session.save(update_fields=["agent_id"])
        task.session_continuation = Task.SessionContinuation.SELF

        assert resume_session_id(task, harness="codex_app_server") == ""

    def test_a_non_claude_harness_resumes_only_its_own_typed_attempt(self) -> None:
        task = self._task()
        task.session.agent_id = _PARENT_SESSION
        task.session.save(update_fields=["agent_id"])
        task.session_continuation = Task.SessionContinuation.SELF
        TaskAttempt.objects.create(
            task=task,
            agent_session_id=_OWN_SESSION,
            selected_harness="codex_app_server",
        )

        assert resume_session_id(task, harness="codex_app_server") == _OWN_SESSION


class TestAPydanticRunReopenedInPlaceContinuesItsOwnConversation(TestCase):
    """The pydantic lane through its production seam: real ``run_agent``, real sweep, real ``run_agent``.

    Its conversation lives only in teatree's own store, so what is on trial is whether a failed
    run leaves that conversation behind — a hand-seeded thread would assume the answer.
    """

    _FIRST_REPLY = "first run: the lock is taken twice on the retry path"
    _PARENT_REPLY = "the parent's unrelated conversation"

    def setUp(self) -> None:
        ConfigSetting.objects.set_value("agent_harness", "pydantic_ai")
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        self.ticket = planned_ticket(role=Ticket.Role.AUTHOR, state=Ticket.State.STARTED)
        Worktree.objects.create(
            ticket=self.ticket, repo_path=str(root), branch="feature", extra={"worktree_path": str(root)}
        )
        self.notes = root / "notes.txt"
        self.notes.write_text("lock order", encoding="utf-8")
        self.parent = Task.objects.create(
            ticket=self.ticket, session=Session.objects.create(ticket=self.ticket), phase="debugging"
        )
        persist_parked_thread(self.parent, _history(self._PARENT_REPLY))
        self.task = Task.objects.create(
            ticket=self.ticket,
            session=Session.objects.create(ticket=self.ticket, agent_id="debugging"),
            phase="debugging",
            parent_task=self.parent,
        )
        self.requests: list[list[ModelMessage]] = []

    async def _stream(self, messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[str | DeltaToolCalls]:
        await asyncio.sleep(0)
        self.requests.append(list(messages))
        if len(self.requests) == 1:
            yield self._FIRST_REPLY
        elif len(self.requests) == 2:
            yield {0: DeltaToolCall(name=TOOL_READ, json_args=json.dumps({"path": str(self.notes)}), tool_call_id="r")}
        else:
            yield json.dumps({"summary": "fixed the double lock"})

    def _dispatch(self) -> TaskAttempt:
        model = FunctionModel(stream_function=self._stream)
        with (
            patch.object(harness_mod.PydanticAiHarness, "_resolve_model", lambda _self, _options, _run: model),
            patch.object(runner_mod.TaskUsage, "for_task", classmethod(lambda cls, _task: TaskUsage(0, 0.0))),
            patch.object(lane_b_toolsets, "build_mcp_toolsets", return_value=[]),
        ):
            return run_agent(self.task, phase="debugging", overlay_skill_metadata={})

    @staticmethod
    def _texts(messages: list[ModelMessage]) -> list[str]:
        return [part.content for message in messages for part in message.parts if isinstance(part, TextPart)]

    def _threads(self) -> dict[str, object]:
        self.ticket.refresh_from_db()
        return self.ticket.extra.get("pydantic_ai_threads", {})

    def test_the_retry_is_sent_the_failed_runs_conversation(self) -> None:
        first = self._dispatch()
        assert first.error == NO_ENVELOPE_ERROR
        assert requeue_transient_failed() == 1
        self.task.refresh_from_db()

        second = self._dispatch()

        self.task.refresh_from_db()
        assert second.exit_code == 0
        assert second.error == ""
        assert self.task.status == Task.Status.COMPLETED
        retry_opening = self._texts(self.requests[1])
        assert self._FIRST_REPLY in retry_opening
        assert self._PARENT_REPLY not in retry_opening

    def test_a_completed_run_releases_its_own_conversation_and_nobody_elses(self) -> None:
        self._dispatch()
        requeue_transient_failed()
        self._dispatch()

        threads = self._threads()
        assert str(self.task.pk) not in threads, "nothing will continue a run that completed without asking"
        assert str(self.parent.pk) in threads


class TestAPydanticRunThatStoredNoThreadNeverClaimsItself(_Lanes):
    """On the metered lane a recorded session id is not a conversation; only the stored thread is."""

    def _reopened_resume(self, session_id: str) -> Task:
        parked, _ = self._parked()
        resume = schedule_resume(parked, answer="postgres-1")
        rehydrate_thread_for_resume(resume)
        self._fail(resume, session_id=session_id)
        return self._reopened(resume)

    def test_a_metered_session_id_alone_keeps_the_answered_lineage(self) -> None:
        reopened = self._reopened_resume(uuid.uuid4().hex)

        assert reopened.session_continuation == Task.SessionContinuation.PARENT

    def test_a_server_side_session_id_is_a_conversation_of_its_own(self) -> None:
        reopened = self._reopened_resume(_OWN_SESSION)

        assert reopened.session_continuation == Task.SessionContinuation.SELF
        assert resume_session_id(reopened) == _OWN_SESSION


class TestANeedsInputContinuationResumesItsParent(_Lanes):
    """``schedule_resume`` exists to carry the owner's answer back into the parked conversation."""

    def test_the_claude_lane_resumes_the_parked_session(self) -> None:
        parked, _ = self._parked()

        assert resume_session_id(schedule_resume(parked, answer="postgres-1")) == _PARENT_SESSION

    def test_the_pydantic_lane_rehydrates_the_parked_thread(self) -> None:
        parked, history = self._parked()

        resumed = rehydrate_thread_for_resume(schedule_resume(parked, answer="postgres-1"))

        assert resumed is not None
        assert resumed.ancestor == parked
        assert resumed.history == history


class TestACrossPhaseChildNeverResumes(_Lanes):
    def test_the_claude_lane_starts_a_fresh_session(self) -> None:
        planning, _ = self._parked("planning")

        assert resume_session_id(self._task("coding", parent=planning)) == ""

    def test_the_pydantic_lane_leaves_the_previous_phase_thread_alone(self) -> None:
        planning, _ = self._parked("planning")
        coding = self._task("coding", parent=planning)

        assert rehydrate_thread_for_resume(coding) is None

        self.ticket.refresh_from_db()
        assert str(planning.pk) in self.ticket.extra["pydantic_ai_threads"]


class TestAPreOpenFailureKeepsTheAnswersLineage(_Lanes):
    """A resume that dies before the harness opens has no conversation of its OWN yet.

    ``resolve_harness`` pops the parked thread while BUILDING the harness, and a refused
    dispatch restores it under the PARENT's key — so stamping the reopened row SELF points
    the retry at a task with nothing stored and silently drops the owner's answer.
    """

    def _resume_that_died_before_opening(self) -> tuple[Task, Task, list[ModelMessage]]:
        parked, history = self._parked()
        resume = schedule_resume(parked, answer="postgres-1")
        popped = rehydrate_thread_for_resume(resume)
        assert popped is not None
        persist_parked_thread(popped.ancestor, popped.history)
        self._fail(resume)
        reopened = self._reopened(resume)
        # Pinned on the marker as well as on each lane's behaviour: `schedule_resume` copies
        # the parked session id onto the followup's own Session, so the claude lane survives
        # a clobbered lineage by accident where the pydantic lane loses the answer outright.
        assert reopened.session_continuation == Task.SessionContinuation.PARENT
        return parked, reopened, history

    def test_the_claude_lane_still_resumes_the_parked_session(self) -> None:
        _, resume, _ = self._resume_that_died_before_opening()

        assert resume_session_id(resume) == _PARENT_SESSION

    def test_the_pydantic_lane_still_rehydrates_the_answered_conversation(self) -> None:
        parked, resume, history = self._resume_that_died_before_opening()

        resumed = rehydrate_thread_for_resume(resume)

        assert resumed is not None
        assert resumed.ancestor == parked
        assert resumed.history == history


class TestAFreshRetryKeepsNothingOfTheConversationThatRanOut(_Lanes):
    """Answering FRESH is only half the decision — what the exhausted run LEFT behind must go too.

    The stored thread outlives the answer: ``_holds_a_conversation`` reads it back on the next
    sweep, so a FRESH retry refused before the harness opens is re-stamped SELF and rehydrates
    the very conversation that ran out.
    """

    def _exhausted_then_reopened(self) -> Task:
        task, _ = self._parked()
        self._fail(task, error=_EXHAUSTED)
        reopened = self._reopened(task)
        assert reopened.session_continuation == Task.SessionContinuation.FRESH
        return reopened

    def test_the_exhausted_conversation_is_dropped(self) -> None:
        reopened = self._exhausted_then_reopened()

        self.ticket.refresh_from_db()
        assert str(reopened.pk) not in (self.ticket.extra.get("pydantic_ai_threads") or {})

    def test_a_later_sweep_never_re_adopts_it(self) -> None:
        """The observable cost of keeping it: the retry after a pre-open refusal resumes what ran out."""
        reopened = self._exhausted_then_reopened()
        self._fail(reopened)

        again = self._reopened(reopened)

        assert again.session_continuation != Task.SessionContinuation.SELF
        assert rehydrate_thread_for_resume(again) is None

    def test_a_siblings_conversation_survives(self) -> None:
        sibling, _ = self._parked("planning")
        self._exhausted_then_reopened()

        self.ticket.refresh_from_db()
        assert str(sibling.pk) in self.ticket.extra["pydantic_ai_threads"]


class TestAFreshRetryStopsPromisingAConversationItNoLongerHas(_Lanes):
    """``schedule_resume`` writes "continue where you left off"; the requeue rule can overrule it.

    The answer stays true whatever conversation the retry carries. The instruction does not: on
    FRESH there is no earlier point to continue from, and the prompt says to find one anyway.
    """

    def _answered_resume_that_ran_out(self) -> Task:
        parked, _ = self._parked()
        resume = schedule_resume(parked, answer="postgres-1")
        assert resume.session_continuation == Task.SessionContinuation.PARENT
        self._fail(resume, error=_EXHAUSTED)
        return self._reopened(resume)

    def test_a_resumed_dispatch_is_still_told_to_continue(self) -> None:
        parked, _ = self._parked()

        assert RESUME_CONTINUATION_CLAUSE in dispatch_reason(schedule_resume(parked, answer="postgres-1"))

    def test_a_fresh_dispatch_is_not(self) -> None:
        assert RESUME_CONTINUATION_CLAUSE not in dispatch_reason(self._answered_resume_that_ran_out())

    def test_the_answer_survives_either_way(self) -> None:
        assert "postgres-1" in dispatch_reason(self._answered_resume_that_ran_out())
