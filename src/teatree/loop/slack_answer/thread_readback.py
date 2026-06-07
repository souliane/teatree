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

from teatree.backends.slack.self_identity import is_self_authored, resolve_own_identity
from teatree.core.backend_protocols import MessagingBackend

logger = logging.getLogger(__name__)


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


def bot_reply_present_in_thread(backend: MessagingBackend, *, channel: str, thread_root: str) -> bool:
    """True iff the bot already has a reply under *thread_root*.

    Reads the thread root's replies and tests each against the bot's own
    identity. Used for both pre-post dedup (skip when a reply is already
    there) and post-delivery verification (confirm the just-posted reply
    landed). Conservative on every uncertainty — an unresolved bot identity,
    an empty read, or a read raise all report ``False`` (absent), so the
    verification caller retries rather than stamping an unconfirmed post.
    """
    identity = resolve_own_identity(backend)
    if identity is None:
        logger.warning("Bot identity unresolved; cannot confirm reply under %s/%s", channel, thread_root)
        return False
    try:
        replies = backend.fetch_thread_replies(channel=channel, thread_ts=thread_root)
    except Exception as exc:  # noqa: BLE001 — a read raise must never break the cycle
        logger.warning("Thread read-back raised for %s/%s: %s", channel, thread_root, exc)
        return False
    return any(is_self_authored(reply, identity) for reply in replies)


__all__ = ["bot_reply_present_in_thread", "resolve_thread_root"]
