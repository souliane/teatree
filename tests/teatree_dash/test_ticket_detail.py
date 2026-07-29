"""The drawer read model keeps the newest slice of each unbounded history (#3873).

A ticket the factory worked for weeks accumulates thousands of transitions and
attempts. Reading all of them is what turned the drawer into a multi-megabyte
response the operator experienced as a card that would not open, so each panel
takes the most recent rows and reports the total it truncated from.
"""

from django.test import TestCase

from teatree.core.models.task_attempt import TaskAttempt
from teatree.core.models.ticket import Ticket
from teatree.core.models.transition import TicketTransition
from teatree.dash.ticket_detail import ATTEMPT_ROWS, TASK_ROWS, TRANSITION_ROWS, build_ticket_detail
from tests.factories import TaskFactory, TicketFactory

State = Ticket.State


class DrawerHistoryIsCappedTestCase(TestCase):
    EXTRA = 7

    @classmethod
    def setUpTestData(cls) -> None:
        cls.ticket = TicketFactory(state=State.STARTED)
        TicketTransition.objects.bulk_create(
            TicketTransition(ticket=cls.ticket, from_state=State.SCOPED, to_state=State.STARTED, triggered_by="start")
            for _ in range(TRANSITION_ROWS + cls.EXTRA)
        )
        cls.tasks = [TaskFactory(ticket=cls.ticket, phase="coding") for _ in range(TASK_ROWS + cls.EXTRA)]
        TaskAttempt.objects.bulk_create(
            TaskAttempt(task=cls.tasks[-1], execution_target="headless") for _ in range(ATTEMPT_ROWS + cls.EXTRA)
        )

    def test_transition_history_keeps_the_cap_and_reports_the_total(self) -> None:
        detail = build_ticket_detail(self.ticket.pk)
        assert len(detail.transitions) == TRANSITION_ROWS
        assert detail.transitions_total == TRANSITION_ROWS + self.EXTRA

    def test_transition_history_still_reads_oldest_first(self) -> None:
        rows = build_ticket_detail(self.ticket.pk).transitions
        assert [row.created_at for row in rows] == sorted(row.created_at for row in rows)

    def test_task_list_keeps_the_newest_tasks_and_reports_the_total(self) -> None:
        detail = build_ticket_detail(self.ticket.pk)
        assert [task.task_id for task in detail.tasks] == [task.pk for task in reversed(self.tasks[-TASK_ROWS:])]
        assert detail.tasks_total == TASK_ROWS + self.EXTRA

    def test_attempts_are_capped_per_task_not_across_the_drawer(self) -> None:
        newest = build_ticket_detail(self.ticket.pk).tasks[0]
        assert len(newest.attempts) == ATTEMPT_ROWS
        assert newest.attempts_total == ATTEMPT_ROWS + self.EXTRA

    def test_the_newest_attempts_are_the_ones_kept(self) -> None:
        newest = build_ticket_detail(self.ticket.pk).tasks[0]
        kept = [attempt.attempt_id for attempt in newest.attempts]
        every = list(
            TaskAttempt.objects.filter(task=self.tasks[-1]).order_by("-pk").values_list("pk", flat=True),
        )
        assert kept == every[:ATTEMPT_ROWS]
