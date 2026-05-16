from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from teatree.core.models import IncomingEvent, ReplyDispatch, Session, Task, Ticket, Worktree


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

    def test_claim_next_pending_atomically_claims_oldest(self) -> None:
        """#786: claim_next_pending atomically selects+claims the oldest PENDING task."""
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket, agent_id="a")
        first = Task.objects.create(ticket=ticket, session=session, phase="reviewing")
        Task.objects.create(ticket=ticket, session=session, phase="reviewing")

        claimed = Task.objects.claim_next_pending(claimed_by="loop-slot")

        assert claimed is not None
        assert claimed.pk == first.pk  # FIFO (oldest first)
        assert claimed.status == Task.Status.CLAIMED
        assert claimed.claimed_by == "loop-slot"

    def test_claim_next_pending_never_returns_same_task_twice(self) -> None:
        """N4 at the manager level: a single PENDING task is never handed out twice.

        Two sequential claims (two ticks): the first claims it, the
        second gets None.
        """
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket, agent_id="a")
        only = Task.objects.create(ticket=ticket, session=session, phase="reviewing")

        a = Task.objects.claim_next_pending(claimed_by="tick-1")
        b = Task.objects.claim_next_pending(claimed_by="tick-2")

        assert a is not None
        assert a.pk == only.pk
        assert b is None  # nothing left to claim — no double-hand-out

    def test_claim_next_pending_none_when_no_pending(self) -> None:
        assert Task.objects.claim_next_pending(claimed_by="loop-slot") is None

    def test_claim_next_pending_skips_already_claimed(self) -> None:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket, agent_id="a")
        claimed = Task.objects.create(ticket=ticket, session=session, phase="reviewing")
        claimed.claim(claimed_by="someone")
        fresh = Task.objects.create(ticket=ticket, session=session, phase="reviewing")

        got = Task.objects.claim_next_pending(claimed_by="loop-slot")

        assert got is not None
        assert got.pk == fresh.pk  # skipped the already-claimed one


class TestReplyDispatchQuerySet(TestCase):
    def test_due_for_retry_orders_by_oldest_due_first(self) -> None:
        """``due_for_retry`` returns rows oldest-due-first by ``next_retry_at``.

        Not oldest-dispatched-first — this matches the
        ``Index(["status", "next_retry_at"])`` on the model.
        """
        event = IncomingEvent.objects.create(
            source=IncomingEvent.Source.SLACK,
            actor="U_ALICE",
            channel_ref="C-eng",
            thread_ref="t1",
            body="orig",
            idempotency_key="slack:e1",
        )
        now = timezone.now()
        early_dispatch_late_retry = ReplyDispatch.objects.create(
            event=event,
            target_ref="C-eng",
            action_name="post_in_thread",
            idempotency_key="k-early-dispatch",
            status=ReplyDispatch.Status.FAILED,
            dispatched_at=now - timedelta(hours=5),
            next_retry_at=now - timedelta(minutes=1),
        )
        late_dispatch_early_retry = ReplyDispatch.objects.create(
            event=event,
            target_ref="C-eng",
            action_name="post_in_thread",
            idempotency_key="k-late-dispatch",
            status=ReplyDispatch.Status.FAILED,
            dispatched_at=now - timedelta(hours=1),
            next_retry_at=now - timedelta(minutes=30),
        )

        # Ordered by next_retry_at: the one due longest ago comes first,
        # regardless of dispatched_at.
        assert list(ReplyDispatch.objects.due_for_retry(now)) == [
            late_dispatch_early_retry,
            early_dispatch_late_retry,
        ]
