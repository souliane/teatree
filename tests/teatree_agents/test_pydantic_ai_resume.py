"""Durable pydantic_ai conversation persistence — park/resume parity (#2886)."""

import asyncio

from django.test import TestCase
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter
from pydantic_ai.models.test import TestModel

from teatree.agents.pydantic_ai_resume import persist_parked_thread, rehydrate_thread_for_resume
from teatree.core.models import Session, Task, Ticket


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


class TestRehydrateThreadForResume(TestCase):
    def setUp(self) -> None:
        self.ticket = Ticket.objects.create()
        self.session = Session.objects.create(ticket=self.ticket)
        self.parked = Task.objects.create(ticket=self.ticket, session=self.session)

    def test_no_parent_task_returns_empty(self) -> None:
        assert rehydrate_thread_for_resume(self.parked) == []

    def test_immediate_parent_thread_is_rehydrated(self) -> None:
        history = _run("hello", output="hi")
        persist_parked_thread(self.parked, history)
        resumed = Task.objects.create(ticket=self.ticket, session=self.session, parent_task=self.parked)

        assert rehydrate_thread_for_resume(resumed) == history

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

        assert rehydrate_thread_for_resume(resumed) == history

    def test_no_thread_anywhere_in_the_chain_returns_empty(self) -> None:
        resumed = Task.objects.create(ticket=self.ticket, session=self.session, parent_task=self.parked)
        assert rehydrate_thread_for_resume(resumed) == []

    def test_malformed_stored_thread_degrades_to_empty_without_raising(self) -> None:
        self.ticket.merge_extra(set_keys={"pydantic_ai_threads": {str(self.parked.pk): [{"not": "a message"}]}})
        resumed = Task.objects.create(ticket=self.ticket, session=self.session, parent_task=self.parked)

        assert rehydrate_thread_for_resume(resumed) == []
