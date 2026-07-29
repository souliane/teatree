"""Bounded-retry HTTP transport for the Slack Web API (#1110, reliability).

``SlackBotBackend`` previously issued every ``httpx.post`` / ``httpx.get``
with a hard ``timeout=10.0`` and no retry. A single transient
``ReadTimeout`` / ``ConnectTimeout`` / ``5xx`` / Slack ``ratelimited``
then broke a loop tick — a missed ``reactions.add``, a dropped
merge-notify ``chat.postMessage`` during the PR sweep — even though the
very next attempt would have succeeded. :class:`SlackHttpClient`
centralises the transport so every call gets a configurable timeout and
a bounded exponential backoff on *transient* failures only.

The retry machine itself lives in
:class:`teatree.backends.http_retry.BoundedRetryTransport` — the shared
bounded-retry transport GitLab's client is built on too. This subclass
adds the two Slack-specific seams: the ``ratelimited`` classification
(:meth:`SlackHttpClient._extra_transient`) and the scope-cache guard
(:func:`_scope_guarded`). It also flips
``_RAISE_FOR_STATUS_ON_RETURN`` on so a persistent ``5xx`` surfaces as an
exception (GitLab, by contrast, hands the raw response back).

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

import time
from collections.abc import Callable
from typing import cast, override

import httpx

from teatree.backends.http_retry import (
    DEFAULT_BACKOFF_BASE_SECONDS,
    DEFAULT_MAX_RETRIES,
    DEFAULT_TIMEOUT_SECONDS,
    BoundedRetryTransport,
    RetryClass,
    SleepFn,
    env_float,
)
from teatree.backends.slack.scopes import SLACK_METHOD_SCOPES, slack_scope_failure
from teatree.core.intake.scope_cache import ScopeMissingError, guarded_scope_call, token_scope_id
from teatree.types import RawAPIDict

__all__ = [
    "DEFAULT_BACKOFF_BASE_SECONDS",
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_TIMEOUT_SECONDS",
    "RetryClass",
    "SlackHttpClient",
]

_RATELIMITED = "ratelimited"


def _scope_guarded(method: str, token: str, call: Callable[[], RawAPIDict]) -> RawAPIDict:
    """Run *call* under the token-scope cache (souliane/teatree#1450, PR-19).

    A known-missing ``(token, method-scope)`` pair short-circuits pre-HTTP; the
    first live ``missing_scope`` records the pair and banners once. The first live
    failure returns Slack's verbatim body (its ``needed``/``provided`` fields
    intact, so a caller can report exactly which scope is missing); the pre-HTTP
    short-circuit — where no response exists — reconstructs the minimal
    ``missing_scope`` body every caller already tolerates. A method with no mapped
    scope is passed through unguarded.
    """
    token_id = token_scope_id(token)
    scope = SLACK_METHOD_SCOPES.get(method, "")
    try:
        return guarded_scope_call(token_id, scope, call, slack_scope_failure)
    except ScopeMissingError as exc:
        if exc.body is not None:
            return cast("RawAPIDict", exc.body)
        return {"ok": False, "error": "missing_scope", "needed": exc.scope}


class SlackHttpClient(BoundedRetryTransport):
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
    _RAISE_FOR_STATUS_ON_RETURN = True

    def __init__(
        self,
        *,
        timeout: float | None = None,
        max_retries: int | None = None,
        backoff_base: float | None = None,
        sleep: SleepFn = time.sleep,
    ) -> None:
        self._timeout = timeout if timeout is not None else env_float("T3_SLACK_HTTP_TIMEOUT", DEFAULT_TIMEOUT_SECONDS)
        self._configure_retry(
            env_prefix="T3_SLACK_HTTP", max_retries=max_retries, backoff_base=backoff_base, sleep=sleep
        )

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

    @override
    def _is_transient_response(self, response: httpx.Response) -> bool:
        return super()._is_transient_response(response) or self._is_slack_ratelimited(response)

    @staticmethod
    def _is_slack_ratelimited(response: httpx.Response) -> bool:
        if response.status_code != httpx.codes.OK:
            return False
        try:
            body = response.json()
        except ValueError:
            return False
        return isinstance(body, dict) and body.get("error") == _RATELIMITED
