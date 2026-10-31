"""A carrier PR declares its merge round with one ``Carries: <url> <url>`` line; its members freeze."""

import re
from collections.abc import Sequence

from teatree.core.models import PullRequest, Ticket
from teatree.core.models.carrier_freeze import frozen_by_carrier
from teatree.url_classify import repo_and_iid

__all__ = ["carried_pr_ids", "frozen_by_carrier", "parse_carries", "record_carry_freeze", "record_published_round"]

_CARRIES_LINE = re.compile(r"^Carries:[ \t]*(?P<urls>.*)$", re.MULTILINE)
_TRAILING_PUNCTUATION = ",.;)"


def parse_carries(body: str) -> tuple[str, ...]:
    """The URLs of the body's single ``Carries:`` line; two or more such lines are ambiguous and declare nothing."""
    lines = _CARRIES_LINE.findall(body)
    if len(lines) != 1:
        return ()
    tokens = (token.rstrip(_TRAILING_PUNCTUATION) for token in lines[0].split())
    return tuple(token for token in tokens if token.startswith(("http://", "https://")))


def carried_pr_ids(body: str, *, repo: str) -> frozenset[int]:
    """The numbers of *repo*'s PRs the body's ``Carries:`` line names; other repos' PRs are not this repo's heads."""
    pairs = (repo_and_iid(url) for url in parse_carries(body))
    return frozenset(pair[1] for pair in pairs if pair and pair[0].casefold() == repo.casefold())


def record_carry_freeze(*, carrier_ticket: Ticket, carrier_url: str, member_urls: Sequence[str]) -> None:
    """Declare the round as exactly *member_urls*: freeze each member, release one the carrier no longer names."""
    dropped = [url for url in carrier_ticket.extra.get("carries") or [] if url not in member_urls]
    for pull_request in PullRequest.objects.filter(url__in=dropped).select_related("ticket"):
        if pull_request.ticket.extra.get("carried_by") == carrier_url:
            pull_request.ticket.merge_extra(pop_keys=["carried_by"])
    members = PullRequest.objects.filter(url__in=member_urls).exclude(ticket=carrier_ticket).select_related("ticket")
    for pull_request in members:
        pull_request.ticket.merge_extra(set_keys={"carried_by": carrier_url})
    if member_urls:
        carrier_ticket.merge_extra(set_keys={"carries": list(member_urls)})
    else:
        carrier_ticket.merge_extra(pop_keys=["carries"])


def record_published_round(*, carrier_url: str, member_urls: Sequence[str]) -> bool:
    """Record the round a published carrier body declares; ``False`` when no ticket tracks the carrier PR."""
    carrier = PullRequest.objects.filter(url=carrier_url).select_related("ticket").first()
    if carrier is None:
        return False
    record_carry_freeze(carrier_ticket=carrier.ticket, carrier_url=carrier_url, member_urls=member_urls)
    return True
