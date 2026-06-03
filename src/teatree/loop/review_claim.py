"""Single chokepoint for review-claim signals on colleague MRs (#113, #86, #123).

A *review claim* is any signal that tells colleagues "this MR is being
reviewed" — the ``:eyes:`` reaction on a review-broadcast message and the
``slack.review_intent`` dispatch the loop routes to ``t3:reviewer``. The
binding discipline:

1. **Claim only at review-DONE, never at discovery.** The ``:eyes:``
    reaction is a claim; posting it the moment a scanner *finds* an open
    colleague MR tells colleagues the review is happening before any work
    has been done. Discovery scanners therefore never react ``:eyes:`` —
    they only queue the reviewer dispatch. The engagement/outcome reaction
    is posted by the FSM transition path (``add_reactions_for_transition`` /
    ``add_approval_reaction``) once a review actually lands.
2. **Respect "review loop stopped".** When the review mini-loop is
    disabled (``[loops.review] enabled = false`` /
    ``T3_LOOPS_DISABLED=review``), no review-intent signal is queued — the
    loop must not claim work it has been told to stop doing. The state is
    *read* from :class:`LoopsConfig`; this module never invents a new flag.
3. **Dedup against existing reactors.** A reaction already present from a
    colleague or the bot is never re-added — :func:`reaction_already_present`
    consults the live message reactions and the :class:`OutboundClaim`
    ledger before any ``reactions.add``.
4. **Idempotent — no per-tick re-fire.** Every reaction the loop does post
    is recorded in the :class:`OutboundClaim` ``SLACK_REACTION`` ledger,
    keyed on ``(channel, ts, emoji)``, so a second tick finds the claim
    already recorded and skips.

The gate is *fail-safe*: when the review-loop-enabled state cannot be read
(config error, Django not ready) the claim fires — losing a review is
worse than an extra dispatch, and the symptom this module fixes is
over-claiming, which the discovery-time ``:eyes:`` removal already cures.
"""

import logging
from collections.abc import Iterable
from typing import TYPE_CHECKING, cast

from teatree.types import RawAPIDict

if TYPE_CHECKING:
    from teatree.backends.protocols import MessagingBackend
    from teatree.loop.scanners.base import ScanSignal

logger = logging.getLogger(__name__)

_REVIEW_LOOP_NAME = "review"


def review_loop_enabled() -> bool:
    """Read the current review-mini-loop enable state (#79 reads, never invents).

    Resolves through :func:`teatree.config.loop_enabled_by_name` — the same
    env-kill-switch → per-loop → global doctrine the orchestrator and the
    live-tick fan-out apply via :class:`LoopsConfig`, factored into the
    config layer so this :mod:`teatree.loop` module reaches an identical
    verdict without importing :mod:`teatree.loops` (a forbidden up-stack
    dependency). Fail-safe: any read error resolves to enabled so an
    unreadable config never silently suppresses every review — the
    discovery-time claim removal is what cures the over-claim, not this gate.
    """
    try:
        from teatree.loop_enabled import loop_enabled_by_name  # noqa: PLC0415

        return loop_enabled_by_name(_REVIEW_LOOP_NAME)
    except Exception:  # noqa: BLE001 — an unreadable loop config must never wedge the scan.
        logger.debug("review_loop_enabled: config read failed — failing safe to enabled")
        return True


def _reaction_claim_key(*, channel: str, ts: str, emoji: str) -> str:
    return f"slack_reaction:{channel}:{ts}:{emoji}"


def _claim_already_recorded(key: str) -> bool:
    try:
        from teatree.core.models import OutboundClaim  # noqa: PLC0415

        return OutboundClaim.objects.filter(idempotency_key=key).exists()
    except Exception:  # noqa: BLE001 — ledger read must never crash a tick.
        return False


def reaction_already_present(
    *,
    message: RawAPIDict | None,
    channel: str,
    ts: str,
    emoji: str,
) -> bool:
    """True when *emoji* is already on the message (any reactor) or in the ledger.

    Two dedup sources, either is sufficient: the live message ``reactions``
    block (a colleague or the bot already placed it) and the
    :class:`OutboundClaim` ``SLACK_REACTION`` ledger (the loop already
    recorded posting it on a prior tick). Checking both means a reaction is
    never double-posted across reactors or across ticks (#113, #123).
    """
    if message is not None and _emoji_in_reactions(message, emoji):
        return True
    return _claim_already_recorded(_reaction_claim_key(channel=channel, ts=ts, emoji=emoji))


def _emoji_in_reactions(message: RawAPIDict, emoji: str) -> bool:
    reactions = message.get("reactions")
    if not isinstance(reactions, list):
        return False
    for raw in reactions:
        if not isinstance(raw, dict):
            continue
        reaction = cast("RawAPIDict", raw)
        if reaction.get("name") != emoji:
            continue
        users = reaction.get("users")
        count = reaction.get("count")
        if (isinstance(users, list) and users) or (isinstance(count, int) and count > 0):
            return True
    return False


def record_reaction_claim(*, channel: str, ts: str, emoji: str, target_url: str = "") -> None:
    """Record one posted reaction in the :class:`OutboundClaim` ledger (#123).

    Best-effort and idempotent on ``idempotency_key``: a duplicate key
    collapses to a no-op so a re-post on a later tick records nothing new and
    the dedup read above finds the existing row. A ledger-write failure never
    breaks the post it is auditing. Writes the row through the core model
    directly (``teatree.loop`` may not depend on ``teatree.outbound_claim``);
    the field shape matches :func:`teatree.outbound_claim.record_claim`.
    """
    try:
        from teatree.core.models import OutboundClaim  # noqa: PLC0415

        OutboundClaim.objects.get_or_create(
            idempotency_key=_reaction_claim_key(channel=channel, ts=ts, emoji=emoji),
            defaults={"kind": OutboundClaim.Kind.SLACK_REACTION.value, "target_url": target_url},
        )
    except Exception:  # noqa: BLE001 — ledger write must never break the post it audits.
        logger.debug("record_reaction_claim: ledger write failed for %s/%s :%s:", channel, ts, emoji)


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
    emoji is posted at most once: skipped when already present (a colleague
    or the bot) and recorded in the :class:`OutboundClaim` ledger so a later
    tick does not re-fire it. Reacting routes through
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
    posted: list[str] = []
    for emoji in emojis:
        if reaction_already_present(message=None, channel=channel, ts=ts, emoji=emoji):
            continue
        if _react_routed(messaging, channel=channel, ts=ts, emoji=emoji):
            record_reaction_claim(channel=channel, ts=ts, emoji=emoji, target_url=target_url)
            posted.append(emoji)
    return posted


def _react_routed(messaging: "MessagingBackend", *, channel: str, ts: str, emoji: str) -> bool:
    """React via the token-routed path; True on success, False on any failure.

    Treats a Slack ``already_reacted`` response as success — the desired end
    state is the emoji being present. A transport error never crashes the
    caller (a review verdict is recorded regardless of the Slack signal).
    """
    try:
        response = messaging.react_routed(channel=channel, ts=ts, emoji=emoji)
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
        from teatree.core.models import ReviewRequestPost  # noqa: PLC0415
        from teatree.utils.url_slug import pr_ref_from_url  # noqa: PLC0415

        for post in ReviewRequestPost.objects.exclude(slack_thread_ts="").iterator():
            ref = pr_ref_from_url(post.mr_url)
            if ref is not None and ref.slug == slug and ref.number == pr_id and post.slack_channel_id:
                return post.slack_channel_id, post.slack_thread_ts, post.mr_url
    except Exception:  # noqa: BLE001 — ledger read must never crash the caller.
        return None
    return None


def filter_review_intent_signals(signals: Iterable["ScanSignal"]) -> list["ScanSignal"]:
    """Drop every review-intent signal when the review loop is stopped (rule 2).

    A no-op when the review loop is enabled. When stopped, returns ``[]`` so
    no reviewer dispatch is queued and no statusline claim is shown — the
    loop respects "stop the review loop" without inventing a new flag.
    """
    if review_loop_enabled():
        return list(signals)
    return []


__all__ = [
    "emit_review_done_reactions",
    "filter_review_intent_signals",
    "reaction_already_present",
    "record_reaction_claim",
    "review_loop_enabled",
]
