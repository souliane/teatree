"""Rule F of the board janitor — a pre-ship ticket whose own ISSUE the forge closed (#4711).

Rules A-E all resolve a ticket's URL as a PULL REQUEST or gate on a post-ship state, so a
PRE-SHIP ticket behind a closed ISSUE reached none of them: B/C read an ``/issues/<n>`` URL
as ``PrOpenState.UNKNOWN``, D polls only ``completable_states()`` and asks a COMPLETION
predicate a ``not_planned`` close never satisfies, and E filters DELIVERED. The measured
cost of that gap was a backlog prune the board never learned about — twelve rows left
``planned`` behind closed issues, each a permanent coding-dispatch source, one of them
re-dispatched eleven times.

The retirement lands on IGNORED rather than DELIVERED because nothing shipped, and IGNORED
is the one state that hands the issue back to intake (``issue_owning_states``) — so an
issue reopened later is re-admitted normally instead of being wedged by the retired row.
A COMPLETED close and a NOT_PLANNED close both retire the row; which one it was is kept in
``extra["issue_close_reason"]`` so the board stays able to tell them apart.

Unshipped work is surfaced, never used as a veto: the teardown a terminal state enqueues
keeps unsynced changes (the reaper's analyze-before-wipe, #706), while leaving the row
``planned`` to protect that work is what re-dispatched it forever. So a retired ticket whose
worktree holds uncommitted tracked changes raises ONE durable question naming the paths.
"""

import logging
from typing import TYPE_CHECKING

from django_fsm import can_proceed

from teatree.backends.issue_close import IssueCloseVerdict
from teatree.backends.issue_reads import issue_close_verdict
from teatree.core.backend_protocols import IssueOpenState
from teatree.loop.scanners.board_reconcile_apply import collect, planned, scoped
from teatree.loop.scanners.board_reconcile_report import BoardAction, BoardTransition

if TYPE_CHECKING:
    from teatree.core.models import Ticket

logger = logging.getLogger(__name__)

#: Dedupe key for the unshipped-work escalation — one question per ticket, ever. The
#: factory's own guard drops a duplicate only while the earlier question is unanswered,
#: and answering it does not move the row off IGNORED, so the janitor would re-ask forever.
_UNSHIPPED_WORK_MARKER = "issue-closed-unshipped-work:{pk}"


def closed_issue_transitions(
    *, overlay: str, dry_run: bool, probe_budget: int, already_moved: "frozenset[int]" = frozenset()
) -> tuple[list[BoardTransition], int]:
    """Retire every pre-ship AUTHOR ticket whose own issue the forge reports CLOSED.

    Newest-first and bounded by *probe_budget*, like rules B/C/E, so the janitor can
    never saturate the box. Reviewer tickets are excluded: their ``issue_url`` IS a PR,
    which rules B/C own.

    *already_moved* holds the rows an earlier rule moved in THIS run. Rule F runs last
    and reads live state, so without it a ticket rule E had just revived to STARTED —
    a pre-ship state — would be re-probed and could be retired in the same tick that
    resurrected it. One rule per row per run.
    """
    from teatree.core.models import Ticket  # noqa: PLC0415 — ORM import needs the app registry

    candidates = list(
        scoped(
            Ticket.objects.filter(state__in=Ticket.pre_ship_states())
            .exclude(issue_url="")
            .exclude(role=Ticket.Role.REVIEWER)
            .filter(remote_missing=False),
            overlay,
        )
        .exclude(pk__in=already_moved)
        .order_by("-pk")
    )
    probed = candidates[: max(probe_budget, 0)]
    if len(probed) < len(candidates):
        logger.warning(
            "board_reconcile rule F probed the %d newest of %d pre-ship ticket(s); %d unchecked this run",
            len(probed),
            len(candidates),
            len(candidates) - len(probed),
        )
    closed, reads = _closed_issue_verdicts(probed)
    transitions = collect(
        [t for t in probed if t.issue_url in closed],
        lambda ticket: _retire(ticket, closed[ticket.issue_url], dry_run=dry_run),
    )
    return transitions, reads


def _closed_issue_verdicts(tickets: "list[Ticket]") -> tuple[dict[str, IssueCloseVerdict], int]:
    """The subset of *tickets*' issue URLs their own overlay's forge reports CLOSED, and the reads spent.

    Grouped by the ticket's own overlay for the same reason rules D/E group: each URL is
    judged by the overlay that owns it, and a ticket whose overlay is not installed here is
    simply not judged. Only a DEFINITE CLOSED counts — an OPEN issue is live work, and the
    UNKNOWN every failure collapses to leaves the ticket exactly where it is.

    The second element counts the reads actually ISSUED, not the candidates considered, so
    the run's reported probe spend never charges for a URL nothing judged.
    """
    from teatree.core.overlay_loader import get_all_overlays  # noqa: PLC0415 — deferred: registry read at call time

    overlays = get_all_overlays()
    verdicts: dict[str, IssueCloseVerdict] = {}
    for ticket in tickets:
        if ticket.overlay not in overlays or ticket.issue_url in verdicts:
            continue
        verdicts[ticket.issue_url] = issue_close_verdict(overlays[ticket.overlay], ticket.issue_url)
    closed = {url: verdict for url, verdict in verdicts.items() if verdict.state is IssueOpenState.CLOSED}
    return closed, len(verdicts)


def _retire(ticket: "Ticket", verdict: IssueCloseVerdict, *, dry_run: bool) -> BoardTransition | None:
    """Resolve one pre-ship ticket to IGNORED, carrying the forge's own close reason."""
    from teatree.core.models import Ticket  # noqa: PLC0415 — ORM import needs the app registry

    if not can_proceed(ticket.ignore):
        return None
    reason = f"forge says the issue closed ({verdict.reason or 'no reason given'})"
    if dry_run:
        return planned(ticket, Ticket.State.IGNORED, BoardAction.IGNORED_ISSUE_CLOSED, reason)
    from_state = ticket.state
    # Read before the transition: entering a terminal state enqueues the worktree teardown.
    unshipped = _unshipped_work_paths(ticket)
    extra = ticket.extra or {}
    extra["issue_close_reason"] = verdict.reason
    ticket.extra = extra
    ticket.ignore()
    ticket.save()
    _escalate_unshipped_work_once(ticket, paths=unshipped)
    logger.info("Board reconcile retired ticket %s %s → ignored (%s)", ticket.pk, from_state, reason)
    return BoardTransition(
        ticket_id=int(ticket.pk),
        issue_url=ticket.issue_url,
        from_state=from_state,
        to_state=ticket.state,
        action=BoardAction.IGNORED_ISSUE_CLOSED,
        reason=reason,
        applied=True,
    )


def _unshipped_work_paths(ticket: "Ticket") -> list[str]:
    """The ticket's worktrees holding uncommitted tracked changes; empty when unreadable."""
    from teatree.core.models.ticket_worktree_checks import (  # noqa: PLC0415 — ORM import needs the app registry
        collect_dirty_worktree_paths,
    )

    try:
        return collect_dirty_worktree_paths(ticket)
    except Exception:  # noqa: BLE001 — an unreadable checkout must never abort the retirement
        logger.warning("Could not read worktree state for ticket %s — retiring without the probe", ticket.pk)
        return []


def _escalate_unshipped_work_once(ticket: "Ticket", *, paths: list[str]) -> None:
    """Hand the operator a retired ticket's uncommitted work, once per ticket ever."""
    from teatree.core.models.deferred_question import DeferredQuestion  # noqa: PLC0415 — ORM import needs the registry

    if not paths:
        return
    marker = _UNSHIPPED_WORK_MARKER.format(pk=ticket.pk)
    if DeferredQuestion.objects.filter(dedupe_marker=marker).exists():
        return
    question = (
        f"{ticket.issue_url or f'Ticket {ticket.pk}'} was retired because the forge closed its "
        f"issue, and its worktree still holds uncommitted changes ({', '.join(paths)}). "
        "How should that work proceed — salvage it to a PR, or discard it?"
    )
    DeferredQuestion.record(question, session_id="", dedupe_marker=marker)
