"""Process-lifetime token-scope-failure cache (souliane/teatree#1450, PR-19).

A backend call that fails because its OAuth token lacks a scope is a hard
configuration failure, not a transient one: retrying it in this loop process
fails identically. Yet the loop issues the same call from many sites per tick,
so a single missing scope produced dozens of identical late failures — each a
real HTTP round-trip, each a fresh mid-tick surprise, and each silently
swallowed as an ``ok:false`` no-op.

This module caches the ``(token_id, scope)`` pairs known-missing for the current
loop process. A guarded call consults the cache BEFORE issuing any HTTP: a
known-missing pair short-circuits to :class:`ScopeMissingError` (``cached=True``)
with zero network traffic. The first real failure for a pair records it, logs
once, and emits exactly ONE bot->user banner (idempotency key
``scope_missing:<token_id>:<scope>``) so the operator learns of the missing
scope once, not once per call site.

``token_id`` is a short non-secret fingerprint of the token
(:func:`token_scope_id`, a sha256 prefix) — the literal token never enters the
cache, a log line, or a banner. The cache lives for the loop-process lifetime; a
new tick resets it (:func:`reset_scope_cache`) so a scope granted between ticks
is re-tested. ``t3 doctor authorizations`` clears an individual entry on a
verified-success probe (:meth:`ScopeCache.clear`).
"""

import hashlib
import logging
from collections.abc import Callable

logger = logging.getLogger(__name__)

type Notifier = Callable[..., bool]

_BANNER = (
    "Token {token_id} is missing the {scope!r} OAuth scope{detail} — every call "
    "needing it fails this loop run. Grant the scope (re-run `t3 doctor "
    "authorizations` / re-auth the connector), then re-run."
)


def _notify_user_deferred(text: str, *, kind: str, idempotency_key: str) -> bool:
    """Default banner sink — a function-scoped :func:`notify_user` import.

    Deferred on purpose: this cache is consulted from the Django-free Slack
    transport (:class:`teatree.backends.slack.http.SlackHttpClient`). A
    module-scope ``teatree.core.notify`` import would pull ``teatree.core.models``
    at import time and force Django/AppRegistry on every importer of the
    transport. The banner only fires at RUNTIME on a real scope failure — well
    after ``ensure_django`` — so importing here keeps the import graph clean.
    """
    from teatree.core.notify import notify_user  # noqa: PLC0415 — Django-free import graph; see docstring.

    return notify_user(text, kind=kind, idempotency_key=idempotency_key)


class ScopeMissingError(RuntimeError):
    """A backend call could not run because its token lacks the required scope.

    ``cached`` distinguishes the two arrival paths: ``False`` is the first live
    failure (the HTTP call ran and returned a scope error), ``True`` is a
    pre-HTTP short-circuit against an already-recorded ``(token_id, scope)`` pair
    — the whole point of the cache. Callers that tolerate a missing scope
    (a best-effort reaction, a non-critical post) catch this and no-op; the
    banner has already told the operator.

    ``body`` is the verbatim response that triggered a live failure, so a caller
    can surface the transport's raw error fields (e.g. Slack's ``provided``)
    instead of a lossy reconstruction. It is ``None`` on a pre-HTTP short-circuit,
    where no response exists.
    """

    def __init__(
        self, *, token_id: str, scope: str, cached: bool, detail: str = "", body: object | None = None
    ) -> None:
        self.token_id = token_id
        self.scope = scope
        self.cached = cached
        self.detail = detail
        self.body = body
        suffix = " (cached)" if cached else ""
        message = f"token {token_id} is missing scope {scope!r}{suffix}"
        if detail:
            message = f"{message}: {detail}"
        super().__init__(message)


def token_scope_id(token: str) -> str:
    """A short non-secret fingerprint of *token* for cache/log/banner keys.

    Never the literal token: a sha256 prefix so two different tokens never
    collide in the cache while the secret itself never reaches a log line, a
    banner, or the transcript. An empty token yields ``""`` (the caller then
    skips guarding — there is no token to attribute a scope to).
    """
    if not token:
        return ""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


class ScopeCache:
    """Records ``(token_id, scope)`` pairs known-missing for this loop process.

    The notifier is injectable so a unit test observes the banner without a DB;
    production uses :func:`teatree.core.notify.notify_user` (DB-backed
    idempotency ledger), so a re-recorded pair (same key) sends no second
    banner even across the record dedup below.
    """

    def __init__(self, *, notifier: Notifier | None = None) -> None:
        self._missing: dict[tuple[str, str], str] = {}
        self._notifier = notifier if notifier is not None else _notify_user_deferred

    def is_missing(self, token_id: str, scope: str) -> bool:
        return (token_id, scope) in self._missing

    def raise_if_cached(self, token_id: str, scope: str) -> None:
        """Short-circuit to ``ScopeMissingError(cached=True)`` for a known-missing pair.

        The pre-HTTP gate: a call for a pair already recorded this loop run
        raises before any network traffic. A pair that has never failed is a
        no-op — the caller proceeds to the live call.
        """
        if (token_id, scope) in self._missing:
            raise ScopeMissingError(
                token_id=token_id,
                scope=scope,
                cached=True,
                detail=self._missing[token_id, scope],
            )

    def record_missing(self, token_id: str, scope: str, *, detail: str = "") -> None:
        """Record a first live scope failure: cache it, log once, banner once.

        A pair already recorded is a no-op (no duplicate log, no second banner)
        — the load-bearing dedup that turns N identical failures into one signal.
        """
        key = (token_id, scope)
        if key in self._missing:
            return
        self._missing[key] = detail
        logger.warning("token %s is missing scope %r%s", token_id, scope, f": {detail}" if detail else "")
        self._notifier(
            _BANNER.format(token_id=token_id, scope=scope, detail=f" ({detail})" if detail else ""),
            kind="info",
            idempotency_key=f"scope_missing:{token_id}:{scope}",
        )

    def clear(self, token_id: str, scope: str) -> bool:
        """Forget a pair after a verified-success probe; ``True`` if one was cleared."""
        return self._missing.pop((token_id, scope), None) is not None

    def entries(self) -> list[tuple[str, str]]:
        return sorted(self._missing)

    def reset(self) -> None:
        """Drop every entry — a new tick re-tests each scope from scratch."""
        self._missing.clear()


_CACHE: ScopeCache | None = None


def get_scope_cache() -> ScopeCache:
    """The process-lifetime scope cache singleton (created on first use)."""
    global _CACHE  # noqa: PLW0603 — module-level process-lifetime singleton, mirrors backend caches.
    if _CACHE is None:
        _CACHE = ScopeCache()
    return _CACHE


def reset_scope_cache() -> None:
    """Reset the singleton so the next tick re-tests every scope."""
    if _CACHE is not None:
        _CACHE.reset()


def guarded_scope_call[T](
    token_id: str,
    scope: str,
    call: Callable[[], T],
    detect: Callable[[T], str | None],
    *,
    cache: ScopeCache | None = None,
) -> T:
    """Run *call* under the scope cache, short-circuiting a known-missing scope.

    The response-inspection form used by the Slack transport (GitLab deferred):
    a pair already recorded raises ``ScopeMissingError(cached=True)`` before
    *call* runs (zero HTTP); otherwise *call* runs and *detect* classifies its
    result. ``detect`` returns a detail string when the result IS a scope
    failure (else ``None``) — the pair is then recorded (log + one banner) and
    ``ScopeMissingError(cached=False, body=result)`` raised, carrying the raw
    response so the caller keeps the transport's verbatim error fields. An empty
    *scope* means the call has no scope requirement to guard, so it runs unguarded.
    """
    if not token_id or not scope:
        return call()
    cache = cache if cache is not None else get_scope_cache()
    cache.raise_if_cached(token_id, scope)
    result = call()
    detail = detect(result)
    if detail is not None:
        cache.record_missing(token_id, scope, detail=detail)
        raise ScopeMissingError(token_id=token_id, scope=scope, cached=False, detail=detail, body=result)
    return result


__all__ = [
    "ScopeCache",
    "ScopeMissingError",
    "get_scope_cache",
    "guarded_scope_call",
    "reset_scope_cache",
    "token_scope_id",
]
