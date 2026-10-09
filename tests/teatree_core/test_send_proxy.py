"""Behaviour of the outbound send-proxy chokepoint (#117).

The proxy enforces the destination allowlist and redacts matching terms on
every send. The operator's own DM remains allowed with an empty allowlist.
"""

import logging
from unittest.mock import patch

import pytest

from teatree.core.models import ConfigSetting, SendAudit
from teatree.core.models.provenance import Provenance
from teatree.core.send_proxy import (
    REDACTION_PLACEHOLDER,
    SendBlockedError,
    SendChannel,
    SendRequest,
    _redact_terms,
    destination_allowed,
    read_posting_credential,
    redact_payload,
    route_send,
)

# These tests need pytest's monkeypatch fixture (patching _redact_terms / read_pass),
# which django.test.TestCase cannot provide — so pytest.mark.django_db is the right tool.
# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


def _set_allowlist(entries: list[str]) -> None:
    ConfigSetting.objects.set_value("send_proxy_allowlist", entries)


def _request(**overrides: object) -> SendRequest:
    base: dict[str, object] = {
        "channel": SendChannel.SLACK,
        "destination": "C_TEAM",
        "payload": "hello team",
        "action": "post",
    }
    base.update(overrides)
    return SendRequest(**base)


class TestUnconditionalEnforcement:
    def test_records_an_allowed_audit_row_for_an_allowlisted_destination(self) -> None:
        _set_allowlist(["C_TEAM"])
        verdict = route_send(_request(destination="C_TEAM"))
        assert verdict.allowlist_ok is True
        assert SendAudit.objects.get().allowlist_verdict == SendAudit.Verdict.ALLOWED.value

    def test_denies_a_non_allowlisted_destination(self) -> None:
        _set_allowlist(["C_TEAM"])
        verdict = route_send(_request(destination="C_ATTACKER"))
        assert verdict.allowed is False
        assert "not on the send-proxy allowlist" in verdict.reason
        assert SendAudit.objects.get().allowlist_verdict == SendAudit.Verdict.DENIED.value

    def test_allows_an_allowlisted_destination(self) -> None:
        _set_allowlist(["C_TEAM"])
        verdict = route_send(_request(destination="C_TEAM"))
        assert verdict.allowed is True
        assert SendAudit.objects.get().allowlist_verdict == SendAudit.Verdict.ALLOWED.value

    def test_empty_allowlist_denies_every_non_self_destination(self) -> None:
        verdict = route_send(_request(destination="C_ANY"))
        assert verdict.allowed is False

    def test_send_blocked_error_carries_the_verdict(self) -> None:
        verdict = route_send(_request(destination="C_ANY"))
        err = SendBlockedError(verdict)
        assert err.verdict is verdict
        assert "allowlist" in str(err)


class TestSelfDmNeverLockout:
    def test_self_dm_is_allowed_with_empty_allowlist(self) -> None:
        verdict = route_send(_request(destination="D_USER", is_self_dm=True))
        assert verdict.allowed is True
        assert verdict.allowlist_ok is True
        assert SendAudit.objects.get().allowlist_verdict == SendAudit.Verdict.ALLOWED.value


class TestUnconditionalRedaction:
    def test_redacts_a_matching_term_in_the_payload(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _set_allowlist(["C_TEAM"])
        monkeypatch.setattr("teatree.core.send_proxy._redact_terms", lambda _overlay: ["SECRETCORP"])
        verdict = route_send(_request(destination="C_TEAM", payload="leak SECRETCORP here"))
        assert REDACTION_PLACEHOLDER in verdict.payload
        assert "SECRETCORP" not in verdict.payload
        assert verdict.payload_redacted is True
        assert verdict.redaction_matches == ("SECRETCORP",)
        assert SendAudit.objects.get().redaction_applied is True


class TestAllowlistFailure:
    def test_unreadable_allowlist_denies_the_send(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _unreadable(*_args: object, **_kwargs: object) -> bool:
            raise OSError

        monkeypatch.setattr("teatree.core.send_proxy.destination_allowed", _unreadable)
        verdict = route_send(_request())
        assert verdict.allowed is False
        assert SendAudit.objects.get().allowlist_verdict == SendAudit.Verdict.DENIED.value


class TestAnUnreadableAllowlistIsLoud:
    def test_the_failure_is_a_warning_that_names_the_overlay(self, caplog: pytest.LogCaptureFixture) -> None:
        with (
            patch("teatree.core.send_proxy.get_effective_settings", side_effect=RuntimeError("settings store down")),
            caplog.at_level(logging.WARNING, logger="teatree.core.send_proxy"),
        ):
            assert destination_allowed(SendChannel.SLACK, "C-eng", overlay="acme") is False

        [record] = [r for r in caplog.records if r.name == "teatree.core.send_proxy"]
        assert record.levelno == logging.WARNING
        assert "acme" in record.getMessage()
        assert record.exc_info is not None


class TestRedactPayloadUnit:
    def test_whole_token_match_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A short redact term must not surface inside a longer word (the shared
        # whole-token matcher, not naive substring).
        monkeypatch.setattr("teatree.core.send_proxy._redact_terms", lambda _overlay: ["ab"])
        redacted, matches = redact_payload("the abroad ab works", overlay="")
        assert matches == ("ab",)
        assert redacted == f"the abroad {REDACTION_PLACEHOLDER} works"

    def test_no_terms_is_a_noop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("teatree.core.send_proxy._redact_terms", lambda _overlay: [])
        redacted, matches = redact_payload("nothing to hide", overlay="")
        assert redacted == "nothing to hide"
        assert matches == ()


class TestDestinationAllowed:
    def test_glob_match_on_bare_destination(self) -> None:
        _set_allowlist(["org/*"])
        assert destination_allowed(SendChannel.GITHUB, "org/repo", overlay="") is True
        assert destination_allowed(SendChannel.GITHUB, "other/repo", overlay="") is False

    def test_channel_qualified_match(self) -> None:
        _set_allowlist(["slack:C_TEAM"])
        assert destination_allowed(SendChannel.SLACK, "C_TEAM", overlay="") is True
        # A github destination with the same id must NOT match a slack-qualified rule.
        assert destination_allowed(SendChannel.GITHUB, "C_TEAM", overlay="") is False

    def test_empty_allowlist_matches_nothing(self) -> None:
        assert destination_allowed(SendChannel.SLACK, "C_TEAM", overlay="") is False


class TestAuditProvenanceDelegation:
    def test_records_provenance_and_authorized_by(self) -> None:
        route_send(
            _request(
                authorized_by="directive:42",
                provenance=Provenance.PUBLIC.value,
            ),
        )
        row = SendAudit.objects.get()
        assert row.authorized_by == "directive:42"
        assert row.provenance == Provenance.PUBLIC.value

    def test_defaults_provenance_to_owner(self) -> None:
        route_send(_request())
        assert SendAudit.objects.get().provenance == Provenance.OWNER.value

    def test_overlong_destination_is_truncated_to_column_width(self) -> None:
        route_send(_request(destination="D" * 900))
        assert len(SendAudit.objects.get().destination) == 512


class TestReadPostingCredential:
    def test_blank_ref_short_circuits(self) -> None:
        assert read_posting_credential("") == ""

    def test_delegates_to_read_pass(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("teatree.utils.secrets.read_pass", lambda ref: f"token-for-{ref}")
        assert read_posting_credential("slack/bot") == "token-for-slack/bot"


class TestNeverRaise:
    def test_audit_write_failure_does_not_break_the_send(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(**_kwargs: object) -> None:
            msg = "db down"
            raise RuntimeError(msg)

        monkeypatch.setattr(SendAudit.objects, "create", _boom)
        # The send still resolves a verdict — the audit is a side ledger.
        _set_allowlist(["C_TEAM"])
        verdict = route_send(_request())
        assert verdict.allowed is True
        assert SendAudit.objects.count() == 0


class TestRedactTerms:
    def test_redact_terms_is_empty_when_privacy_rules_are_unresolvable(self) -> None:
        """A ``None`` from overlay_privacy_rules degrades to no redaction terms (best-effort)."""
        with patch("teatree.core.send_proxy.overlay_privacy_rules", return_value=None):
            assert _redact_terms("some-overlay") == []
