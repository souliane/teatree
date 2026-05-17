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


class TestReapStaleClaimsCasOnSqlite(TestCase):
    """#800 N5 — the reap must not spurious-fail a just-renewed lease.

    Same leak-free in-process interleave technique as
    ``TestClaimNextPendingConcurrencyOnSqlite`` (no threads, no
    file-backed DB — runs on the production SQLite test backend; the
    cross-thread file-SQLite harness is intractable vs the project-wide
    ``filterwarnings=error`` and is unnecessary given this).

    A live worker renews its lease *inside* ``reap_stale_claims``'s
    write boundary (the shared seam: ``QuerySet.update`` for the fixed
    conditional-UPDATE CAS, ``Task.save`` for the pre-fix
    select-then-``fail()``). Fixed: the reap's
    ``UPDATE ... WHERE status=CLAIMED AND lease_expires_at < now``
    re-evaluates at write time ⇒ the renewed row no longer matches ⇒
    survives (GREEN). Pre-fix: the row was scanned-as-stale and
    ``fail()``-ed unconditionally ⇒ the renewed task is spuriously
    FAILED (RED). Reverting the real ``reap_stale_claims`` body to the
    pre-fix shape flips this RED — the genuine mutation-revert proof on
    the real method.
    """

    def test_backend_is_sqlite(self) -> None:
        from django.db import connection  # noqa: PLC0415

        assert connection.vendor == "sqlite"

    def test_renew_inside_reap_write_boundary_spares_the_lease(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        from django.db.models import QuerySet  # noqa: PLC0415

        from teatree.core.models.task import Task as TaskModel  # noqa: PLC0415

        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket, agent_id="a")
        expired = timezone.now() - timedelta(seconds=30)
        stale = Task.objects.create(
            ticket=ticket,
            session=session,
            status=Task.Status.CLAIMED,
            claimed_by="worker-1",
            claimed_at=expired,
            lease_expires_at=expired,
            heartbeat_at=expired,
        )

        fired: list[str] = []
        real_update = QuerySet.update
        real_save = TaskModel.save

        def _renew_once() -> None:
            if fired:
                return
            fired.append("x")
            # The live worker heartbeats its still-valid claim inside the
            # reaper's critical section, before the reaper's write lands.
            Task.objects.filter(pk=stale.pk).update(
                lease_expires_at=timezone.now() + timedelta(seconds=300),
                heartbeat_at=timezone.now(),
            )

        def update_with_rival(self: object, *args: object, **kwargs: object) -> object:
            _renew_once()
            return real_update(self, *args, **kwargs)

        def save_with_rival(self: object, *args: object, **kwargs: object) -> object:
            _renew_once()
            return real_save(self, *args, **kwargs)

        with (
            patch.object(QuerySet, "update", update_with_rival),
            patch.object(TaskModel, "save", save_with_rival),
        ):
            Task.objects.reap_stale_claims()

        stale.refresh_from_db()
        # The lease was renewed before the reap's write committed: the CAS
        # re-evaluates lease_expires_at < now and skips it. A pre-fix
        # scan-then-unconditional-fail body would have FAILED it here.
        assert stale.status == Task.Status.CLAIMED, (
            f"renewed lease was spuriously reaped (pre-fix scan-then-fail behaviour): {stale.status!r}"
        )

    def test_reap_still_fails_a_genuinely_stale_lease(self) -> None:
        # Anti-trivial-vacuity guard: with no racing renew, the real
        # reap MUST fail the stale CLAIMED task (so the green above is
        # not passing simply because reap never fails anything).
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket, agent_id="a")
        expired = timezone.now() - timedelta(seconds=30)
        stale = Task.objects.create(
            ticket=ticket,
            session=session,
            status=Task.Status.CLAIMED,
            claimed_by="worker-1",
            claimed_at=expired,
            lease_expires_at=expired,
            heartbeat_at=expired,
        )

        Task.objects.reap_stale_claims()

        stale.refresh_from_db()
        assert stale.status == Task.Status.FAILED


class TestClaimNextPendingConcurrencyOnSqlite(TestCase):
    """#786 B1 keystone — double-CLAIM race closed on the PRODUCTION SQLite backend.

    SQLite (settings.py:88-89, ENGINE = django.db.backends.sqlite3) has
    ``has_select_for_update_skip_locked = False``, so
    ``select_for_update(skip_locked=True)`` is a silent no-op here. This
    test runs under that real SQLite test DB and reproduces a *concurrent
    interleave* (two ticks both past the candidate SELECT before either
    writes), NOT a sequential "second call finds it gone" (that stays
    green with or without real locking — the B1/N1 anti-vacuous trap).
    RED before the conditional-UPDATE CAS fix (both callers "claim" the
    same task → double-dispatch); GREEN after (the ``WHERE
    status='pending'`` guard lets exactly one writer win).
    """

    def test_backend_is_sqlite(self) -> None:
        # Pin the premise: if this ever runs on Postgres the race-shape
        # below no longer reflects the production backend.
        from django.db import connection  # noqa: PLC0415

        assert connection.vendor == "sqlite"
        assert connection.features.has_select_for_update_skip_locked is False

    def test_interleaved_ticks_claim_one_task_exactly_once(self) -> None:
        """The B1 interleave: a stale tick-1 must not double-claim tick-2's row.

        tick-1 has already selected the oldest pending candidate; tick-2
        then runs to completion and claims that SAME row; tick-1 resumes
        and attempts its claim write on the now-stale view.

        The seam is the write boundary shared by BOTH code shapes — the
        conditional ``QuerySet.update`` (fixed) and the row ``Task.save``
        (pre-fix select-then-save). The rival is fired exactly once, just
        before the first write executes, so tick-1's write lands on a row
        tick-2 already moved out of PENDING. Fixed: tick-1's
        ``WHERE status='pending'`` matches 0 rows ⇒ returns None (exactly
        one claimer). Pre-fix: tick-1's unconditional ``save`` clobbers ⇒
        BOTH "claim" the same task ⇒ assertion fails (RED).
        """
        from unittest.mock import patch  # noqa: PLC0415

        from django.db.models import QuerySet  # noqa: PLC0415

        from teatree.core.models.task import Task as TaskModel  # noqa: PLC0415

        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket, agent_id="a")
        only = Task.objects.create(ticket=ticket, session=session, phase="reviewing")

        fired: list[str] = []
        rival_result: list[object] = [None]
        real_update = QuerySet.update
        real_save = TaskModel.save

        def _fire_rival_once() -> None:
            if fired:
                return
            fired.append("x")
            # tick-2 runs fully (its own select + claim) inside tick-1's
            # critical section, before tick-1's first write commits.
            rival_result[0] = Task.objects.claim_next_pending(claimed_by="tick-2")

        def update_with_rival(self: object, *args: object, **kwargs: object) -> object:
            _fire_rival_once()
            return real_update(self, *args, **kwargs)

        def save_with_rival(self: object, *args: object, **kwargs: object) -> object:
            _fire_rival_once()
            return real_save(self, *args, **kwargs)

        with (
            patch.object(QuerySet, "update", update_with_rival),
            patch.object(TaskModel, "save", save_with_rival),
        ):
            caller1 = Task.objects.claim_next_pending(claimed_by="tick-1")

        rival = rival_result[0]
        only.refresh_from_db()
        # Exactly ONE of the two interleaved ticks claimed the single task;
        # the other got None. Never double-dispatched.
        claimers = [c for c in (caller1, rival) if c is not None]
        assert len(claimers) == 1, f"double-claim race NOT closed on SQLite: {caller1=} {rival=}"
        assert only.status == Task.Status.CLAIMED
        winner = claimers[0]
        assert winner.pk == only.pk
        assert only.claimed_by == winner.claimed_by
        assert only.claimed_by in {"tick-1", "tick-2"}


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
