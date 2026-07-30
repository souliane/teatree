"""Meeting-aware presence resolution — the local-TTS auto-mute abstraction (#2171).

Pins the resolver contract in :mod:`teatree.core.presence`: it reads the
``[teatree.speak] presence_backend`` opt-in, builds the registered backend via
the injected token ref, probes it (cached ~60 s), and degrades to
:attr:`~teatree.core.presence.Presence.UNKNOWN` — never a suppression — on an
unconfigured name, an absent backend, or any probe failure. Distinct from
``availability.PresenceHeartbeat`` (keyboard activity), which is unrelated.
"""

from collections.abc import Iterator

import pytest

from teatree.config import cold_reader
from teatree.core import presence
from teatree.types import LocalPlayback, SpeakConfig


class _StubBackend:
    def __init__(self, result: presence.Presence, *, boom: bool = False) -> None:
        self._result = result
        self._boom = boom
        self.calls = 0

    def current_presence(self) -> presence.Presence:
        self.calls += 1
        if self._boom:
            msg = "graph down"
            raise RuntimeError(msg)
        return self._result


@pytest.fixture(autouse=True)
def _clean_registry_and_cache() -> Iterator[None]:
    presence.reset_presence_cache()
    presence._FACTORIES.clear()
    yield
    presence.reset_presence_cache()
    presence._FACTORIES.clear()


def _config(monkeypatch: pytest.MonkeyPatch, cfg: SpeakConfig) -> None:
    monkeypatch.setattr(presence, "_effective_speak", lambda: cfg)


class TestCurrentPresence:
    def test_unconfigured_backend_is_unknown_no_build(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _config(monkeypatch, SpeakConfig(local=LocalPlayback.ALL))
        built: list[str] = []
        presence.register_presence_backend(
            "msteams", lambda ref: built.append(ref) or _StubBackend(presence.Presence.IN_MEETING)
        )
        assert presence.current_presence() is presence.Presence.UNKNOWN
        assert built == []  # name empty → never even builds the backend

    def test_in_meeting_probe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _config(
            monkeypatch, SpeakConfig(local=LocalPlayback.ALL, presence_backend="msteams", presence_token_ref="ms/tok")
        )
        stub = _StubBackend(presence.Presence.IN_MEETING)
        presence.register_presence_backend("msteams", lambda ref: stub)
        assert presence.current_presence() is presence.Presence.IN_MEETING

    def test_free_probe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _config(monkeypatch, SpeakConfig(presence_backend="msteams", presence_token_ref="ms/tok"))
        presence.register_presence_backend("msteams", lambda ref: _StubBackend(presence.Presence.FREE))
        assert presence.current_presence() is presence.Presence.FREE

    def test_absent_factory_is_unknown(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _config(monkeypatch, SpeakConfig(presence_backend="nope"))
        assert presence.current_presence() is presence.Presence.UNKNOWN

    def test_factory_returns_none_is_unknown(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _config(monkeypatch, SpeakConfig(presence_backend="msteams"))
        presence.register_presence_backend("msteams", lambda ref: None)
        assert presence.current_presence() is presence.Presence.UNKNOWN

    def test_factory_build_exception_is_unknown(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _config(monkeypatch, SpeakConfig(presence_backend="msteams", presence_token_ref="ms/tok"))

        def _boom(_ref: str) -> presence.PresenceBackend:
            msg = "build blew up"
            raise RuntimeError(msg)

        presence.register_presence_backend("msteams", _boom)
        assert presence.current_presence() is presence.Presence.UNKNOWN

    def test_probe_exception_is_unknown_not_suppression(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _config(monkeypatch, SpeakConfig(presence_backend="msteams", presence_token_ref="ms/tok"))
        presence.register_presence_backend("msteams", lambda ref: _StubBackend(presence.Presence.FREE, boom=True))
        assert presence.current_presence() is presence.Presence.UNKNOWN

    def test_token_ref_is_threaded_to_factory(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _config(monkeypatch, SpeakConfig(presence_backend="msteams", presence_token_ref="ms/access"))
        seen: list[str] = []
        presence.register_presence_backend(
            "msteams", lambda ref: seen.append(ref) or _StubBackend(presence.Presence.FREE)
        )
        presence.current_presence()
        assert seen == ["ms/access"]


class TestEffectiveSpeakRead:
    def test_effective_speak_reads_cold_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Django-free (cold_reader) so it is safe on the local-TTS daemon threads.
        monkeypatch.setattr(cold_reader, "read_setting", lambda *a, **k: {"presence_backend": "msteams"})
        cfg = presence._effective_speak()
        assert cfg.presence_backend == "msteams"

    def test_effective_speak_defaults_when_no_row(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cold_reader, "read_setting", lambda *a, **k: None)
        assert presence._effective_speak().presence_backend == ""

    def test_config_read_failure_is_unknown(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom() -> SpeakConfig:
            msg = "config corrupt"
            raise ValueError(msg)

        monkeypatch.setattr(presence, "_effective_speak", _boom)
        assert presence.current_presence() is presence.Presence.UNKNOWN


class TestProbeCache:
    def test_known_result_cached_within_ttl(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _config(monkeypatch, SpeakConfig(presence_backend="msteams", presence_token_ref="ms/tok"))
        stub = _StubBackend(presence.Presence.IN_MEETING)
        presence.register_presence_backend("msteams", lambda ref: stub)
        clock = {"t": 1000.0}
        monkeypatch.setattr(presence, "_now", lambda: clock["t"])
        assert presence.current_presence() is presence.Presence.IN_MEETING
        clock["t"] += presence._PROBE_TTL_SECONDS / 2
        assert presence.current_presence() is presence.Presence.IN_MEETING
        assert stub.calls == 1  # second call served from cache

    def test_cache_expires_after_ttl(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _config(monkeypatch, SpeakConfig(presence_backend="msteams", presence_token_ref="ms/tok"))
        stub = _StubBackend(presence.Presence.FREE)
        presence.register_presence_backend("msteams", lambda ref: stub)
        clock = {"t": 1000.0}
        monkeypatch.setattr(presence, "_now", lambda: clock["t"])
        presence.current_presence()
        clock["t"] += presence._PROBE_TTL_SECONDS + 1
        presence.current_presence()
        assert stub.calls == 2  # re-probed after TTL

    def test_unknown_is_not_cached(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _config(monkeypatch, SpeakConfig(presence_backend="msteams", presence_token_ref="ms/tok"))
        stub = _StubBackend(presence.Presence.FREE, boom=True)
        presence.register_presence_backend("msteams", lambda ref: stub)
        clock = {"t": 1000.0}
        monkeypatch.setattr(presence, "_now", lambda: clock["t"])
        presence.current_presence()
        presence.current_presence()
        assert stub.calls == 2  # a transient UNKNOWN never sticks for the TTL
