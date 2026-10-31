"""Rule F of the board janitor — a pre-ship ticket whose own ISSUE the forge closed (#4711).

Rules A-E all resolve a ticket's URL as a PULL REQUEST or gate on a post-ship state, so a
PRE-SHIP ticket behind a closed ISSUE reached none of them: B/C read an ``/issues/<n>`` URL
as ``PrOpenState.UNKNOWN``, D polls only ``completable_states()`` and asks a COMPLETION
predicate a ``not_planned`` close never satisfies, and E filters DELIVERED. The measured
cost of that gap was a backlog prune the board never learned about — twelve rows left
``plan_recorded`` behind closed issues, each a permanent coding-dispatch source, one of them
re-dispatched eleven times.

The retirement lands on IGNORED rather than DELIVERED because nothing shipped, and IGNORED
is the one state that hands the issue back to intake (``issue_owning_states``) — so an
issue reopened later is re-admitted normally instead of being wedged by the retired row.
A COMPLETED close and a NOT_PLANNED close both retire the row; which one it was is kept in
``extra["issue_close_reason"]`` so the board stays able to tell them apart.

Unshipped work is surfaced, never used as a veto: the teardown a terminal state enqueues
keeps unsynced changes (the reaper's analyze-before-wipe, #706), while leaving the row
``plan_recorded`` to protect that work is what re-dispatched it forever. So a retired ticket whose
worktree holds uncommitted tracked changes raises ONE durable question naming the paths.
"""

import logging
from typing import TYPE_CHECKING

from django.db.models import Q
from django.utils import timezone
from django_fsm import can_proceed

from teatree.backends.issue_close import IssueCloseVerdict
from teatree.backends.issue_reads import issue_close_verdict
from teatree.core.backend_protocols import IssueOpenState
from teatree.core.models.conversation_ticket import CONVERSATION_QUIET_LIMIT
from teatree.core.worktree.unshipped_work import escalate_unshipped_work_once, unshipped_work_paths
from teatree.loop.scanners.board_reconcile_apply import collect, planned, scoped, without_voided
from teatree.loop.scanners.board_reconcile_reads import read_per_overlay
from teatree.loop.scanners.board_reconcile_report import BoardAction, BoardTransition

if TYPE_CHECKING:
    from teatree.core.models import Ticket

logger = logging.getLogger(__name__)


def closed_issue_transitions(
    *, overlay: str, dry_run: bool, probe_budget: int, already_moved: "frozenset[int]" = frozenset()
) -> tuple[list[BoardTransition], int]:
    """Retire every pre-ship AUTHOR ticket whose own issue the forge reports CLOSED.

    Newest-first and bounded by *probe_budget*, like rules B/C/E, so the janitor can
    never saturate the box. Reviewer tickets are excluded: their ``issue_url`` IS a PR,
    which rules B/C own.

    A Slack conversation row anchors on the loop umbrella, whose forge state says nothing about the
    talk, so one with a live task or a recent thread is skipped before it can spend a probe.

    *already_moved* holds the rows an earlier rule moved in THIS run. Rule F runs last
    and reads live state, so without it a ticket rule E had just revived to WORK_STARTED —
    a pre-ship state — would be re-probed and could be retired in the same tick that
    resurrected it. One rule per row per run.
    """
    from teatree.core.models import Task, ThreadBinding, Ticket  # noqa: PLC0415 — ORM import needs the app registry

    since = timezone.now() - CONVERSATION_QUIET_LIMIT
    candidates = without_voided(
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
    conversation_ids = {ticket.pk for ticket in candidates if ticket.is_conversation()}
    occupied_conversation_ids: set[int] = set()
    if conversation_ids:
        occupied_conversation_ids.update(
            Task.objects.filter(ticket_id__in=conversation_ids)
            .filter(Q(status__in=Task.Status.active()) | Q(created_at__gte=since))
            .values_list("ticket_id", flat=True)
        )
        occupied_conversation_ids.update(
            ThreadBinding.objects.filter(ticket_id__in=conversation_ids, created_at__gte=since).values_list(
                "ticket_id", flat=True
            )
        )
    candidates = [ticket for ticket in candidates if ticket.pk not in occupied_conversation_ids]
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
        [t for t in probed if _read_url(t) in closed],
        lambda ticket: _retire(ticket, closed[_read_url(ticket)], dry_run=dry_run),
    )
    return transitions, reads


def _read_url(ticket: "Ticket") -> str:
    """The URL the forge is asked about; a fragment names a row, not another issue, so such rows share one read."""
    return ticket.issue_url.partition("#")[0]


def _closed_issue_verdicts(tickets: "list[Ticket]") -> tuple[dict[str, IssueCloseVerdict], int]:
    """The subset of *tickets*' issue URLs their own overlay's forge reports CLOSED, and the reads spent.

    Each URL is judged by the overlay that owns it, and a ticket whose overlay is not installed
    here is simply not judged. Only a DEFINITE CLOSED counts — an OPEN issue is live work, and the
    UNKNOWN every failure collapses to leaves the ticket exactly where it is.

    The second element counts the reads that completed, not the candidates considered, so
    the run's reported probe spend never charges for a URL nothing judged.
    """
    verdicts = read_per_overlay(tickets, issue_close_verdict, url_of=_read_url)
    closed = {_read_url(ticket): verdict for ticket, verdict in verdicts if verdict.state is IssueOpenState.CLOSED}
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
    unshipped = unshipped_work_paths(ticket)
    extra = ticket.extra or {}
    extra["issue_close_reason"] = verdict.reason
    ticket.extra = extra
    ticket.ignore()
    ticket.save()
    escalate_unshipped_work_once(ticket, paths=unshipped)
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
