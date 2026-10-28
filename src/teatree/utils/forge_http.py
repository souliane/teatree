"""Bounded HTTP connection pools keyed by a forge origin, with stale keep-alive and connect handling."""

import contextvars
import logging
import random
import time
from collections.abc import Callable, Mapping
from threading import Lock
from typing import BinaryIO

import httpx

logger = logging.getLogger(__name__)

type Sleep = Callable[[float], None]
type Jitter = Callable[[], float]
type Clock = Callable[[], float]
type Headers = Mapping[str, str]

KEEPALIVE_EXPIRY_SECONDS = 30.0
SUMMARY_INTERVAL_SECONDS = 60.0
CONNECT_JITTER_SECONDS = (0.1, 0.5)

CONNECT_TIMEOUT_SECONDS = 5.0
MAX_SAME_ORIGIN_REDIRECTS = 5

_TIMEOUT = httpx.Timeout(15.0, connect=CONNECT_TIMEOUT_SECONDS)
_REPLAYED_METHODS = frozenset({"GET", "HEAD"})
_STALE_KEEPALIVE_ERRORS = (httpx.RemoteProtocolError, httpx.ReadError)
_CONNECT_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout)
_RNG = random.SystemRandom()


def _connect_jitter() -> float:
    return _RNG.uniform(*CONNECT_JITTER_SECONDS)


def _bounded(seconds: float) -> httpx.Timeout:
    return httpx.Timeout(seconds, connect=min(seconds, CONNECT_TIMEOUT_SECONDS))


def _origin(url: str | httpx.URL) -> str:
    return str(httpx.URL(url).copy_with(path="", query=None, fragment=None))


class _ConnectionTrace:
    def __init__(self) -> None:
        self.opened = False

    def __call__(self, event_name: str, _info: object) -> None:
        if event_name == "connection.connect_tcp.started":
            self.opened = True


def _same_origin_redirects(url: str, fetch: Callable[[str], httpx.Response]) -> httpx.Response:
    response = fetch(url)
    for _ in range(MAX_SAME_ORIGIN_REDIRECTS):
        location = response.headers.get("location")
        if not response.is_redirect or not location:
            break
        target = str(response.url.join(location))
        if _origin(target) != _origin(url):
            break
        response = fetch(target)
    return response


class ForgeSession:
    """One forge origin's pooled client, behind the verbs the forge backends use."""

    def __init__(self, owner: "ForgeConnections", client: httpx.Client) -> None:
        self._owner = owner
        self._client = client

    def get(self, url: str, *, headers: Headers, timeout: float) -> httpx.Response:
        return self._owner.send(
            "GET",
            url,
            lambda: _same_origin_redirects(
                url, lambda target: self._client.get(target, headers=headers, timeout=_bounded(timeout))
            ),
        )

    def delete(self, url: str, *, headers: Headers, timeout: float) -> httpx.Response:
        return self._owner.send(
            "DELETE", url, lambda: self._client.delete(url, headers=headers, timeout=_bounded(timeout))
        )

    def post(self, url: str, *, headers: Headers, json: object, timeout: float) -> httpx.Response:
        return self._owner.send(
            "POST", url, lambda: self._client.post(url, headers=headers, json=json, timeout=_bounded(timeout))
        )

    def put(self, url: str, *, headers: Headers, json: object, timeout: float) -> httpx.Response:
        return self._owner.send(
            "PUT", url, lambda: self._client.put(url, headers=headers, json=json, timeout=_bounded(timeout))
        )

    def upload(
        self, url: str, *, headers: Headers, files: Mapping[str, tuple[str, BinaryIO]], timeout: float
    ) -> httpx.Response:
        return self._owner.send(
            "POST", url, lambda: self._client.post(url, headers=headers, files=files, timeout=_bounded(timeout))
        )

    def close(self) -> None:
        self._client.close()

    def request(
        self, method: str, url: str, *, headers: Headers, payload: str | Mapping[str, object] | None, timeout: float
    ) -> httpx.Response:
        content = payload if isinstance(payload, str) else None
        fields = None if isinstance(payload, str) else payload

        def send(target: str) -> httpx.Response:
            return self._client.request(
                method, target, headers=headers, content=content, json=fields, timeout=_bounded(timeout)
            )

        if method not in _REPLAYED_METHODS:
            return self._owner.send(method, url, lambda: send(url))
        return self._owner.send(method, url, lambda: _same_origin_redirects(url, send))


class ForgeConnections:
    def __init__(
        self,
        *,
        max_connections: int,
        sleep: Sleep = time.sleep,
        jitter: Jitter = _connect_jitter,
        clock: Clock = time.monotonic,
    ) -> None:
        self._limits = httpx.Limits(
            max_connections=max_connections,
            max_keepalive_connections=max_connections,
            keepalive_expiry=KEEPALIVE_EXPIRY_SECONDS,
        )
        self._sleep = sleep
        self._jitter = jitter
        self._clock = clock
        self._sessions: dict[str, ForgeSession] = {}
        self._lock = Lock()
        self._trace: contextvars.ContextVar[_ConnectionTrace] = contextvars.ContextVar("forge_connection_trace")
        self._window_started = clock()
        self._requests = 0
        self._reused = 0

    def session_for(self, url: str) -> ForgeSession:
        origin = _origin(url)
        with self._lock:
            if (session := self._sessions.get(origin)) is None:
                client = httpx.Client(
                    limits=self._limits, timeout=_TIMEOUT, event_hooks={"request": [self._attach_trace]}
                )
                session = self._sessions[origin] = ForgeSession(self, client)
            return session

    def close(self) -> None:
        with self._lock:
            sessions = tuple(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            session.close()

    def send(self, method: str, url: str, attempt: Callable[[], httpx.Response]) -> httpx.Response:
        trace = self._begin_trace()
        try:
            response = attempt()
        except _CONNECT_ERRORS:
            self._sleep(self._jitter())
        except _STALE_KEEPALIVE_ERRORS:
            if method not in _REPLAYED_METHODS or trace.opened:
                raise
        else:
            self._record(url, trace)
            return response
        trace = self._begin_trace()
        response = attempt()
        self._record(url, trace)
        return response

    def _attach_trace(self, request: httpx.Request) -> None:
        request.extensions["trace"] = self._trace.get()

    def _begin_trace(self) -> _ConnectionTrace:
        trace = _ConnectionTrace()
        self._trace.set(trace)
        return trace

    def _record(self, url: str, trace: _ConnectionTrace) -> None:
        logger.debug(
            "forge pool %s: %s", httpx.URL(url).host, "opened a new connection" if trace.opened else "reused one"
        )
        with self._lock:
            self._requests += 1
            self._reused += 0 if trace.opened else 1
            if self._clock() - self._window_started < SUMMARY_INTERVAL_SECONDS:
                return
            requests, reused = self._requests, self._reused
            self._window_started, self._requests, self._reused = self._clock(), 0, 0
        logger.info("forge pool: %d requests, %d on reused connections, in the last minute", requests, reused)
