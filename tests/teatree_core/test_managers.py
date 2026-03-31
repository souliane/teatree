from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from teatree.core.models import Session, Task, Ticket, Worktree


class TestTicketQuerySet(TestCase):
    def test_in_flight_excludes_delivered_items(self) -> None:
        active = Ticket.objects.create(state=Ticket.State.STARTED)
        Ticket.objects.create(state=Ticket.State.DELIVERED)

        assert list(Ticket.objects.in_flight()) == [active]


class TestWorktreeQuerySet(TestCase):
    def test_active_excludes_created_items(self) -> None:
        active = Worktree.objects.create(
            ticket=Ticket.objects.create(),
            repo_path="/tmp/backend",
            branch="active",
            state=Worktree.State.READY,
        )
        Worktree.objects.create(
            ticket=Ticket.objects.create(),
            repo_path="/tmp/frontend",
            branch="created",
            state=Worktree.State.CREATED,
        )

        assert list(Worktree.objects.active()) == [active]


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
