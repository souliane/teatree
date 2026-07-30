"""👀-back self-ack when the owner reacts to teatree's OWN message.

When the owner reacts (any emoji) to a message teatree ITSELF authored,
teatree posts a ``:eyes:`` reaction back on that same message so the owner
sees the loop noticed their reaction. This collaborator implements ONLY the
self-ack decision + idempotent post; it does NOT drain the reactions queue.

The ``slack-reactions.jsonl`` atomic-rename drain is single-consumer and owned
by :class:`teatree.loop.scanners.slack_review_intent.SlackReviewIntentScanner`
— a second on-disk drain would race the rename (#1047). So the review-intent
scanner (the single drain owner) hands the already-drained reaction snapshot to
:meth:`SlackSelfAckReactor.ack_owner_reactions`, which is why this rides inside
that scanner's reaction pass rather than as its own JSONL-draining scanner.

Per ``reaction_added`` the reactor acks iff BOTH hold:

* the reacting ``event.user`` is the owner (``backend.user_id``), and
* the reacted ``item`` message is bot-authored — resolved via
    ``backend.fetch_message`` + the own-identity machinery
    (:func:`resolve_own_identity` / :func:`is_self_authored`) the DM-inbound
    self-filter uses.

The ack is idempotent on ``(overlay, channel, item_ts)`` via
:class:`~teatree.core.models.slack_self_ack.SlackSelfAckReaction`: the claim is
taken BEFORE the post and released (row deleted) if the post raises, so a
transient Slack failure retries next tick instead of carrying a phantom ack.
Fail-closed on an unresolved bot identity (mirrors the DM-inbound scanner):
without it the reactor cannot prove a message is the bot's own, so it acks
nothing that turn rather than 👀 a message teatree did not write.
"""

import logging
from dataclasses import dataclass, field
from typing import cast

from teatree.core.backend_protocols import MessagingBackend
from teatree.core.models.slack_self_ack import SlackSelfAckReaction
from teatree.loop.scanners.slack_self_filter import OwnSlackIdentity, is_self_authored, resolve_own_identity
from teatree.types import RawAPIDict

logger = logging.getLogger(__name__)

_EYES_EMOJI = "eyes"


def _event_user(event: RawAPIDict) -> str:
    user = event.get("user")
    return user if isinstance(user, str) else ""


def _reaction_item(event: RawAPIDict) -> tuple[str, str]:
    raw = event.get("item")
    if not isinstance(raw, dict):
        return "", ""
    item = cast("RawAPIDict", raw)
    channel = item.get("channel")
    ts = item.get("ts")
    return (
        channel if isinstance(channel, str) else "",
        ts if isinstance(ts, str) else "",
    )


@dataclass(slots=True)
class SlackSelfAckReactor:
    """Post a 👀-back on the owner's reaction to a bot-authored message.

    *overlay* tags the idempotency rows so a multi-overlay deployment acks
    per overlay; v1 single-overlay use sets ``overlay=""``. The reactor is
    safe to over-observe because the ack is keyed on
    ``(overlay, channel, item_ts)``.

    ``_cached_identity`` memoises the bot's own Slack identity probed once via
    :func:`resolve_own_identity`; a successful resolve is cached for the
    reactor's lifetime. An unresolved identity is NOT cached so a transient
    failure that later recovers is re-probed.
    """

    backend: MessagingBackend
    overlay: str = ""
    _cached_identity: OwnSlackIdentity | None = field(default=None, init=False, repr=False)

    def _identity(self) -> OwnSlackIdentity | None:
        if self._cached_identity is not None:
            return self._cached_identity
        identity = resolve_own_identity(self.backend)
        if identity is not None:
            self._cached_identity = identity
        return identity

    def ack_owner_reactions(self, reactions: list[RawAPIDict]) -> int:
        """Ack every owner reaction on a bot-authored message; return the count posted.

        Reads the already-drained *reactions* snapshot the review-intent
        scanner owns — never a second JSONL drain. Fails closed on an
        unresolved bot identity or a missing owner id.
        """
        target_user = getattr(self.backend, "user_id", "")
        if not target_user:
            return 0
        identity = self._identity()
        if identity is None:
            # Bot identity unknown — cannot prove a message is the bot's own,
            # so ack nothing this turn rather than 👀 a message we did not write.
            return 0
        acked = 0
        for event in reactions:
            try:
                if self._ack_one(event, target_user, identity):
                    acked += 1
            except Exception:
                logger.exception("SlackSelfAckReactor failed on reaction event %s", event.get("event_ts", "<unknown>"))
        return acked

    def _ack_one(self, event: RawAPIDict, target_user: str, identity: OwnSlackIdentity) -> bool:
        if _event_user(event) != target_user:
            return False
        channel, ts = _reaction_item(event)
        if not channel or not ts:
            return False
        message = self.backend.fetch_message(channel=channel, ts=ts)
        if not is_self_authored(message, identity):
            return False
        row = SlackSelfAckReaction.record(overlay=self.overlay, channel=channel, item_ts=ts)
        if row is None:
            # Already acked this (overlay, channel, item_ts) — no re-react.
            return False
        try:
            # ``react`` enforces the DM-only owner guard at the token funnel
            # (``assert_owner_dm``), so a self-ack can only land on the owner's
            # own DM — a bot→user ack, never an on-behalf colleague post.
            self.backend.react(channel=channel, ts=ts, emoji=_EYES_EMOJI)
        except Exception:
            # Release the claim so a transient Slack failure retries rather than
            # carrying a phantom ack for a reaction that never landed.
            row.delete()
            raise
        return True
