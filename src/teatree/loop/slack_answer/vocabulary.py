"""The reaction vocabulary for the owner's DM surface — one symbol, one meaning.

The owner reads Slack and nothing else, so a reaction is not decoration: it is
the whole status report for a message. Receipt and completion used the SAME
symbol, which made "I have seen this" and "this is handled" indistinguishable —
and a :white_check_mark: on an unanswered question is worse than no reaction at
all, because it tells the owner to stop waiting for an answer that is never
coming.

Four symbols, deliberately disjoint, ordered by how much they claim:

======================  ===============================================================
:eyes:                  RECEIVED. The message is in the queue. Claims nothing else.
:hammer_and_wrench:     IN FLIGHT. A lane exists for this — newly dispatched, or one
                        that already covered it. Explicitly NOT finished.
:pray:                  NOTED. Read, needs no reply and no work (thanks, an FYI).
                        Terminal, but not a completion claim.
:white_check_mark:      DONE. The thing asked for is finished and its result is
                        visible to the owner.
======================  ===============================================================

:data:`InboundReaction.DONE` carries the only claim that can mislead, so it has
one rule with no exceptions: **it is placed only after the completion it claims
has been verified** — for an answer, that means the reply was posted AND a
thread read-back saw it (:func:`~teatree.loop.slack_answer.cycle.verify_reply_visible`).
Dispatching work is not completing it; being already covered is not completing
it; acknowledging a message is not completing it. Those get
:data:`~InboundReaction.IN_FLIGHT` or :data:`~InboundReaction.NOTED`.
"""

from enum import StrEnum


class InboundReaction(StrEnum):
    """Slack emoji names for the four states an inbound owner message can be in."""

    RECEIVED = "eyes"
    IN_FLIGHT = "hammer_and_wrench"
    NOTED = "pray"
    DONE = "white_check_mark"


#: The reactions that make no claim about the work being finished. A path that
#: cannot prove completion must pick from this set — pinned by the conformance
#: test so a future route cannot quietly reach for :attr:`InboundReaction.DONE`.
NON_COMPLETION_REACTIONS: frozenset[InboundReaction] = frozenset(
    {InboundReaction.RECEIVED, InboundReaction.IN_FLIGHT, InboundReaction.NOTED}
)


__all__ = ["NON_COMPLETION_REACTIONS", "InboundReaction"]
