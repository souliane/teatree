"""Is this review request PAUSED right now — read live, never cached.

The owner holds a review request by reacting to its Slack message with
``:double_vertical_bar:`` or ``:pause_button:``: "I have more to fix, do not
count this as ready". The reaction IS the state. Nothing here writes a ``paused`` flag,
because a cached one is the failure where the owner lifts the reaction and the
request stays held forever with no signal that anything is wrong — the reaction
is gone, so there is nothing left to look at and no way to notice.

Every failure answers :attr:`PauseState.UNKNOWN`. A transport that errors, a
payload that comes back empty, and a caller holding no messaging backend are
each indistinguishable from "not paused" to a caller downstream, so the
distinction has to be made HERE: a probe that answers "not paused" on a failed
read makes the hold mechanism inert exactly when it is needed, silently. The
caller decides what an UNKNOWN costs it (skip this tick, retry, surface); the
reader's job is to never manufacture a confident answer it does not have.

Read from the thread ROOT (``fetch_message`` on the post's own
``slack_thread_ts``), because that is the message the owner reacts to and the
only one whose ``reactions`` array carries the hold.
"""

import logging
from enum import StrEnum
from typing import cast

from teatree.core.backend_protocols import MessagingBackend
from teatree.core.models import ReviewRequestPost
from teatree.types import RawAPIDict

logger = logging.getLogger(__name__)

#: The reactions that mean "paused — not reviewable yet". Two glyphs for one meaning,
#: because Slack renders the pause symbol under both names depending on the client.
_PAUSE_REACTIONS: frozenset[str] = frozenset({"double_vertical_bar", "pause_button"})


class PauseState(StrEnum):
    """Whether a review request is held, free, or unreadable — three answers, never two."""

    PAUSED = "paused"
    NOT_PAUSED = "not_paused"
    UNKNOWN = "unknown"


def read_pause_state(post: ReviewRequestPost, messaging: MessagingBackend | None) -> PauseState:
    if messaging is None or not post.slack_channel_id or not post.slack_thread_ts:
        return PauseState.UNKNOWN
    try:
        message = messaging.fetch_message(channel=post.slack_channel_id, ts=post.slack_thread_ts)
        if not message:
            return PauseState.UNKNOWN
        return PauseState.PAUSED if _carries_pause_reaction(message) else PauseState.NOT_PAUSED
    except Exception:
        logger.exception("pause read failed for %s — answering UNKNOWN rather than NOT_PAUSED", post.mr_url)
        return PauseState.UNKNOWN


def _carries_pause_reaction(message: RawAPIDict) -> bool:
    """Whether the root message carries a configured pause reaction.

    A message with no ``reactions`` key is a genuine empty — Slack omits the
    array when nothing is reacted — and reaches here only on a payload the read
    already proved non-empty, so it is NOT_PAUSED rather than a refusal.
    """
    reactions = message.get("reactions")
    if not isinstance(reactions, list):
        return False
    return any(
        isinstance(reaction, dict) and cast("RawAPIDict", reaction).get("name") in _PAUSE_REACTIONS
        for reaction in reactions
    )
