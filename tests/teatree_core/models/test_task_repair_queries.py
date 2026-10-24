import sqlite3

from django.db import connection
from django.test import TestCase
from django.utils import timezone

from teatree.core.modelkit.task_failure_taxonomy import (
    CANCELLED_PREFIX,
    LEASE_EXPIRED_PREFIX,
    PLAN_STALE_PREFIX,
    SUPERSEDED_PREFIX,
)
from teatree.core.models import Session, Task, TaskAttempt, Ticket
from teatree.core.models.task_repair_queries import adjacent_work_attempts, repair_iterations, repair_observations
from teatree.core.models.usage_window_state import LIMIT_PARKED_PREFIX, REVIEW_HELD_PREFIX
from teatree.llm.anthropic_limits import LimitCause, LimitMatch

_CAPACITY_DIP = LimitMatch(phrase="5-hour limit", cause=LimitCause.SUBSCRIPTION_SESSION).as_reason()


def _failed(task: Task, error: str, *, fallback_from_attempt: TaskAttempt | None = None) -> TaskAttempt:
    return TaskAttempt.objects.create(
        task=task,
        error=error,
        exit_code=1,
        ended_at=timezone.now(),
        fallback_from_attempt=fallback_from_attempt,
    )


class TestRepairQueries(TestCase):
    def _phase_task(self) -> Task:
        ticket = Ticket.objects.create()
        return Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase="coding")

    def test_successor_outside_the_input_window_still_supersedes_its_source(self) -> None:
        ticket = Ticket.objects.create()
        task = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase="coding")
        source = TaskAttempt.objects.create(task=task, error="route refusal", exit_code=1, ended_at=timezone.now())
        TaskAttempt.objects.create(
            task=task,
            exit_code=0,
            ended_at=timezone.now(),
            fallback_from_attempt=source,
        )

        assert repair_iterations(TaskAttempt.objects.filter(pk=source.pk)) == []

    def test_only_a_work_successor_supersedes_and_audit_breaks_stall_adjacency(self) -> None:
        ticket = Ticket.objects.create()
        task = Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase="coding")
        source = TaskAttempt.objects.create(task=task, error="route refusal", exit_code=1, ended_at=timezone.now())
        TaskAttempt.objects.create(
            task=task,
            error=f"{LIMIT_PARKED_PREFIX}capacity unavailable",
            exit_code=1,
            ended_at=timezone.now(),
            fallback_from_attempt=source,
        )
        TaskAttempt.objects.create(
            task=task,
            error=f"{CANCELLED_PREFIX}operator paused retry",
            exit_code=1,
            ended_at=timezone.now(),
        )
        latest = TaskAttempt.objects.create(task=task, error="route refusal", exit_code=1, ended_at=timezone.now())
        queryset = TaskAttempt.objects.filter(task__ticket=ticket)

        work = repair_iterations(queryset)
        observations = repair_observations(queryset)

        assert [attempt.pk for attempt in work] == [source.pk, latest.pk]
        assert adjacent_work_attempts(work, observations) == []

    def test_a_review_hold_is_a_park_and_never_a_repair_iteration(self) -> None:
        task = self._phase_task()
        held = _failed(task, f"{REVIEW_HELD_PREFIX}the review waits")
        worked = _failed(task, "route refusal")
        queryset = TaskAttempt.objects.filter(task=task)

        assert list(queryset.parks()) == [held]
        assert [attempt.pk for attempt in repair_iterations(queryset)] == [worked.pk]
        assert [attempt.pk for attempt in repair_observations(queryset)] == [worked.pk]

    def test_a_capacity_dip_between_identical_failures_keeps_them_adjacent(self) -> None:
        task = self._phase_task()
        first = _failed(task, "same defect")
        dip = _failed(task, _CAPACITY_DIP)
        second = _failed(task, "same defect")
        queryset = TaskAttempt.objects.filter(task=task)

        work = repair_iterations(queryset)
        observations = repair_observations(queryset)

        assert [attempt.pk for attempt in observations] == [first.pk, second.pk]
        assert dip.pk not in {attempt.pk for attempt in work}
        assert adjacent_work_attempts(work, observations) == [first, second]

    def test_an_audit_row_between_identical_failures_breaks_adjacency_until_the_next_failure(self) -> None:
        task = self._phase_task()
        _failed(task, "same defect")
        _failed(task, f"{LEASE_EXPIRED_PREFIX}worker died")
        _failed(task, "same defect")
        queryset = TaskAttempt.objects.filter(task=task)
        assert adjacent_work_attempts(repair_iterations(queryset), repair_observations(queryset)) == []

        third = _failed(task, "same defect")

        work = repair_iterations(queryset)
        pair = adjacent_work_attempts(work, repair_observations(queryset))
        assert [attempt.pk for attempt in pair] == [work[-2].pk, third.pk]

    def test_only_a_work_successor_supersedes_its_source(self) -> None:
        for audit_or_dip in (
            f"{CANCELLED_PREFIX}operator paused retry",
            f"{SUPERSEDED_PREFIX}rework",
            f"{PLAN_STALE_PREFIX}base moved",
            f"{LEASE_EXPIRED_PREFIX}worker died",
            _CAPACITY_DIP,
        ):
            with self.subTest(successor=audit_or_dip):
                task = self._phase_task()
                source = _failed(task, "route refusal")
                _failed(task, audit_or_dip, fallback_from_attempt=source)

                assert repair_iterations(TaskAttempt.objects.filter(task=task)) == [source]

    def test_a_work_successor_supersedes_its_source(self) -> None:
        task = self._phase_task()
        source = _failed(task, "route refusal")
        successor = _failed(task, "second refusal", fallback_from_attempt=source)

        assert repair_iterations(TaskAttempt.objects.filter(task=task)) == [successor]

    def test_the_work_filter_binds_no_variable_per_attempt(self) -> None:
        variable_limit = 50
        task = self._phase_task()
        previous = None
        for index in range(variable_limit + 10):
            previous = _failed(task, f"route refusal {index}", fallback_from_attempt=previous)
        connection.ensure_connection()
        sqlite_connection = connection.connection
        old_limit = sqlite_connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, variable_limit)
        self.addCleanup(sqlite_connection.setlimit, sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, old_limit)

        work = repair_iterations(TaskAttempt.objects.filter(task=task))

        assert work == [previous]
