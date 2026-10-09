"""Has anyone been asked to review this merge request — the fact ``MrFacts.review_request`` carries.

The ``ReviewRequestPost`` ledger can only ever answer REQUESTED. A request made
before the ledger existed, or in a repo whose channel nothing watches, leaves no
row either, so an absent row is silence rather than evidence — and the triage
ladder's whole review-request rung sits behind NONE, which no ledger can supply.

NONE is therefore EARNED from the review channel: the same recency-bounded live
read the #1084 dedup guard posts behind, so the surveyor and the poster can never
disagree about whether an ask is already out there. It is claimed only when the
read SUCCEEDED and its window reaches back past the merge request's own opening —
a clean thirty-day read says nothing about a merge request opened ninety days ago.

Every other outcome is UNKNOWN, and the ladder answers UNKNOWN with an owner
question rather than a broadcast. That polarity is the point: over-claiming NONE
posts a duplicate review request into a colleague channel, while over-claiming
UNKNOWN costs one question the owner can answer in a word.
"""

import datetime as dt
from collections.abc import Iterable

from teatree.core.gates.review_request_guard import GuardOptions, GuardTarget, LiveAskRead, read_live_asks
from teatree.core.review.mr_triage import ReviewRequestState


def read_asks(
    mr_urls: Iterable[str],
    *,
    target: GuardTarget | None,
    options: GuardOptions | None = None,
) -> LiveAskRead | None:
    """The ONE channel read every merge request in a tick is answered from.

    ``None`` — an unpostable channel and a failed read alike — leaves every
    :func:`mr_ask_state` verdict built on it at UNKNOWN.
    """
    if target is None:
        return None
    return read_live_asks(mr_urls, target, options=options)


def mr_ask_state(mr_url: str, *, opened_at: dt.datetime | None, read: LiveAskRead | None) -> ReviewRequestState:
    """Whether anyone has been asked to review *mr_url*, per the channel *read*.

    *opened_at* is the merge request's own creation time; without it the read's
    coverage cannot be established and the answer stays UNKNOWN.
    """
    if read is None:
        return ReviewRequestState.UNKNOWN
    if read.carries(mr_url):
        return ReviewRequestState.REQUESTED
    if opened_at is None or opened_at < read.since:
        return ReviewRequestState.UNKNOWN
    return ReviewRequestState.NONE


__all__ = ["mr_ask_state", "read_asks"]
