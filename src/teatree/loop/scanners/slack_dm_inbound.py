"""Slack-DM-inbound scanner — user replies → ``PendingChatInjection`` (#1014).

The Slack ↔ Claude-Code bidirectional bridge (WS1). Each tick this
scanner polls the overlay's :class:`MessagingBackend.fetch_dms` for new
user messages and records one :class:`PendingChatInjection` row per
unique Slack ``ts``. The matching ``UserPromptSubmit`` handler
(``hook_router.handle_inject_pending_chat``) drains unconsumed rows into
the agent's next ``additionalContext`` block — so a Slack DM reaches the
agent as if the user had typed it in Claude Code chat (BLUEPRINT §17.1
invariant 2 / §5.6).

The Slack backend's ``fetch_dms`` already filters out bot-authored
messages (`SlackBotBackend.fetch_dms` matches ``user`` and ``bot_id``
against the resolved bot id); the scanner trusts that contract and adds
only idempotent persistence. Duplicate ``ts`` values across polls are
swallowed by ``PendingChatInjection.record``'s ``unique(overlay, ts)``
constraint, so over-polling is safe.

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

from dataclasses import dataclass

from teatree.backends.protocols import MessagingBackend
from teatree.core.models.pending_chat_injection import PendingChatInjection
from teatree.loop.scanners.base import ScanSignal
from teatree.types import RawAPIDict


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
    """

    backend: MessagingBackend
    overlay: str = ""
    name: str = "slack_dm_inbound"

    def scan(self) -> list[ScanSignal]:
        dms = self.backend.fetch_dms()
        signals: list[ScanSignal] = []
        for event in dms:
            ts = _event_ts(event)
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
        return signals
