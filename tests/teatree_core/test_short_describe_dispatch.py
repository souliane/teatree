"""A completed ``short_describe`` task actually writes ``Ticket.short_description`` (#3570).

The deterministic writer had no caller in the dispatch path: the scanner
enqueued the task, ``execute_headless_task`` handed it to the generic agentic
runner, and the runner recorded a plausible exit-0 narration while the field
stayed blank forever (the scanner's COMPLETED dedup then suppressed the
re-enqueue permanently). The phase now routes through its deterministic writer.
"""

from unittest import mock

from django.test import TestCase

from teatree.core.models import Session, Task, Ticket
from teatree.core.tasks import execute_headless_task


def _ticket(title: str = "Loop lease is reclaimed on every tick") -> Ticket:
    return Ticket.objects.create(
        issue_url="https://example.invalid/org/repo/issues/1",
        extra={"issue_title": title},
    )


def _short_describe_task(ticket: Ticket) -> Task:
    return Task.objects.create(
        ticket=ticket,
        session=Session.objects.create(ticket=ticket, agent_id="t"),
        phase="short_describe",
        execution_target=Task.ExecutionTarget.HEADLESS,
        status=Task.Status.PENDING,
    )


class TestShortDescribeWritesTheField(TestCase):
    def _no_llm(self) -> None:
        """Force the truncation fallback so the phase needs no model call."""
        patched = mock.patch("teatree.agents.ticket_short_description._summarize", return_value="")
        patched.start()
        self.addCleanup(patched.stop)

    def test_completed_task_yields_a_non_empty_short_description(self) -> None:
        self._no_llm()
        ticket = _ticket()
        task = _short_describe_task(ticket)

        result = execute_headless_task.func(task.pk, "short_describe")

        ticket.refresh_from_db()
        assert ticket.short_description, "a completed short_describe task must populate the field"
        assert result["exit_code"] == "0"

    def test_the_agentic_runner_is_never_reached(self) -> None:
        self._no_llm()

        def _explode(*_args: object, **_kwargs: object) -> None:
            msg = "short_describe must not route to the generic agentic runner"
            raise AssertionError(msg)

        patched = mock.patch("teatree.core.headless_dispatch.get_headless_runner", return_value=_explode)
        patched.start()
        self.addCleanup(patched.stop)
        task = _short_describe_task(_ticket())

        execute_headless_task.func(task.pk, "short_describe")

        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED

    def test_a_ticket_without_a_cached_title_completes_without_a_description(self) -> None:
        self._no_llm()
        ticket = Ticket.objects.create(issue_url="https://example.invalid/org/repo/issues/2", extra={})
        task = _short_describe_task(ticket)

        result = execute_headless_task.func(task.pk, "short_describe")

        ticket.refresh_from_db()
        assert ticket.short_description == ""
        assert result["exit_code"] == "0"
