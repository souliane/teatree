"""Who the broadcast scanner must never mistake for a colleague, and its forge probe (#159).

The three wiring pieces :func:`teatree.loop.scanner_factories._slack_broadcasts_scanner_for`
hands the scanner: the owner's forge identity for the own-MR skip (#1844), the wider
self-set of note authors, and the production ``review_taken`` probe. Carved out of
``scanner_factories`` by concern (module-health cap), like its sibling
``scanner_factory_config``.
"""

import logging
from typing import TYPE_CHECKING

from teatree.core.backend_factory import OverlayBackends
from teatree.core.review.review_taken import ReviewTaken, review_taken_by_other
from teatree.loop.scanners.slack_broadcast_claims import ReviewTakenProbe

if TYPE_CHECKING:
    from teatree.core.overlay import OverlayBase

logger = logging.getLogger(__name__)


def _own_author_identity(backend: OverlayBackends) -> str:
    """Resolve the user's forge username for the own-MR review skip (#1844 L3).

    The own-author ``:eyes:``-and-dispatch skip in
    :class:`SlackBroadcastsScanner` needs to know who "we" are. Deriving
    this from ``overlay.config.get_gitlab_username()`` breaks for every
    overlay that leaves the getter at the core default ``""`` — an empty
    value disables the skip and the loop reviews the user's own MRs. The
    self-identity source of truth is the same one
    :class:`ReviewerPrsScanner` uses: ``backend.identities`` (the
    multi-alias operator set) with a ``host.current_user()`` fallback, so
    the skip works regardless of whether an overlay implements the getter.
    """
    if backend.identities:
        return backend.identities[0]
    for host in backend.hosts:
        user = host.current_user()
        if user:
            return user
    return ""


def _self_forge_identities(backend: OverlayBackends) -> tuple[str, ...]:
    """The owner's aliases plus each host's token identity — note authors who are never a colleague (#159).

    The factory posts its own findings under the host token's identity, which is not
    necessarily one of the owner's aliases; without it the factory's earlier notes
    would read as a colleague's and stop its own re-review of a new head.
    """
    names = list(backend.identities)
    for host in backend.hosts:
        try:
            names.append(host.current_user())
        except Exception:  # a host that cannot name itself narrows the self-set; it never blocks the tick
            logger.warning(
                "current_user() failed on a code host — its own notes may read as a colleague's", exc_info=True
            )
    return tuple(dict.fromkeys(name for name in names if name))


def _review_taken_probe(overlay: "OverlayBase", self_identities: tuple[str, ...]) -> ReviewTakenProbe:
    """The production ``review_taken`` probe: resolve the URL's host with the overlay's tokens, then read (#159)."""

    def probe(pr_url: str) -> ReviewTaken:
        from teatree.backends.loader import get_code_host_for_url  # noqa: PLC0415 — deferred: tick-time import

        try:
            host = get_code_host_for_url(overlay, pr_url)
        except Exception:  # an unresolvable host is UNKNOWN, never FREE
            logger.warning("review-taken probe could not resolve a code host for %s", pr_url, exc_info=True)
            return ReviewTaken.UNKNOWN
        return review_taken_by_other(pr_url, self_identities=self_identities, host=host)

    return probe


__all__ = ["_own_author_identity", "_review_taken_probe", "_self_forge_identities"]
