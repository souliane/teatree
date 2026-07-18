from datetime import timedelta

import pytest
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


class TestIncomingEventQuerySet(TestCase):
    def _slack(self, *, channel: str, thread_ref: str, key: str) -> IncomingEvent:
        return IncomingEvent.objects.create(
            source=IncomingEvent.Source.SLACK,
            channel_ref=channel,
            thread_ref=thread_ref,
            idempotency_key=key,
        )

    def test_active_dm_thread_returns_most_recent_thread_ref_for_channel(self) -> None:
        self._slack(channel="D1", thread_ref="1700000000.0001", key="slack:a")
        self._slack(channel="D1", thread_ref="1700000099.0009", key="slack:b")

        assert IncomingEvent.objects.active_dm_thread(channel="D1") == "1700000099.0009"

    def test_active_dm_thread_scopes_to_the_requested_channel(self) -> None:
        self._slack(channel="D-other", thread_ref="9999999999.0001", key="slack:other")
        self._slack(channel="D1", thread_ref="1700000000.0001", key="slack:mine")

        assert IncomingEvent.objects.active_dm_thread(channel="D1") == "1700000000.0001"

    def test_active_dm_thread_ignores_non_slack_sources(self) -> None:
        IncomingEvent.objects.create(
            source=IncomingEvent.Source.GITHUB,
            channel_ref="D1",
            thread_ref="github-ref",
            idempotency_key="github:1",
        )

        assert IncomingEvent.objects.active_dm_thread(channel="D1") == ""

    def test_active_dm_thread_empty_when_no_event_for_channel(self) -> None:
        self._slack(channel="D-other", thread_ref="1700000000.0001", key="slack:other")

        assert IncomingEvent.objects.active_dm_thread(channel="D1") == ""

    def test_active_dm_thread_empty_channel_matches_nothing(self) -> None:
        self._slack(channel="D1", thread_ref="1700000000.0001", key="slack:a")

        assert IncomingEvent.objects.active_dm_thread(channel="") == ""


class TestSessionQuerySet(TestCase):
    def test_for_agent_filters_by_agent_identifier(self) -> None:
        wanted = Session.objects.create(ticket=Ticket.objects.create(), agent_id="agent-1")
        Session.objects.create(ticket=Ticket.objects.create(), agent_id="agent-2")

        assert list(Session.objects.for_agent("agent-1")) == [wanted]


class TestTaskQuerySet(TestCase):
    def test_for_claude_session_scopes_to_matching_agent_id_newest_first(self) -> None:
        ticket = Ticket.objects.create()
        mine = Session.objects.create(ticket=ticket, agent_id="claude-abc")
        other = Session.objects.create(ticket=ticket, agent_id="claude-xyz")
        first = Task.objects.create(ticket=ticket, session=mine, phase="coding")
        second = Task.objects.create(ticket=ticket, session=mine, phase="testing")
        Task.objects.create(ticket=ticket, session=other, phase="coding")

        assert list(Task.objects.for_claude_session("claude-abc")) == [second, first]

    def test_for_claude_session_empty_id_matches_nothing(self) -> None:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket, agent_id="claude-abc")
        Task.objects.create(ticket=ticket, session=session)

        assert list(Task.objects.for_claude_session("")) == []

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

    def test_claim_next_pending_session_defaults_to_empty(self) -> None:
        """#1917 inert default: a claim with no session leaves ``claimed_by_session`` empty."""
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket, agent_id="a")
        Task.objects.create(ticket=ticket, session=session, phase="reviewing")

        claimed = Task.objects.claim_next_pending(claimed_by="loop-slot")

        assert claimed is not None
        assert claimed.claimed_by == "loop-slot"
        assert claimed.claimed_by_session == ""

    def test_claim_next_pending_records_session_orthogonally_to_claimed_by(self) -> None:
        """#1917: a supplied session rides the claim; the role-label ``claimed_by`` is independent."""
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket, agent_id="a")
        Task.objects.create(ticket=ticket, session=session, phase="reviewing")

        claimed = Task.objects.claim_next_pending(claimed_by="loop-slot", claimed_by_session="sess-A")

        assert claimed is not None
        assert claimed.claimed_by == "loop-slot"
        assert claimed.claimed_by_session == "sess-A"


class TestActiveClaimExists(TestCase):
    """#1760: the deferred-reinstall drain reads this to defer while a unit runs."""

    def _task(self, *, status: str, lease_offset_seconds: int | None) -> Task:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket, agent_id="a")
        lease = None if lease_offset_seconds is None else timezone.now() + timedelta(seconds=lease_offset_seconds)
        return Task.objects.create(
            ticket=ticket,
            session=session,
            status=status,
            lease_expires_at=lease,
        )

    def test_false_when_no_tasks(self) -> None:
        assert Task.objects.active_claim_exists() is False

    def test_true_for_a_live_claimed_lease(self) -> None:
        self._task(status=Task.Status.CLAIMED, lease_offset_seconds=300)

        assert Task.objects.active_claim_exists() is True

    def test_false_for_an_expired_claimed_lease(self) -> None:
        self._task(status=Task.Status.CLAIMED, lease_offset_seconds=-10)

        assert Task.objects.active_claim_exists() is False

    def test_false_for_a_pending_task(self) -> None:
        self._task(status=Task.Status.PENDING, lease_offset_seconds=300)

        assert Task.objects.active_claim_exists() is False

    def test_false_for_a_completed_task(self) -> None:
        self._task(status=Task.Status.COMPLETED, lease_offset_seconds=300)

        assert Task.objects.active_claim_exists() is False


class TestReclaimOrphanedClaims(TestCase):
    """#652 — an orphaned in-flight task must be *taken over*, not failed.

    When the Claude session driving the loop exits mid-task, its CLAIMED
    Task stops heartbeating and the lease expires. The pre-#652 behaviour
    (``reap_stale_claims``) transitions that row CLAIMED→FAILED, which
    needs a manual ``reopen()`` before any other open session can resume
    it — so the loop silently stalls. ``reclaim_orphaned_claims`` instead
    returns the expired-lease CLAIMED row to PENDING so the next tick's
    ``PendingTasksScanner`` (in any still-open session) re-surfaces it and
    the loop continues. Same backend-agnostic conditional-UPDATE CAS as
    ``claim_next_pending`` — fastest tick wins, losers update 0 rows.
    """

    def test_backend_is_sqlite(self) -> None:
        from django.db import connection  # noqa: PLC0415

        assert connection.vendor == "sqlite"

    def test_expired_claimed_task_is_returned_to_pending_not_failed(self) -> None:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket, agent_id="a")
        expired = timezone.now() - timedelta(seconds=30)
        orphan = Task.objects.create(
            ticket=ticket,
            session=session,
            status=Task.Status.CLAIMED,
            claimed_by="pid-99999",
            claimed_at=expired,
            lease_expires_at=expired,
            heartbeat_at=expired,
        )

        reclaimed = Task.objects.reclaim_orphaned_claims()

        orphan.refresh_from_db()
        assert reclaimed == 1
        # Takeover, NOT fail: the row is claimable again so another open
        # session's loop tick resumes it (issue #652 "fastest wins").
        assert orphan.status == Task.Status.PENDING, (
            f"orphaned task was not taken over (got {orphan.status!r}) — the loop stalls until a manual reopen()"
        )
        assert orphan.claimed_by == ""
        assert orphan.claimed_at is None
        assert orphan.lease_expires_at is None
        assert orphan.heartbeat_at is None

    def test_reclaim_clears_claimed_by_session(self) -> None:
        """#1917: the session attribution is cleared alongside ``claimed_by`` on reclaim.

        A row taken over by the orphan sweep is claimable again, so a stale
        session attribution must not survive — symmetric with ``claimed_by``.
        """
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket, agent_id="a")
        expired = timezone.now() - timedelta(seconds=30)
        orphan = Task.objects.create(
            ticket=ticket,
            session=session,
            status=Task.Status.CLAIMED,
            claimed_by="pid-99999",
            claimed_by_session="sess-dead",
            claimed_at=expired,
            lease_expires_at=expired,
            heartbeat_at=expired,
        )

        reclaimed = Task.objects.reclaim_orphaned_claims()

        orphan.refresh_from_db()
        assert reclaimed == 1
        assert orphan.status == Task.Status.PENDING
        assert orphan.claimed_by == ""
        assert orphan.claimed_by_session == ""

    def test_a_live_claim_is_left_untouched(self) -> None:
        # Anti-vacuity: a healthy in-flight task (lease in the future)
        # must NOT be yanked away from its live owner.
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket, agent_id="a")
        future = timezone.now() + timedelta(seconds=300)
        live = Task.objects.create(
            ticket=ticket,
            session=session,
            status=Task.Status.CLAIMED,
            claimed_by="pid-1",
            claimed_at=timezone.now(),
            lease_expires_at=future,
            heartbeat_at=timezone.now(),
        )

        reclaimed = Task.objects.reclaim_orphaned_claims()

        live.refresh_from_db()
        assert reclaimed == 0
        assert live.status == Task.Status.CLAIMED
        assert live.claimed_by == "pid-1"

    def test_terminal_tasks_are_not_resurrected(self) -> None:
        # A COMPLETED/FAILED task must never be dragged back to PENDING by
        # the orphan sweep even if its (stale) lease columns are in the past.
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket, agent_id="a")
        expired = timezone.now() - timedelta(seconds=30)
        done = Task.objects.create(
            ticket=ticket,
            session=session,
            status=Task.Status.COMPLETED,
            lease_expires_at=expired,
        )
        failed = Task.objects.create(
            ticket=ticket,
            session=session,
            status=Task.Status.FAILED,
            lease_expires_at=expired,
        )

        reclaimed = Task.objects.reclaim_orphaned_claims()

        done.refresh_from_db()
        failed.refresh_from_db()
        assert reclaimed == 0
        assert done.status == Task.Status.COMPLETED
        assert failed.status == Task.Status.FAILED

    def test_concurrent_ticks_reclaim_one_orphan_exactly_once(self) -> None:
        """#652 fastest-wins on the PRODUCTION SQLite backend.

        Two ticks both observe the orphan; the conditional-UPDATE CAS
        (``WHERE status=CLAIMED AND lease_expires_at < now``) lets exactly
        one tick's UPDATE match — the other updates 0 rows. Same in-process
        interleave technique as ``TestClaimNextPendingConcurrencyOnSqlite``
        so it runs under the real SQLite test DB where
        ``select_for_update(skip_locked=True)`` is a silent no-op.
        """
        from unittest.mock import patch  # noqa: PLC0415

        from django.db.models import QuerySet  # noqa: PLC0415

        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket, agent_id="a")
        expired = timezone.now() - timedelta(seconds=30)
        Task.objects.create(
            ticket=ticket,
            session=session,
            status=Task.Status.CLAIMED,
            claimed_by="pid-dead",
            claimed_at=expired,
            lease_expires_at=expired,
            heartbeat_at=expired,
        )

        fired: list[str] = []
        rival_result: list[int] = [-1]
        real_update = QuerySet.update

        def _fire_rival_once() -> None:
            if fired:
                return
            fired.append("x")
            rival_result[0] = Task.objects.reclaim_orphaned_claims()

        def update_with_rival(self: object, *args: object, **kwargs: object) -> object:
            _fire_rival_once()
            return real_update(self, *args, **kwargs)

        with patch.object(QuerySet, "update", update_with_rival):
            caller1 = Task.objects.reclaim_orphaned_claims()

        rival = rival_result[0]
        # Exactly one tick reclaimed the single orphan (count 1); the other
        # raced inside the first's write boundary and updated 0 rows.
        assert sorted([caller1, rival]) == [0, 1], (
            f"orphan reclaimed by both ticks (not fastest-wins): {caller1=} {rival=}"
        )


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

    def test_reap_clears_claimed_by_session(self) -> None:
        """#1917: the session attribution is cleared alongside ``claimed_by`` on reap."""
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket, agent_id="a")
        expired = timezone.now() - timedelta(seconds=30)
        stale = Task.objects.create(
            ticket=ticket,
            session=session,
            status=Task.Status.CLAIMED,
            claimed_by="worker-1",
            claimed_by_session="sess-dead",
            claimed_at=expired,
            lease_expires_at=expired,
            heartbeat_at=expired,
        )

        Task.objects.reap_stale_claims()

        stale.refresh_from_db()
        assert stale.status == Task.Status.FAILED
        assert stale.claimed_by == ""
        assert stale.claimed_by_session == ""


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


class TestTaskClaimAtomic(TestCase):
    """``Task.claim`` is an atomic CAS — the create-and-assign-to-one-session claim.

    The owner-reported bug: two concurrent Claude sessions picked up the SAME
    unit because ``Task.claim`` was a read-then-write — ``select_for_update()``
    (a silent no-op on the production SQLite backend, ``has_select_for_update``
    is ``False``) then an UNCONDITIONAL ``save()`` with no affected-row guard.
    Both sessions passed the in-Python check on the same stale view and both
    wrote. It is now a single guarded ``UPDATE ... WHERE pk AND <claimable>``
    whose row count is the CAS token, the same backend-agnostic shape the
    sibling ``claim_next_pending`` / ``reap_stale_claims`` paths already use.

    Three directions, all pinned here:

    * race — two concurrent claims on one available unit ⇒ EXACTLY one wins;
    * dead-lease reclaim — a unit whose owner's lease lapsed is re-claimable;
    * live-lease protection — a unit with a FRESH lease is NOT stolen.
    """

    def _task(self) -> Task:
        ticket = Ticket.objects.create()
        session = Session.objects.create(ticket=ticket, agent_id="a")
        return Task.objects.create(ticket=ticket, session=session, phase="reviewing")

    def test_backend_is_sqlite(self) -> None:
        # Pin the premise: the race shape below reflects the PRODUCTION backend,
        # where ``select_for_update`` is a no-op — the whole reason the previous
        # read-then-write raced.
        from django.db import connection  # noqa: PLC0415

        assert connection.vendor == "sqlite"
        assert connection.features.has_select_for_update is False

    def test_two_concurrent_claims_on_one_task_exactly_one_wins(self) -> None:
        """The race: session A and session B both claim the same PENDING unit.

        Same deterministic single-connection interleave as
        ``TestClaimNextPendingConcurrencyOnSqlite``: session B runs its FULL
        claim inside session A's critical section, just before A's write
        commits, so A's write lands on a row B already moved to CLAIMED. The
        seam patches BOTH write primitives (``QuerySet.update`` — the fixed
        CAS — and ``Task.save`` — the pre-fix read-then-write), so the test is
        RED on the buggy code (both "win") and GREEN on the fix (the CAS
        ``WHERE`` lets exactly one writer match).
        """
        from unittest.mock import patch  # noqa: PLC0415

        from django.db.models import QuerySet  # noqa: PLC0415

        from teatree.core.models.errors import InvalidTransitionError  # noqa: PLC0415
        from teatree.core.models.task import Task as TaskModel  # noqa: PLC0415

        row = self._task()
        session_a = Task.objects.get(pk=row.pk)
        session_b = Task.objects.get(pk=row.pk)

        fired: list[str] = []
        rival_won = [False]
        real_update = QuerySet.update
        real_save = TaskModel.save

        def _fire_rival_once() -> None:
            if fired:
                return
            fired.append("x")
            try:
                session_b.claim(claimed_by="session-B")
                rival_won[0] = True
            except InvalidTransitionError:
                rival_won[0] = False

        def update_with_rival(self: object, *args: object, **kwargs: object) -> object:
            _fire_rival_once()
            return real_update(self, *args, **kwargs)

        def save_with_rival(self: object, *args: object, **kwargs: object) -> object:
            _fire_rival_once()
            return real_save(self, *args, **kwargs)

        caller_won = False
        with (
            patch.object(QuerySet, "update", update_with_rival),
            patch.object(TaskModel, "save", save_with_rival),
        ):
            try:
                session_a.claim(claimed_by="session-A")
                caller_won = True
            except InvalidTransitionError:
                caller_won = False

        row.refresh_from_db()
        # EXACTLY one of the two interleaved sessions claimed the single unit.
        assert (caller_won, rival_won[0]).count(True) == 1, (
            f"double-claim race NOT closed on SQLite: {caller_won=} {rival_won[0]=}"
        )
        assert row.status == Task.Status.CLAIMED
        assert row.claimed_by in {"session-A", "session-B"}

    def test_claim_reclaims_a_dead_sessions_expired_lease(self) -> None:
        """Dead-lease reclaim: a unit whose owner's lease lapsed is re-claimable.

        Session A claims the unit, then dies — its lease is forced into the
        past. The next healthy session B claims the SAME unit directly via
        ``Task.claim``; the CAS ``<claimable>`` predicate admits a CLAIMED row
        whose lease is expired, so B reclaims it (no duplicate unit, no manual
        reopen).
        """
        task = self._task()
        task.claim(claimed_by="session-A", lease_seconds=300)
        # Session A dies: its lease lapses (no more heartbeats).
        task.lease_expires_at = timezone.now() - timedelta(seconds=10)
        task.save(update_fields=["lease_expires_at"])

        session_b = Task.objects.get(pk=task.pk)
        session_b.claim(claimed_by="session-B")

        task.refresh_from_db()
        assert task.status == Task.Status.CLAIMED
        assert task.claimed_by == "session-B"  # reclaimed, not duplicated
        assert task.lease_expires_at is not None
        assert task.lease_expires_at > timezone.now()  # a fresh lease for B

    def test_claim_does_not_steal_a_unit_with_a_fresh_lease(self) -> None:
        """Live-lease protection: a unit with a FRESH lease is NOT stolen.

        Session A holds a live lease. Session B's claim must lose — the CAS
        ``<claimable>`` predicate excludes a CLAIMED row whose lease is still
        in the future, so A's claim is left intact and B raises the typed
        ``InvalidTransitionError``.
        """
        from teatree.core.models.errors import InvalidTransitionError  # noqa: PLC0415

        task = self._task()
        task.claim(claimed_by="session-A", lease_seconds=300)

        session_b = Task.objects.get(pk=task.pk)
        with pytest.raises(InvalidTransitionError):
            session_b.claim(claimed_by="session-B")

        task.refresh_from_db()
        assert task.status == Task.Status.CLAIMED
        assert task.claimed_by == "session-A"  # the live owner keeps its claim

    def test_claim_refuses_a_terminal_task(self) -> None:
        """A COMPLETED/FAILED unit is never re-claimed — the typed refusal stands."""
        from teatree.core.models.errors import InvalidTransitionError  # noqa: PLC0415

        task = self._task()
        task.fail()  # terminal

        with pytest.raises(InvalidTransitionError):
            task.claim(claimed_by="session-A")
        task.refresh_from_db()
        assert task.status == Task.Status.FAILED


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


class TestReplayOrphanedTransitions(TestCase):
    """#883 — a mid-transition crash must leave *recoverable* state.

    ``Task.complete`` does the task ``save()`` then ``_advance_ticket()``.
    Pre-#883 these were two separate write boundaries: a crash between
    them left the task COMPLETED but the ticket on its old state. Lease
    expiry can't rescue it (the task is already COMPLETED, not CLAIMED),
    so ``reclaim_orphaned_claims`` / ``reap_stale_claims`` never see it
    and the loop silently stalls forever on a half-advanced ticket.

    Two complementary guarantees. ``Task.complete`` is now one
    ``transaction.atomic``: the crash window is gone — either both writes
    land or neither does. ``replay_orphaned_transitions`` is the boot/tick
    recovery sweep (sibling of ``reclaim_orphaned_claims``) for the rows
    that *did* slip through before the fix shipped, or any future seam: it
    finds a COMPLETED task whose phase implies an FSM transition the
    ticket has not yet taken and replays the *same* idempotent
    ``_advance_ticket`` path — no parallel transition mechanism.
    """

    def test_backend_is_sqlite(self) -> None:
        from django.db import connection  # noqa: PLC0415

        assert connection.vendor == "sqlite"

    def test_completed_task_with_unapplied_phase_transition_is_replayed(self) -> None:
        # Simulate the half-advanced state a mid-transition crash leaves:
        # the coding task is COMPLETED but the ticket is still PLANNED
        # (the FSM ``code()`` transition never landed).
        ticket = Ticket.objects.create(state=Ticket.State.PLANNED)
        session = Session.objects.create(ticket=ticket, agent_id="a")
        Task.objects.create(
            ticket=ticket,
            session=session,
            phase="coding",
            status=Task.Status.COMPLETED,
        )

        replayed = Task.objects.replay_orphaned_transitions()

        ticket.refresh_from_db()
        assert replayed == 1
        assert ticket.state == Ticket.State.CODED, (
            f"orphaned mid-transition ticket was not replayed (still {ticket.state!r}) — the loop stalls forever"
        )

    def test_already_advanced_ticket_is_left_untouched(self) -> None:
        # Anti-vacuity: the common case (complete() already advanced the
        # ticket) must NOT be double-fired — the phase/state guards no-op.
        ticket = Ticket.objects.create(state=Ticket.State.CODED)
        session = Session.objects.create(ticket=ticket, agent_id="a")
        Task.objects.create(
            ticket=ticket,
            session=session,
            phase="coding",
            status=Task.Status.COMPLETED,
        )

        replayed = Task.objects.replay_orphaned_transitions()

        ticket.refresh_from_db()
        assert replayed == 0
        assert ticket.state == Ticket.State.CODED

    def test_replay_preserves_state_preconditions_no_gate_skip(self) -> None:
        # GATE-INTEGRITY (#883): replay must never let a ticket reach a
        # state it didn't earn. A COMPLETED *shipping* task whose ticket
        # is only STARTED (it never went through code→test→review) must
        # NOT be teleported to SHIPPED — the same phase+state guard that
        # protects the live ``complete()`` path protects replay, because
        # replay reuses that exact path.
        ticket = Ticket.objects.create(state=Ticket.State.STARTED)
        session = Session.objects.create(ticket=ticket, agent_id="a")
        Task.objects.create(
            ticket=ticket,
            session=session,
            phase="shipping",
            status=Task.Status.COMPLETED,
        )

        replayed = Task.objects.replay_orphaned_transitions()

        ticket.refresh_from_db()
        assert replayed == 0
        assert ticket.state == Ticket.State.STARTED, (
            f"replay skipped the lifecycle gate — ticket reached {ticket.state!r} it never earned"
        )

    def test_replays_scoping_transition_when_guard_holds(self) -> None:
        # The scoping→start branch of the shared transition path: a
        # SCOPED ticket whose completed scoping task's start() was lost.
        ticket = Ticket.objects.create(state=Ticket.State.SCOPED)
        session = Session.objects.create(ticket=ticket, agent_id="a")
        Task.objects.create(ticket=ticket, session=session, phase="scoping", status=Task.Status.COMPLETED)

        replayed = Task.objects.replay_orphaned_transitions()

        ticket.refresh_from_db()
        assert replayed == 1
        assert ticket.state == Ticket.State.STARTED

    def test_replays_shipping_transition_only_from_reviewed(self) -> None:
        # The shipping→ship branch: only fires from REVIEWED (the earned
        # state). A REVIEWED ticket whose completed shipping task's
        # ship() was lost to a crash is recovered to SHIPPED.
        #
        # #1284 (codex #1282-2): the replay sweep goes through the same
        # ``_apply_phase_transition`` path the live ``complete()`` chain
        # uses, so the visited-phases gate applies here too. Record
        # ``testing``/``reviewing`` to satisfy the gate — a ticket that
        # legitimately reached REVIEWED would have those attested.
        ticket = Ticket.objects.create(state=Ticket.State.REVIEWED)
        session = Session.objects.create(ticket=ticket, agent_id="a")
        session.visit_phase("testing", agent_id="a")
        session.visit_phase("reviewing", agent_id="a")
        Task.objects.create(ticket=ticket, session=session, phase="shipping", status=Task.Status.COMPLETED)

        replayed = Task.objects.replay_orphaned_transitions()

        ticket.refresh_from_db()
        assert replayed == 1
        assert ticket.state == Ticket.State.SHIPPED

    def test_replays_testing_and_reviewing_transitions(self) -> None:
        # The testing→test and reviewing→review branches of the shared
        # path, each from its earned predecessor state.
        from unittest.mock import patch  # noqa: PLC0415

        coded = Ticket.objects.create(state=Ticket.State.CODED)
        s1 = Session.objects.create(ticket=coded, agent_id="a")
        Task.objects.create(ticket=coded, session=s1, phase="testing", status=Task.Status.COMPLETED)
        tested = Ticket.objects.create(state=Ticket.State.TESTED)
        s2 = Session.objects.create(ticket=tested, agent_id="b")
        Task.objects.create(ticket=tested, session=s2, phase="reviewing", status=Task.Status.COMPLETED)

        # Shippable so `tested`'s replayed review lands REVIEWED (not
        # auto-ignored) — this test pins the replay branch, not the #3313
        # unshippable-review disposition.
        with patch.object(Ticket, "has_shippable_diff", return_value=True):
            replayed = Task.objects.replay_orphaned_transitions()

        coded.refresh_from_db()
        tested.refresh_from_db()
        assert replayed == 2
        assert coded.state == Ticket.State.TESTED
        assert tested.state == Ticket.State.REVIEWED

    def test_replays_reviewer_role_external_review(self) -> None:
        # The reviewing+REVIEWER branch (mark_reviewed_externally): a
        # reviewer-role ticket whose completed reviewing task's external
        # review transition was lost is recovered to DELIVERED.
        ticket = Ticket.objects.create(state=Ticket.State.STARTED, role=Ticket.Role.REVIEWER)
        session = Session.objects.create(ticket=ticket, agent_id="a")
        Task.objects.create(ticket=ticket, session=session, phase="reviewing", status=Task.Status.COMPLETED)

        replayed = Task.objects.replay_orphaned_transitions()

        ticket.refresh_from_db()
        assert replayed == 1
        assert ticket.state == Ticket.State.DELIVERED

    def test_only_latest_completed_task_per_ticket_is_replayed(self) -> None:
        # A ticket accrues one COMPLETED task per phase. The sweep must
        # replay only the *latest* completed task's transition (newest
        # pk), not re-fire every historical phase task — the older ones
        # would all no-op on the guards anyway, but the dedup keeps the
        # sweep O(tickets) not O(all completed tasks) and proves the
        # latest-per-ticket selection is exercised.
        ticket = Ticket.objects.create(state=Ticket.State.PLANNED)
        session = Session.objects.create(ticket=ticket, agent_id="a")
        # Older completed coding task, then the latest is also coding
        # (e.g. a re-run). Both COMPLETED on the same PLANNED ticket.
        Task.objects.create(ticket=ticket, session=session, phase="coding", status=Task.Status.COMPLETED)
        Task.objects.create(ticket=ticket, session=session, phase="coding", status=Task.Status.COMPLETED)

        replayed = Task.objects.replay_orphaned_transitions()

        ticket.refresh_from_db()
        # Counted once (one ticket recovered), not once per completed task.
        assert replayed == 1
        assert ticket.state == Ticket.State.CODED

    def test_pending_and_failed_tasks_are_not_replayed(self) -> None:
        # Only COMPLETED tasks represent finished work whose transition
        # may have been lost; PENDING/FAILED tasks are handled by the
        # claim/reap sweeps and must not be force-advanced here.
        ticket = Ticket.objects.create(state=Ticket.State.STARTED)
        session = Session.objects.create(ticket=ticket, agent_id="a")
        Task.objects.create(ticket=ticket, session=session, phase="coding", status=Task.Status.PENDING)
        Task.objects.create(ticket=ticket, session=session, phase="coding", status=Task.Status.FAILED)

        replayed = Task.objects.replay_orphaned_transitions()

        ticket.refresh_from_db()
        assert replayed == 0
        assert ticket.state == Ticket.State.STARTED

    def test_needs_user_input_held_task_is_not_force_advanced(self) -> None:
        # #927 BLOCKER — a headless coding task that returned
        # ``{"needs_user_input": True}`` is correctly *held* by
        # ``_advance_ticket`` (ticket stays STARTED, an interactive
        # followup is scheduled, the task ends COMPLETED). The replay
        # sweep then finds that COMPLETED task as latest-per-ticket and
        # must NOT force-advance the ticket past the phase the agent
        # said it could not finish. The needs-user-input suppression
        # is part of the shared transition path, not only the live
        # ``complete()`` chain.
        ticket = Ticket.objects.create(state=Ticket.State.STARTED)
        session = Session.objects.create(ticket=ticket, agent_id="a")
        task = Task.objects.create(ticket=ticket, session=session, phase="coding")
        task.complete_with_attempt(
            exit_code=0,
            result={"needs_user_input": True, "user_input_reason": "blocked on a design decision"},
        )
        # Precondition: the live path held the ticket and scheduled the
        # interactive followup — this is the state the sweep then sees.
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.STARTED
        followup = Task.objects.filter(parent_task=task).first()
        assert followup is not None
        assert followup.execution_target == Task.ExecutionTarget.INTERACTIVE

        replayed = Task.objects.replay_orphaned_transitions()

        ticket.refresh_from_db()
        assert replayed == 0
        assert ticket.state == Ticket.State.STARTED, (
            f"replay force-advanced a needs-user-input-held ticket to {ticket.state!r} — "
            "the agent said it could not finish coding; the interactive followup is orphaned"
        )
        # The interactive followup must survive the sweep untouched.
        followup.refresh_from_db()
        assert followup.status == Task.Status.PENDING

    def test_completed_task_without_needs_user_input_still_replays(self) -> None:
        # #927 anti-vacuity: the fix must suppress *only* the
        # needs-user-input case. A genuinely orphaned COMPLETED coding
        # task (last attempt did NOT request user input) must still be
        # replay-advanced, exactly as before — the recovery sweep is
        # not over-blocked into uselessness.
        ticket = Ticket.objects.create(state=Ticket.State.PLANNED)
        session = Session.objects.create(ticket=ticket, agent_id="a")
        task = Task.objects.create(ticket=ticket, session=session, phase="coding")
        task.complete_with_attempt(exit_code=0, result={"summary": "done"})
        # Simulate the half-advanced orphan: complete() advanced the
        # ticket; reset it to PLANNED so the sweep has work to replay.
        ticket.state = Ticket.State.PLANNED
        ticket.save(update_fields=["state"])

        replayed = Task.objects.replay_orphaned_transitions()

        ticket.refresh_from_db()
        assert replayed == 1
        assert ticket.state == Ticket.State.CODED


class TestCompleteIsAtomic(TestCase):
    """#883 — ``Task.complete`` must be one transaction.

    The crash window is the gap between the task ``save()`` and the
    ticket ``save()`` inside ``_advance_ticket``. We prove the gap is
    closed by forcing the FSM transition to raise *after* the task save:
    pre-fix the task save had already committed (separate boundary) so
    the task is COMPLETED while the ticket is stale; post-fix the whole
    ``complete()`` rolls back as a unit, so a retry can complete cleanly
    rather than the ticket being permanently half-advanced.
    """

    def test_backend_is_sqlite(self) -> None:
        from django.db import connection  # noqa: PLC0415

        assert connection.vendor == "sqlite"

    def test_complete_rolls_back_task_save_when_advance_fails(self) -> None:
        from unittest.mock import patch  # noqa: PLC0415

        import pytest  # noqa: PLC0415

        ticket = Ticket.objects.create(state=Ticket.State.PLANNED)
        session = Session.objects.create(ticket=ticket, agent_id="a")
        task = Task.objects.create(
            ticket=ticket,
            session=session,
            phase="coding",
            status=Task.Status.CLAIMED,
        )

        boom = RuntimeError("crash mid-transition")
        with (
            patch.object(Ticket, "code", side_effect=boom),
            pytest.raises(RuntimeError),
        ):
            task.complete()

        task.refresh_from_db()
        ticket.refresh_from_db()
        # Atomic: the task save is rolled back together with the failed
        # FSM transition. Pre-fix the task was COMPLETED here (its save
        # had committed on a separate boundary) while the ticket stayed
        # PLANNED — the unrecoverable half-advance #883 is about.
        assert task.status == Task.Status.CLAIMED, (
            f"task.complete() was not atomic — task is {task.status!r} but the FSM transition failed"
        )
        assert ticket.state == Ticket.State.PLANNED
