"""The compact backlog token a two-call issue create binds its judgments to."""

import hashlib
from collections.abc import Iterable


def snapshot_token_for_urls(urls: Iterable[str]) -> str:
    return hashlib.sha256("\n".join(sorted(set(urls))).encode()).hexdigest()[:16]


def appeared_since_snapshot(token: str, *, judged_urls: set[str], live_urls: Iterable[str]) -> tuple[str, ...]:
    """The open tickets nobody judged; a *token* that does not name the judged set is refused."""
    if token != snapshot_token_for_urls(judged_urls):
        msg = "snapshot_token does not match the judged candidates; judge the full snapshot again"
        raise ValueError(msg)
    return tuple(sorted(set(live_urls) - judged_urls))
