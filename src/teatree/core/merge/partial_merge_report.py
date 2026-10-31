"""Read-only inventory of settled tickets whose issue remains open."""

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import TypedDict

from teatree.core.issue_coverage import IssueRead
from teatree.core.merge.ci_rollup import CodeHostQuery
from teatree.core.merge.last_merged_pr import last_merged_pr
from teatree.core.merge.partial_merge import MergedPrReading
from teatree.core.models import Ticket

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
    issue: IssueRead | None = None
    outcome: InspectionOutcome = InspectionOutcome.UNREADABLE


def inspect_settled_ticket(ticket: Ticket) -> SettledTicketInspection:
    latest = last_merged_pr(ticket)
    if latest is None:
        return SettledTicketInspection(reason="last merged pull request unknown")
    pr, ref = latest
    try:
        query = CodeHostQuery.for_ref(ref)
    except Exception:  # core never imports backends (§17.6.2), so a resolution error cannot be named
        logger.warning("partial merge report: no code host resolves for %s", pr.url, exc_info=True)
        return SettledTicketInspection(reason="issue unreadable on the forge")
    reading = MergedPrReading.read(query, ticket, with_issue=True)
    issue = reading.issue
    if reading.unreadable or issue is None:
        return SettledTicketInspection(reason=reading.unreadable or "ticket has no forge issue", issue=issue)
    if not issue.is_open:
        return SettledTicketInspection(reason="issue closed", issue=issue, outcome=InspectionOutcome.CLOSED)
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
