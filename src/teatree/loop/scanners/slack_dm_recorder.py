"""Record inbound bot-DM user messages as ``PendingChatInjection`` rows (#1014).

One row per unique Slack ``ts``, carrying the event's ``thread_ts`` — a reply's only
piece of binding identity, which :mod:`teatree.loop.question_binding` joins on to find
the question it answers. The reactive Slack-answer cycle is the rows' consumer. Every
writer goes through :meth:`SlackDmRecorder.record_events`: the receiver's live hot
path, the listener-downtime catch-up, and the inbox drain of the JSONL queue.

Two write-side filters apply, so the consumer inherits them without re-implementing:

* :func:`teatree.loop.scanners.slack_self_filter.filter_self_messages` drops the bot's
    OWN outbound DMs, keyed on the bot's resolved identity (#1346).
* An app-posted owner reply survives only when its Slack thread holds a recorded
    owner card. Other app posts still cannot enter the reactive work queue (#1941).

Fail-closed (the identity filter only): when the bot's own identity cannot be resolved,
:func:`filter_self_messages` returns ``None`` and nothing is recorded — better silent
than spawning ``t3:answerer`` sub-agents against the bot's own traffic.
"""

import logging
from dataclasses import dataclass, field

from teatree.core.backend_protocols import MessagingBackend
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.core.models.pending_chat_injection import DmContext, PendingChatInjection
from teatree.loop.question_binding import cards_in_thread, configured_owner_id
from teatree.loop.scanners.slack_self_filter import (
    OwnSlackIdentity,
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
    retry_events: list[RawAPIDict] = field(default_factory=list, init=False, repr=False)

    def _identity(self) -> OwnSlackIdentity | None:
        if self._cached_identity is None:
            self._cached_identity = resolve_own_identity(self.backend)
        return self._cached_identity

    def record_events(self, events: list[RawAPIDict], *, log_ignored: bool = True) -> int:
        """Record each DM in *events* that survives the write-side filters; return the NEW rows."""
        self.retry_events = []
        dms = filter_self_messages(events, self._identity())
        if dms is None:
            return 0
        recorded = 0
        for event in dms:
            app_posted = is_on_behalf_posted(event)
            if (
                app_posted
                and PendingChatInjection.objects.filter(overlay=self.overlay, slack_ts=_event_ts(event)).exists()
            ):
                continue
            try:
                owner_card_reply = app_posted and self._owner_card_reply(event)
            except Exception:
                logger.exception("Slack thread read failed for DM event %s; retrying", _event_ts(event))
                self.retry_events.append(event)
                continue
            if app_posted and not owner_card_reply:
                if log_ignored and _event_thread_ts(event):
                    logger.info("Slack reply %s ignored: posted through an app token", _event_ts(event))
                continue
            recorded += self._record_one(event)
        return recorded

    def _owner_card_reply(self, event: RawAPIDict) -> bool:
        thread_ts = _event_thread_ts(event)
        owner_id = configured_owner_id(self.backend)
        if not thread_ts or not owner_id or _str_field(event, "user") != owner_id or event.get("bot_id"):
            return False
        cards, _ = cards_in_thread(
            channel=_str_field(event, "channel"),
            thread_ts=thread_ts,
            before_ts=_event_ts(event),
            backend=self.backend,
        )
        return any(card.is_pending and card.audience == DeferredQuestion.Audience.OWNER_QUESTION for card in cards)

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
