from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from teatree.core.managers import WorktreeQuerySet
from teatree.core.models import Session, Task, Ticket, Worktree


class TestTicketQuerySet(TestCase):
    def test_in_flight_excludes_delivered_items(self) -> None:
        active = Ticket.objects.create(state=Ticket.State.STARTED)
        Ticket.objects.create(state=Ticket.State.DELIVERED)

        assert list(Ticket.objects.in_flight()) == [active]

    def test_in_flight_excludes_done_tracker_status(self) -> None:
        active = Ticket.objects.create(state=Ticket.State.STARTED, extra={"tracker_status": "In progress"})
        Ticket.objects.create(state=Ticket.State.STARTED, extra={"tracker_status": "Done"})

        assert list(Ticket.objects.in_flight()) == [active]


class TestWorktreeQuerySet(TestCase):
    def test_active_excludes_delivered_and_ignored_tickets(self) -> None:
        """Matches the worktrees panel filter so KPI count and table size agree."""
        active = Worktree.objects.create(
            ticket=Ticket.objects.create(state=Ticket.State.STARTED),
            repo_path="/tmp/backend",
            branch="active",
            state=Worktree.State.READY,
        )
        also_active = Worktree.objects.create(
            ticket=Ticket.objects.create(state=Ticket.State.STARTED),
            repo_path="/tmp/frontend",
            branch="just-created",
            state=Worktree.State.CREATED,
        )
        Worktree.objects.create(
            ticket=Ticket.objects.create(state=Ticket.State.DELIVERED),
            repo_path="/tmp/done",
            branch="done",
            state=Worktree.State.READY,
        )
        Worktree.objects.create(
            ticket=Ticket.objects.create(state=Ticket.State.IGNORED),
            repo_path="/tmp/ignored",
            branch="ignored",
            state=Worktree.State.READY,
        )

        assert list(Worktree.objects.active()) == [active, also_active]

    def test_done_ticket_states_match_ticket_enum(self) -> None:
        """Lock the hardcoded state strings to the Ticket.State enum values."""
        assert (
            Ticket.State.DELIVERED.value,
            Ticket.State.IGNORED.value,
        ) == WorktreeQuerySet._DONE_TICKET_STATES


class TestSessionQuerySet(TestCase):
    def test_for_agent_filters_by_agent_identifier(self) -> None:
        wanted = Session.objects.create(ticket=Ticket.objects.create(), agent_id="agent-1")
        Session.objects.create(ticket=Ticket.objects.create(), agent_id="agent-2")

        assert list(Session.objects.for_agent("agent-1")) == [wanted]


class TestTaskQuerySet(TestCase):
    def test_claimable_queries_respect_target_status_and_leases(self) -> None:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket, agent_id="agent-1")
        expired = timezone.now() - timedelta(minutes=1)
        future = timezone.now() + timedelta(minutes=5)

        sdk_ready = Task.objects.create(ticket=ticket, session=session)
        sdk_reclaimable = Task.objects.create(
            ticket=ticket,
            session=session,
            status=Task.Status.CLAIMED,
            claimed_by="worker-1",
            lease_expires_at=expired,
            heartbeat_at=expired,
        )
        Task.objects.create(
            ticket=ticket,
            session=session,
            status=Task.Status.CLAIMED,
            claimed_by="worker-2",
            lease_expires_at=future,
            heartbeat_at=timezone.now(),
        )
        user_ready = Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.INTERACTIVE,
        )

        sdk_tasks = list(Task.objects.claimable_for_headless())
        user_tasks = list(Task.objects.claimable_for_interactive())

        assert sdk_tasks == [sdk_ready, sdk_reclaimable]
        assert user_tasks == [user_ready]
