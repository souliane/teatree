"""Rule E of the board janitor — the upstream ISSUE behind a DELIVERED ticket was REOPENED (#4152).

DELIVERED is terminal and a ticket owns its issue URL in every state but IGNORED, so intake
could never re-admit that issue and no other rule could reach the ticket — the issue was
stranded silently, forever. The revived ticket lands on WORK_STARTED and the hard-bounded
``stuck_ticket_redispatch`` sweep schedules its planning. A ticket revived
:data:`MAX_REOPEN_REVIVALS` times halts instead and is handed to the operator as one durable
question.
"""

import logging
from typing import TYPE_CHECKING

from django_fsm import can_proceed

from teatree.backends.issue_reads import issue_reopen_state
from teatree.core.backend_protocols import IssueReopenState
from teatree.loop.scanners.board_reconcile_apply import collect, planned, scoped
from teatree.loop.scanners.board_reconcile_report import BoardAction, BoardTransition

if TYPE_CHECKING:
    from teatree.core.models import Ticket

logger = logging.getLogger(__name__)

#: Revivals rule E grants one ticket before it halts and escalates instead. An issue
#: reopened again after the factory has delivered against it that many times is evidence
#: those attempts did not answer it, so a human decides rather than the board re-running
#: the whole ladder forever.
MAX_REOPEN_REVIVALS = 3

#: Dedupe key for the cap escalation — one queued question per ticket, ever.
_REVIVAL_HALT_MARKER = "reopen-revival-capped:{pk}"


def reopened_issue_transitions(*, overlay: str, dry_run: bool, probe_budget: int) -> tuple[list[BoardTransition], int]:
    """Rule E — DELIVERED author tickets whose upstream issue the forge says was REOPENED.

    DELIVERED is where a ticket stops, and a ticket owns its issue URL in every state
    but IGNORED, so nothing could reach either the ticket or its issue again. Releasing
    the row to IGNORED instead would be worse than the gap: intake re-admits, the
    persistence handler finds the same row past NOT_STARTED and returns early, and the
    #4133 every-tick re-admission is back. Reviving the ticket is what actually restores
    a path, and the sweep is idempotent because WORK_STARTED is not a candidate.

    Reviewer tickets are excluded: their ``issue_url`` IS a PR, which rules B/C own.
    """
    from teatree.core.models import Ticket  # noqa: PLC0415 — ORM import needs the app registry

    candidates = list(
        scoped(
            Ticket.objects.filter(state=Ticket.State.DELIVERED)
            .exclude(issue_url="")
            .exclude(role=Ticket.Role.REVIEWER)
            .filter(remote_missing=False),
            overlay,
        ).order_by("-pk")
    )
    probed = candidates[: max(probe_budget, 0)]
    if len(probed) < len(candidates):
        logger.warning(
            "board_reconcile rule E probed the %d newest of %d delivered ticket(s); %d unchecked this run",
            len(probed),
            len(candidates),
            len(candidates) - len(probed),
        )
    reopened, reads = _reopened_issue_urls(probed)
    transitions = collect(
        [t for t in probed if t.issue_url in reopened],
        lambda ticket: _revive_reopened(ticket, dry_run=dry_run),
    )
    return transitions, reads


def _reopened_issue_urls(tickets: "list[Ticket]") -> tuple[set[str], int]:
    """The subset of *tickets*' issue URLs their own overlay's forge reports as REOPENED.

    Grouped by the ticket's own overlay for the same reason rule D groups: each URL is
    judged by the overlay that owns it, and a ticket whose overlay is not installed here
    is simply not judged. Only a DEFINITE ``REOPENED`` counts — the ``UNKNOWN`` every
    failure and every forge without a reopen marker collapses to leaves the ticket alone.

    The second element counts the reads actually ISSUED, not the candidates considered
    (mirrors rule F's ``_closed_issue_verdicts``), so a ticket whose overlay is not
    installed here is never charged against the probe budget.
    """
    from teatree.core.overlay_loader import get_all_overlays  # noqa: PLC0415 — deferred: registry read at call time

    overlays = get_all_overlays()
    judged = [ticket for ticket in tickets if ticket.overlay in overlays]
    reopened = {
        ticket.issue_url
        for ticket in judged
        if issue_reopen_state(overlays[ticket.overlay], ticket.issue_url) is IssueReopenState.REOPENED
    }
    return reopened, len(judged)


def _revive_reopened(ticket: "Ticket", *, dry_run: bool) -> BoardTransition | None:
    """Revive one delivered ticket to WORK_STARTED, or halt it loudly at the revival cap."""
    from teatree.core.models import Ticket  # noqa: PLC0415 — ORM import needs the app registry

    if not can_proceed(ticket.reopen):
        return None
    revivals = _revival_count(ticket)
    if revivals >= MAX_REOPEN_REVIVALS:
        _escalate_revival_cap_once(ticket, revivals=revivals)
        return None
    reason = "forge says the issue was reopened"
    if dry_run:
        return planned(ticket, Ticket.State.WORK_STARTED, BoardAction.REVIVED_REOPENED, reason)
    from_state = ticket.state
    ticket.reopen()
    extra = ticket.extra or {}
    extra["reopen_revivals"] = revivals + 1
    ticket.extra = extra
    ticket.save()
    logger.info("Board reconcile revived ticket %s %s → started (%s)", ticket.pk, from_state, reason)
    return BoardTransition(
        ticket_id=int(ticket.pk),
        issue_url=ticket.issue_url,
        from_state=from_state,
        to_state=ticket.state,
        action=BoardAction.REVIVED_REOPENED,
        reason=reason,
        applied=True,
    )


def _revival_count(ticket: "Ticket") -> int:
    raw = (ticket.extra or {}).get("reopen_revivals", 0)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _escalate_revival_cap_once(ticket: "Ticket", *, revivals: int) -> None:
    """Record a durable question for a ticket at the revival cap, once per ticket.

    The cap must not become the new silence this rule exists to remove, so the ticket
    that stops being revived is handed to the operator on the §17.1 invariant 9 surface
    rather than quietly left delivered behind an open issue.

    Once ever, not once per pending row: the factory's own ``dedupe_marker`` guard drops a
    duplicate only while the earlier question is UNANSWERED, and an answer here does not by
    itself move the ticket off DELIVERED — so the hourly janitor would re-ask forever.
    """
    from teatree.core.models.deferred_question import DeferredQuestion  # noqa: PLC0415 — ORM import needs the registry

    marker = _REVIVAL_HALT_MARKER.format(pk=ticket.pk)
    if DeferredQuestion.objects.filter(dedupe_marker=marker).exists():
        return
    question = (
        f"{ticket.issue_url or f'Ticket {ticket.pk}'} has been reopened after delivery "
        f"{revivals} time(s) and has hit the auto-revival cap, so the board will not restart it "
        "again. How should it proceed — investigate, rework by hand, or ignore the issue?"
    )
    DeferredQuestion.record(question, session_id="", dedupe_marker=marker)
