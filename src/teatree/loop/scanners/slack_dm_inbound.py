"""Slack-DM-inbound scanner — user replies → ``PendingChatInjection`` (#1014).

The Slack ↔ Claude-Code bidirectional bridge (WS1). Each tick this
scanner polls the overlay's :class:`MessagingBackend.fetch_dms` for new
user messages and records one :class:`PendingChatInjection` row per
unique Slack ``ts``. The matching ``UserPromptSubmit`` handler
(``hook_router.handle_inject_pending_chat``) drains unconsumed rows into
the agent's next ``additionalContext`` block — so a Slack DM reaches the
agent as if the user had typed it in Claude Code chat (BLUEPRINT §17.1
invariant 2 / §5.6).

The scanner applies two write-side filters so both downstream consumers
— the reactive Slack-answer cycle and the ``UserPromptSubmit`` injection
handler, which both read from :class:`PendingChatInjection` — inherit
them for free without either consumer re-implementing the check:

* :func:`teatree.loop.scanners.slack_self_filter.filter_self_messages`
    drops rows that came from the bot's OWN outbound DMs, keyed on the
    bot's resolved identity (#1346).
* :func:`teatree.loop.scanners.slack_self_filter.drop_on_behalf_messages`
    drops rows posted via an app/bot token even when they display under
    the human's OWN identity — an on-behalf post carries the human's
    own ``user`` id, so the identity filter above cannot catch it (#1941).

Fail-closed (the identity filter only): when the bot's own identity
cannot be resolved (network down at startup, ``auth.test`` returning
``ok:false``, no bot token configured), :func:`filter_self_messages`
returns ``None`` and the scanner refuses to enqueue any row that turn —
better silent for one tick than spam-spawning ``t3:answerer`` sub-agents
against the bot's own traffic.

This scanner does NOT post anything back to Slack — recording is its only
job. The reactive replies (the :eyes: receipt, an ack reaction, a
threaded status answer, or a delegated ``t3:answerer`` task) are owned by
the dedicated reactive Slack-answer loop — the third ``/loop`` slot
(``teatree.loop.slack_answer``, ``manage.py loop_slack_answer``). That
loop reads the rows this scanner records and stamps its own orthogonal
``loop_replied_at`` / ``eyes_reacted_at`` columns — deliberately
distinct from #1069's ``answered_at`` turn-end gate (#1075 / Option B),
so a token-cheap loop reply never satisfies the "agent personally
replied" Stop-hook gate. ``consumed_at`` (the prompt-drain) stays
independent of both, so a row can be drained, loop-replied, and
agent-answered without a double reply (#1014).
"""

import logging
from dataclasses import dataclass, field

from teatree.core.backend_protocols import MessagingBackend
from teatree.core.models.pending_chat_injection import PendingChatInjection
from teatree.loop.scanners.base import ScanSignal
from teatree.loop.scanners.slack_self_filter import (
    OwnSlackIdentity,
    drop_on_behalf_messages,
    filter_self_messages,
    resolve_own_identity,
)
from teatree.types import RawAPIDict

logger = logging.getLogger(__name__)


def _event_ts(event: RawAPIDict) -> str:
    ts = event.get("ts") or event.get("event_ts")
    return ts if isinstance(ts, str) else ""


def _event_text(event: RawAPIDict) -> str:
    text = event.get("text")
    return text if isinstance(text, str) else ""


def _event_channel(event: RawAPIDict) -> str:
    channel = event.get("channel")
    return channel if isinstance(channel, str) else ""


def _event_user(event: RawAPIDict) -> str:
    user = event.get("user")
    return user if isinstance(user, str) else ""


@dataclass(slots=True)
class SlackDmInboundScanner:
    """Persist each new bot-DM user message as a ``PendingChatInjection`` row.

    *overlay* tags rows so a multi-overlay deployment can drain per
    overlay; v1 single-overlay use sets ``overlay=""``. The scanner is
    safe to over-poll because the row is keyed on ``(overlay, ts)``.

    ``_cached_identity`` memoises the bot's own Slack identity probed
    once via :func:`resolve_own_identity`; a successful resolve is
    cached for the scanner's lifetime so the hot path costs zero Slack
    API calls beyond the first scan. An unresolved identity is NOT
    cached so a transient failure that later recovers is re-probed.
    """

    backend: MessagingBackend
    overlay: str = ""
    name: str = "slack_dm_inbound"
    _cached_identity: OwnSlackIdentity | None = field(default=None, init=False, repr=False)

    def _identity(self) -> OwnSlackIdentity | None:
        if self._cached_identity is not None:
            return self._cached_identity
        identity = resolve_own_identity(self.backend)
        if identity is not None:
            self._cached_identity = identity
        return identity

    def scan(self) -> list[ScanSignal]:
        raw = self.backend.fetch_dms()
        dms = filter_self_messages(raw, self._identity())
        if dms is None:
            # Identity unknown — fail closed for this tick rather than
            # enqueue rows that may include the bot's own outbound DMs.
            return []
        dms = drop_on_behalf_messages(dms)
        signals: list[ScanSignal] = []
        for event in dms:
            ts = _event_ts(event)
            try:
                text = _event_text(event)
                if not ts or not text.strip():
                    continue
                channel = _event_channel(event)
                user_id = _event_user(event)
                row = PendingChatInjection.record(
                    channel=channel,
                    slack_ts=ts,
                    text=text,
                    overlay=self.overlay,
                    user_id=user_id,
                )
                if row is None:
                    # Duplicate ``ts`` — the scanner over-polled. Skip the
                    # signal so the dispatcher doesn't re-route a row that
                    # the previous tick already queued.
                    continue
                signals.append(
                    ScanSignal(
                        kind="slack.user_reply",
                        summary=f"Slack user reply {ts}: {text[:80]}",
                        payload={
                            "ts": ts,
                            "channel": channel,
                            "user_id": user_id,
                            "text": text,
                            "overlay": self.overlay,
                        },
                    )
                )
            except Exception:
                logger.exception("SlackDmInboundScanner failed on DM event %s", ts)
                continue
        return signals
