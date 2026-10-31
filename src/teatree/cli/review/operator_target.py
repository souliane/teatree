"""Resolve a review target to the operator whose forge grant may act on it."""

from dataclasses import dataclass
from urllib.parse import urlsplit

from teatree.cli.review.operator_approval import OperatorApprover
from teatree.core.models import OnBehalfApproval, OperatorIdentity, PullRequest, Ticket
from teatree.core.models.live_post_approval import canonical_mr_scope
from teatree.core.models.operator_identity import canonical_operator_key
from teatree.core.operator_auth.operator_oauth import OperatorOAuthUnavailableError, operator_access_token

_FORGE_LABEL = {"github": "GitHub", "gitlab": "GitLab"}


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


def _sole_consenting_key(target: str, action: str) -> str:
    keys = sorted(
        set(
            OnBehalfApproval.objects.filter(target=target, action=action, consumed_at__isnull=True).values_list(
                "approver_id", flat=True
            )
        )
    )
    remedy = f"`t3 review approve-on-behalf {target} {action} --approver <platform:handle>`"
    if not keys:
        message = f"No consent for {action} on {target}: record one with {remedy}"
        raise PermissionError(message)
    if len(keys) > 1:
        message = f"Several operators consented to {action} on {target} ({', '.join(keys)}); leave exactly one"
        raise PermissionError(message)
    try:
        canonical = canonical_operator_key(keys[0])
    except ValueError:
        canonical = ""
    if canonical != keys[0]:
        message = f"Consent approver {keys[0]!r} is not canonical platform:handle; re-record it with {remedy}"
        raise PermissionError(message)
    return canonical


def resolve_operator(repo: str, number: int, action: str, forge: str) -> tuple[OperatorApprover, str]:
    """The consenting operator and their *forge* grant token for *action* on ``repo!number``.

    The operator is the ticket's requester, or — when the ticket records none — the one operator
    who consented. A personal token is never consulted: no grant means a refusal naming the login.
    """
    target = f"{repo}!{number}"
    try:
        requester = requester_key_for_mr(target) or _sole_consenting_key(target, action)
    except ValueError as error:
        raise PermissionError(str(error)) from error
    if not OnBehalfApproval.objects.filter(
        target=target, action=action, approver_id=requester, consumed_at__isnull=True
    ).exists():
        message = f"No consent from {requester} for {action} on {target}"
        raise PermissionError(message)
    platform, handle = requester.split(":", 1)
    identity = (
        OperatorIdentity.objects.filter(platform=platform, handle=handle, user__is_active=True, user__is_superuser=True)
        .select_related("user")
        .first()
    )
    if identity is None:
        message = f"{requester} has no active operator login"
        raise PermissionError(message)
    forge_identity = OperatorIdentity.objects.filter(user=identity.user, platform=forge).first()
    if forge_identity is None:
        message = f"{requester} has no {_FORGE_LABEL[forge]} operator login"
        raise PermissionError(message)
    try:
        token = operator_access_token(identity.user, forge, action=action, target=target, approver_id=requester)
    except (OperatorOAuthUnavailableError, PermissionError) as error:
        message = f"{requester}: {error}"
        raise PermissionError(message) from error
    return OperatorApprover(requester, forge_identity.key), token
