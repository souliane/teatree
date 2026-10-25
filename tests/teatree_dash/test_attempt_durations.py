"""A finished attempt is timed from its task's admission on the live, sessions and ticket pages (#5507).

``TaskAttempt.started_at`` is stamped when the row is written, which is when the run
ends, so ``ended_at - started_at`` reads ``0s`` for almost every finished attempt.
"""

# test-path: cross-cutting — one display-start rule shared by the live, sessions and ticket pages

import datetime as dt
from operator import attrgetter
from typing import cast

from django.test import TestCase
from django.utils import timezone

from teatree.core.models.task import Task
from teatree.core.models.task_attempt import TaskAttempt
from teatree.core.models.ticket import Ticket
from teatree.dash.live import build_live_view
from teatree.dash.sessions import build_session_index
from teatree.dash.ticket_detail import ATTEMPT_ROWS, build_ticket_detail
from tests.factories import TaskFactory, TicketFactory

State = Ticket.State


def _durations_by_page(ticket: Ticket) -> dict[str, list[str]]:
    on_ticket_page = build_ticket_detail(ticket.pk).tasks[0].attempts
    return {
        "live": [row.duration for row in sorted(build_live_view().outcomes, key=attrgetter("attempt_id"))],
        "sessions": [row.duration for row in sorted(build_session_index(), key=attrgetter("agent_session_id"))],
        "ticket": [row.duration for row in sorted(on_ticket_page, key=attrgetter("attempt_id"))],
    }


def _task(*, admitted_at: dt.datetime | None) -> Task:
    task = cast("Task", TaskFactory(ticket=TicketFactory(state=State.WORK_STARTED), phase="coding"))
    Task.objects.filter(pk=task.pk).update(admitted_at=admitted_at)
    task.refresh_from_db()
    return task


def _finished_attempt(task: Task, *, ended_at: dt.datetime, session: str) -> TaskAttempt:
    attempt = TaskAttempt.objects.create(task=task, agent_session_id=session)
    TaskAttempt.objects.filter(pk=attempt.pk).update(ended_at=ended_at)
    attempt.refresh_from_db()
    return attempt


class AttemptDurationTestCase(TestCase):
    def assert_pages_show(self, ticket: Ticket, expected: list[str]) -> None:
        assert _durations_by_page(ticket) == {"live": expected, "sessions": expected, "ticket": expected}

    def test_a_finished_attempt_is_timed_from_its_tasks_admission(self) -> None:
        now = timezone.now()
        task = _task(admitted_at=now - dt.timedelta(seconds=90))
        _finished_attempt(task, ended_at=now, session="s1")
        self.assert_pages_show(task.ticket, ["1m 30s"])

    def test_a_second_attempt_never_starts_before_the_previous_one_ended(self) -> None:
        now = timezone.now()
        task = _task(admitted_at=now - dt.timedelta(seconds=130))
        _finished_attempt(task, ended_at=now - dt.timedelta(seconds=40), session="s1")
        _finished_attempt(task, ended_at=now, session="s2")
        self.assert_pages_show(task.ticket, ["1m 30s", "40s"])

    def test_the_oldest_shown_attempt_still_sees_a_predecessor_outside_the_window(self) -> None:
        now = timezone.now()
        task = _task(admitted_at=now - dt.timedelta(hours=1))
        attempts = ATTEMPT_ROWS + 1
        for position in range(attempts):
            ended_at = now - dt.timedelta(seconds=(attempts - position) * 10)
            _finished_attempt(task, ended_at=ended_at, session=f"s{position:02d}")
        assert _durations_by_page(task.ticket)["ticket"] == ["10s"] * ATTEMPT_ROWS

    def test_a_task_without_admission_keeps_the_insert_to_end_figure(self) -> None:
        task = _task(admitted_at=None)
        attempt = TaskAttempt.objects.create(task=task, agent_session_id="s1")
        TaskAttempt.objects.filter(pk=attempt.pk).update(ended_at=attempt.started_at + dt.timedelta(seconds=95))
        self.assert_pages_show(task.ticket, ["1m 35s"])

    def test_an_attempt_that_ended_before_its_tasks_latest_admission_keeps_the_insert_to_end_figure(self) -> None:
        task = _task(admitted_at=None)
        attempt = TaskAttempt.objects.create(task=task, agent_session_id="s1")
        Task.objects.filter(pk=task.pk).update(admitted_at=attempt.started_at + dt.timedelta(seconds=200))
        TaskAttempt.objects.filter(pk=attempt.pk).update(ended_at=attempt.started_at + dt.timedelta(seconds=95))
        self.assert_pages_show(task.ticket, ["1m 35s"])

    def test_started_at_stays_the_insert_stamp(self) -> None:
        admitted = timezone.now() - dt.timedelta(seconds=90)
        task = _task(admitted_at=admitted)
        attempt = TaskAttempt.objects.create(task=task)
        assert attempt.started_at - admitted >= dt.timedelta(seconds=90)
