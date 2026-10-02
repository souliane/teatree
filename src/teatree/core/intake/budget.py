"""Why issue intake claimed nothing this tick — the in-flight budget, made legible (#3978).

Intake claims only while ``in_flight_count < issue_implementer_max_concurrent``. At a
full budget the scanner factory returns ``None``, so the tick legitimately does nothing
and reports success: the loop reads enabled, its last-run stamp advances, no error is
raised, and labelled issues pile up unreachable with no surface anywhere saying intake
is at budget and claiming nothing. The only way to see it was to read the marker ledger
by hand.

This module is that surface, and deliberately the ONLY one: the per-tick report and the
``t3 doctor check`` alarm both read it, so the tick and the operator can never hold two
opinions about whether intake is jammed. The limit is passed in by the caller that
resolved the overlay's settings — the reading decides nothing about how big the budget
should be, only what is in it.

:func:`release_deadlocked_holder` is the one ACTION drawn from that reading, and lives
here for the same reason: "the budget is deadlocked" and "so free the longest-held slot"
are one statement, and splitting them is how a verdict ends up computed and then only
ever reported (#4389).
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from django.utils import timezone

if TYPE_CHECKING:
    from collections.abc import Callable

    from django.db.models import QuerySet

    from teatree.core.models.implemented_issue_marker import ImplementedIssueMarker
    from teatree.core.models.ticket import Ticket

#: A claim lands its ticket and first task in the seconds after the marker row is
#: written, so inside this window a healthy claim is indistinguishable from a stuck
#: one. A marker younger than this counts as progressing on its age alone.
_DEFAULT_SETTLE = timedelta(minutes=30)


@dataclass(frozen=True, slots=True)
class HeldSlot:
    """One occupied budget slot: which issue holds it, and whether it is going anywhere."""

    issue_url: str
    #: The state of the ticket for this issue, or ``""`` when no ticket exists.
    ticket_state: str
    progressing: bool
    dispatched_at: datetime

    def __str__(self) -> str:
        return f"{self.issue_url} ({self.ticket_state or 'no ticket'})"


@dataclass(frozen=True, slots=True)
class IntakeBudget:
    """The overlay's in-flight budget as one reading, plus the two verdicts drawn from it."""

    overlay: str
    limit: int
    holders: tuple[HeldSlot, ...]
    #: The operator's ``issue_implementer_max_concurrent``, when the caller knows it and
    #: the resource loop's adaptive reading (#3992) is what ``limit`` actually became.
    #: Reported so raising the setting and seeing nothing change is legible for once.
    static_limit: int = 0

    @property
    def in_flight(self) -> int:
        return len(self.holders)

    @property
    def at_budget(self) -> bool:
        """True when no further issue may be claimed — the factory's own gate, verbatim."""
        return self.in_flight >= self.limit

    @property
    def deadlocked(self) -> bool:
        """True when a full budget is held ENTIRELY by claims that are going nowhere.

        A full budget is normal while the factory is busy; it is a deadlock only when
        no holder has an active task or an open PR, because then nothing will release a
        slot until a grace expires and no new issue can be admitted meanwhile.
        """
        return bool(self.holders) and self.at_budget and not any(slot.progressing for slot in self.holders)

    @property
    def longest_held(self) -> HeldSlot | None:
        """The slot claimed earliest — the one that has had the most time to show something."""
        if not self.holders:
            return None
        return min(self.holders, key=lambda slot: slot.dispatched_at)

    def report(self) -> str:
        """The one-line reason a tick claimed nothing, naming every slot and its holder."""
        progressing = sum(1 for slot in self.holders if slot.progressing)
        held = ", ".join(str(slot) for slot in self.holders) or "none"
        override = (
            f" (adaptive; issue_implementer_max_concurrent is {self.static_limit})"
            if self.static_limit and self.static_limit != self.limit
            else ""
        )
        return (
            f"issue intake at budget for {self.overlay or 'every overlay'}: "
            f"{self.in_flight}/{self.limit} slots held{override}, {progressing} progressing — {held}"
        )


def read_intake_budget(
    overlay: str, limit: int, *, static_limit: int = 0, settle: timedelta | None = None
) -> IntakeBudget:
    """Read *overlay*'s in-flight budget against *limit*. ``overlay=""`` spans every overlay."""
    cutoff = timezone.now() - (_DEFAULT_SETTLE if settle is None else settle)
    return IntakeBudget(
        overlay=overlay,
        limit=limit,
        holders=tuple(_held_slot(marker, cutoff=cutoff) for marker in _holders(overlay)),
        static_limit=static_limit,
    )


def release_deadlocked_holder(budget: IntakeBudget) -> HeldSlot | None:
    """Free *budget*'s longest-held slot when it is deadlocked; return it, else ``None``.

    The graces exist to protect an attempt that might still be alive. Under a deadlock
    there is no such attempt — every holder is already past the settle window with no
    active task and no open PR — so waiting the rest of them out buys nothing and costs
    the whole budget, which is how intake sat at 3/3 for most of a day with 64 candidates
    queued behind it.

    Exactly one slot, so the remaining holders keep the grace their own class is entitled
    to, and freeing one clears ``at_budget``: the next reading is no longer deadlocked, so
    this cannot walk the budget down. ABANDONED, matching the give-up semantics
    ``reconcile_stale`` releases a stalled claim with — an issue whose ticket still exists
    is not re-admitted anyway (the intake decision table's work-exists rule holds it).
    """
    slot = budget.longest_held if budget.deadlocked else None
    if slot is None:
        return None
    held = _holders(budget.overlay).filter(issue_url=slot.issue_url)
    # The enum off the queryset's own model: this file reaches the ORM once, in ``_holders``.
    held.update(state=held.model.State.ABANDONED)
    return slot


def reconcile_holder_pr_rows(overlay: str, *, read_state: "Callable[[str], str]") -> int:
    """Settle the ledger rows of every ticket holding a slot; return the count moved (#3984).

    Both readings above are drawn from ``PullRequest.state``, so a row nobody advanced
    after its PR merged does two things at once: it holds the slot (the release rule
    asks for ``MERGED`` and never sees it) and it silences the alarm that exists to
    report that (:attr:`IntakeBudget.deadlocked` reads the same row as proof of a live
    attempt). Asking the forge before either reading is what keeps them from disagreeing
    with it.

    Scoped to the HOLDERS, not the ledger: the probe count is the slot count, so the
    cost is the budget limit however large the ledger grows. *read_state* is injected —
    reading a PR's live state needs a concrete backend, which ``teatree.core`` may not
    import (the same split as ``open_pr_teardown_gate``'s ``PrStateReader``).
    """
    from teatree.core.models import PullRequest, Ticket  # noqa: PLC0415 — ORM import needs the app registry

    issue_urls = [url for url in _holders(overlay).values_list("issue_url", flat=True) if url]
    if not issue_urls:
        return 0
    held = Ticket.objects.filter(issue_url__in=issue_urls).values_list("pk", flat=True)
    return PullRequest.objects.filter(ticket_id__in=held).reconcile_forge_states(read_state=read_state)


def _holders(overlay: str) -> "QuerySet[ImplementedIssueMarker]":
    """The non-terminal markers occupying *overlay*'s budget. ``overlay=""`` spans every one."""
    from teatree.core.models import ImplementedIssueMarker  # noqa: PLC0415 — ORM import needs the app registry

    markers = ImplementedIssueMarker.objects.exclude(state__in=ImplementedIssueMarker.State.terminal())
    if overlay:
        markers = markers.filter(overlay=overlay)
    return markers.order_by("pk")


def _held_slot(marker: "ImplementedIssueMarker", *, cutoff: datetime) -> HeldSlot:
    from teatree.core.models import Ticket  # noqa: PLC0415 — ORM import needs the app registry

    ticket = Ticket.objects.filter(issue_url=marker.issue_url).only("pk", "state").first()
    return HeldSlot(
        issue_url=marker.issue_url,
        ticket_state=ticket.state if ticket is not None else "",
        progressing=_progressing(marker, ticket, cutoff=cutoff),
        dispatched_at=marker.dispatched_at,
    )


def _progressing(marker: "ImplementedIssueMarker", ticket: "Ticket | None", *, cutoff: datetime) -> bool:
    """True when this claim shows evidence its attempt is still going somewhere.

    Evidence is a task still being worked or a PR still open. A ticket already in a
    marker-release state is the opposite of evidence: the slot is releasable now and is
    held only because nothing has reconciled it yet.
    """
    from teatree.core.models import Task, Ticket  # noqa: PLC0415 — ORM import needs the app registry
    from teatree.core.models.pull_request import PullRequest  # noqa: PLC0415 — ORM import needs the app registry

    if marker.dispatched_at > cutoff:
        return True
    if ticket is None or ticket.state in Ticket.marker_release_states():
        return False
    if Task.objects.filter(ticket=ticket, status__in=Task.Status.active()).exists():
        return True
    return PullRequest.objects.live().filter(ticket=ticket).exists()
