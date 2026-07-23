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

The discovery-stratum primitives (``filter_review_intent_signals`` /
``reaction_already_present`` / ``record_reaction_claim`` /
``review_loop_enabled``) now live in :mod:`teatree.loop.review_claim_signals`,
a leaf below :mod:`teatree.loop.scanners`, so a scanner reaches them without an
up-edge into this orchestration-top module. This module re-exports them for the
existing call sites and adds the outcome stratum (``emit_review_done_reactions``)
that builds on them.
"""

import logging
from collections.abc import Iterable
from typing import TYPE_CHECKING

from teatree.core.on_behalf_egress import OnBehalfPostBlockedError, OnBehalfSlackEgress
from teatree.loop.review_claim_signals import (
    filter_review_intent_signals,
    reaction_already_present,
    record_reaction_claim,
    review_loop_enabled,
)

if TYPE_CHECKING:
    from teatree.core.backend_protocols import MessagingBackend
    from teatree.types import RawAPIDict

logger = logging.getLogger(__name__)


def emit_review_done_reactions(
    *,
    slug: str,
    pr_id: int,
    emojis: Iterable[str],
    messaging: "MessagingBackend | None",
) -> list[str]:
    """Post the review-DONE reaction set on the PR's Slack message (#113/#88).

    The ONLY Slack signal a finished review produces: ``:eyes:`` (review is
    DONE — never posted at claim/start) plus the verdict emoji
    (``:white_check_mark:`` clean / ``:question:`` has blocking comments).
    The substance of the review is the GitLab inline comments; this never
    DMs or messages the author.

    Resolves the message coordinates from the :class:`ReviewRequestPost`
    ledger by matching ``(slug, pr_id)`` against each tracked MR URL. Each
    emoji is posted at most once: skipped when already present and recorded in
    the :class:`OutboundClaim` ledger so a later tick does not re-fire it.
    "Already present" spans BOTH dedup sources — the live message is fetched once
    per emission (:func:`_live_message`) so a COLLEAGUE's reaction is seen, not only
    teatree's own ledger (#3564). Reacting routes through
    :meth:`MessagingBackend.react_routed` so a colleague/channel message
    goes out under the personal ``xoxp`` token (#1750). Returns the emojis
    actually posted; ``[]`` when the PR has no tracked Slack message or no
    messaging backend is available.
    """
    if messaging is None:
        return []
    resolved = _slack_message_for_pr(slug, pr_id)
    if resolved is None:
        return []
    channel, ts, target_url = resolved
    egress = OnBehalfSlackEgress(messaging)
    live = _live_message(messaging, channel=channel, ts=ts)
    posted: list[str] = []
    for emoji in emojis:
        if reaction_already_present(message=live, channel=channel, ts=ts, emoji=emoji):
            continue
        try:
            reacted = _egress_react(egress, channel=channel, ts=ts, emoji=emoji, target_url=target_url)
        except OnBehalfPostBlockedError as blocked:
            logger.info("emit_review_done_reactions: review-DONE reaction gated: %s", blocked)
            break
        if reacted:
            record_reaction_claim(channel=channel, ts=ts, emoji=emoji, target_url=target_url)
            posted.append(emoji)
    return posted


def _live_message(messaging: "MessagingBackend", *, channel: str, ts: str) -> "RawAPIDict | None":
    """The tracked message with its live ``reactions``, or ``None`` when unreadable.

    Read once per emission rather than per emoji, so the colleague-dedup costs one
    Slack call. ``None`` degrades the dedup to the :class:`OutboundClaim` ledger alone:
    an unreadable message must not stop a review verdict from being signalled, and the
    worst case is the double-react the ledger already prevents across ticks.
    """
    try:
        return messaging.fetch_message(channel=channel, ts=ts)
    except Exception:  # noqa: BLE001 — an unreadable message degrades dedup, never blocks the signal
        logger.info("emit_review_done_reactions: live reactions unreadable for %s/%s", channel, ts)
        return None


def _egress_react(
    egress: OnBehalfSlackEgress,
    *,
    channel: str,
    ts: str,
    emoji: str,
    target_url: str,
) -> bool:
    """React via the gated egress; True when the emoji is present, False on transport failure.

    Treats a Slack ``already_reacted`` response as success — the desired end
    state is the emoji being present. A transport error never crashes the
    caller (a review verdict is recorded regardless of the Slack signal). A
    BLOCK verdict propagates as :class:`OnBehalfPostBlockedError` for the
    caller to surface.
    """
    try:
        response = egress.react(
            channel=channel,
            ts=ts,
            emoji=emoji,
            target=target_url,
            action=f"review_done_reaction:{emoji}",
            destination=f"review-request for {target_url}",
            artifact_url=target_url,
            summary=f":{emoji}: review-DONE reaction",
        )
    except OnBehalfPostBlockedError:
        raise
    except Exception as exc:  # noqa: BLE001 — a Slack failure must not break verdict recording.
        logger.warning("emit_review_done_reactions: react failed for %s/%s :%s:: %s", channel, ts, emoji, exc)
        return False
    if not isinstance(response, dict):
        return False
    error = str(response.get("error") or "")
    return bool(response.get("ok")) or error == "already_reacted"


def _slack_message_for_pr(slug: str, pr_id: int) -> tuple[str, str, str] | None:
    """Resolve ``(channel, ts, mr_url)`` for ``(slug, pr_id)`` from the request ledger.

    Reads the :class:`ReviewRequestPost` rows (the canonical "where was this
    MR posted" record) and matches the first whose tracked URL parses to the
    same ``(slug, pr_id)``. Returns ``None`` when there is no tracked Slack
    message for the PR — a review of an MR that was never broadcast has no
    Slack signal to post.
    """
    if not slug or not pr_id:
        return None
    try:
        from teatree.core.models import ReviewRequestPost  # noqa: PLC0415 — deferred: ORM import needs the app registry
        from teatree.utils.url_slug import pr_ref_from_url  # noqa: PLC0415 — deferred: loaded at tick time, not import

        for post in ReviewRequestPost.objects.exclude(slack_thread_ts="").iterator():
            ref = pr_ref_from_url(post.mr_url)
            if ref is not None and ref.slug == slug and ref.pr_id == pr_id and post.slack_channel_id:
                return post.slack_channel_id, post.slack_thread_ts, post.mr_url
    except Exception:  # noqa: BLE001 — ledger read must never crash the caller.
        return None
    return None


__all__ = [
    "emit_review_done_reactions",
    "filter_review_intent_signals",
    "reaction_already_present",
    "record_reaction_claim",
    "review_loop_enabled",
]
