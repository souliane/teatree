"""Thread-root read-back for the Slack-answer pipeline (#2061).

A user message that is itself a thread reply has a non-root ``ts``. A reply
posted with ``thread_ts=<that ts>`` re-parents to the thread ROOT (standard
Slack semantics), so any read-back keyed on the user-message ts misses the
just-posted reply — the dedup check then sees "no prior reply" and posts a
duplicate, and the delivery verification declares a delivered reply absent.

The fix is to resolve the thread ROOT first and read back against it. These
two functions are the single chokepoint both the dedup check and the
post-delivery verification use, so the root-resolution logic is never
re-derived per call site.
"""

import logging
import re
from decimal import Decimal

from teatree.backends.slack.self_identity import is_self_authored, resolve_own_identity
from teatree.core.backend_protocols import MessagingBackend

logger = logging.getLogger(__name__)
_SLACK_TS = re.compile(r"^[0-9]{1,15}\.[0-9]{1,9}$")


def _timestamp(value: object) -> Decimal | None:
    if not isinstance(value, str) or _SLACK_TS.fullmatch(value) is None:
        return None
    return Decimal(value)


def resolve_thread_root(backend: MessagingBackend, *, channel: str, ts: str) -> str:
    """Canonicalise *ts* UP to its thread ROOT ts.

    Reads the target message and returns its ``thread_ts`` (the root the
    bot reply will actually re-parent under). A message that is a thread
    root has ``thread_ts == ts``; a standalone message has no ``thread_ts``.
    In both of those cases — and when the message cannot be fetched or the
    read raises — the message's own *ts* is already the root, so it is the
    fallback. The return is always a usable thread key, never empty.
    """
    try:
        message = backend.fetch_message(channel=channel, ts=ts)
    except Exception as exc:  # noqa: BLE001 — a read raise must never break the cycle
        logger.warning("Thread-root resolve raised for %s/%s: %s", channel, ts, exc)
        return ts
    thread_ts = message.get("thread_ts")
    if isinstance(thread_ts, str) and thread_ts:
        return thread_ts
    return ts


def bot_reply_present_in_thread(
    backend: MessagingBackend,
    *,
    channel: str,
    thread_root: str,
    after_ts: str | None = None,
    expected_ts: str | None = None,
) -> bool:
    """True iff a relevant bot reply is visible under *thread_root*.

    Reads the thread root's replies and tests each against the bot's own
    ``after_ts`` binds dedup to the current inbound turn; ``expected_ts``
    binds post-delivery verification to the exact reply Slack accepted. An
    older answer in the same thread must never acknowledge a new question.
    Conservative on uncertainty — invalid timestamps, unresolved identity,
    empty reads, and read failures all report ``False``.
    """
    floor = _timestamp(after_ts) if after_ts is not None else None
    expected = _timestamp(expected_ts) if expected_ts is not None else None
    if (after_ts is not None and floor is None) or (expected_ts is not None and expected is None):
        return False
    identity = resolve_own_identity(backend)
    if identity is None:
        logger.warning("Bot identity unresolved; cannot confirm reply under %s/%s", channel, thread_root)
        return False
    try:
        replies = backend.fetch_thread_replies(channel=channel, thread_ts=thread_root)
    except Exception as exc:  # noqa: BLE001 — a read raise must never break the cycle
        logger.warning("Thread read-back raised for %s/%s: %s", channel, thread_root, exc)
        return False
    for reply in replies:
        if not is_self_authored(reply, identity):
            continue
        if floor is None and expected is None:
            return True
        reply_time = _timestamp(reply.get("ts"))
        if reply_time is None or (floor is not None and reply_time <= floor):
            continue
        if expected is None or reply_time == expected:
            return True
    return False


__all__ = ["bot_reply_present_in_thread", "resolve_thread_root"]
