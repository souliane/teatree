"""Durable pydantic_ai conversation persistence — park/resume parity (#2886)."""

import asyncio
from collections.abc import AsyncIterator
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone
from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel

import teatree.agents.harness as harness_mod
import teatree.agents.runner as runner_mod
from teatree.agents.pydantic_ai_resume import (
    persist_parked_thread,
    rehydrate_thread_for_resume,
    release_finished_thread,
    retain_run_thread,
)
from teatree.agents.runner import TaskUsage, run_agent
from teatree.core.models import ConfigSetting, Session, Task, TaskAttempt, Ticket
from teatree.core.models.task_handoff import schedule_resume
from tests.factories import planned_ticket


def _run(prompt: str, *, output: str) -> list[ModelMessage]:
    agent = Agent(TestModel(custom_output_text=output))
    return asyncio.run(agent.run(prompt)).all_messages()


class TestPersistParkedThread(TestCase):
    def setUp(self) -> None:
        self.ticket = Ticket.objects.create()
        self.session = Session.objects.create(ticket=self.ticket)
        self.task = Task.objects.create(ticket=self.ticket, session=self.session)

    def test_stores_the_serialized_history_keyed_by_task_pk(self) -> None:
        history = _run("hello", output="hi")

        persist_parked_thread(self.task, history)

        self.ticket.refresh_from_db()
        stored = self.ticket.extra["pydantic_ai_threads"][str(self.task.pk)]
        assert ModelMessagesTypeAdapter.validate_python(stored) == history

    def test_does_not_clobber_an_unrelated_extra_key(self) -> None:
        self.ticket.merge_extra(set_keys={"tests_passed": True})

        persist_parked_thread(self.task, _run("hello", output="hi"))

        self.ticket.refresh_from_db()
        assert self.ticket.extra["tests_passed"] is True
        assert str(self.task.pk) in self.ticket.extra["pydantic_ai_threads"]

    def test_two_parked_tasks_on_the_same_ticket_both_persist(self) -> None:
        other = Task.objects.create(ticket=self.ticket, session=self.session)

        persist_parked_thread(self.task, _run("first", output="a"))
        persist_parked_thread(other, _run("second", output="b"))

        self.ticket.refresh_from_db()
        threads = self.ticket.extra["pydantic_ai_threads"]
        assert str(self.task.pk) in threads
        assert str(other.pk) in threads

    def test_a_long_held_ticket_keeps_a_sibling_thread_stored_meanwhile(self) -> None:
        held = Task.objects.select_related("ticket").get(pk=self.task.pk)
        sibling = Task.objects.create(ticket=self.ticket, session=self.session)
        persist_parked_thread(sibling, _run("sibling", output="b"))

        persist_parked_thread(held, _run("held", output="a"))

        self.ticket.refresh_from_db()
        assert set(self.ticket.extra["pydantic_ai_threads"]) == {str(held.pk), str(sibling.pk)}

    def test_a_resume_from_a_long_held_ticket_keeps_a_sibling_thread_stored_meanwhile(self) -> None:
        persist_parked_thread(self.task, _run("held", output="a"))
        Task.objects.filter(pk=self.task.pk).update(session_continuation=Task.SessionContinuation.SELF)
        held = Task.objects.select_related("ticket").get(pk=self.task.pk)
        sibling = Task.objects.create(ticket=self.ticket, session=self.session)
        persist_parked_thread(sibling, _run("sibling", output="b"))

        assert rehydrate_thread_for_resume(held) is not None

        self.ticket.refresh_from_db()
        assert set(self.ticket.extra["pydantic_ai_threads"]) == {str(sibling.pk)}


class TestTheRunThreadLifecycle(TestCase):
    """A run's conversation is kept for whatever continues it, and dropped once nothing will."""

    def setUp(self) -> None:
        self.ticket = Ticket.objects.create()
        self.session = Session.objects.create(ticket=self.ticket)
        self.task = Task.objects.create(ticket=self.ticket, session=self.session)
        self.history = _run("hello", output="hi")

    def _finished(self, status: str, result: dict[str, object]) -> None:
        retain_run_thread(self.task, self.history)
        TaskAttempt.objects.create(task=self.task, result=result)
        Task.objects.filter(pk=self.task.pk).update(status=status)
        release_finished_thread(self.task)

    def _stored(self) -> bool:
        self.ticket.refresh_from_db()
        return str(self.task.pk) in self.ticket.extra.get("pydantic_ai_threads", {})

    def test_a_finished_run_retains_its_conversation_under_its_own_pk(self) -> None:
        retain_run_thread(self.task, self.history)

        self.ticket.refresh_from_db()
        stored = self.ticket.extra["pydantic_ai_threads"][str(self.task.pk)]
        assert ModelMessagesTypeAdapter.validate_python(stored) == self.history

    def test_a_transport_with_no_conversation_retains_nothing(self) -> None:
        retain_run_thread(self.task, None)

        self.ticket.refresh_from_db()
        assert "pydantic_ai_threads" not in self.ticket.extra

    def test_a_completed_run_that_did_not_ask_releases_its_conversation(self) -> None:
        self._finished(Task.Status.COMPLETED, {"summary": "done"})

        assert not self._stored()

    def test_a_run_that_asked_for_input_keeps_its_conversation_for_the_answer(self) -> None:
        self._finished(Task.Status.COMPLETED, {"summary": "blocked", "needs_user_input": True})

        assert self._stored()

    def test_a_failed_run_keeps_its_conversation_for_the_retry(self) -> None:
        self._finished(Task.Status.FAILED, {})

        assert self._stored()

    def test_a_limit_parked_row_resumes_its_own_conversation(self) -> None:
        retain_run_thread(self.task, self.history)
        self.task.park(not_before=timezone.now() + timedelta(hours=1))

        resumed = rehydrate_thread_for_resume(self.task)

        assert resumed is not None
        assert resumed.history == self.history
        assert resumed.ancestor == self.task


class TestRehydrateThreadForResume(TestCase):
    """The store mechanics of a resume, once the continuation lineage has selected its source."""

    def setUp(self) -> None:
        self.ticket = Ticket.objects.create()
        self.session = Session.objects.create(ticket=self.ticket)
        self.parked = Task.objects.create(ticket=self.ticket, session=self.session)

    def _continuing(self, parent: Task) -> Task:
        return Task.objects.create(
            ticket=self.ticket,
            session=self.session,
            parent_task=parent,
            session_continuation=Task.SessionContinuation.PARENT,
        )

    def test_no_parent_task_returns_none(self) -> None:
        assert rehydrate_thread_for_resume(self.parked) is None

    def test_immediate_parent_thread_is_rehydrated(self) -> None:
        history = _run("hello", output="hi")
        persist_parked_thread(self.parked, history)
        resumed = self._continuing(self.parked)

        result = rehydrate_thread_for_resume(resumed)

        assert result is not None
        assert result.history == history
        assert result.ancestor == self.parked

    def test_rehydration_consumes_the_entry(self) -> None:
        history = _run("hello", output="hi")
        persist_parked_thread(self.parked, history)
        resumed = self._continuing(self.parked)

        rehydrate_thread_for_resume(resumed)

        self.ticket.refresh_from_db()
        assert str(self.parked.pk) not in self.ticket.extra.get("pydantic_ai_threads", {})

    def test_walks_multiple_parent_hops_to_find_the_thread(self) -> None:
        history = _run("hello", output="hi")
        persist_parked_thread(self.parked, history)
        resumed = self._continuing(self._continuing(self.parked))

        result = rehydrate_thread_for_resume(resumed)

        assert result is not None
        assert result.history == history
        assert result.ancestor == self.parked

    def test_no_thread_anywhere_in_the_chain_returns_none(self) -> None:
        resumed = self._continuing(self.parked)
        assert rehydrate_thread_for_resume(resumed) is None

    def test_malformed_stored_thread_degrades_to_empty_without_raising(self) -> None:
        self.ticket.merge_extra(set_keys={"pydantic_ai_threads": {str(self.parked.pk): [{"not": "a message"}]}})
        resumed = self._continuing(self.parked)

        result = rehydrate_thread_for_resume(resumed)

        assert result is not None
        assert result.history == []
        assert result.ancestor == self.parked


class TestAnAnsweredContinuationThatHitsAUsageLimit(TestCase):
    """#3605 through the production seam: a limit park must not strand the answered conversation.

    The resume pops its parent's thread while BUILDING the harness, so by the time the 429
    parks the row the conversation exists only in the run — and the park stamps the row's
    continuation from whatever is stored at that moment.
    """

    def setUp(self) -> None:
        ConfigSetting.objects.set_value("agent_harness", "pydantic_ai")
        ticket = planned_ticket()
        session = Session.objects.create(ticket=ticket, agent_id="agent-1")
        self.parked = Task.objects.create(ticket=ticket, session=session, phase="coding")
        TaskAttempt.objects.create(task=self.parked, result={"needs_user_input": True, "user_input_reason": "DB?"})
        self.history = _run("which database?", output="postgres or sqlite?")
        persist_parked_thread(self.parked, self.history)
        self.resume = schedule_resume(self.parked, answer="postgres")

    @staticmethod
    async def _rate_limited(_messages: object, _info: AgentInfo) -> AsyncIterator[str]:
        await asyncio.sleep(0)
        raise ModelHTTPError(status_code=429, model_name="m", body={"error": {"type": "rate_limit_error"}})
        yield ""

    def test_the_parked_retry_resumes_the_answered_conversation(self) -> None:
        model = FunctionModel(stream_function=self._rate_limited)
        with (
            patch.object(harness_mod.PydanticAiHarness, "_resolve_model", lambda _self, _options, _run: model),
            patch.object(runner_mod.TaskUsage, "for_task", classmethod(lambda cls, _task: TaskUsage(0, 0.0))),
        ):
            attempt = run_agent(self.resume, phase="coding", overlay_skill_metadata={})

        self.resume.refresh_from_db()
        assert self.resume.status == Task.Status.PENDING
        assert "limit_parked: " in attempt.error
        resumed = rehydrate_thread_for_resume(self.resume)
        assert resumed is not None
        assert resumed.history == self.history
