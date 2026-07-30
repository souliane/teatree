"""Durable pydantic_ai conversation persistence — park/resume parity (#2886)."""

import asyncio

import django.test as django_test
from claude_agent_sdk import ResultMessage
from django.test import TestCase
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter
from pydantic_ai.models.test import TestModel

from teatree.agents.headless import HarnessOutcome, _outcome_failure
from teatree.agents.pydantic_ai_resume import (
    maybe_persist_on_limit_park,
    maybe_persist_on_park,
    persist_parked_thread,
    rehydrate_thread_for_resume,
)
from teatree.core.models import ConfigSetting, Session, Task, TaskAttempt, Ticket


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


class TestMaybePersistOnPark(TestCase):
    def setUp(self) -> None:
        self.ticket = Ticket.objects.create()
        self.session = Session.objects.create(ticket=self.ticket)
        self.task = Task.objects.create(ticket=self.ticket, session=self.session)

    def test_persists_when_result_needs_user_input_and_thread_present(self) -> None:
        history = _run("hello", output="hi")

        maybe_persist_on_park(self.task, {"needs_user_input": True}, history)

        self.ticket.refresh_from_db()
        assert str(self.task.pk) in self.ticket.extra["pydantic_ai_threads"]

    def test_no_op_when_result_does_not_need_user_input(self) -> None:
        maybe_persist_on_park(self.task, {"needs_user_input": False}, _run("hello", output="hi"))

        self.ticket.refresh_from_db()
        assert "pydantic_ai_threads" not in self.ticket.extra

    def test_no_op_when_thread_is_none(self) -> None:
        maybe_persist_on_park(self.task, {"needs_user_input": True}, None)

        self.ticket.refresh_from_db()
        assert "pydantic_ai_threads" not in self.ticket.extra


class TestMaybePersistOnLimitPark(TestCase):
    """#3605 — a usage-limit park re-queues the SAME task, so its thread must survive."""

    def setUp(self) -> None:
        self.ticket = Ticket.objects.create()
        self.session = Session.objects.create(ticket=self.ticket)
        self.task = Task.objects.create(ticket=self.ticket, session=self.session)

    def test_persists_the_thread_under_the_parked_task_own_pk(self) -> None:
        history = _run("hello", output="hi")

        maybe_persist_on_limit_park(self.task, history)

        self.ticket.refresh_from_db()
        stored = self.ticket.extra["pydantic_ai_threads"][str(self.task.pk)]
        assert ModelMessagesTypeAdapter.validate_python(stored) == history

    def test_no_op_when_the_run_carried_no_thread(self) -> None:
        maybe_persist_on_limit_park(self.task, None)

        self.ticket.refresh_from_db()
        assert "pydantic_ai_threads" not in self.ticket.extra

    def test_the_requeued_same_task_resumes_the_parked_conversation(self) -> None:
        history = _run("hello", output="hi")
        maybe_persist_on_limit_park(self.task, history)

        resumed = rehydrate_thread_for_resume(self.task)

        assert resumed is not None
        assert resumed.history == history
        assert resumed.ancestor == self.task


class TestRehydrateThreadForResume(TestCase):
    def setUp(self) -> None:
        self.ticket = Ticket.objects.create()
        self.session = Session.objects.create(ticket=self.ticket)
        self.parked = Task.objects.create(ticket=self.ticket, session=self.session)

    def test_no_parent_task_returns_none(self) -> None:
        assert rehydrate_thread_for_resume(self.parked) is None

    def test_immediate_parent_thread_is_rehydrated(self) -> None:
        history = _run("hello", output="hi")
        persist_parked_thread(self.parked, history)
        resumed = Task.objects.create(ticket=self.ticket, session=self.session, parent_task=self.parked)

        result = rehydrate_thread_for_resume(resumed)

        assert result is not None
        assert result.history == history
        assert result.ancestor == self.parked

    def test_rehydration_consumes_the_entry(self) -> None:
        history = _run("hello", output="hi")
        persist_parked_thread(self.parked, history)
        resumed = Task.objects.create(ticket=self.ticket, session=self.session, parent_task=self.parked)

        rehydrate_thread_for_resume(resumed)

        self.ticket.refresh_from_db()
        assert str(self.parked.pk) not in self.ticket.extra.get("pydantic_ai_threads", {})

    def test_walks_multiple_parent_hops_to_find_the_thread(self) -> None:
        history = _run("hello", output="hi")
        persist_parked_thread(self.parked, history)
        interactive_followup = Task.objects.create(ticket=self.ticket, session=self.session, parent_task=self.parked)
        resumed = Task.objects.create(ticket=self.ticket, session=self.session, parent_task=interactive_followup)

        result = rehydrate_thread_for_resume(resumed)

        assert result is not None
        assert result.history == history
        assert result.ancestor == self.parked

    def test_no_thread_anywhere_in_the_chain_returns_none(self) -> None:
        resumed = Task.objects.create(ticket=self.ticket, session=self.session, parent_task=self.parked)
        assert rehydrate_thread_for_resume(resumed) is None

    def test_malformed_stored_thread_degrades_to_empty_without_raising(self) -> None:
        self.ticket.merge_extra(set_keys={"pydantic_ai_threads": {str(self.parked.pk): [{"not": "a message"}]}})
        resumed = Task.objects.create(ticket=self.ticket, session=self.session, parent_task=self.parked)

        result = rehydrate_thread_for_resume(resumed)

        assert result is not None
        assert result.history == []
        assert result.ancestor == self.parked


class TestHeadlessLimitParkKeepsTheConversation(django_test.TestCase):
    """#3605 — the headless park path itself persists, not just the seam it calls."""

    def setUp(self) -> None:
        ConfigSetting.objects.set_value("limit_autorecovery_enabled", value=True)
        self.ticket = Ticket.objects.create()
        self.session = Session.objects.create(ticket=self.ticket)
        self.task = Task.objects.create(ticket=self.ticket, session=self.session)

    def _limit_outcome(self, thread: list[ModelMessage] | None) -> HarnessOutcome:
        return HarnessOutcome(
            agent_text="",
            result_message=ResultMessage(
                subtype="error",
                duration_ms=1,
                duration_api_ms=1,
                is_error=True,
                num_turns=1,
                session_id="s",
                result="5-hour limit reached",
            ),
            stuck_reason=None,
            thread=thread,
        )

    def test_a_limit_park_persists_the_run_conversation(self) -> None:
        history = _run("hello", output="hi")

        attempt = _outcome_failure(self.task, self._limit_outcome(history), lane=TaskAttempt.Lane.METERED)

        assert attempt is not None
        self.ticket.refresh_from_db()
        stored = self.ticket.extra["pydantic_ai_threads"][str(self.task.pk)]
        assert ModelMessagesTypeAdapter.validate_python(stored) == history

    def test_a_claude_sdk_run_with_no_thread_persists_nothing(self) -> None:
        _outcome_failure(self.task, self._limit_outcome(None), lane=TaskAttempt.Lane.SUBSCRIPTION)

        self.ticket.refresh_from_db()
        assert "pydantic_ai_threads" not in self.ticket.extra
