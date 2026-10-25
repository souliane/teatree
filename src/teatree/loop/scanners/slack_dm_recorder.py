"""Record inbound bot-DM user messages as ``PendingChatInjection`` rows (#1014).

One row per unique Slack ``ts``, carrying the event's ``thread_ts`` — a reply's only
piece of binding identity, which :mod:`teatree.loop.question_binding` joins on to find
the question it answers. The reactive Slack-answer cycle is the rows' consumer. Every
writer goes through :meth:`SlackDmRecorder.record_events`: the receiver's live hot
path, the listener-downtime catch-up, and the inbox drain of the JSONL queue.

Two write-side filters apply, so the consumer inherits them without re-implementing:

* :func:`teatree.loop.scanners.slack_self_filter.filter_self_messages` drops the bot's
    OWN outbound DMs, keyed on the bot's resolved identity (#1346).
* :func:`teatree.loop.scanners.slack_self_filter.drop_on_behalf_messages` drops rows
    posted via an app/bot token even when they display under the human's own identity
    — an on-behalf post carries the human's own ``user`` id (#1941).

Fail-closed (the identity filter only): when the bot's own identity cannot be resolved,
:func:`filter_self_messages` returns ``None`` and nothing is recorded — better silent
than spawning ``t3:answerer`` sub-agents against the bot's own traffic.
"""

import logging
from dataclasses import dataclass, field

from teatree.core.backend_protocols import MessagingBackend
from teatree.core.models.pending_chat_injection import DmContext, PendingChatInjection
from teatree.loop.scanners.slack_self_filter import (
    OwnSlackIdentity,
    drop_on_behalf_messages,
    filter_self_messages,
    is_on_behalf_posted,
    resolve_own_identity,
)
from teatree.types import RawAPIDict

logger = logging.getLogger(__name__)


def _str_field(event: RawAPIDict, key: str) -> str:
    value = event.get(key)
    return value if isinstance(value, str) else ""


def _event_ts(event: RawAPIDict) -> str:
    return _str_field(event, "ts") or _str_field(event, "event_ts")


def _event_thread_ts(event: RawAPIDict) -> str:
    """The thread root this message replies to; ``""`` for a top-level message or a thread's own root."""
    thread_ts = _str_field(event, "thread_ts")
    return "" if thread_ts == _event_ts(event) else thread_ts


@dataclass(slots=True)
class SlackDmRecorder:
    """Persist each new bot-DM user message, idempotent on ``(overlay, ts)``.

    A resolved bot identity is cached for the recorder's lifetime; an unresolved one is
    re-probed on the next call.
    """

    backend: MessagingBackend
    overlay: str = ""
    _cached_identity: OwnSlackIdentity | None = field(default=None, init=False, repr=False)

    def _identity(self) -> OwnSlackIdentity | None:
        if self._cached_identity is None:
            self._cached_identity = resolve_own_identity(self.backend)
        return self._cached_identity

    def record_events(self, events: list[RawAPIDict]) -> int:
        """Record each DM in *events* that survives the write-side filters; return the NEW rows."""
        dms = filter_self_messages(events, self._identity())
        if dms is None:
            return 0
        for event in dms:
            if _event_thread_ts(event) and is_on_behalf_posted(event):
                logger.info("Slack reply %s ignored: posted through an app token", _event_ts(event))
        return sum(1 for event in drop_on_behalf_messages(dms) if self._record_one(event))

    def _record_one(self, event: RawAPIDict) -> bool:
        ts = _event_ts(event)
        body = _str_field(event, "text")
        if not ts or not body.strip():
            return False
        try:
            row = PendingChatInjection.record(
                channel=_str_field(event, "channel"),
                slack_ts=ts,
                text=body,
                overlay=self.overlay,
                context=DmContext(user_id=_str_field(event, "user"), thread_ts=_event_thread_ts(event)),
            )
        except Exception:
            logger.exception("SlackDmRecorder failed on DM event %s", ts)
            return False
        return row is not None
