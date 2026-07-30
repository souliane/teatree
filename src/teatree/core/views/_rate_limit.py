"""Per-source token-bucket rate limiter for the inbound webhook views (#673 item 3).

A misconfigured platform (or a genuine retry storm) can hammer
``/hooks/<platform>/`` thousands of times a minute. Each accepted POST
writes an ``IncomingEvent`` row, so an unthrottled storm fills the DB.
The receivers consult a process-local token bucket keyed by source
*after* signature verification (unauthenticated junk already 401s
without a DB write) and return ``429`` once the bucket is empty.

In-memory is deliberate: a storm is transient, the state need not
survive a restart, and a DB-backed counter would itself write to the DB
— defeating the purpose. The bucket is **per process**: under a
multi-worker WSGI server (gunicorn ``--workers N``) the effective
ceiling is ``capacity * N``, not ``capacity``. teatree assumes the
single-process dev/loop topology (the loop tick that drains the queue
is itself the flock-singleton from #676); tune
``TEATREE_WEBHOOK_RATE_CAPACITY`` accordingly if you run the receiver
under multiple workers. This is a DB-bloat guard — it turns an
unbounded storm into a bounded trickle — not a precise quota or a
CPU/DoS guard (unauthenticated floods already 401 before any DB write
and are intentionally not bucketed).
"""

import logging
import threading
from collections.abc import Callable

from django.conf import settings

logger = logging.getLogger(__name__)

_DEFAULT_CAPACITY = 60
_DEFAULT_REFILL_PER_SECOND = 1.0


class TokenBucket:
    """Classic token bucket. ``allow()`` consumes one token if available."""

    def __init__(
        self,
        *,
        capacity: int,
        refill_per_second: float,
        now: Callable[[], float],
    ) -> None:
        self._capacity = float(capacity)
        self._refill_per_second = float(refill_per_second)
        self._now = now
        self._tokens = float(capacity)
        self._last = now()

    def allow(self) -> bool:
        moment = self._now()
        elapsed = moment - self._last
        self._last = moment
        self._tokens = min(self._capacity, self._tokens + elapsed * self._refill_per_second)
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True
        return False


class WebhookRateLimiter:
    """One :class:`TokenBucket` per source, created on first use."""

    def __init__(
        self,
        *,
        capacity: int,
        refill_per_second: float,
        now: Callable[[], float],
    ) -> None:
        self._capacity = capacity
        self._refill_per_second = refill_per_second
        self._now = now
        self._buckets: dict[str, TokenBucket] = {}
        self._lock = threading.Lock()

    def allow(self, source: str) -> bool:
        # An unknown source must not mint an unbounded bucket — a
        # misconfigured/forged platform value would otherwise get an
        # uncapped allowance. Treat it as rate-limited (rejected).
        from teatree.core.models import IncomingEvent  # noqa: PLC0415 — deferred: ORM import needs the app registry

        if source not in IncomingEvent.Source.values:
            logger.warning("Rejecting webhook from unknown source %r — not creating a bucket", source)
            return False
        with self._lock:
            bucket = self._buckets.get(source)
            if bucket is None:
                bucket = TokenBucket(
                    capacity=self._capacity,
                    refill_per_second=self._refill_per_second,
                    now=self._now,
                )
                self._buckets[source] = bucket
            return bucket.allow()


_limiter: WebhookRateLimiter | None = None
_limiter_lock = threading.Lock()


def _build_limiter() -> WebhookRateLimiter:
    import time  # noqa: PLC0415 — deferred: loaded only on this code path

    capacity = int(getattr(settings, "TEATREE_WEBHOOK_RATE_CAPACITY", _DEFAULT_CAPACITY))
    refill = float(getattr(settings, "TEATREE_WEBHOOK_RATE_REFILL_PER_SECOND", _DEFAULT_REFILL_PER_SECOND))
    return WebhookRateLimiter(capacity=capacity, refill_per_second=refill, now=time.monotonic)


def webhook_rate_limiter() -> WebhookRateLimiter:
    global _limiter  # noqa: PLW0603 — module-level process singleton
    if _limiter is None:
        with _limiter_lock:
            if _limiter is None:
                _limiter = _build_limiter()
    return _limiter


def reset_webhook_rate_limiter() -> None:
    """Drop the process limiter so the next call rebuilds it (test hook)."""
    global _limiter  # noqa: PLW0603 — module-level process singleton
    with _limiter_lock:
        _limiter = None
