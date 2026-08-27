"""A scanner's admission decision and its write land together, or not at all.

Two task-queuing scanners split the "is one already in flight?" question from
the rows that answer it. ``PhaseCadence.queue_task`` documented the pair as one
transaction while the check ran in the CALLER, entirely before the atomic block
opened; ``ActiveTicketsScanner``'s short-describe enqueue committed the budget
spend and the Session independently of the Task they exist for, so a failed
insert left a spent budget and an orphan Session behind.
"""

from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.modelkit.phases import SHORT_DESCRIBE_PHASE
from teatree.core.models.session import Session
from teatree.core.models.task import Task
from teatree.core.models.ticket import Ticket
from teatree.loop.scanners.active_tickets import _enqueue_short_describe
from teatree.loop.scanners.phase_cadence import PhaseCadence

OVERLAY = "acme"
PLACEHOLDER = "backlog-sweep://acme"


class TestQueueTaskRechecksInFlight(TestCase):
    """``queue_task`` is self-guarding — the caller's pre-check is an early-out."""

    def _cadence(self) -> PhaseCadence:
        return PhaseCadence(OVERLAY, phase="backlog_sweep", cadence_hours=168)

    def _queue(self) -> Task | None:
        return self._cadence().queue_task(
            placeholder_issue_url=PLACEHOLDER,
            agent_id="backlog-sweep-acme",
            execution_reason="cadence",
            log_label="BacklogSweepScanner",
        )

    def test_a_second_queue_without_the_caller_pre_check_is_refused(self) -> None:
        first = self._queue()

        second = self._queue()

        assert first is not None
        assert second is None, "the in-flight guard must live inside the write transaction"
        assert Task.objects.filter(phase="backlog_sweep").count() == 1

    def test_a_terminal_previous_run_does_not_block_the_next_window(self) -> None:
        first = self._queue()
        assert first is not None
        Task.objects.filter(pk=first.pk).update(status=Task.Status.COMPLETED)

        assert self._queue() is not None


class TestShortDescribeEnqueueIsAtomic(TestCase):
    """A failed Task insert rolls back the budget spend and the Session with it."""

    def _ticket(self) -> Ticket:
        return Ticket.objects.create(
            overlay=OVERLAY,
            issue_url="https://x/1",
            state="started",
            extra={"issue_title": "Cached tracker title"},
        )

    def test_failed_task_insert_leaves_no_spent_budget_or_orphan_session(self) -> None:
        ticket = self._ticket()

        with (
            patch.object(Task.objects, "create", side_effect=RuntimeError("insert failed")),
            pytest.raises(RuntimeError),
        ):
            _enqueue_short_describe(ticket)

        ticket.refresh_from_db()
        assert (ticket.extra or {}).get("phase_attempts", {}).get(SHORT_DESCRIBE_PHASE, 0) == 0
        assert not Session.objects.filter(ticket=ticket).exists()

    def test_the_happy_path_still_enqueues(self) -> None:
        ticket = self._ticket()

        _enqueue_short_describe(ticket)

        assert Task.objects.filter(ticket=ticket, phase=SHORT_DESCRIBE_PHASE).count() == 1
