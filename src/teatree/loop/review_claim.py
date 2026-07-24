"""Review-DONE reaction chokepoint on colleague MRs (#113, #86, #88, #123).

A *review claim* is any signal that tells colleagues "this MR is being
reviewed" — the ``:eyes:`` reaction on a review-broadcast message and the
``slack.review_intent`` dispatch the loop routes to ``t3:reviewer``. The
binding discipline:

1. **Claim only at review-DONE, never at discovery.** The ``:eyes:``
    reaction is a claim; posting it the moment a scanner *finds* an open
    colleague MR tells colleagues the review is happening before any work
    has been done. Discovery scanners therefore never react ``:eyes:`` —
    they only queue the reviewer dispatch (the discovery-time filtering and
    dedup live in :mod:`teatree.loop.review_claim_signals`, carved below the
    scanners). The engagement/outcome reaction is posted by the FSM
    transition path (``add_reactions_for_transition`` /
    ``add_approval_reaction``) once a review actually lands — this module is
    that outcome path.
2. **Respect "review loop stopped".** When the review mini-loop is
    disabled (``t3 loop disable review`` — a durable DB ``LoopState`` hold),
    no review-intent signal is queued — the discovery stratum reads that state
    from :func:`teatree.loop.loop_state_db.loop_held_in_db`.
3. **Dedup against existing reactors.** A reaction already present from a
    colleague or the bot is never re-added — :func:`reaction_already_present`
    consults the live message reactions and the :class:`OutboundClaim`
    ledger before any ``reactions.add``. The outcome path fetches that live
    message itself (#3564); passing ``message=None`` there silently reduced the
    predicate to a ledger-only read, so a colleague's reaction was never seen.
4. **Idempotent — no per-tick re-fire.** Every reaction the loop does post
    is recorded in the :class:`OutboundClaim` ``SLACK_REACTION`` ledger,
    keyed on ``(channel, ts, emoji)``, so a second tick finds the claim
    already recorded and skips.

Both strata live in leaves below :mod:`teatree.loop.scanners` — the discovery
primitives (``filter_review_intent_signals`` / ``reaction_already_present`` /
``record_reaction_claim`` / ``review_loop_enabled``) in
:mod:`teatree.loop.review_claim_signals`, the outcome primitive
(``emit_review_done_reactions``) in :mod:`teatree.loop.review_done_reactions` —
so a scanner reaches either without an up-edge into this orchestration-top
module. This module is the named chokepoint the discipline above is documented
on, and re-exports both strata for the orchestration call sites.
"""

from teatree.loop.review_claim_signals import (
    filter_review_intent_signals,
    reaction_already_present,
    record_reaction_claim,
    review_loop_enabled,
)
from teatree.loop.review_done_reactions import emit_review_done_reactions

__all__ = [
    "emit_review_done_reactions",
    "filter_review_intent_signals",
    "reaction_already_present",
    "record_reaction_claim",
    "review_loop_enabled",
]
