"""The PR's live head, read when a returned review verdict is recorded (#4737).

A dispatch pins the head SHA when it arms the review, and the factory pushes fix commits
to its own PRs while that review runs. Without this read the recorder can only compare a
returned ``reviewed_sha`` against the pinned SHA, so a reviewer that checked out the PR's
CURRENT head — the correct thing to do — is refused exactly like one that wandered onto
an unrelated tree, and the only correct verdict available is discarded.

Every failure to reach the forge answers UNREADABLE rather than an empty SHA, because the
caller's whole job is telling "the head moved" apart from "nobody could say what the head
is": the first supersedes the claim, the second must leave it alone and retry.
"""

import logging
from typing import Protocol

from teatree.core.modelkit.forge_readability import LiveHeadRead

logger = logging.getLogger(__name__)


class LiveHeadProbe(Protocol):
    """Reads a pull request's current head SHA. Satisfied by :func:`live_head_at`."""

    def __call__(self, *, slug: str, pr_id: int, host_kind: str) -> LiveHeadRead: ...  # pragma: no branch


def live_head_at(*, slug: str, pr_id: int, host_kind: str = "github") -> LiveHeadRead:
    """The head *slug*``#``*pr_id* currently points at, or an UNREADABLE read.

    Never raises and never degrades a read failure to an empty SHA — an empty head
    compares unequal to every reviewed tree, so a forge hiccup would read as a branch
    that moved and spend a claim the reviewer could still have satisfied.
    """
    from teatree.core.merge.ci_rollup import CodeHostQuery  # noqa: PLC0415 — deferred: core.merge cycle
    from teatree.utils.pr_ref import PrRef  # noqa: PLC0415 — deferred: paired with the query above

    try:
        return CodeHostQuery.for_ref(PrRef(slug=slug, pr_id=pr_id, host_kind=host_kind)).live_head_read()
    except Exception:
        logger.warning("live_head_at: could not read the head of %s#%d", slug, pr_id, exc_info=True)
        return LiveHeadRead(sha="", unreadable=True)


__all__ = ["LiveHeadProbe", "live_head_at"]
