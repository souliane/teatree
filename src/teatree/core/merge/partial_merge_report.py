"""Read-only inventory of settled tickets whose issue remains open."""

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import TypedDict

from teatree.core.issue_writes.issue_candidate_selection import description_of
from teatree.core.merge.ci_rollup import CodeHostQuery
from teatree.core.merge.partial_merge import MergedPrReading, PrLink
from teatree.core.models import MergeAudit, PullRequest, Ticket
from teatree.types import RawAPIDict
from teatree.utils.pr_ref import PrRef
from teatree.utils.url_slug import pr_ref_from_url

logger = logging.getLogger(__name__)


class PartialMergeMatch(TypedDict):
    ticket_id: int
    state: str
    issue_url: str
    pr_url: str
    link: str
    unmet: list[str]


class PartialMergeUnreadable(TypedDict):
    ticket_id: int
    reason: str


class PartialMergeReport(TypedDict):
    matches: list[PartialMergeMatch]
    unreadable: list[PartialMergeUnreadable]


class InspectionOutcome(StrEnum):
    MATCH = "match"
    CLOSED = "closed"
    UNREADABLE = "unreadable"


@dataclass(frozen=True, slots=True)
class SettledTicketInspection:
    match: PartialMergeMatch | None = None
    reason: str = ""
    issue: RawAPIDict | None = None
    outcome: InspectionOutcome = InspectionOutcome.UNREADABLE


def _last_merged_pr(ticket: Ticket) -> tuple[PullRequest, PrRef] | None:
    rows = [
        (row, ref)
        for row in PullRequest.objects.filter(ticket=ticket, state=PullRequest.State.MERGED)
        if (ref := pr_ref_from_url(row.url)) is not None
    ]
    if len(rows) == 1:
        return rows[0]
    if not rows:
        return None

    audits = MergeAudit.objects.filter(clear__pr_id__in=[ref.pr_id for _, ref in rows]).select_related("clear")
    stamped: list[tuple[MergeAudit, PullRequest, PrRef]] = []
    for audit in audits:
        audit_ref = PrRef(
            slug=audit.repo_slug or audit.clear.slug,
            pr_id=audit.clear.pr_id,
            host_kind=audit.clear.host_kind,
        )
        stamped.extend((audit, row, ref) for row, ref in rows if ref.same_pr(audit_ref))
    if {row.pk for _, row, _ in stamped} != {row.pk for row, _ in rows}:
        return None
    _, row, ref = max(stamped, key=lambda entry: (entry[0].merged_at, entry[0].pk))
    return row, ref


def inspect_settled_ticket(ticket: Ticket) -> SettledTicketInspection:
    latest = _last_merged_pr(ticket)
    if latest is None:
        return SettledTicketInspection(reason="last merged pull request unknown")
    pr, ref = latest
    try:
        query = CodeHostQuery.for_ref(ref)
        issue = query.backend.get_issue(ticket.issue_url)
    except Exception:  # core never imports backends (§17.6.2), so a 404 or forge error cannot be named
        logger.warning("partial merge report: could not read %s", ticket.issue_url, exc_info=True)
        return SettledTicketInspection(reason="issue unreadable on the forge")
    if not isinstance(issue, dict) or issue.get("state") not in {"open", "opened", "closed"}:
        return SettledTicketInspection(reason="issue state unreadable", issue=issue)
    if issue["state"] == "closed":
        return SettledTicketInspection(reason="issue closed", issue=issue, outcome=InspectionOutcome.CLOSED)
    try:
        message = query.pr_message()
    except Exception:
        logger.warning("partial merge report: could not read %s", pr.url, exc_info=True)
        message = None
    if message is None:
        return SettledTicketInspection(reason="pull request body unreadable", issue=issue)
    reading = MergedPrReading.parse(message.body, ticket)
    if reading.link is PrLink.PARTIAL and not reading.unmet:
        reading = reading.with_unclaimed_issue_boxes(description_of(issue), message.body)
    return SettledTicketInspection(
        match={
            "ticket_id": ticket.pk,
            "state": ticket.state,
            "issue_url": ticket.issue_url,
            "pr_url": pr.url,
            "link": reading.link.value,
            "unmet": list(reading.unmet),
        },
        issue=issue,
        outcome=InspectionOutcome.MATCH,
    )


def find_partial_merges(*, overlay: str = "") -> PartialMergeReport:
    report: PartialMergeReport = {"matches": [], "unreadable": []}
    tickets = Ticket.objects.filter(
        state__in=Ticket.merged_states(), role=Ticket.Role.AUTHOR, issue_url__startswith="http"
    )
    if overlay:
        tickets = tickets.filter(overlay=overlay)

    for ticket in tickets.order_by("pk"):
        inspection = inspect_settled_ticket(ticket)
        if inspection.match is not None:
            report["matches"].append(inspection.match)
        elif inspection.outcome is InspectionOutcome.UNREADABLE:
            report["unreadable"].append({"ticket_id": ticket.pk, "reason": inspection.reason})
    return report
