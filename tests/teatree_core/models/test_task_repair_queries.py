from django.test import TestCase
from django.utils import timezone

from teatree.core.modelkit.task_failure_taxonomy import CANCELLED_PREFIX
from teatree.core.models import Session, Task, TaskAttempt, Ticket
from teatree.core.models.task_repair_queries import adjacent_work_attempts, repair_iterations, repair_observations
from teatree.core.models.usage_window_state import LIMIT_PARKED_PREFIX


class TestRepairQueries(TestCase):
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
