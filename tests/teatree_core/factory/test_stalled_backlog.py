"""What counts as admitted work left with no execution path (souliane/teatree#4704)."""

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from teatree.core.factory.parked_tickets import AGED_PARK
from teatree.core.factory.stalled_backlog import STALLED_BACKLOG_WINDOW, stranded_ticket_count
from teatree.core.modelkit.task_failure_taxonomy import HALT_STAMP, FailureKind
from teatree.core.models import PullRequest, Session, Task, Ticket


class TestStrandedTicketCount(TestCase):
    def _queued(self, n: int, *, status: str = Task.Status.FAILED, age: timedelta = timedelta(hours=3)) -> None:
        for i in range(n):
            ticket = Ticket.objects.create(
                issue_url=f"https://example.com/s/{status}/{i}", state=Ticket.State.WORK_STARTED
            )
            session = Session.objects.create(overlay="test", ticket=ticket)
            task = Task.objects.create(ticket=ticket, session=session, status=status)
            Task.objects.filter(pk=task.pk).update(created_at=timezone.now() - age)

    def test_a_failed_task_outside_the_window_is_stranded(self) -> None:
        self._queued(2)
        assert stranded_ticket_count() == 2

    def test_a_recent_halted_failure_still_counts_toward_a_dead_lane(self) -> None:
        self._queued(2)
        Task.objects.update(execution_reason=HALT_STAMP)
        assert stranded_ticket_count() == 2

    def test_a_halted_failure_leaves_the_count_once_the_owner_is_asked(self) -> None:
        self._queued(2, age=AGED_PARK + timedelta(days=1))
        Task.objects.update(execution_reason=HALT_STAMP)
        assert stranded_ticket_count() == 0

    def test_an_aged_halted_failure_the_owner_is_not_asked_about_keeps_counting(self) -> None:
        self._queued(2, age=AGED_PARK + timedelta(days=1))
        Task.objects.update(execution_reason=HALT_STAMP)
        for ticket in Ticket.objects.all():
            PullRequest.objects.create(ticket=ticket, url=f"https://example.com/pull/{ticket.pk}", repo="o/r", iid="1")
        assert stranded_ticket_count() == 2

    def test_an_aged_failure_nobody_parked_keeps_counting(self) -> None:
        self._queued(2, age=AGED_PARK + timedelta(days=1))
        assert stranded_ticket_count() == 2

    def test_a_cancelled_failure_is_a_decision_not_a_stranding(self) -> None:
        self._queued(2)
        Task.objects.update(failure_kind=FailureKind.CANCELLED)
        assert stranded_ticket_count() == 0

    def test_newest_task_uses_creation_time_before_primary_key(self) -> None:
        self._queued(1)
        ticket = Ticket.objects.get()
        later_pk = Task.objects.create(ticket=ticket, session=ticket.sessions.first(), status=Task.Status.COMPLETED)
        Task.objects.filter(pk=later_pk.pk).update(created_at=timezone.now() - timedelta(hours=4))
        assert stranded_ticket_count() == 1

    def test_synthetic_ticket_still_counts_as_a_stalled_lane(self) -> None:
        self._queued(1)
        Ticket.objects.update(issue_url="loop://test")
        assert stranded_ticket_count() == 1

    def test_an_aged_halted_failure_on_a_synthetic_ticket_keeps_counting_because_no_card_covers_it(self) -> None:
        self._queued(2, age=AGED_PARK + timedelta(days=1))
        Task.objects.update(execution_reason=HALT_STAMP)
        for ticket in Ticket.objects.all():
            Ticket.objects.filter(pk=ticket.pk).update(issue_url=f"loop://test/{ticket.pk}")
        assert stranded_ticket_count() == 2

    def test_a_recent_failure_is_still_in_flight(self) -> None:
        # The window exists so a retry that has not been requeued yet never counts.
        self._queued(2, age=timedelta(minutes=5))
        assert stranded_ticket_count() == 0

    def test_a_completed_task_is_a_human_working_the_ticket(self) -> None:
        self._queued(2, status=Task.Status.COMPLETED)
        assert stranded_ticket_count() == 0

    def test_a_pending_retry_means_the_lane_is_alive(self) -> None:
        # The retry is aged INTO the window too, so only the in-flight exclusion can
        # discount these — a fresh one would be discounted by the window instead and the
        # exclusion would go untested. A wedged queue is `_stale_tick_signals`' signal.
        self._queued(2)
        for ticket in Ticket.objects.filter(state=Ticket.State.WORK_STARTED):
            retry = Task.objects.create(ticket=ticket, session=ticket.sessions.first(), status=Task.Status.PENDING)
            Task.objects.filter(pk=retry.pk).update(created_at=timezone.now() - STALLED_BACKLOG_WINDOW * 2)
        assert stranded_ticket_count() == 0

    def test_a_ticket_that_was_never_queued_is_not_stranded(self) -> None:
        for i in range(2):
            Ticket.objects.create(issue_url=f"https://example.com/nq/{i}", state=Ticket.State.WORK_STARTED)
        assert stranded_ticket_count() == 0

    def test_a_ticket_past_started_is_making_progress(self) -> None:
        self._queued(2)
        Ticket.objects.update(state=Ticket.State.CODED)
        assert stranded_ticket_count() == 0

    def test_a_later_completion_retires_an_older_failure(self) -> None:
        # The tell is the NEWEST task having failed. An older failure that a later
        # completed task superseded is not a ticket without an execution path.
        self._queued(2)
        for ticket in Ticket.objects.filter(state=Ticket.State.WORK_STARTED):
            done = Task.objects.create(ticket=ticket, session=ticket.sessions.first(), status=Task.Status.COMPLETED)
            Task.objects.filter(pk=done.pk).update(created_at=timezone.now() - timedelta(hours=2, minutes=30))
        assert stranded_ticket_count() == 0
