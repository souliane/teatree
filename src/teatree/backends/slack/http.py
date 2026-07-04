"""Bounded-retry HTTP transport for the Slack Web API (#1110, reliability).

``SlackBotBackend`` previously issued every ``httpx.post`` / ``httpx.get``
with a hard ``timeout=10.0`` and no retry. A single transient
``ReadTimeout`` / ``ConnectTimeout`` / ``5xx`` / Slack ``ratelimited``
then broke a loop tick — a missed ``reactions.add``, a dropped
merge-notify ``chat.postMessage`` during the PR sweep — even though the
very next attempt would have succeeded. :class:`SlackHttpClient`
centralises the transport so every call gets a configurable timeout and
a bounded exponential backoff on *transient* failures only.

Idempotency is the load-bearing safety constraint. A
``chat.postMessage`` is **not** idempotent: a ``ReadTimeout`` after the
request reached Slack may mean the message *was* posted and only the
response was lost — a blind retry would double-post. So retries are
gated by *both* the failure class and the call's idempotency:

*   :class:`RetryClass.CONNECT` — the request never reached Slack
    (``ConnectTimeout`` / ``ConnectError`` / ``PoolTimeout``). Safe to
    retry for *every* call, idempotent or not, because nothing was sent.
*   :class:`RetryClass.RESPONSE` — the request reached Slack but the
    response failed or signalled "try again" (``ReadTimeout``,
    ``5xx``, Slack ``ratelimited``). Retried only for an *idempotent*
    call (a ``GET`` read, ``reactions.add`` — adding the same reaction
    twice is the no-op ``already_reacted``). A non-idempotent
    ``chat.postMessage`` is **not** retried on a response-phase failure;
    its body surfaces to the caller, which already tolerates a bare
    ``ok:false`` / transport error without double-posting.

``ratelimited`` is honoured by sleeping the ``Retry-After`` header (Slack
sends it in seconds) when present, else the standard backoff. The
backoff and timeout are read once from the environment so a slow link
can widen them without a code change.
"""

import os
import time
from collections.abc import Callable
from enum import Enum
from typing import cast

import httpx

from teatree.backends.slack.scopes import SLACK_METHOD_SCOPES, slack_scope_failure
from teatree.core.scope_cache import ScopeMissingError, guarded_scope_call, token_scope_id
from teatree.types import RawAPIDict

type SleepFn = Callable[[float], None]
type AttemptFn = Callable[[], httpx.Response]

__all__ = [
    "DEFAULT_BACKOFF_BASE_SECONDS",
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_TIMEOUT_SECONDS",
    "RetryClass",
    "SlackHttpClient",
]

DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_RETRIES = 3
DEFAULT_BACKOFF_BASE_SECONDS = 0.5
_MAX_BACKOFF_SECONDS = 30.0
_RATELIMITED = "ratelimited"
_CONNECT_ERRORS = (httpx.ConnectTimeout, httpx.ConnectError, httpx.PoolTimeout)


def _scope_guarded(method: str, token: str, call: Callable[[], RawAPIDict]) -> RawAPIDict:
    """Run *call* under the token-scope cache (souliane/teatree#1450, PR-19).

    A known-missing ``(token, method-scope)`` pair short-circuits pre-HTTP; the
    first live ``missing_scope`` records the pair and banners once. Both outcomes
    surface the same ``missing_scope`` body every caller already tolerates, so the
    cache stays non-breaking while collapsing the repeated HTTP into a single call
    per loop run. A method with no mapped scope is passed through unguarded.
    """
    token_id = token_scope_id(token)
    scope = SLACK_METHOD_SCOPES.get(method, "")
    try:
        return guarded_scope_call(token_id, scope, call, slack_scope_failure)
    except ScopeMissingError as exc:
        return {"ok": False, "error": "missing_scope", "needed": exc.scope}


class RetryClass(Enum):
    """How a transient failure may be retried.

    ``CONNECT`` — the request never reached Slack, so a retry cannot
    duplicate a side effect; safe even for a non-idempotent post.
    ``RESPONSE`` — the request reached Slack; retry only when the call
    itself is idempotent (a read, or ``reactions.add``).
    """

    CONNECT = "connect"
    RESPONSE = "response"


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


class SlackHttpClient:
    """Transport for the Slack Web API with a bounded retry on transient errors.

    ``timeout`` and ``max_retries`` default from
    ``T3_SLACK_HTTP_TIMEOUT`` / ``T3_SLACK_HTTP_MAX_RETRIES`` /
    ``T3_SLACK_HTTP_BACKOFF`` so a slow workspace can widen them without a
    code change. ``max_retries`` is the number of *additional* attempts
    after the first, so the default ``3`` means up to four total tries.
    ``sleep`` is injectable purely so tests assert the backoff schedule
    without real delay — production uses ``time.sleep``.
    """

    _BASE_URL = "https://slack.com/api"

    def __init__(
        self,
        *,
        timeout: float | None = None,
        max_retries: int | None = None,
        backoff_base: float | None = None,
        sleep: SleepFn = time.sleep,
    ) -> None:
        self._timeout = timeout if timeout is not None else _env_float("T3_SLACK_HTTP_TIMEOUT", DEFAULT_TIMEOUT_SECONDS)
        self._max_retries = (
            max_retries if max_retries is not None else _env_int("T3_SLACK_HTTP_MAX_RETRIES", DEFAULT_MAX_RETRIES)
        )
        self._backoff_base = (
            backoff_base
            if backoff_base is not None
            else _env_float("T3_SLACK_HTTP_BACKOFF", DEFAULT_BACKOFF_BASE_SECONDS)
        )
        self._sleep = sleep

    def post(
        self,
        method: str,
        *,
        token: str,
        json: RawAPIDict,
        idempotent: bool,
    ) -> RawAPIDict:
        def call() -> RawAPIDict:
            return cast("RawAPIDict", self._post_response(method, token=token, json=json, idempotent=idempotent).json())

        return _scope_guarded(method, token, call)

    def post_with_header(
        self,
        method: str,
        *,
        token: str,
        json: RawAPIDict,
        header: str,
    ) -> tuple[RawAPIDict, str]:
        """Idempotent POST returning ``(body, header_value)`` for header-carried data.

        ``auth.test`` reports the granted OAuth scopes in a response
        *header*, not the JSON body, so the backend needs the header back.
        Always idempotent — ``auth.test`` has no side effect.
        """
        response = self._post_response(method, token=token, json=json, idempotent=True)
        return cast("RawAPIDict", response.json()), response.headers.get(header, "")

    def _post_response(self, method: str, *, token: str, json: RawAPIDict, idempotent: bool) -> httpx.Response:
        def attempt() -> httpx.Response:
            return httpx.post(
                f"{self._BASE_URL}/{method}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json; charset=utf-8",
                },
                json=json,
                timeout=self._timeout,
            )

        return self._run(attempt, idempotent=idempotent)

    def get(self, method: str, *, token: str, params: dict[str, str | int]) -> RawAPIDict:
        def attempt() -> httpx.Response:
            return httpx.get(
                f"{self._BASE_URL}/{method}",
                headers={"Authorization": f"Bearer {token}"},
                params=params,
                timeout=self._timeout,
            )

        def call() -> RawAPIDict:
            return cast("RawAPIDict", self._run(attempt, idempotent=True).json())

        return _scope_guarded(method, token, call)

    def post_external(self, url: str, *, content: bytes) -> int:
        """POST raw ``content`` to an off-Slack upload URL, returning the status code.

        The Slack ``files.getUploadURLExternal`` step returns a one-shot
        ``upload_url`` on Slack's file storage host (not ``slack.com/api``)
        that the caller POSTs the file bytes to before
        ``files.completeUploadExternal``. The host accepts the bytes only on
        POST and 302-redirects any other verb to ``slack.com``. The POST
        carries no token (the URL is itself the capability) and the upload is
        idempotent — the same bytes to the same one-shot URL — so it is
        retried under the standard bounded-backoff like any read.
        """

        def attempt() -> httpx.Response:
            return httpx.post(url, content=content, timeout=self._timeout)

        return self._run(attempt, idempotent=True).status_code

    def _run(self, attempt: AttemptFn, *, idempotent: bool) -> httpx.Response:
        # The final iteration (retries_left == 0) always returns or re-raises,
        # so the loop is exhaustive — no unreachable fall-through.
        for retries_left in range(self._max_retries, -1, -1):
            last = retries_left == 0
            try:
                response = attempt()
            except _CONNECT_ERRORS:
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
                response.raise_for_status()
                return response
            self._sleep_for(retry_after if retry_after > 0 else self._backoff_seconds(self._max_retries - retries_left))
        unreachable = "retry loop is exhaustive: the final iteration always returns or raises"
        raise AssertionError(unreachable)  # pragma: no cover

    def _transient_response_wait(self, response: httpx.Response, *, idempotent: bool) -> float | None:
        """Seconds to wait before a response-phase retry, or ``None`` when not retryable.

        ``0.0`` means "retry on the standard backoff"; a positive value is
        an explicit ``Retry-After`` to honour. ``None`` means surface the
        response to the caller (success, a non-transient ``ok:false``, or a
        non-idempotent post that must not be replayed on a response-phase
        failure).
        """
        if not self._is_transient_response(response):
            return None
        if not self._may_retry(RetryClass.RESPONSE, idempotent=idempotent):
            return None
        return self._retry_after_seconds(response)

    def _is_transient_response(self, response: httpx.Response) -> bool:
        if response.status_code >= httpx.codes.INTERNAL_SERVER_ERROR:
            return True
        if response.status_code == httpx.codes.TOO_MANY_REQUESTS:
            return True
        return self._is_slack_ratelimited(response)

    @staticmethod
    def _is_slack_ratelimited(response: httpx.Response) -> bool:
        if response.status_code != httpx.codes.OK:
            return False
        try:
            body = response.json()
        except ValueError:
            return False
        return isinstance(body, dict) and body.get("error") == _RATELIMITED

    @staticmethod
    def _retry_after_seconds(response: httpx.Response) -> float:
        header = response.headers.get("Retry-After", "").strip()
        if not header:
            return 0.0
        try:
            return max(0.0, float(header))
        except ValueError:
            return 0.0

    @staticmethod
    def _may_retry(failure: RetryClass, *, idempotent: bool) -> bool:
        return failure is RetryClass.CONNECT or idempotent

    def _backoff(self, attempt_index: int) -> None:
        self._sleep_for(self._backoff_seconds(attempt_index))

    def _backoff_seconds(self, attempt_index: int) -> float:
        return min(_MAX_BACKOFF_SECONDS, self._backoff_base * (2**attempt_index))

    def _sleep_for(self, seconds: float) -> None:
        if seconds > 0:
            self._sleep(min(_MAX_BACKOFF_SECONDS, seconds))
