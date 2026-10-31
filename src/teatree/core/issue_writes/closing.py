"""Closing an issue: what a close says, what it did, and the delivery a ``completed`` one needs.

A ``completed`` close claims the work was done, so it needs a delivery behind it: a delivered
ticket or a merged closing pull request.
"""

import logging
from dataclasses import dataclass
from typing import Literal

from teatree.core.backend_protocols import CodeHostBackend, IssueCloseReason
from teatree.core.merge.partial_merge import MergedPrReading, PrLink
from teatree.core.models import PullRequest, Ticket
from teatree.utils.url_slug import pr_ref_from_url

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IssueClose:
    """What a close says and why: the rationale for the description, the forge reason, and the audit label."""

    rationale: str
    reason: IssueCloseReason
    action: str = "issue_close_rationale"


@dataclass(frozen=True)
class CloseOutcome:
    """What a close attempt did."""

    kind: Literal["closed", "external_refused"]
    issue_url: str
    digest: str = ""


def _merged_pull_request_closes(host: CodeHostBackend, ticket: Ticket) -> bool:
    for row in PullRequest.objects.filter(ticket=ticket, state=PullRequest.State.MERGED):
        ref = pr_ref_from_url(row.url)
        if ref is None:
            continue
        try:
            message = host.fetch_pr_message(slug=ref.slug, pr_id=ref.pr_id)
        except Exception:
            logger.warning("closing: could not read the body of %s", row.url, exc_info=True)
            continue
        if message is not None and MergedPrReading.parse(message.body, ticket).link is PrLink.CLOSING:
            return True
    return False


def completion_refusal(host: CodeHostBackend, issue_url: str) -> str:
    """Why *issue_url* may not be closed as completed, or ``""`` when a delivery covers it."""
    tickets = Ticket.objects.filter(issue_url=issue_url, role=Ticket.Role.AUTHOR)
    if tickets.filter(state=Ticket.State.DELIVERED).exists() or any(
        _merged_pull_request_closes(host, ticket) for ticket in tickets.filter(state__in=Ticket.merged_states())
    ):
        return ""
    return (
        f"{issue_url} has no delivered ticket and no merged pull request that closes it, so it cannot be closed as "
        "completed; close it as not_planned, or deliver the work first"
    )
