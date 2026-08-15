"""A re-fix after a review HOLD is re-planned first — findings are not a work order (#4348).

Every ticket behind a long-held PR carried exactly ONE ``PlanArtifact``, recorded
at the ORIGINAL implementation, and zero planning-phase tasks: the FIRST
implementation of a ticket is planned, every RE-implementation after a review
HOLD is not. So "does this ticket have a plan?" answered YES for all eight held
tickets while the plan predated every review finding by days, and the implementing
agent was handed a findings list — "fix this line" — where a plan would have
forced "what is the defect CLASS, and where else does it occur". Both PRs that
produced the measurement closed the blocker they were handed and were held again
for the same defect through a different door.

The fix is that the question is a COMPARISON, never a presence check. This module
owns that one predicate and the surfaces it feeds:

``refix_plan_stale_reason``
    the block reason for one ticket — non-empty iff a HOLD verdict is NEWER than
    the ticket's governing planning signal. Read by the ``plan_currency`` FSM gate
    (``code()`` / ``schedule_coding``) and by :func:`not_awaiting_refix_plan_q` at
    the claim boundary, so the schedule seam and the claim seam cannot disagree.
    The claim seam is load-bearing on its own: a post-HOLD coding task is minted
    outside ``schedule_coding`` (by ``core.signals`` or by hand), so a gate on the
    scheduler alone never sees it.

``coding_tasks_since_last_plan``
    the cheap detector the issue names. ``> 1`` on an open ticket means re-fixes
    ran with no intervening plan; nothing reported it, which is why the ratio had
    to be hand-queried out of the control DB.

The ticket→verdict binding is deliberately two-sided. A PR carries two ticket
rows and the auto-review lane hangs the verdict off the REVIEWER one (#4366)
while the coding task sits on the author, so binding by the verdict's own FK
alone misses every real case; the ``(slug, pr_id)`` pairs the author ticket owns
are read too, from BOTH stores ``PullRequest.objects.owning_ticket`` reads.

Never a lockout. ``ticket plan-reaffirm`` appends a ``PlanArtifact`` with NO FSM
transition, so it reaches a ``shipped``/``in_review`` ticket that ``ticket plan``
(STARTED→PLANNED) cannot; ``ticket skip-planning`` is the audited trivial-work
carve-out, counted only when recorded AFTER the verdict — the carve-out is a
decision about THIS re-fix, and a months-old skip cannot stand in for one.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, TypedDict

from django.db.models import Q

from teatree.core.modelkit.phases import phase_spellings
from teatree.core.models.plan_artifact import PlanArtifact
from teatree.core.models.pull_request import PullRequest
from teatree.core.models.review_verdict import ReviewVerdict
from teatree.core.models.task import Task
from teatree.core.models.ticket import Ticket
from teatree.core.models.types import validated_ticket_extra

#: The phases whose dispatch produces code. A ``reviewing``/``testing``/``shipping``
#: task on a held ticket is never blocked — a held PR must stay re-reviewable, which
#: is the very thing #4366 found broken.
IMPLEMENTING_PHASES: Final[tuple[str, ...]] = ("coding", "debugging")

_TRIVIAL_SKIP_KEY: Final = "trivial_plan_skip"


class RefixPlanRow(TypedDict):
    """The JSON-serialisable shape of one :class:`AwaitingRefixPlan` (the CLI report)."""

    ticket_id: int
    issue_url: str
    state: str
    overlay: str
    hold_recorded_at: str
    plan_recorded_at: str
    coding_tasks_since_last_plan: int
    open_implementing_tasks: int


@dataclass(frozen=True, slots=True)
class AwaitingRefixPlan:
    """One ticket whose next implementing dispatch would run on findings alone."""

    ticket_id: int
    issue_url: str
    state: str
    overlay: str
    hold_recorded_at: datetime
    plan_recorded_at: datetime | None
    coding_tasks_since_last_plan: int
    open_implementing_tasks: int

    def as_row(self) -> RefixPlanRow:
        """This row as the report's serialisable shape; an absent plan is an empty stamp."""
        return RefixPlanRow(
            ticket_id=self.ticket_id,
            issue_url=self.issue_url,
            state=self.state,
            overlay=self.overlay,
            hold_recorded_at=self.hold_recorded_at.isoformat(),
            plan_recorded_at=self.plan_recorded_at.isoformat() if self.plan_recorded_at else "",
            coding_tasks_since_last_plan=self.coding_tasks_since_last_plan,
            open_implementing_tasks=self.open_implementing_tasks,
        )


def _implementing_phase_spellings() -> tuple[str, ...]:
    """Every accepted spelling of every implementing phase — the SSOT both seams filter on."""
    return tuple(spelling for phase in IMPLEMENTING_PHASES for spelling in phase_spellings(phase))


def _pr_refs_for_ticket(ticket: Ticket) -> set[tuple[str, int]]:
    """Every ``(casefolded slug, pr_id)`` *ticket* owns, across both PR stores.

    The ``PullRequest`` rows AND the ticket's own recorded urls, for the same
    reason :meth:`PullRequestQuerySet.owning_ticket` reads both: a PR opened
    outside the pipeline, or before the row write existed, is recorded only in
    ``extra`` — and a resolver that reads one store while the writer filled the
    other resolves nothing.
    """
    return PullRequest.objects.refs_for_ticket(ticket)


def newest_hold_verdict(ticket: Ticket) -> ReviewVerdict | None:
    """The newest HOLD verdict bound to *ticket* by either binding, or ``None``.

    The row itself, not just its timestamp, because the replan brief quotes the
    findings the re-fix has to generalise from.
    """
    refs = _pr_refs_for_ticket(ticket)
    bound = Q(ticket=ticket)
    for slug, pr_id in refs:
        bound |= Q(slug__iexact=slug, pr_id=pr_id)
    return (
        ReviewVerdict.objects.filter(verdict=ReviewVerdict.Verdict.HOLD)
        .filter(bound)
        .order_by("-recorded_at", "-pk")
        .first()
    )


def governing_plan_at(ticket: Ticket) -> datetime | None:
    """When *ticket*'s governing (latest) ``PlanArtifact`` was recorded, or ``None``."""
    return (
        PlanArtifact.objects.filter(ticket=ticket)
        .order_by("-recorded_at", "-pk")
        .values_list("recorded_at", flat=True)
        .first()
    )


def planning_signal_at(ticket: Ticket) -> datetime | None:
    """The newest satisfying planning signal — a ``PlanArtifact`` or a trivial-skip marker.

    The two signals the plan gate already accepts as interchangeable, compared on
    one timeline so a re-fix is admitted by whichever came last.
    """
    signals = [moment for moment in (governing_plan_at(ticket), _trivial_skip_at(ticket)) if moment is not None]
    return max(signals) if signals else None


def refix_plan_stale_reason(ticket: Ticket) -> str:
    """Why *ticket* may not be implemented yet, or ``""`` to admit.

    Non-empty iff a HOLD verdict bound to this ticket is NEWER than its newest
    planning signal — including the no-plan-at-all case, which is the same defect
    with nothing to compare against. A tie resolves to STALE, the safe direction
    :meth:`ReviewVerdictManager.effective_state_at` already takes.

    A strict no-op for a reviewer-role row (it never implements and never carries
    a plan) and for any ticket with no HOLD verdict, so ordinary work — the
    overwhelming majority — is never over-blocked.
    """
    return _stale_reason(ticket, index=_hold_index())


def coding_tasks_since_last_plan(ticket: Ticket) -> int:
    """How many implementing tasks were created after *ticket*'s newest plan.

    The issue's named detector: ``> 1`` on an open ticket means re-fixes ran with
    no intervening plan. With no plan at all every implementing task counts, which
    is the more severe reading of the same gap.
    """
    tasks = Task.objects.filter(ticket=ticket, phase__in=_implementing_phase_spellings())
    planned_at = governing_plan_at(ticket)
    if planned_at is not None:
        tasks = tasks.filter(created_at__gt=planned_at)
    return tasks.count()


def blocked_refix_task_pks() -> list[int]:
    """PKs of PENDING implementing tasks whose ticket must re-plan before coding.

    Bounded by construction: only PENDING implementing tasks on AUTHOR tickets
    that carry at least one HOLD verdict are ever inspected, so an idle queue
    costs one indexed count and a busy one a handful of rows.
    """
    index = _hold_index()
    if not index.by_ticket and not index.by_pr:
        return []
    candidates = Task.objects.filter(
        status=Task.Status.PENDING,
        phase__in=_implementing_phase_spellings(),
        ticket__role=Ticket.Role.AUTHOR,
    ).select_related("ticket")
    return [task.pk for task in candidates if _stale_reason(task.ticket, index=index)]


def not_awaiting_refix_plan_q() -> Q:
    """``Q`` admitting every task EXCEPT an implementing one awaiting a post-HOLD replan.

    Built as an explicit pk exclusion rather than a correlated subquery because the
    ticket→verdict binding spans a JSON store no ORM join reaches. The excluded set
    is resolved at Q-build time, which is the claim boundary — the same instant the
    decision is made — so there is no window where a stale snapshot admits a task
    the predicate has already blocked.
    """
    blocked = blocked_refix_task_pks()
    return ~Q(pk__in=blocked) if blocked else Q()


def claimable_dispatch_q() -> Q:
    """``Task.dispatchable_q()`` narrowed to what may be CLAIMED right now — the SSOT both claim sites use.

    Deliberately NOT a second conjunct on ``dispatchable_q`` itself: that one is
    also the in-flight budget's counting set, and a CLAIMED coding task on a held
    ticket still occupies its slot, so narrowing it there would silently
    over-admit. It lives here rather than on ``Task`` because ``core.models`` may
    not depend on ``core`` (the tach boundary), and this exclusion needs the
    verdict/PR binding that does.
    """
    return Task.dispatchable_q() & not_awaiting_refix_plan_q()


def tickets_awaiting_refix_plan(overlay: str = "") -> list[AwaitingRefixPlan]:
    """Every AUTHOR ticket whose next implementing dispatch would run on findings alone.

    The surface the issue asks for. Ordered by ticket pk so a report is stable
    across runs; scoped to *overlay* when given.
    """
    index = _hold_index()
    rows: list[AwaitingRefixPlan] = []
    for ticket in _held_candidates(index):
        if overlay and ticket.overlay != overlay:
            continue
        hold_at = _newest_hold_at(ticket, index=index)
        if hold_at is None or not _stale_reason(ticket, index=index):
            continue
        rows.append(
            AwaitingRefixPlan(
                ticket_id=int(ticket.pk),
                issue_url=ticket.issue_url,
                state=str(ticket.state),
                overlay=ticket.overlay,
                hold_recorded_at=hold_at,
                plan_recorded_at=governing_plan_at(ticket),
                coding_tasks_since_last_plan=coding_tasks_since_last_plan(ticket),
                open_implementing_tasks=Task.objects.filter(
                    ticket=ticket, phase__in=_implementing_phase_spellings(), status__in=Task.Status.active()
                ).count(),
            )
        )
    return rows


@dataclass(frozen=True, slots=True)
class _HoldIndex:
    """The newest HOLD timestamp per verdict-FK ticket id and per ``(slug, pr_id)``.

    One read of the HOLD verdicts, shared by every per-ticket question in a sweep,
    so a report over N tickets is two queries rather than 2N.
    """

    by_ticket: dict[int, datetime]
    by_pr: dict[tuple[str, int], datetime]


def _held_candidates(index: _HoldIndex) -> list[Ticket]:
    """The AUTHOR tickets any recorded HOLD could bind to, derived from *index*.

    Walking every author ticket would make the tick sweep O(board); the held
    population is O(PRs under a hold), which the index already names. Each held PR
    resolves through :meth:`PullRequestQuerySet.owning_ticket` — the very method
    :meth:`~PullRequestQuerySet.refs_for_ticket` inverts — so the two directions
    agree by construction rather than by two hand-kept lookups.
    """
    by_pk: dict[int, Ticket] = {}
    for slug, pr_id in index.by_pr:
        owner = PullRequest.objects.owning_ticket(slug=slug, pr_id=pr_id)
        if owner is not None and owner.role == Ticket.Role.AUTHOR:
            by_pk[int(owner.pk)] = owner
    for ticket in Ticket.objects.filter(pk__in=list(index.by_ticket), role=Ticket.Role.AUTHOR):
        by_pk[int(ticket.pk)] = ticket
    return sorted(by_pk.values(), key=lambda ticket: int(ticket.pk))


def _hold_index() -> _HoldIndex:
    by_ticket: dict[int, datetime] = {}
    by_pr: dict[tuple[str, int], datetime] = {}
    rows = ReviewVerdict.objects.filter(verdict=ReviewVerdict.Verdict.HOLD).values_list(
        "ticket_id", "slug", "pr_id", "recorded_at"
    )
    for ticket_id, slug, pr_id, recorded_at in rows:
        if ticket_id is not None:
            _keep_newest(by_ticket, int(ticket_id), recorded_at)
        _keep_newest(by_pr, (slug.casefold(), int(pr_id)), recorded_at)
    return _HoldIndex(by_ticket=by_ticket, by_pr=by_pr)


def _keep_newest[K](index: dict[K, datetime], key: K, moment: datetime) -> None:
    seen = index.get(key)
    index[key] = moment if seen is None else max(seen, moment)


def _newest_hold_at(ticket: Ticket, *, index: _HoldIndex) -> datetime | None:
    """The newest HOLD bound to *ticket* by EITHER binding, or ``None``."""
    moments = [moment for moment in (index.by_ticket.get(int(ticket.pk)),) if moment is not None]
    moments.extend(index.by_pr[ref] for ref in _pr_refs_for_ticket(ticket) if ref in index.by_pr)
    return max(moments) if moments else None


def _stale_reason(ticket: Ticket, *, index: _HoldIndex) -> str:
    if ticket.role != Ticket.Role.AUTHOR:
        return ""
    hold_at = _newest_hold_at(ticket, index=index)
    if hold_at is None:
        return ""
    planned_at = planning_signal_at(ticket)
    if planned_at is not None and planned_at > hold_at:
        return ""
    return _block_message(ticket, hold_at=hold_at, planned_at=planned_at)


def _block_message(ticket: Ticket, *, hold_at: datetime, planned_at: datetime | None) -> str:
    standing = (
        "no plan recorded for it at all"
        if planned_at is None
        else f"its newest plan was recorded {planned_at.isoformat()}"
    )
    return (
        f"Refusing to implement ticket {ticket.pk} — a review HOLD was recorded {hold_at.isoformat()} and "
        f"{standing}. A findings list is not a plan: re-plan the defect CLASS independently of any line "
        f"number and enumerate every site in the touched module that can exhibit it, then record it with "
        f"`t3 <overlay> ticket plan-reaffirm {ticket.pk} --base-sha <current-40-char-HEAD>` — it appends a "
        f"PlanArtifact with no FSM transition, so it reaches this ticket at state {str(ticket.state)!r}. For a "
        f"genuinely trivial re-fix record the carve-out instead: "
        f"`t3 <overlay> ticket skip-planning {ticket.pk} --reason <why>`."
    )


def _trivial_skip_at(ticket: Ticket) -> datetime | None:
    """When the trivial-skip carve-out was recorded, or ``None`` when absent/malformed.

    A marker with no parseable ``at`` is treated as absent rather than as an
    infinitely-old signal: it cannot be compared against the verdict, and a
    carve-out that cannot be dated cannot excuse a re-fix.
    """
    marker = validated_ticket_extra(ticket.extra).get(_TRIVIAL_SKIP_KEY)
    if not isinstance(marker, dict) or not str(marker.get("reason", "")).strip():
        return None
    raw = marker.get("at")
    if not isinstance(raw, str):
        return None
    try:
        stamped = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return stamped if stamped.tzinfo else stamped.replace(tzinfo=UTC)
