"""Rolling daily DM digest (#654 phase 8, #672).

One Slack DM thread per UTC day. All user-facing comms — AskUserQuestion
mirrors, error escalations, "PR merged" notices, the end-of-day recap —
post as replies under that day's root message. The first post of a new
UTC date opens a fresh thread (new ``DailyDigestThread`` row + new Slack
root message); the previous day's thread is closed by its recap.

This is a standalone service over the messaging backend, intentionally
*not* routed through ``Replier``/``ReplyDispatch``: digest posts
(errors, summaries, question mirrors) have no originating
``IncomingEvent``, and ``ReplyDispatch.event`` is non-nullable. The
``DailyDigestThread`` row is the durable state; ``DailyDigestMessage``
(unique ``idempotency_key``) makes a retried post a no-op.

Rewiring every existing user-facing comms path (the AskUserQuestion DM
hook, error escalation, ``Replier.post_dm`` mirroring) to funnel
through this service, and the end-of-day-close trigger, is a tracked
follow-up — this PR lands the model + service + idempotency primitive.
"""

import datetime as dt
import logging
from collections.abc import Callable

from django.db import IntegrityError, transaction
from django.utils import timezone

from teatree.backends.protocols import MessagingBackend
from teatree.core.models import DailyDigestMessage, DailyDigestThread

logger = logging.getLogger(__name__)


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

    def _ensure_thread(self) -> DailyDigestThread:
        today = self._now().date()
        existing = DailyDigestThread.objects.filter(date=today).first()
        if existing is not None:
            return existing

        channel = self._backend.open_dm(self._user_id)
        opener = self._backend.post_message(
            channel=channel,
            text=f"🌳 teatree daily digest — {today:%Y-%m-%d}",
            thread_ts="",
        )
        root_ts = str(opener.get("ts", "")) if isinstance(opener, dict) else ""
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
