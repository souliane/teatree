"""Discovery-time review-claim primitives shared by scanners and the review chokepoint.

The review-claim discipline (#113 / #86 / #123) has two strata. The
*discovery* stratum — signal filtering and reaction dedup — is consumed by
the scanners (``slack_broadcasts`` queues review-intent dispatches and dedups
reactions; ``slack_review_intent`` filters the same signals). The *outcome*
stratum — posting the review-DONE reaction set — lives in
:mod:`teatree.loop.review_claim` and is driven by the FSM / ``t3 teatree review record``
path. The outcome stratum builds on this one.

This module is the discovery stratum, carved DOWN below
:mod:`teatree.loop.scanners` so a scanner imports it without an up-edge into
the orchestration top. Its only eager dependency is :mod:`teatree.types`; the
``core.models`` / ``utils`` reads (via :func:`loop_held_in_db`) are deferred
(fail-safe, call-time), so it is a true leaf in the loop dependency DAG.
"""

import logging
from collections.abc import Iterable
from typing import TYPE_CHECKING, cast

from teatree.loop.loop_state_db import loop_held_in_db
from teatree.types import RawAPIDict

if TYPE_CHECKING:
    from teatree.loop.scanners.base import ScanSignal

logger = logging.getLogger(__name__)

_REVIEW_LOOP_NAME = "review"


def review_loop_enabled() -> bool:
    """Read the current review-mini-loop enable state (#79 reads, never invents).

    DB-only: resolves through the durable ``LoopState`` control tier (#1913) via
    :func:`teatree.loop.loop_state_db.loop_held_in_db`. A ``PAUSED`` / ``DISABLED``
    ``LoopState`` row durably stops review claims across a restart; an absent row
    or a runnable one leaves them running. This is the discovery-time claim gate,
    not a loop-run decision — it fails OPEN to enabled by design (#79 / #1913), so
    it intentionally does NOT read the ``Loop.enabled`` column (the loop-run sites
    — the loop tick and the off-live-tick loop gates — combine ``Loop.enabled``
    AND the ``LoopState`` hold via
    :func:`teatree.loop.loop_state_db.loop_state_admits`; this chokepoint
    suppresses over-claiming and must never silently swallow every review on an
    unreadable or absent row). There is no env kill-switch and no ``[loops]`` toml
    fallback — loop control is ``/loops`` + the DB only.

    Fail-safe: any read error resolves to enabled so an unreadable source never
    silently suppresses every review — the discovery-time claim removal is what
    cures the over-claim, not this gate.
    """
    try:
        return not loop_held_in_db(_REVIEW_LOOP_NAME)
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
    "filter_review_intent_signals",
    "reaction_already_present",
    "record_reaction_claim",
    "review_loop_enabled",
]
