"""The ORM-free lease-liveness predicates (#1073/#1604/#3571).

Pure decisions over a lease's ``(session_id, owner_pid, expires_at)`` triple plus the
slot's ``trust_pid_past_ttl`` policy — tested without a DB row. ``pid_alive`` is the
one external probe, so it is the only thing patched.
"""

from datetime import UTC, datetime, timedelta

import pytest

import teatree.utils.singleton as singleton_mod
from teatree.core.loop_lease_liveness import (
    anchorable_owner_pid,
    lease_is_live,
    live_foreign_owner_session,
    pid_alive_probe,
    pid_is_foreign,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)
FUTURE = NOW + timedelta(minutes=10)
PAST = NOW - timedelta(minutes=10)


class TestPidAliveProbe:
    def test_resolves_the_singleton_probe_when_importable(self) -> None:
        assert pid_alive_probe() is singleton_mod.pid_alive

    def test_a_missing_probe_degrades_to_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # An environment where the probe cannot be imported degrades to None everywhere,
        # so every caller falls through to the TTL backstop identically.
        monkeypatch.delattr(singleton_mod, "pid_alive")
        assert pid_alive_probe() is None


class TestAnchorableOwnerPid:
    def test_a_null_pid_stays_null(self) -> None:
        assert anchorable_owner_pid(None) is None

    def test_a_provably_dead_pid_drops_to_null(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(singleton_mod, "pid_alive", lambda _p: False)
        assert anchorable_owner_pid(4321) is None

    def test_a_live_pid_is_kept(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(singleton_mod, "pid_alive", lambda _p: True)
        assert anchorable_owner_pid(4321) == 4321

    def test_an_unprobeable_pid_is_kept_not_dropped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delattr(singleton_mod, "pid_alive")
        assert anchorable_owner_pid(4321) == 4321


class TestLeaseIsLive:
    def test_an_empty_session_is_never_live(self) -> None:
        assert lease_is_live("", 100, FUTURE, NOW, trust_pid_past_ttl=True) is False

    def test_a_dead_pid_is_not_live_at_any_ttl(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(singleton_mod, "pid_alive", lambda _p: False)
        assert lease_is_live("s", 100, FUTURE, NOW, trust_pid_past_ttl=True) is False

    def test_the_master_slot_keeps_an_alive_pid_live_past_ttl(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(singleton_mod, "pid_alive", lambda _p: True)
        assert lease_is_live("s", 100, PAST, NOW, trust_pid_past_ttl=True) is True

    def test_a_per_loop_slot_falls_through_to_ttl_for_a_reused_pid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(singleton_mod, "pid_alive", lambda _p: True)
        assert lease_is_live("s", 100, PAST, NOW, trust_pid_past_ttl=False) is False
        assert lease_is_live("s", 100, FUTURE, NOW, trust_pid_past_ttl=False) is True

    def test_a_null_pid_falls_to_the_ttl(self) -> None:
        assert lease_is_live("s", None, FUTURE, NOW, trust_pid_past_ttl=True) is True
        assert lease_is_live("s", None, PAST, NOW, trust_pid_past_ttl=True) is False

    def test_an_unprobeable_pid_falls_to_the_ttl(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delattr(singleton_mod, "pid_alive")
        assert lease_is_live("s", 100, FUTURE, NOW, trust_pid_past_ttl=True) is True


class TestLiveForeignOwnerSession:
    def test_a_live_foreign_owner_is_reported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(singleton_mod, "pid_alive", lambda _p: True)
        row = {"session_id": "other", "owner_pid": 100, "lease_expires_at": FUTURE}
        assert live_foreign_owner_session(row, "me", NOW, trust_pid_past_ttl=True) == "other"

    def test_the_owning_session_itself_is_never_foreign(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(singleton_mod, "pid_alive", lambda _p: True)
        row = {"session_id": "me", "owner_pid": 100, "lease_expires_at": FUTURE}
        assert live_foreign_owner_session(row, "me", NOW, trust_pid_past_ttl=True) == ""

    def test_an_unowned_slot_is_not_foreign(self) -> None:
        assert live_foreign_owner_session(None, "me", NOW, trust_pid_past_ttl=True) == ""

    def test_a_dead_or_expired_owner_is_not_foreign(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(singleton_mod, "pid_alive", lambda _p: False)
        row = {"session_id": "other", "owner_pid": 100, "lease_expires_at": PAST}
        assert live_foreign_owner_session(row, "me", NOW, trust_pid_past_ttl=True) == ""


class TestPidIsForeign:
    def test_a_null_current_pid_is_treated_as_foreign(self) -> None:
        assert pid_is_foreign(100, None) is True

    def test_a_different_pid_is_foreign(self) -> None:
        assert pid_is_foreign(100, 200) is True

    def test_the_same_process_is_not_foreign(self) -> None:
        assert pid_is_foreign(100, 100) is False

    def test_a_null_stored_pid_biases_to_foreign(self) -> None:
        assert pid_is_foreign(None, 200) is True
