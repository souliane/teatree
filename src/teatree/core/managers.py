import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, cast

from django.apps import apps
from django.db import models, transaction
from django.db.models import Q
from django.utils import timezone

from teatree.config import worker_is_quiescing
from teatree.core.loop_lease_manager import (
    PER_LOOP_OWNER_PREFIX,
    T3_MASTER_SLOT,
    LoopLeaseManager,
    LoopLeaseQuerySet,
    OwnershipStatus,
    is_per_loop_owner_slot,
    per_loop_owner_slot,
)
from teatree.core.managers_inbound import IncomingEventQuerySet, ReplyDispatchQuerySet
from teatree.core.managers_issue_match import matching_issue_q
from teatree.core.managers_overlay import for_overlay as _for_overlay
from teatree.core.managers_overlay import overlay_scope_q
from teatree.core.managers_phase_cadence import in_flight_for_phase as _in_flight_for_phase
from teatree.core.managers_phase_cadence import last_run_at_for_phase as _last_run_at_for_phase
from teatree.core.managers_session import SessionQuerySet
from teatree.core.managers_task_claim import ClaimOrder, _claimable_now_q, schema_behind_code
from teatree.core.managers_task_sweeps import reap_stale_claims as _reap_stale_claims
from teatree.core.managers_task_sweeps import reclaim_orphaned_claims as _reclaim_orphaned_claims
from teatree.core.managers_task_sweeps import replay_orphaned_transitions as _replay_orphaned_transitions
from teatree.core.session_handover_manager import SessionHandoverManager, SessionHandoverQuerySet

if TYPE_CHECKING:
    from teatree.core.models.task import Task
    from teatree.core.models.ticket import Ticket
    from teatree.core.models.worktree import Worktree


def _phase_spellings(phase: str) -> tuple[str, ...]:
    """Every stored spelling of *phase* — the one call-time hop to the phase vocabulary.

    ``completed_in_phase`` / ``pending_in_phase`` / ``in_flight_for_phase`` all key on
    the same spelling set, so the deferred ``modelkit.phases`` import lives here once
    rather than being restated in each — one intra-core edge, not three.
    """
    from teatree.core.modelkit.phases import phase_spellings  # noqa: PLC0415 — deferred: call-time import

    return phase_spellings(phase)


__all__ = [
    "PER_LOOP_OWNER_PREFIX",
    "T3_MASTER_SLOT",
    "ClaimOrder",
    "IncomingEventManager",
    "LoopLeaseManager",
    "LoopLeaseQuerySet",
    "OwnershipStatus",
    "ReplyDispatchManager",
    "SessionHandoverManager",
    "SessionHandoverQuerySet",
    "SessionManager",
    "TaskManager",
    "TicketManager",
    "WorktreeManager",
    "is_per_loop_owner_slot",
    "overlay_scope_q",
    "per_loop_owner_slot",
]


logger = logging.getLogger(__name__)

#: How long an admitted-but-unclaimed row keeps its seat in the cheap lane (#4098).
#: It covers the runner handoff — the seconds between ``enqueue`` and the worker's
#: claim — and no more: past it the seat is released, so a runner that died holding
#: an admission cannot wedge the lane shut, and the drain re-admits (and re-stamps)
#: the row on its next pass. Erring long would trade a melt for a stall; erring short
#: reopens the burst window this bounds.
ADMITTED_INFLIGHT_WINDOW = timedelta(minutes=5)


class TicketQuerySet(models.QuerySet):
    def for_overlay(self, overlay: str | None = None) -> models.QuerySet:
        return _for_overlay(self, overlay)

    def resolve(self, ref: str) -> "Ticket":
        """Resolve a ticket from a pk, an issue number, an issue URL, or a repo key.

        Accepts a numeric pk (``"314"`` — direct DB lookup), a full issue URL
        (``"https://github.com/owner/repo/issues/466"`` — exact match on
        ``issue_url``), a bare issue number when no pk exists (``"466"`` —
        matches an ``issue_url`` ending in ``/466`` *or* one stored as the
        bare string ``"466"``, #707), or the collision-free repo-namespaced
        key (``"owner/repo#466"`` — exact match on ``repo_namespaced_key``,
        #2293). The bare-number fallback stays ambiguous by construction (a
        digit alone carries no repo information) — pass the repo-namespaced
        key or the full URL when two repos share an issue number. Shared by
        ``pr create`` and ``lifecycle visit-phase`` so both accept the same
        identifier set (#694) — callers naturally pass the forge issue number
        and must not silently hit ``DoesNotExist``.
        """
        ticket_model = cast("type[Ticket]", apps.get_model("core", "Ticket"))

        if ref.isdigit():
            try:
                return self.get(pk=int(ref))
            except ticket_model.DoesNotExist:
                # No such pk — fall back to issue_url. Match either a forge
                # URL ending in /<ref> or a bare-number issue_url stored as
                # just the issue number (#707), keeping the match exact.
                ticket = self.filter(Q(issue_url__endswith=f"/{ref}") | Q(issue_url=ref)).first()
                if ticket is not None:
                    return ticket
                raise
        keyed = self.filter(repo_namespaced_key=ref).first()
        if keyed is not None:
            return keyed
        ticket = self.filter(issue_url=ref).first()
        if ticket is None:
            msg = f"No ticket matching {ref!r} (looked up by pk, issue_url, and repo_namespaced_key)"
            raise ticket_model.DoesNotExist(msg)
        return ticket

    def matching_issue(self, issue_url: str) -> models.QuerySet:
        # Tickets that ARE the given issue — the issue-URL alias-collapse predicate
        # (#2293) lives in :func:`~teatree.core.managers_issue_match.matching_issue_q`.
        return self.filter(matching_issue_q(issue_url))

    def in_flight(self, overlay: str | None = None) -> models.QuerySet:
        ticket_model = cast("type[Ticket]", apps.get_model("core", "Ticket"))

        return (
            self.for_overlay(overlay)
            .exclude(state__in=ticket_model.in_flight_excluded_states())
            .filter(Q(extra__tracker_status__isnull=True) | ~Q(extra__tracker_status="Done"))
            .order_by("pk")
        )


class WorktreeQuerySet(models.QuerySet):
    def for_overlay(self, overlay: str | None = None) -> models.QuerySet:
        return _for_overlay(self, overlay)

    def for_ticket(self, ticket: "Ticket") -> models.QuerySet:
        return self.filter(ticket=ticket)

    def active(self, overlay: str | None = None) -> models.QuerySet:
        """Worktrees whose ticket is still in flight (not delivered, review-posted, or ignored).

        Matches the worktrees panel one-to-one so the KPI count and table size agree.
        """
        ticket_model = cast("type[Ticket]", apps.get_model("core", "Ticket"))

        return (
            self.for_overlay(overlay).exclude(ticket__state__in=ticket_model.in_flight_excluded_states()).order_by("pk")
        )

    def stamp_e2e_run(self, ticket_pk: int, *, now: datetime | None = None) -> int:
        """Stamp ``last_e2e_run`` on the running worktrees of *ticket_pk* (#2227).

        Called by ``lifecycle record-e2e-run`` so the idle-stack reaper KEEPS a
        stack that an E2E/evidence run just touched (the live target of in-flight
        work). Scoped to ``services_up``/``ready`` rows — a dormant worktree holds
        no stack, so there is nothing for the reaper to preserve. Returns the
        number of rows stamped.
        """
        worktree_model = cast("type[Worktree]", apps.get_model("core", "Worktree"))

        return self.filter(
            ticket_id=ticket_pk,
            state__in=[worktree_model.State.SERVICES_UP, worktree_model.State.READY],
        ).update(last_e2e_run=now or timezone.now())


class TaskQuerySet(models.QuerySet):
    def for_overlay(self, overlay: str | None = None) -> models.QuerySet:
        """Tasks scoped to an overlay through the ticket OR the session.

        A ``Task`` has no overlay column of its own — its overlay is the
        ticket's or the session's, so the scope clause spans both relations
        and includes legacy empty-overlay rows. An empty ``overlay`` returns
        every task. Delegates to :func:`overlay_scope_q`, the single source of
        truth for the Task overlay clause, shared with ``_claimable_for_target``
        (the loop claim), the MCP ``loop_stats`` read, and the dashboard
        selectors (F1.6).
        """
        return self.filter(overlay_scope_q(overlay))

    def for_claude_session(self, claude_session_id: str) -> models.QuerySet:
        """Tasks whose session is the given Claude session, newest first.

        Scopes the task list to the work persisted under one Claude session:
        ``Session.agent_id`` holds the Claude session UUID (set by Claude Code),
        so the join is ``task.session.agent_id == claude_session_id``. An empty
        id matches nothing — an anonymous caller has no session-scoped list.
        """
        if not claude_session_id:
            return self.none()
        return self.filter(session__agent_id=claude_session_id).order_by("-pk")

    def completed_in_phase(self, phase: str) -> models.QuerySet:
        """Completed tasks whose phase normalizes to ``phase`` (#757).

        Matches any accepted spelling (short verb or gerund) — the FSM
        ``review()`` / ``mark_reviewed_externally()`` conditions must see
        a short-verb ``review`` task the same as a canonical
        ``reviewing`` one, mirroring the ``normalize_phase`` contract the
        rest of the system honours.
        """
        task_model = cast("type[Task]", apps.get_model("core", "Task"))

        return self.filter(phase__in=_phase_spellings(phase), status=task_model.Status.COMPLETED)

    def pending_in_phase(self, phase: str) -> models.QuerySet:
        """Non-terminal tasks whose phase normalizes to ``phase`` (#769).

        The consume-side mirror of ``completed_in_phase`` (#757):
        ``_consume_pending_phase_tasks`` must match a short-verb
        ``review`` task the same as a canonical ``reviewing`` one, so a
        direct-CLI path does not orphan a short-verb PENDING/CLAIMED task
        as a zombie session. Same SSOT (``phase_spellings``), opposite
        status set.
        """
        task_model = cast("type[Task]", apps.get_model("core", "Task"))

        return self.filter(
            phase__in=_phase_spellings(phase),
            status__in=task_model.Status.active(),
        )

    def not_auto_review_armed(self) -> models.QuerySet:
        """Tasks the #68 auto-review dispatch did NOT arm — the stray/armed discriminator (#3910).

        Reaping an armed task costs its PR a full dispatch deadline: the claim is
        keyed per ``(slug, pr_id, head_sha)``, so nothing re-arms that head until
        the deadline passes and the next sweep reclaims it (#3920). Bounded, not
        permanent — but still a wasted cycle, so an armed task stays off the
        reaper's list.
        """
        return self.filter(auto_review_dispatches__isnull=True)

    def in_flight_for_phase(self, overlay: str, phase: str) -> models.QuerySet:
        """Pending/claimed tasks for one overlay+phase — the dedupe lock (SSOT).

        Read by the periodic cadence scanners AND by the phase-task mint itself
        (``Ticket._schedule_headless``, #3903), so the one lock the codebase
        documents is consulted at the write rather than restated per caller.
        Matches every accepted spelling of *phase* via ``phase_spellings``, the
        same SSOT ``pending_in_phase`` reads.
        """
        return _in_flight_for_phase(self, overlay, _phase_spellings(phase))

    def last_run_at_for_phase(
        self, overlay: str, phase: str, *, statuses: frozenset[str] | None = None
    ) -> datetime | None:
        """Most recent ``Session.started_at`` for an overlay+phase task, or ``None`` — the cadence clock."""
        return _last_run_at_for_phase(self, overlay, phase, statuses=statuses)

    def claimable_for_headless(self, overlay: str | None = None) -> models.QuerySet:
        task_model = cast("type[Task]", apps.get_model("core", "Task"))

        return self._claimable_for_target(task_model.ExecutionTarget.HEADLESS, overlay)

    def claimable_for_interactive(self, overlay: str | None = None) -> models.QuerySet:
        task_model = cast("type[Task]", apps.get_model("core", "Task"))

        return self._claimable_for_target(task_model.ExecutionTarget.INTERACTIVE, overlay)

    def claim_next_pending(
        self,
        *,
        claimed_by: str,
        claimed_by_session: str = "",
        lease_seconds: int = 300,
        extra_filter: "Q | None" = None,
        ordering: "ClaimOrder | None" = None,
    ) -> "Task | None":
        """Atomically claim the oldest PENDING task — backend-agnostic (#786, N4).

        The claim is the dispatch boundary: callers spawn a sub-agent only
        for the returned task, so a second concurrent loop tick cannot
        double-dispatch a task the first already took (the spawn-then-claim
        race this replaces).

        Atomicity does NOT rely on ``select_for_update(skip_locked=True)``:
        teatree's production DB is SQLite, where
        ``has_select_for_update_skip_locked`` is ``False`` and Django
        silently drops the clause, so two ticks would both SELECT the same
        row. Instead this is a single conditional ``UPDATE ... WHERE
        status='pending' AND pk=<oldest>``: the row's status is the
        compare-and-swap token. Exactly one writer's UPDATE matches
        (``rowcount == 1``); the loser's ``WHERE status='pending'`` no
        longer holds so it updates 0 rows and returns ``None``. Correct on
        SQLite AND Postgres. ``extra_filter`` (a ``Q``) narrows the
        candidate set (e.g. dispatchable-only) so the command and the
        manager share ONE audited claim path. ``claimed_by_session``
        attributes the claim to the worker session that took it,
        orthogonal to the role-label ``claimed_by``; it rides the SET
        clause only and never the CAS WHERE predicate, so the claim
        semantics are byte-identical with or without it.
        ``ordering`` (a :class:`ClaimOrder`) lets a caller pick the claim order
        (PR-13 admission priority: a queued TODO/followup before a new-ticket
        auto-start). ``None`` (the default) is today's plain oldest-``pk`` order,
        so a caller that omits it is byte-identical to before.
        Returns the claimed task, or ``None`` when nothing is claimable.
        """
        task_model = cast("type[Task]", apps.get_model("core", "Task"))

        # Two admission gates, both admitting NO new task: the drain gate (a rolling
        # deploy is quiescing this worker) and the deploy-order gate (#3901 — the control
        # DB lags the running code). The CAS never fires, so claimed ≡ spawned stays true,
        # and in-flight CLAIMED leases (which renew via ``renew_lease``, not this path)
        # are untouched by either.
        if worker_is_quiescing() or schema_behind_code():
            return None
        now = timezone.now()
        candidates = self.filter(status=task_model.Status.PENDING).filter(_claimable_now_q(now))
        if extra_filter is not None:
            candidates = candidates.filter(extra_filter)
        if ordering is not None:
            candidates = candidates.annotate(**ordering.annotations)
        order_fields = ordering.order_by if ordering is not None else ("pk",)
        with transaction.atomic():
            oldest_pk = candidates.order_by(*order_fields).values_list("pk", flat=True).first()
            if oldest_pk is None:
                return None
            # Compare-and-swap on status: only the writer that still sees
            # the row PENDING wins; a concurrent tick updates 0 rows. The
            # session attribution rides the SET clause only — the WHERE
            # predicate is the status CAS token and stays untouched by it.
            claimed_count = self.filter(pk=oldest_pk, status=task_model.Status.PENDING).update(
                status=task_model.Status.CLAIMED,
                claimed_by=claimed_by,
                claimed_by_session=claimed_by_session,
                claimed_at=now,
                heartbeat_at=now,
                lease_expires_at=now + timedelta(seconds=lease_seconds),
            )
            if claimed_count != 1:
                return None
        return self.get(pk=oldest_pk)

    def reclaim_orphaned_claims(self) -> int:
        """Return expired-lease CLAIMED tasks to PENDING — the rescue sweep (#652)."""
        return _reclaim_orphaned_claims(self)

    def replay_orphaned_transitions(self) -> int:
        """Replay FSM transitions a mid-transition crash dropped (#883)."""
        return _replay_orphaned_transitions(self)

    def reap_stale_claims(self) -> int:
        """Fail CLAIMED tasks whose lease is *still* expired — runs after the rescue sweep."""
        return _reap_stale_claims(self)

    def in_flight_claimed_count(self, dispatchable_filter: "Q") -> int:
        """Count CLAIMED tasks that match the dispatchable phase/role filter.

        The pipelined WIP cap subtracts this from the raw overlay budget so
        the standing total of CLAIMED dispatchable tasks can never exceed the
        cap, regardless of which tick admitted them. A CLAIMED task whose
        lease has expired is excluded — the reaper will reclaim it and it is
        not truly in flight.
        """
        task_model = cast("type[Task]", apps.get_model("core", "Task"))

        now = timezone.now()
        return (
            self.filter(status=task_model.Status.CLAIMED, lease_expires_at__gt=now).filter(dispatchable_filter).count()
        )

    def live_headless_agent_count(self) -> int:
        """Live HEADLESS agents in flight — CLAIMED, unexpired-lease, HEADLESS target.

        The single divisor for the per-agent test-worker budget AND the
        governor's ceiling comparison (#3644/F9). Counts EVERY live headless
        agent — a registered-phase task AND a free-form one (``architectural_review``
        etc.). ``dispatchable_q()`` selects only ``(role, phase)`` pairs with a
        registered sub-agent, so counting through it UNDERcounted the free-form
        headless agents that go through the very ``with_test_worker_cap`` this
        number divides — a smaller divisor gave each agent MORE pytest workers,
        the melt direction. Counting the true live-agent set fixes it.
        """
        task_model = cast("type[Task]", apps.get_model("core", "Task"))

        now = timezone.now()
        return self.filter(
            status=task_model.Status.CLAIMED,
            lease_expires_at__gt=now,
            execution_target=task_model.ExecutionTarget.HEADLESS,
        ).count()

    def cheap_lane_occupancy(self) -> int:
        """How full the CHEAP admission lane is — the bound BOTH chokepoints read (#4098).

        Deliberately a sibling of :meth:`live_headless_agent_count` rather than a
        parameter on it: that number is also the per-agent test-worker divisor, and it
        must keep counting EVERY live agent. This one answers a different question —
        how full is the small lane the cheap class is admitted through while the
        expensive class is braked — so the exemption is bounded rather than a second
        unbounded lane.

        Occupancy is agents ALREADY RUNNING plus admissions still in the runner's hand:
        a claimed row and a row a chokepoint enqueued a second ago put the same load on
        the box, but the second is still PENDING, so counting only CLAIMED made a burst's
        own admissions invisible to the very next probe. The drain could carry that in
        memory across its loop; the one-row-at-a-time ``post_save`` cannot, so the bound
        has to live where both can see it. :data:`ADMITTED_INFLIGHT_WINDOW` bounds how
        long an unclaimed admission keeps its seat, so a runner that died holding one
        cannot wedge the lane shut. Filters on stored spellings, so a row written as
        ``review`` counts like ``reviewing``.
        """
        from teatree.core.modelkit.phases import cheap_phase_spellings  # noqa: PLC0415 — deferred: call-time import

        task_model = cast("type[Task]", apps.get_model("core", "Task"))

        now = timezone.now()
        return (
            self.filter(
                execution_target=task_model.ExecutionTarget.HEADLESS,
                phase__in=cheap_phase_spellings(),
            )
            .filter(
                models.Q(status=task_model.Status.CLAIMED, lease_expires_at__gt=now)
                | models.Q(status=task_model.Status.PENDING, admitted_at__gt=now - ADMITTED_INFLIGHT_WINDOW)
            )
            .count()
        )

    def record_admission(self, task_pk: int) -> None:
        """Stamp one row as handed to the runner — the write that makes an admission countable.

        A queryset ``UPDATE`` rather than ``instance.save()``: the ``post_save``
        auto-enqueue is itself a ``post_save`` receiver, so saving the instance from
        inside it would re-enter the receiver, and the stamp must not be able to re-arm
        the dispatch it is recording.
        """
        self.filter(pk=task_pk).update(admitted_at=timezone.now())

    def active_claims(self) -> models.QuerySet:
        """Tasks CLAIMED with a still-live lease — the in-flight set (SSOT).

        A live CLAIMED lease means a worker / sub-agent is actively driving a unit
        of loop work right now; an expired lease is not in-flight (the worker is
        gone; the reaper / reclaimer will sweep it). This is the single predicate
        both ``active_claim_exists`` (the deferred-reinstall + drain readiness check)
        and ``t3 worker drain``'s still-claimed listing read, so the two can never
        drift.
        """
        task_model = cast("type[Task]", apps.get_model("core", "Task"))

        now = timezone.now()
        return self.filter(status=task_model.Status.CLAIMED, lease_expires_at__gt=now)

    def active_claim_exists(self) -> bool:
        """True iff some task is CLAIMED with a still-live lease.

        The deferred-reinstall drain and ``t3 worker drain`` read this to DEFER an
        action (re-anchoring the running interpreter / swapping the deploy image)
        until no unit is in flight — never mutate the code out from under an active
        agent.
        """
        return self.active_claims().exists()

    def _claimable_for_target(self, target: str, overlay: str | None = None) -> models.QuerySet:
        task_model = cast("type[Task]", apps.get_model("core", "Task"))

        # Drain gate (rolling deploy): a quiescing worker sees NO claimable work, so
        # the interactive/headless claim commands admit zero new tasks during the
        # deploy window. Orthogonal to the supervisor's stop condition — in-flight
        # leases keep renewing.
        if worker_is_quiescing() or schema_behind_code():
            return self.none()
        now = timezone.now()
        qs = (
            self.filter(
                execution_target=target,
                status__in=task_model.Status.active(),
            )
            .filter(Q(lease_expires_at__isnull=True) | Q(lease_expires_at__lte=now))
            .filter(_claimable_now_q(now))
            .order_by("pk")
        )
        if overlay:
            qs = qs.for_overlay(overlay)
        return qs


TicketManager = models.Manager.from_queryset(TicketQuerySet)
WorktreeManager = models.Manager.from_queryset(WorktreeQuerySet)
SessionManager = models.Manager.from_queryset(SessionQuerySet)
TaskManager = models.Manager.from_queryset(TaskQuerySet)
IncomingEventManager = models.Manager.from_queryset(IncomingEventQuerySet)
ReplyDispatchManager = models.Manager.from_queryset(ReplyDispatchQuerySet)
