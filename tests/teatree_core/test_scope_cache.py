"""Token-scope-failure cache behaviour (souliane/teatree#1450, PR-19 item 6).

Drives the seam directly (no DB, no network): a stub notifier records banner
idempotency keys and a fake transport counts HTTP calls, so the acceptance
simulation — N distinct missing scopes across many loop calls collapsing to N
cache entries, N deduped banners, and zero extra HTTP on the repeats — is
asserted at the seam that every backend transport consults.
"""

import pytest

import teatree.core.intake.scope_cache as scope_cache_module
from teatree.core.intake.scope_cache import (
    ScopeCache,
    ScopeMissingError,
    get_scope_cache,
    guarded_scope_call,
    reset_scope_cache,
    token_scope_id,
)
from teatree.core.notify import NotifyKind


class _BannerRecorder:
    """A stand-in for :func:`notify_user` that records the keys it was sent."""

    def __init__(self) -> None:
        self.keys: list[str] = []
        self.kinds: list[NotifyKind | str] = []

    def __call__(self, text: str, *, kind: NotifyKind | str, idempotency_key: str, **_: object) -> bool:
        assert text
        self.keys.append(idempotency_key)
        self.kinds.append(kind)
        return True


def _missing_scope_transport() -> tuple[list[int], object]:
    """A fake call that counts invocations and always reports a Slack scope failure."""
    counter = [0]

    def call() -> dict[str, str]:
        counter[0] += 1
        return {"ok": "false", "error": "missing_scope", "needed": "reactions:write"}

    return counter, call


def _detect(resp: dict[str, str]) -> str | None:
    return resp.get("needed") if resp.get("error") == "missing_scope" else None


class TestTokenScopeId:
    def test_never_returns_the_literal_token(self) -> None:
        token = "xoxb-super-secret-value"
        fingerprint = token_scope_id(token)
        assert token not in fingerprint
        assert "secret" not in fingerprint
        assert len(fingerprint) == 12

    def test_is_stable_and_distinct_per_token(self) -> None:
        assert token_scope_id("token-a") == token_scope_id("token-a")
        assert token_scope_id("token-a") != token_scope_id("token-b")

    def test_empty_token_yields_empty_id(self) -> None:
        assert token_scope_id("") == ""


class TestScopeCacheDedup:
    def test_first_failure_records_and_banners_exactly_once(self) -> None:
        recorder = _BannerRecorder()
        cache = ScopeCache(notifier=recorder)
        token_id = token_scope_id("xoxb-1")

        cache.record_missing(token_id, "reactions:write", detail="reactions:write")
        cache.record_missing(token_id, "reactions:write", detail="reactions:write")

        assert cache.entries() == [(token_id, "reactions:write")]
        assert recorder.keys == [f"scope_missing:{token_id}:reactions:write"]
        assert recorder.kinds == [NotifyKind.INFO]

    def test_cached_pair_short_circuits_before_the_call(self) -> None:
        recorder = _BannerRecorder()
        cache = ScopeCache(notifier=recorder)
        token_id = token_scope_id("xoxb-2")
        counter, call = _missing_scope_transport()

        # First call runs the transport and records the miss (cached=False).
        with pytest.raises(ScopeMissingError) as first:
            guarded_scope_call(token_id, "reactions:write", call, _detect, cache=cache)
        assert first.value.cached is False
        # Second call must NOT touch the transport (cached=True).
        with pytest.raises(ScopeMissingError) as second:
            guarded_scope_call(token_id, "reactions:write", call, _detect, cache=cache)
        assert second.value.cached is True
        assert counter[0] == 1

    def test_empty_scope_is_unguarded(self) -> None:
        recorder = _BannerRecorder()
        cache = ScopeCache(notifier=recorder)
        _counter, call = _missing_scope_transport()
        # No scope requirement → the call runs, the miss is never recorded.
        result = guarded_scope_call(token_scope_id("xoxb-3"), "", call, _detect, cache=cache)
        assert result["error"] == "missing_scope"
        assert cache.entries() == []
        assert recorder.keys == []

    def test_clear_forgets_a_pair_on_verified_success(self) -> None:
        cache = ScopeCache(notifier=_BannerRecorder())
        token_id = token_scope_id("xoxb-4")
        cache.record_missing(token_id, "chat:write")
        assert cache.is_missing(token_id, "chat:write")
        assert cache.clear(token_id, "chat:write") is True
        assert cache.clear(token_id, "chat:write") is False
        assert not cache.is_missing(token_id, "chat:write")


class TestScopeFailureSimulation:
    """The PR-19 acceptance simulation, asserted at the guard seam."""

    def test_single_scope_across_fifty_calls_one_banner_one_entry(self) -> None:
        recorder = _BannerRecorder()
        cache = ScopeCache(notifier=recorder)
        token_id = token_scope_id("xoxb-single")
        counter, call = _missing_scope_transport()

        short_circuits = 0
        for _ in range(50):
            try:
                guarded_scope_call(token_id, "reactions:write", call, _detect, cache=cache)
            except ScopeMissingError as exc:
                short_circuits += 1 if exc.cached else 0

        assert counter[0] == 1  # one live HTTP, forty-nine short-circuits, zero extra HTTP
        assert short_circuits == 49
        assert cache.entries() == [(token_id, "reactions:write")]
        assert len(recorder.keys) == 1

    def test_four_scopes_across_fifty_calls(self) -> None:
        recorder = _BannerRecorder()
        cache = ScopeCache(notifier=recorder)
        token_id = token_scope_id("xoxb-four")
        scopes = ["reactions:write", "chat:write", "im:write", "channels:read"]

        http_calls = 0
        short_circuits = 0
        live_failures = 0

        def call() -> dict[str, str]:
            nonlocal http_calls
            http_calls += 1
            return {"ok": "false", "error": "missing_scope"}

        def detect(resp: dict[str, str]) -> str | None:
            return "missing_scope" if resp.get("error") == "missing_scope" else None

        for index in range(50):
            scope = scopes[index % len(scopes)]
            try:
                guarded_scope_call(token_id, scope, call, detect, cache=cache)
            except ScopeMissingError as exc:
                if exc.cached:
                    short_circuits += 1
                else:
                    live_failures += 1

        assert http_calls == 4  # exactly one live HTTP per distinct scope — zero extra
        assert live_failures == 4
        assert short_circuits == 46
        assert cache.entries() == sorted((token_id, scope) for scope in scopes)
        # One deduped banner per distinct pair; the 46 repeats add none.
        assert len(recorder.keys) == 4
        assert len(set(recorder.keys)) == 4


class TestScopeCacheReset:
    def test_reset_clears_every_entry(self) -> None:
        cache = ScopeCache(notifier=_BannerRecorder())
        cache.record_missing("tok", "scope")
        assert cache.is_missing("tok", "scope")
        cache.reset()
        assert not cache.is_missing("tok", "scope")


class TestProcessSingleton:
    def test_get_scope_cache_is_a_stable_singleton(self) -> None:
        scope_cache_module._CACHE = None
        first = get_scope_cache()
        assert get_scope_cache() is first

    def test_reset_scope_cache_covers_both_branches(self) -> None:
        scope_cache_module._CACHE = None
        reset_scope_cache()  # None branch: nothing created yet — no-op
        get_scope_cache()  # create the singleton
        reset_scope_cache()  # not-None branch: reset() runs on the (empty) singleton
        assert get_scope_cache().entries() == []
