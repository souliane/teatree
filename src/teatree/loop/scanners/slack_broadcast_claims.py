"""Somebody else already took this review — the broadcast scanner's stop logic (#159).

The Slack-side sign is :func:`teatree.core.review.review_candidate.broadcast_claimed_by_other`
(the scanner calls it inline); this module holds the two pieces around it: the
self-set those reactions are judged against, and the forge-side probe that pins a
broadcast ``taken``. Carved out of :mod:`teatree.loop.scanners.slack_broadcasts` by
concern (module-health cap); ``MrState`` is imported for typing only so the two
modules never cycle at runtime.
"""

import logging
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

from teatree.core.backend_protocols import MessagingBackend
from teatree.core.models import ScannedBroadcast
from teatree.core.review.review_taken import ReviewTaken

if TYPE_CHECKING:
    from teatree.loop.scanners.slack_broadcasts import MrState

logger = logging.getLogger(__name__)

ReviewTakenProbe = Callable[[str], ReviewTaken]
"""Function answering whether somebody else already reviews the MR at a URL.

Injected into the scanner like its ``MrStateClassifier``; the production wiring
resolves the URL's code host with the overlay's credentials and reads notes +
approvals through :func:`teatree.core.review.review_taken.review_taken_by_other`.
"""


def resolve_self_slack_ids(backend: MessagingBackend, *, user_id: str) -> frozenset[str]:
    """The owner's Slack id plus the bot's own — the reactors who are never a colleague.

    The factory's review-DONE reactions land under the bot in an internal channel
    (``token_policy.channel_token``), so without the bot's id its own ``:eyes:`` would
    read as a colleague's claim and stop the next review.
    """
    ids = {user_id}
    try:
        bot_user = backend.auth_test().get("user_id")
    except Exception:
        logger.debug("auth.test unreadable — the reaction self-set narrows to the owner's id", exc_info=True)
        bot_user = None
    if isinstance(bot_user, str):
        ids.add(bot_user)
    return frozenset(sid for sid in ids if sid)


def not_taken_on_the_forge(
    row: ScannedBroadcast,
    open_states: Sequence["MrState"],
    probe: ReviewTakenProbe | None,
) -> list["MrState"]:
    """The open MRs nobody else reviews yet; pins the row ``taken`` when that is none of them.

    ``UNKNOWN`` defers rather than dispatches: the row stays awaiting, so the next tick
    asks again — a delayed review recovers, a second review under the owner's identity
    does not. ``None`` (no probe wired) keeps a legacy caller on the Slack-side signals.
    """
    if probe is None:
        return list(open_states)
    verdicts = {state.url: probe(state.url) for state in open_states}
    free = [state for state in open_states if verdicts[state.url] is ReviewTaken.FREE]
    if not free:
        if all(verdict is ReviewTaken.TAKEN for verdict in verdicts.values()):
            row.mark_manually_classified(ScannedBroadcast.Classification.TAKEN)
        else:
            logger.warning("SlackBroadcastsScanner: review-taken probe UNKNOWN for %s — deferring dispatch", row)
    return free


__all__ = ["ReviewTakenProbe", "not_taken_on_the_forge", "resolve_self_slack_ids"]
