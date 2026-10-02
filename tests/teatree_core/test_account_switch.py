"""Tests for ``teatree.core.account_switch`` — in-session `/login` recovery (#1916).

The account fingerprint comes from synthetic ``~/.claude.json`` under
``tmp_path``; the connector reachability probe is exercised against a fake
``MessagingBackend`` whose ``auth_test`` is the only mocked boundary. Backend
construction is intercepted so the cycle never touches a real ``pass`` store or
Slack.
"""

import datetime as dt
import json
from pathlib import Path

import pytest
from django.test import TestCase
from django.utils import timezone

from teatree.core.account_fingerprint import fingerprint_switched
from teatree.core.account_switch import (
    AccountSwitchOutcome,
    AccountSwitchRecovery,
    ConnectorProbeResult,
    current_account_fingerprint,
    expire_token_health_cache,
    load_recorded_fingerprint,
    probe_connectors,
    record_fingerprint,
)
from teatree.core.models.anthropic_token_usage import AnthropicTokenUsage, TokenHealthReading


def _write_active_account(home: Path, account_uuid: str, email: str = "user@example.com") -> None:
    (home / ".claude.json").write_text(
        json.dumps(
            {
                "userID": "abcd",
                "oauthAccount": {
                    "accountUuid": account_uuid,
                    "emailAddress": email,
                    "organizationUuid": "org-1",
                },
            },
        ),
        encoding="utf-8",
    )


class _FakeBackend:
    def __init__(self, *, ok: bool, name: str = "slack") -> None:
        self._ok = ok
        self.name = name
        self.auth_calls = 0

    def auth_test(self) -> dict:
        self.auth_calls += 1
        return {"ok": self._ok, "team": "T1"} if self._ok else {"ok": False, "error": "invalid_auth"}


class TestFingerprint:
    def test_reads_account_uuid_from_claude_json(self, tmp_path: Path) -> None:
        _write_active_account(tmp_path, "uuid-A")
        assert current_account_fingerprint(home=tmp_path) == "uuid-A"

    def test_missing_file_is_empty_fingerprint(self, tmp_path: Path) -> None:
        assert current_account_fingerprint(home=tmp_path) == ""

    def test_malformed_file_is_empty_fingerprint(self, tmp_path: Path) -> None:
        (tmp_path / ".claude.json").write_text("{not json", encoding="utf-8")
        assert current_account_fingerprint(home=tmp_path) == ""

    def test_record_then_load_roundtrips(self, tmp_path: Path) -> None:
        record_fingerprint("uuid-A", home=tmp_path)
        assert load_recorded_fingerprint(home=tmp_path) == "uuid-A"

    def test_load_absent_record_is_empty(self, tmp_path: Path) -> None:
        assert load_recorded_fingerprint(home=tmp_path) == ""


class _RaisingBackend:
    def auth_test(self) -> dict:
        msg = "connection reset"
        raise ConnectionError(msg)


class TestProbeConnectors:
    def test_reachable_backend_passes(self) -> None:
        results = probe_connectors([_FakeBackend(ok=True, name="slack")])
        assert results == [ConnectorProbeResult(name="slack", reachable=True, detail="")]

    def test_unreachable_backend_carries_error_detail(self) -> None:
        [result] = probe_connectors([_FakeBackend(ok=False, name="slack")])
        assert result.reachable is False
        assert "invalid_auth" in result.detail

    def test_probe_calls_auth_test_live(self) -> None:
        backend = _FakeBackend(ok=True)
        probe_connectors([backend])
        assert backend.auth_calls == 1

    def test_raising_backend_is_unreachable_not_a_crash(self) -> None:
        [result] = probe_connectors([_RaisingBackend()])
        assert result.reachable is False
        assert result.name == "_RaisingBackend"
        assert "ConnectionError" in result.detail


class TestDetectAndRecover:
    @pytest.fixture(autouse=True)
    def _seams(self) -> None:
        self.backends = [_FakeBackend(ok=True, name="slack")]
        self.reset_calls = 0

    def _run(self, home: Path) -> AccountSwitchOutcome:
        def _reset() -> None:
            self.reset_calls += 1

        recovery = AccountSwitchRecovery(
            reset_caches=_reset, backends=lambda: list(self.backends), expire_token_health=lambda: 0
        )
        return recovery.run(home=home)

    def test_first_run_records_fingerprint_no_switch(self, tmp_path: Path) -> None:
        _write_active_account(tmp_path, "uuid-A")
        outcome = self._run(tmp_path)
        assert outcome.switched is False
        assert outcome.previous_fingerprint == ""
        assert outcome.current_fingerprint == "uuid-A"
        assert load_recorded_fingerprint(home=tmp_path) == "uuid-A"

    def test_same_account_is_noop_no_cache_reset(self, tmp_path: Path) -> None:
        _write_active_account(tmp_path, "uuid-A")
        record_fingerprint("uuid-A", home=tmp_path)
        outcome = self._run(tmp_path)
        assert outcome.switched is False
        assert self.reset_calls == 0

    def test_switch_detected_invalidates_cache_and_reprobes(self, tmp_path: Path) -> None:
        _write_active_account(tmp_path, "uuid-B")
        record_fingerprint("uuid-A", home=tmp_path)
        outcome = self._run(tmp_path)
        assert outcome.switched is True
        assert outcome.previous_fingerprint == "uuid-A"
        assert outcome.current_fingerprint == "uuid-B"
        assert self.reset_calls == 1
        assert outcome.all_reachable is True
        assert load_recorded_fingerprint(home=tmp_path) == "uuid-B"

    def test_switch_with_unreachable_connector_is_surfaced(self, tmp_path: Path) -> None:
        self.backends[:] = [_FakeBackend(ok=False, name="slack")]
        _write_active_account(tmp_path, "uuid-B")
        record_fingerprint("uuid-A", home=tmp_path)
        outcome = self._run(tmp_path)
        assert outcome.switched is True
        assert outcome.all_reachable is False
        assert any(not p.reachable for p in outcome.probes)

    def test_failed_recovery_does_not_record_new_fingerprint(self, tmp_path: Path) -> None:
        self.backends[:] = [_FakeBackend(ok=False, name="slack")]
        _write_active_account(tmp_path, "uuid-B")
        record_fingerprint("uuid-A", home=tmp_path)
        self._run(tmp_path)
        assert load_recorded_fingerprint(home=tmp_path) == "uuid-A"

    def test_failed_recovery_keeps_surfacing_next_session(self, tmp_path: Path) -> None:
        self.backends[:] = [_FakeBackend(ok=False, name="slack")]
        _write_active_account(tmp_path, "uuid-B")
        record_fingerprint("uuid-A", home=tmp_path)
        self._run(tmp_path)
        assert fingerprint_switched(home=tmp_path) is True
        second = self._run(tmp_path)
        assert second.switched is True

    def test_successful_recovery_records_and_clears_next_session(self, tmp_path: Path) -> None:
        _write_active_account(tmp_path, "uuid-B")
        record_fingerprint("uuid-A", home=tmp_path)
        self._run(tmp_path)
        assert load_recorded_fingerprint(home=tmp_path) == "uuid-B"
        assert fingerprint_switched(home=tmp_path) is False

    def test_outcome_is_account_switch_outcome(self, tmp_path: Path) -> None:
        _write_active_account(tmp_path, "uuid-A")
        outcome = self._run(tmp_path)
        assert isinstance(outcome, AccountSwitchOutcome)

    def test_empty_active_fingerprint_records_nothing(self, tmp_path: Path) -> None:
        outcome = self._run(tmp_path)
        assert outcome.switched is False
        assert outcome.current_fingerprint == ""
        assert load_recorded_fingerprint(home=tmp_path) == ""


class TestModuleWrappers:
    def test_detect_wrapper_uses_production_recovery(self, tmp_path: Path) -> None:
        from teatree.core import account_switch  # noqa: PLC0415

        _write_active_account(tmp_path, "uuid-A")
        outcome = account_switch.detect_and_recover_account_switch(home=tmp_path)
        assert outcome.current_fingerprint == "uuid-A"
        assert outcome.switched is False

    def test_overlay_messaging_backends_filters_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from types import SimpleNamespace  # noqa: PLC0415

        from teatree.core import account_switch  # noqa: PLC0415

        backend = _FakeBackend(ok=True)
        overlays = [SimpleNamespace(messaging=backend), SimpleNamespace(messaging=None)]
        monkeypatch.setattr(account_switch, "iter_overlay_backends", lambda: overlays)
        assert account_switch.overlay_messaging_backends() == [backend]


class TestTokenHealthExpiryOnSwitch:
    """A `/login` expires the cached per-account token health (#4736).

    An exhausted row is trusted until its blocking window resets, so without this the
    governor kept denying every dispatch on the OLD account's exhaustion — the switch
    being the very remedy the operator reached for.
    """

    def _run(self, home: Path, *, expired: int = 0) -> tuple[AccountSwitchOutcome, list[str]]:
        calls: list[str] = []

        def _expire() -> int:
            calls.append("expire")
            return expired

        recovery = AccountSwitchRecovery(
            reset_caches=lambda: None,
            backends=lambda: [_FakeBackend(ok=True, name="slack")],
            expire_token_health=_expire,
        )
        return recovery.run(home=home), calls

    def test_switch_expires_the_token_health_cache(self, tmp_path: Path) -> None:
        _write_active_account(tmp_path, "uuid-B")
        record_fingerprint("uuid-A", home=tmp_path)

        outcome, calls = self._run(tmp_path, expired=3)

        assert outcome.switched is True
        assert calls == ["expire"]
        assert outcome.token_health_rows_expired == 3

    def test_no_switch_leaves_the_token_health_cache_alone(self, tmp_path: Path) -> None:
        _write_active_account(tmp_path, "uuid-A")
        record_fingerprint("uuid-A", home=tmp_path)

        outcome, calls = self._run(tmp_path)

        assert outcome.switched is False
        assert calls == []
        assert outcome.token_health_rows_expired == 0

    def test_first_run_leaves_the_token_health_cache_alone(self, tmp_path: Path) -> None:
        _write_active_account(tmp_path, "uuid-A")

        _outcome, calls = self._run(tmp_path)

        assert calls == []


class TestTokenHealthExpiryDefaultSeam(TestCase):
    """The production seam expires real rows — the wiring, not a stub (#4736)."""

    def test_the_default_seam_expires_a_spent_row(self) -> None:
        now = timezone.now()
        AnthropicTokenUsage.objects.record(
            "anthropic/acct",
            TokenHealthReading(
                organization_id="org-old",
                utilization_5h=1.0,
                utilization_7d=0.0,
                status_5h="rejected",
                status_7d="allowed",
                reset_5h=now + dt.timedelta(hours=3),
                reset_7d=None,
            ),
            now=now,
        )
        assert AnthropicTokenUsage.objects.get(pass_path="anthropic/acct").is_fresh(now) is True

        assert expire_token_health_cache() == 1

        assert AnthropicTokenUsage.objects.get(pass_path="anthropic/acct").is_fresh() is False
