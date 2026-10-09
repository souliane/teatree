"""Post-review FSM-state classification for a ticket (PR-08b).

Kept out of ``ticket.py`` (module-health LOC cap): the review-request state
gate is the only consumer, and ``ticket.py`` does not import this module, so a
runtime ``Ticket`` import here is cycle-free.
"""

from teatree.core.models.ticket import Ticket

# States AT or AFTER the SELF_REVIEWED milestone on the maker chain (review → ship →
# request_review → mark_merged → retrospect → mark_delivered). The
# review-request broadcast fires at request_review time (PR_OPENED → REVIEW_REQUESTED),
# so a canonically-progressed ticket sits in PR_OPENED/REVIEW_REQUESTED — not the
# momentary SELF_REVIEWED — when its request goes out. A strict ``state == SELF_REVIEWED``
# review-request gate therefore refused every progressed ticket. Pre-review
# states and the abandoned IGNORED state (reachable from any state, so never a
# post-review signal) are excluded. Enumerated explicitly with a completeness
# partition assertion in the tests so a future-added State member is classified
# consciously rather than silently defaulting to "not passed review".
POST_REVIEW_STATES: frozenset[str] = frozenset(
    {
        Ticket.State.SELF_REVIEWED,
        Ticket.State.PR_OPENED,
        Ticket.State.REVIEW_REQUESTED,
        Ticket.State.MERGED,
        Ticket.State.RETRO_RECORDED,
        Ticket.State.DELIVERED,
    },
)


def has_passed_review(ticket: Ticket) -> bool:
    """True when the FSM reached the SELF_REVIEWED milestone or a later maker state.

    The review-request state gate consumes this instead of a strict
    ``state == SELF_REVIEWED`` check: the broadcast fires at ``request_review`` time
    (PR_OPENED → REVIEW_REQUESTED), so the realistic broadcast-time state is REVIEW_REQUESTED,
    not the momentary SELF_REVIEWED. Pre-review states (NOT_STARTED…TESTED) and the
    abandoned IGNORED state return False.
    """
    return ticket.state in POST_REVIEW_STATES
