"""Rolling daily DM digest (#654 phase 8, #672).

One Slack DM thread per digest day. The day rolls at 08:00 *local*
time (configured ``TIME_ZONE``; hour overridable via
``TEATREE_DAILY_DIGEST_ROLL_HOUR``, default 8) — #654 phase 8. All
user-facing comms — AskUserQuestion mirrors, error escalations, "PR
merged" notices, the end-of-day recap — post as replies under that
day's root message. The first post of a new digest window opens a
fresh thread (new ``DailyDigestThread`` row + new Slack root message);
the previous window's thread is closed by its recap.

This is a standalone service over the messaging backend, intentionally
*not* routed through ``Replier``/``ReplyDispatch``: digest posts
(errors, summaries, question mirrors) have no originating
``IncomingEvent``, and ``ReplyDispatch.event`` is non-nullable. The
``DailyDigestThread`` row is the durable state; ``DailyDigestMessage``
(unique ``idempotency_key``) collapses a retried post — at-least-once
with happy-path dedup, not exactly-once: a crash between the Slack
``post_reply`` and the dedup-row ``create`` lets the retry repost,
same as the rest of the stack (``reply_transport``, webhook ingestion).

Rewiring every existing user-facing comms path (the AskUserQuestion DM
hook, error escalation, ``Replier.post_dm`` mirroring) to funnel
through this service, and the end-of-day-close trigger, is a tracked
follow-up — this PR lands the model + service + idempotency primitive.
"""

import datetime as dt
import logging
from collections.abc import Callable

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from teatree.core.backend_protocols import MessagingBackend
from teatree.core.models import DailyDigestMessage, DailyDigestThread

logger = logging.getLogger(__name__)

_DEFAULT_ROLL_HOUR = 8


class DailyDigest:
    """Resolve-or-open today's digest thread and post threaded messages."""

    def __init__(
        self,
        *,
        backend: MessagingBackend,
        user_id: str,
        now: Callable[[], dt.datetime] = timezone.now,
    ) -> None:
        self._backend = backend
        self._user_id = user_id
        self._now = now

    def post(self, body: str, *, idempotency_key: str) -> DailyDigestThread:
        """Post *body* as a reply in today's thread. Idempotent per key."""
        existing = DailyDigestMessage.objects.filter(idempotency_key=idempotency_key).first()
        if existing is not None:
            logger.debug("Daily digest message %s already posted — idempotent no-op", idempotency_key)
            return existing.thread

        thread = self._ensure_thread()
        self._backend.post_reply(channel=thread.channel_ref, ts=thread.root_ts, text=body)
        try:
            with transaction.atomic():
                DailyDigestMessage.objects.create(thread=thread, idempotency_key=idempotency_key)
        except IntegrityError:
            logger.debug("Daily digest message %s raced — already recorded", idempotency_key)
        return thread

    def close_with_recap(self, recap: str) -> DailyDigestThread:
        """Post the end-of-day recap and close today's thread (idempotent)."""
        thread = self._ensure_thread()
        if thread.closed_at is not None:
            logger.debug("Daily digest %s already closed — recap suppressed", thread.date)
            return thread
        self._backend.post_reply(channel=thread.channel_ref, ts=thread.root_ts, text=recap)
        thread.closed_at = self._now()
        thread.save(update_fields=["closed_at"])
        return thread

    def _digest_date(self) -> dt.date:
        """The digest-window date: rolls at ``roll_hour`` *local* time.

        The window runs ``roll_hour:00`` → next-day ``roll_hour:00``
        (default 08:00, ``TEATREE_DAILY_DIGEST_ROLL_HOUR``). A timestamp
        before the roll hour belongs to the previous day's window.
        Anchoring by subtracting ``roll_hour`` hours from local time and
        taking ``.date()`` yields that windowed date in one step.
        """
        roll_hour = int(getattr(settings, "TEATREE_DAILY_DIGEST_ROLL_HOUR", _DEFAULT_ROLL_HOUR))
        local_now = timezone.localtime(self._now())
        return (local_now - dt.timedelta(hours=roll_hour)).date()

    def _ensure_thread(self) -> DailyDigestThread:
        today = self._digest_date()
        existing = DailyDigestThread.objects.filter(date=today).first()
        if existing is not None:
            return existing

        # Bounded double-open window: two concurrent first-posts of a new
        # day would each post a Slack root, then unique(date) lets one
        # INSERT win and the other refetches (the loser's root is an
        # orphaned empty message). Bounded in practice by the #676
        # loop-tick flock singleton — the only caller is the single
        # loop process; same accepted at-least-once shape as
        # reply_transport._send.
        channel = self._backend.open_dm(self._user_id)
        if not channel:
            msg = f"open_dm returned empty channel for user {self._user_id!r} — will retry next tick"
            raise RuntimeError(msg)
        opener = self._backend.post_message(
            channel=channel,
            text=f"🌳 teatree daily digest — {today:%Y-%m-%d}",
            thread_ts="",
        )
        root_ts = str(opener.get("ts", "")) if isinstance(opener, dict) else ""
        if not root_ts:
            msg = f"post_message did not return a ts for digest root on {today} — will retry next tick"
            raise RuntimeError(msg)
        try:
            with transaction.atomic():
                return DailyDigestThread.objects.create(
                    date=today,
                    channel_ref=channel,
                    root_ts=root_ts,
                )
        except IntegrityError:
            logger.debug("Daily digest thread for %s raced — reusing the winner", today)
            return DailyDigestThread.objects.get(date=today)
