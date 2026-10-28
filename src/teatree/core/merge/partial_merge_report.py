"""Read-only inventory of merged tickets whose open issue still has a declared remainder."""

import logging
from typing import TypedDict

from teatree.core.issue_writes.issue_candidate_selection import description_of
from teatree.core.merge.ci_rollup import CodeHostQuery
from teatree.core.merge.partial_merge import MergedPrReading, PrLink
from teatree.core.models import MergeAudit, PullRequest, Ticket
from teatree.utils.pr_ref import PrRef
from teatree.utils.url_slug import pr_ref_from_url

logger = logging.getLogger(__name__)


class PartialMergeMatch(TypedDict):
    ticket_id: int
    issue_url: str
    pr_url: str
    unmet: list[str]


class PartialMergeUnreadable(TypedDict):
    ticket_id: int
    reason: str


class PartialMergeReport(TypedDict):
    matches: list[PartialMergeMatch]
    unreadable: list[PartialMergeUnreadable]


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


def _read_candidate(ticket: Ticket, pr: PullRequest, ref: PrRef) -> tuple[MergedPrReading | None, str]:
    try:
        query = CodeHostQuery.for_ref(ref)
        issue = query.backend.get_issue(ticket.issue_url)
    except Exception:  # core never imports backends (§17.6.2), so a 404 or forge error cannot be named
        logger.warning("partial merge report: could not read %s", ticket.issue_url, exc_info=True)
        return None, "issue unreadable on the forge"
    state = issue.get("state")
    if state not in {"open", "opened", "closed"}:
        return None, "issue state unreadable"
    if state == "closed":
        return None, ""
    try:
        message = query.pr_message()
    except Exception:
        logger.warning("partial merge report: could not read %s", pr.url, exc_info=True)
        return None, "pull request body unreadable"
    if message is None:
        return None, "pull request body unreadable"
    reading = MergedPrReading.parse(message.body, ticket)
    if reading.link is PrLink.PARTIAL and not reading.unmet:
        reading = reading.with_unclaimed_issue_boxes(description_of(issue), message.body)
    return reading, ""


def find_partial_merges(*, overlay: str = "") -> PartialMergeReport:
    report: PartialMergeReport = {"matches": [], "unreadable": []}
    tickets = Ticket.objects.filter(state=Ticket.State.MERGED, role=Ticket.Role.AUTHOR).exclude(issue_url="")
    if overlay:
        tickets = tickets.filter(overlay=overlay)

    for ticket in tickets.order_by("pk"):
        latest = _last_merged_pr(ticket)
        if latest is None:
            report["unreadable"].append({"ticket_id": ticket.pk, "reason": "last merged pull request unknown"})
            continue
        pr, ref = latest
        reading, reason = _read_candidate(ticket, pr, ref)
        if reason:
            report["unreadable"].append({"ticket_id": ticket.pk, "reason": reason})
        elif reading is not None and reading.leaves_remainder:
            report["matches"].append(
                {
                    "ticket_id": ticket.pk,
                    "issue_url": ticket.issue_url,
                    "pr_url": pr.url,
                    "unmet": list(reading.unmet),
                }
            )
    return report
