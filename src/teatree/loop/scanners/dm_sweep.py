"""Scanner that runs the hourly owner-DM hygiene pass (#3658).

Thin: the decision logic is :mod:`teatree.core.owner_dm_sweep`. This carries the two
tick-level concerns — the per-pass watermark (the ``dm_sweep`` ``Loop`` row's own
``last_run_at``, so each pass covers exactly the new ground with no gaps and no
re-reading history) and the live Slack read that answers "did the owner already reply in
this thread?".

It emits a :class:`ScanSignal` ONLY when it actually resolved something. A pass with
nothing to do is silent by design: the loop exists to reduce what the owner reads, so a
"swept, nothing to do" line would be the exact noise it removes.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, NotRequired, TypedDict, cast

from django.db import OperationalError, ProgrammingError

from teatree.loop.scanners.base import ScanSignal

if TYPE_CHECKING:
    from collections.abc import Callable

    from teatree.core.backend_protocols import MessagingBackend
    from teatree.core.owner_threads import OwnerThread

logger = logging.getLogger(__name__)

_LOOP_NAME = "dm_sweep"


class SlackThreadReply(TypedDict):
    """The ``conversations.replies`` message fields :func:`_is_owner_reply` reads.

    Every field is optional: Slack omits ``bot_id``/``subtype`` on a human message,
    and a malformed payload may omit the rest.
    """

    bot_id: NotRequired[str]
    subtype: NotRequired[str]
    ts: NotRequired[str]
    text: NotRequired[str]


@dataclass(slots=True)
class DmSweepScanner:
    backend: "MessagingBackend | None" = None
    name: str = "dm_sweep"

    def scan(self) -> list[ScanSignal]:
        from teatree.core.owner_dm_sweep import SweepSeams, run_sweep  # noqa: PLC0415 — deferred: tick-time import

        try:
            result = run_sweep(since=_watermark(), seams=SweepSeams(owner_replied=self._owner_replied_probe()))
        except (OperationalError, ProgrammingError):
            logger.info("DmSweepScanner: tables unavailable (DB not migrated yet) — skipping")
            return []
        except Exception:
            logger.exception("DmSweepScanner sweep failed")
            return []
        if result.silent:
            return []
        return [
            ScanSignal(
                kind="dm_sweep.resolved",
                summary=f"resolved {result.resolved} owner DM thread(s) that no longer need you",
                payload={"resolved": result.resolved, "left_open": result.left_open},
            ),
        ]

    def _owner_replied_probe(self) -> "Callable[[OwnerThread], bool] | None":
        """A live "did the owner reply in this thread?" read, or ``None`` with no backend.

        ``None`` skips the rule entirely rather than guessing: an unreadable thread must
        never be mistaken for an answered one.
        """
        backend = self.backend
        if backend is None:
            return None

        def probe(thread: "OwnerThread") -> bool:
            if not thread.channel or not thread.ts:
                return False
            try:
                replies = backend.fetch_thread_replies(channel=thread.channel, thread_ts=thread.ts)
            except Exception:  # noqa: BLE001 — an unreadable thread is "unknown", never "answered"
                logger.info("DmSweepScanner: thread %s/%s unreadable — leaving it open", thread.channel, thread.ts)
                return False
            return any(_is_owner_reply(reply, thread_ts=thread.ts) for reply in replies)

        return probe


def _watermark() -> datetime | None:
    """The previous pass's start, from the loop's own ledger; ``None`` on the first pass."""
    from teatree.core.models import Loop  # noqa: PLC0415 — deferred: ORM import needs the app registry

    row = Loop.objects.filter(name=_LOOP_NAME).first()
    return row.last_run_at if row is not None else None


def _is_owner_reply(reply: object, *, thread_ts: str) -> bool:
    """A human message in the thread that is not the bot's own root post.

    ``bot_id`` is what distinguishes the owner's typed reply from teatree's own posts in
    the same thread — without it every mirrored question would look answered by itself.
    """
    if not isinstance(reply, dict):
        return False
    fields = cast("SlackThreadReply", reply)
    if fields.get("bot_id") or fields.get("subtype"):
        return False
    return str(fields.get("ts", "")) != thread_ts and bool(str(fields.get("text", "")).strip())


__all__ = ["DmSweepScanner", "SlackThreadReply"]
