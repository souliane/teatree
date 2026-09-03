"""Shared bounded-retry HTTP transport for every ``httpx`` backend.

Naked ``httpx`` calls broke a whole loop tick on a single transient ``502`` /
``429`` / connect timeout — the failure both :class:`SlackHttpClient` and
:class:`GitLabHTTPClient` were built to survive. :class:`BoundedRetryTransport`
is the one machine those two — plus the lighter Sentry / Notion / Figma clients
and the worktree readiness probe's boot-time GET — share: a bounded exponential
backoff that honours ``Retry-After`` and is gated by BOTH the failure class and
the call's idempotency.

Idempotency is the load-bearing safety constraint. A non-idempotent write
(``chat.postMessage``, a GitLab note ``POST``) that read-times-out may have
reached the host and only lost the response — a blind replay would double-post.
So retries split by :class:`RetryClass`:

*   :class:`RetryClass.CONNECT` — the request never reached the host
    (``ConnectTimeout`` / ``ConnectError`` / ``PoolTimeout``). Safe to retry for
    *every* call, idempotent or not, because nothing was sent.
*   :class:`RetryClass.RESPONSE` — the request reached the host but the response
    failed or signalled "try again" (a read timeout, ``5xx``, ``429``, or a
    host-specific ratelimit body). Retried only for an *idempotent* call.

Subclasses parameterize three seams:

*   :attr:`BoundedRetryTransport._RAISE_FOR_STATUS_ON_RETURN` — whether the
    returned response is ``raise_for_status``-ed before it surfaces (Slack raises
    so a ``5xx`` becomes an exception; GitLab returns the raw response so
    status-inspecting callers keep working).
*   :meth:`BoundedRetryTransport._is_transient_response` — the transient-status
    classification, which a subclass overrides to union a host-specific transient
    body onto the standard ``5xx`` / ``429`` check (Slack's ``ratelimited`` JSON).
*   The env-var prefix + retry knobs, resolved once via
    :meth:`BoundedRetryTransport._configure_retry`.
"""

import os
import time
from collections.abc import Callable
from enum import Enum

import httpx

type SleepFn = Callable[[float], None]
type AttemptFn = Callable[[], httpx.Response]

DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_RETRIES = 3
DEFAULT_BACKOFF_BASE_SECONDS = 0.5
MAX_BACKOFF_SECONDS = 30.0
CONNECT_ERRORS = (httpx.ConnectTimeout, httpx.ConnectError, httpx.PoolTimeout)


class RetryClass(Enum):
    """How a transient failure may be retried.

    ``CONNECT`` — the request never reached the host, so a retry cannot
    duplicate a side effect; safe even for a non-idempotent write.
    ``RESPONSE`` — the request reached the host; retry only when the call
    itself is idempotent (a read, or an ``add``-the-same-thing no-op).
    """

    CONNECT = "connect"
    RESPONSE = "response"


def env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


class BoundedRetryTransport:
    """Bounded exponential-backoff retry around an ``httpx`` request attempt.

    ``max_retries`` is the number of *additional* attempts after the first, so
    the default ``3`` means up to four total tries. ``sleep`` is injectable
    purely so tests assert the backoff schedule without real delay — production
    uses ``time.sleep``.
    """

    _RAISE_FOR_STATUS_ON_RETURN: bool = False

    def _configure_retry(
        self,
        *,
        env_prefix: str,
        max_retries: int | None,
        backoff_base: float | None,
        sleep: SleepFn,
    ) -> None:
        self._max_retries = (
            max_retries if max_retries is not None else env_int(f"{env_prefix}_MAX_RETRIES", DEFAULT_MAX_RETRIES)
        )
        self._backoff_base = (
            backoff_base
            if backoff_base is not None
            else env_float(f"{env_prefix}_BACKOFF", DEFAULT_BACKOFF_BASE_SECONDS)
        )
        self._sleep = sleep

    def _run(self, attempt: AttemptFn, *, idempotent: bool) -> httpx.Response:
        """Execute *attempt* under a bounded retry, returning the ``httpx.Response``.

        A CONNECT-phase failure is safe to retry for any call (nothing was sent);
        a RESPONSE-phase failure (read timeout, ``5xx``, ``429``, ratelimited) is
        retried only for an idempotent call. A ``429`` / ``5xx`` honours a
        ``Retry-After`` header when present, else the standard exponential
        backoff. The final iteration always returns or raises, so the loop is
        exhaustive — no unreachable fall-through.
        """
        for retries_left in range(self._max_retries, -1, -1):
            last = retries_left == 0
            try:
                response = attempt()
            except CONNECT_ERRORS:
                if last:
                    raise
                self._backoff(self._max_retries - retries_left)
                continue
            except httpx.TimeoutException:
                if last or not self._may_retry(RetryClass.RESPONSE, idempotent=idempotent):
                    raise
                self._backoff(self._max_retries - retries_left)
                continue
            retry_after = self._transient_response_wait(response, idempotent=idempotent)
            if retry_after is None or last:
                if self._RAISE_FOR_STATUS_ON_RETURN:
                    response.raise_for_status()
                return response
            self._sleep_for(retry_after if retry_after > 0 else self._backoff_seconds(self._max_retries - retries_left))
        unreachable = "retry loop is exhaustive: the final iteration always returns or raises"
        raise AssertionError(unreachable)  # pragma: no cover

    def _transient_response_wait(self, response: httpx.Response, *, idempotent: bool) -> float | None:
        """Seconds to wait before a response-phase retry, or ``None`` when not retryable.

        ``0.0`` means "retry on the standard backoff"; a positive value is an
        explicit ``Retry-After`` to honour. ``None`` means surface the response to
        the caller (success, a non-transient status, or a non-idempotent write
        that must not be replayed on a response-phase failure).
        """
        if not self._is_transient_response(response):
            return None
        if not self._may_retry(RetryClass.RESPONSE, idempotent=idempotent):
            return None
        return self._retry_after_seconds(response)

    @staticmethod
    def _is_transient_response(response: httpx.Response) -> bool:
        # A real httpx.Response always carries an int status; a response object
        # that reports no integer status is not classifiable as transient
        # (surfaced as-is). Keeps the transport tolerant of the lightweight
        # stub responses some backend tests hand back. Subclasses that add a
        # host-specific transient body (Slack's ``ratelimited``) OVERRIDE this
        # and union their check onto ``super()._is_transient_response(...)``.
        status = getattr(response, "status_code", None)
        if not isinstance(status, int):
            return False
        return status >= httpx.codes.INTERNAL_SERVER_ERROR or status == httpx.codes.TOO_MANY_REQUESTS

    @staticmethod
    def _may_retry(failure: RetryClass, *, idempotent: bool) -> bool:
        return failure is RetryClass.CONNECT or idempotent

    @staticmethod
    def _retry_after_seconds(response: httpx.Response) -> float:
        header = response.headers.get("Retry-After", "").strip()
        if not header:
            return 0.0
        try:
            return max(0.0, float(header))
        except ValueError:
            return 0.0

    def _backoff(self, attempt_index: int) -> None:
        self._sleep_for(self._backoff_seconds(attempt_index))

    def _backoff_seconds(self, attempt_index: int) -> float:
        return min(MAX_BACKOFF_SECONDS, self._backoff_base * (2**attempt_index))

    def _sleep_for(self, seconds: float) -> None:
        if seconds > 0:
            self._sleep(min(MAX_BACKOFF_SECONDS, seconds))


class SimpleRetryTransport(BoundedRetryTransport):
    """Composable bounded-retry wrapper for a backend that owns its ``httpx.Client``.

    The Sentry / Notion / Figma clients build a fresh ``httpx.Client`` per call
    (their own base URL, auth header, and timeout) and then ``raise_for_status``
    themselves, so they COMPOSE this transport rather than subclass the base:
    each request is a closure passed to :meth:`run`, which adds the shared
    bounded retry without touching the client's own timeout or exception
    handling. ``_RAISE_FOR_STATUS_ON_RETURN`` stays ``False`` — the caller keeps
    its existing ``raise_for_status`` so a persistent error surfaces unchanged.
    """

    def __init__(
        self,
        *,
        env_prefix: str,
        max_retries: int | None = None,
        backoff_base: float | None = None,
        sleep: SleepFn = time.sleep,
    ) -> None:
        self._configure_retry(env_prefix=env_prefix, max_retries=max_retries, backoff_base=backoff_base, sleep=sleep)

    def run(self, attempt: AttemptFn, *, idempotent: bool) -> httpx.Response:
        return self._run(attempt, idempotent=idempotent)
