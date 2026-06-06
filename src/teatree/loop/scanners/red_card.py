"""RED CARD detection scanner — user corrective-action signal (#1130).

The user signals "the agent did something structurally wrong, fix it
*upstream* in teatree" via three surfaces:

1. ``:red_circle:`` reaction on the agent's prior message,
2. ``:no_entry_sign:`` reaction on the agent's prior message,
3. The literal phrase ``"RED CARD"`` (case-insensitive, optional dash
    or single space between the words) in a DM or thread reply.

Each fresh signal lands one :class:`RedCardSignal` row (idempotent on
``(overlay, channel, slack_ts)``), posts the ``:eyes:`` acknowledgement
on the user's signal so they see the loop saw it, and emits one
``red_card.signal`` :class:`ScanSignal`. The dispatcher routes the
signal to the coordinator skill, which is expected to:

* identify the upstream teatree gap the offending action revealed
    (skill prose, scanner gap, hook gap, missing CLI command),
* file the corrective teatree issue describing the gap + the smallest
    deterministic enforcement (per the escalate-to-enforcement doctrine),
* call :meth:`RedCardSignal.link_issue` to record the resulting issue
    URL against the signal row before clearing it.

The scanner itself only detects + records; it never files the upstream
issue (that is a coordinator-side decision and a colleague-visible
write that should go through the standard on-behalf gates).

Mirrors :class:`teatree.loop.scanners.slack_review_intent.SlackReviewIntentScanner`
in shape — drains reactions + DMs from the messaging backend, persists
durable rows, emits a single signal kind. Each scanner instance is
scoped to one overlay so a multi-overlay deployment dispatches per
overlay.
"""

import logging
import re
from dataclasses import dataclass
from typing import cast

from teatree.core.backend_protocols import MessagingBackend
from teatree.core.models import RedCardIntent, RedCardSignal
from teatree.loop.scanners.base import ScanSignal
from teatree.types import RawAPIDict

logger = logging.getLogger(__name__)


#: Reaction emoji names the scanner treats as RED CARD signals.
_REACTION_KINDS: dict[str, str] = {
    "red_circle": RedCardSignal.Kind.RED_CIRCLE,
    "no_entry_sign": RedCardSignal.Kind.NO_ENTRY_SIGN,
}


#: Match the literal phrase ``"red card"`` / ``"red-card"`` (any case)
#: as a standalone token (word boundary on both sides). Embedded forms
#: like ``"redcard"`` (no separator) or generic uses of ``"red"`` must
#: not trigger — those would produce false positives on routine chat.
_RED_CARD_TEXT_RE = re.compile(r"\bred[\s\-]card\b", re.IGNORECASE)


def _event_user(event: RawAPIDict) -> str:
    user = event.get("user")
    return user if isinstance(user, str) else ""


def _event_text(event: RawAPIDict) -> str:
    text = event.get("text")
    return text if isinstance(text, str) else ""


def _event_channel(event: RawAPIDict) -> str:
    channel = event.get("channel")
    return channel if isinstance(channel, str) else ""


def _event_ts(event: RawAPIDict) -> str:
    ts = event.get("ts") or event.get("event_ts")
    return ts if isinstance(ts, str) else ""


def _reaction_name(event: RawAPIDict) -> str:
    name = event.get("reaction")
    return name if isinstance(name, str) else ""


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
class RedCardScanner:
    """Translate Slack red-card surfaces into :class:`RedCardSignal` rows.

    *overlay* tags rows so a multi-overlay deployment can dispatch per
    overlay; v1 single-overlay use sets ``overlay=""``. The scanner is
    safe to over-poll because rows are keyed on
    ``(overlay, channel, slack_ts)``.
    """

    backend: MessagingBackend
    overlay: str = ""
    name: str = "red_card"

    def scan(self) -> list[ScanSignal]:
        target_user = getattr(self.backend, "user_id", "")
        signals: list[ScanSignal] = []

        for event in self._drain_reactions():
            ts = _event_ts(event)
            try:
                signal = self._handle_reaction(event, target_user)
            except Exception:
                logger.exception("RedCardScanner failed on reaction event %s", ts)
                continue
            if signal is not None:
                signals.append(signal)

        for event in self._drain_dms():
            ts = _event_ts(event)
            try:
                signal = self._handle_dm(event, target_user)
            except Exception:
                logger.exception("RedCardScanner failed on DM event %s", ts)
                continue
            if signal is not None:
                signals.append(signal)

        return signals

    def _drain_reactions(self) -> list[RawAPIDict]:
        """Drain reaction events from the backend's in-memory queue.

        We deliberately do not pop from the on-disk JSONL drain here —
        :class:`SlackReviewIntentScanner` already owns that drain. The
        on-disk queue is single-consumer; calling
        :func:`drain_reactions_queue` twice in a tick would race the
        file rename. The backend's ``fetch_reactions`` is the
        in-memory surface (used by both ``SlackBotBackend`` for any
        receiver-pushed events and ``FakeMessaging`` in tests) and is
        safe to read from multiple scanners.
        """
        fetch = getattr(self.backend, "fetch_reactions", None)
        if not callable(fetch):
            return []
        return list(fetch())

    def _drain_dms(self) -> list[RawAPIDict]:
        return self.backend.fetch_dms()

    def _handle_reaction(self, event: RawAPIDict, target_user: str) -> ScanSignal | None:
        user = _event_user(event)
        if not target_user or user != target_user:
            return None
        reaction = _reaction_name(event)
        signal_kind = _REACTION_KINDS.get(reaction)
        if signal_kind is None:
            return None
        channel, agent_ts = _reaction_item(event)
        if not channel or not agent_ts:
            return None
        # The user's reaction has no Slack ``ts`` of its own — the event
        # is identified by the message it was added to plus the
        # ``event_ts``. We use ``event_ts`` for idempotency so the user
        # adding two different RED CARD reactions to the same agent
        # message yields two distinct rows (different ``event_ts``).
        signal_ts = _event_ts(event) or agent_ts
        agent_message = self.backend.fetch_message(channel=channel, ts=agent_ts)
        agent_text = _event_text(agent_message) if isinstance(agent_message, dict) else ""
        intent = RedCardIntent(
            overlay=self.overlay,
            channel=channel,
            slack_ts=signal_ts,
            signal_kind=signal_kind,
            user_id=user,
            offending_message_ts=agent_ts,
            offending_message_text=agent_text,
            signal_text=f":{reaction}:",
        )
        row = RedCardSignal.record(intent)
        if row is None:
            return None
        self._post_eyes(channel=channel, ts=agent_ts, row=row)
        return _signal_from_row(row, overlay=self.overlay)

    def _handle_dm(self, event: RawAPIDict, target_user: str) -> ScanSignal | None:
        text = _event_text(event)
        if not text or not _RED_CARD_TEXT_RE.search(text):
            return None
        ts = _event_ts(event)
        channel = _event_channel(event)
        if not ts or not channel:
            return None
        user = _event_user(event) or target_user
        intent = RedCardIntent(
            overlay=self.overlay,
            channel=channel,
            slack_ts=ts,
            signal_kind=RedCardSignal.Kind.RED_CARD_TEXT,
            user_id=user,
            offending_message_ts="",
            offending_message_text="",
            signal_text=text,
        )
        row = RedCardSignal.record(intent)
        if row is None:
            return None
        self._post_eyes(channel=channel, ts=ts, row=row)
        return _signal_from_row(row, overlay=self.overlay)

    def _post_eyes(self, *, channel: str, ts: str, row: RedCardSignal) -> None:
        """Post ``:eyes:`` on the user's signal and stamp the row.

        Best-effort: a Slack outage logs and continues so the scanner
        never blocks on a transient ack failure. The row stays in
        ``PENDING`` if the ack fails, so the next tick (or the
        coordinator) can retry the ack as a side-effect.
        """
        try:
            self.backend.react(channel=channel, ts=ts, emoji="eyes")
        except Exception:
            logger.exception("Failed to post :eyes: on red-card signal %s/%s", channel, ts)
            return
        row.mark_eyes_added()


def _signal_from_row(row: RedCardSignal, *, overlay: str) -> ScanSignal:
    return ScanSignal(
        kind="red_card.signal",
        summary=f"RED CARD ({row.signal_kind}) from {row.user_id}",
        payload={
            "row_id": row.pk,
            "signal_kind": row.signal_kind,
            "user_id": row.user_id,
            "channel": row.channel,
            "ts": row.slack_ts,
            "offending_message_ts": row.offending_message_ts,
            "offending_message_text": row.offending_message_text,
            "signal_text": row.signal_text,
            "overlay": overlay,
        },
    )
