"""Resolve a review target to the operator who first requested its ticket."""

from dataclasses import dataclass
from urllib.parse import urlsplit

from teatree.core.models import OperatorIdentity, PullRequest, Ticket
from teatree.core.models.live_post_approval import canonical_mr_scope
from teatree.core.models.operator_identity import canonical_operator_key


@dataclass(frozen=True, slots=True)
class ReviewRequester:
    key: str
    slack_user_id: str
    channel: str


def _matching_ticket(mr_url: str, scope: str) -> Ticket | None:
    repo, separator, iid = scope.rpartition("!")
    if not separator or not iid.isdigit():
        return None
    host = urlsplit(mr_url).hostname
    authored = {
        row.ticket_id: row.ticket
        for row in PullRequest.objects.for_pr(slug=repo, pr_id=int(iid)).select_related("ticket")
        if not host or urlsplit(row.url).hostname == host
    }
    if len(authored) > 1:
        message = f"review target {scope} has multiple owning tickets"
        raise ValueError(message)
    if authored:
        return next(iter(authored.values()))
    reviewed = [
        ticket
        for ticket in Ticket.objects.exclude(issue_url="").only("issue_url", "requested_by")
        if canonical_mr_scope(ticket.issue_url) == scope and (not host or urlsplit(ticket.issue_url).hostname == host)
    ]
    if len(reviewed) > 1:
        message = f"review target {scope} has multiple requester tickets"
        raise ValueError(message)
    return reviewed[0] if reviewed else None


def review_requester(mr_url: str, *, owner_id: str, owner_channel: str) -> ReviewRequester:
    key = requester_key_for_mr(mr_url)
    if not key:
        return ReviewRequester("", owner_id, owner_channel)
    platform, handle = key.split(":", 1)
    identity = OperatorIdentity.objects.filter(
        platform=platform, handle=handle, user__is_active=True, user__is_superuser=True
    ).first()
    if identity is None:
        message = f"requester {key} has no active operator login"
        raise ValueError(message)
    slack = OperatorIdentity.objects.filter(user=identity.user, platform="slack").first()
    if slack is None:
        message = f"requester {key} has no registered Slack identity"
        raise ValueError(message)
    return ReviewRequester(key, slack.handle, "")


def requester_key_for_mr(mr_url: str) -> str:
    ticket = _matching_ticket(mr_url, canonical_mr_scope(mr_url))
    return canonical_operator_key(ticket.requested_by) if ticket is not None and ticket.requested_by else ""
