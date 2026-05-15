"""Failed-``ReplyDispatch`` retry sweep with backoff + dead-letter (#673 items 1+2).

The repliers (#668) record a ``ReplyDispatch`` row with ``status=failed``
+ ``error_message`` whenever a platform call raises. This sweep walks the
due-for-retry rows, asks the caller-supplied resolver for a live
``Replier`` for the event's source, and re-attempts delivery via
``Replier.redeliver`` (which bypasses the idempotency short-circuit in
``_send``).

Success sets status ``sent`` and clears the error / ``next_retry_at``.
Failure increments ``retry_count`` and pushes ``next_retry_at`` out by
exponential backoff (``base_delay_seconds`` on the first retry, doubling
thereafter). When ``retry_count`` reaches ``max_retries`` the status
becomes ``dead_letter`` and a DM goes to the originating actor naming
the event id and last error. That alert is itself a ``ReplyDispatch``
with ``action_name="dead_letter_alert"`` which ``due_for_retry``
excludes, so a permanently-broken DM channel cannot create a storm.

Loop-tick integration (resolving a real per-overlay replier) is the same
deferred follow-up as #669/#668 — the source alone does not identify the
overlay. This module is the unblocked state machine; the resolver is
injected.
"""

import datetime as dt
import logging
from collections.abc import Callable
from dataclasses import dataclass

from django.utils import timezone

from teatree.core.models import ReplyDispatch
from teatree.core.reply_transport import Replier

logger = logging.getLogger(__name__)

ReplierResolver = Callable[[str], Replier | None]

_DEFAULT_MAX_RETRIES = 5
_DEFAULT_BASE_DELAY_SECONDS = 60
_DEFAULT_LIMIT = 50


@dataclass(slots=True)
class RetrySweep:
    resolver: ReplierResolver
    now: dt.datetime
    max_retries: int = _DEFAULT_MAX_RETRIES
    base_delay_seconds: int = _DEFAULT_BASE_DELAY_SECONDS
    limit: int = _DEFAULT_LIMIT

    def run(self) -> int:
        """Retry due failed dispatches. Returns the number actually attempted.

        Per-row saves are not wrapped in ``atomic()``: the same bounded
        double-fire window documented in ``reply_transport._send`` applies
        (a post can succeed then the status save fail). The eventual
        loop-tick caller is the machine-wide flock singleton (#676); a
        direct/cron caller must add ``select_for_update(skip_locked=True)``.
        """
        attempted = 0
        for dispatch in ReplyDispatch.objects.due_for_retry(self.now)[: self.limit]:
            replier = self.resolver(dispatch.event.source)
            if replier is None:
                logger.debug("No replier for %s — skipping retry of %s", dispatch.event.source, dispatch.pk)
                continue
            attempted += 1
            try:
                replier.redeliver(dispatch)
            except Exception as exc:  # noqa: BLE001 — any backend failure is a retry outcome
                self._handle_failure(dispatch, replier, exc)
                continue
            dispatch.status = ReplyDispatch.Status.SENT
            dispatch.error_message = ""
            dispatch.next_retry_at = None
            dispatch.save(update_fields=["status", "error_message", "next_retry_at"])
        return attempted

    def _handle_failure(self, dispatch: ReplyDispatch, replier: Replier, exc: Exception) -> None:
        attempts_made = dispatch.retry_count  # 0-based: 0 → base_delay, 1 → 2·base_delay, …
        dispatch.retry_count += 1
        dispatch.error_message = str(exc)
        if dispatch.retry_count >= self.max_retries:
            dispatch.status = ReplyDispatch.Status.DEAD_LETTER
            dispatch.save(update_fields=["status", "error_message", "retry_count"])
            self._alert_dead_letter(dispatch, replier, exc)
            return
        backoff = self.base_delay_seconds * (2**attempts_made)
        dispatch.next_retry_at = self.now + dt.timedelta(seconds=backoff)
        dispatch.save(update_fields=["error_message", "retry_count", "next_retry_at"])

    @staticmethod
    def _alert_dead_letter(dispatch: ReplyDispatch, replier: Replier, exc: Exception) -> None:
        event = dispatch.event
        body = (
            f"Reply to event {event.pk} ({dispatch.action_name} → {dispatch.target_ref}) "
            f"failed permanently after {dispatch.retry_count} retries. Last error: {exc}"
        )
        try:
            replier.post_dm(
                event=event,
                actor=event.actor,
                body=body,
                idempotency_key=f"{dispatch.idempotency_key}:deadletter",
            )
        except Exception:
            logger.exception("Dead-letter alert for dispatch %s could not be delivered", dispatch.pk)


def sweep_failed_dispatches(
    *,
    resolver: ReplierResolver,
    now: dt.datetime | None = None,
    max_retries: int = _DEFAULT_MAX_RETRIES,
    base_delay_seconds: int = _DEFAULT_BASE_DELAY_SECONDS,
    limit: int = _DEFAULT_LIMIT,
) -> int:
    return RetrySweep(
        resolver=resolver,
        now=now or timezone.now(),
        max_retries=max_retries,
        base_delay_seconds=base_delay_seconds,
        limit=limit,
    ).run()
