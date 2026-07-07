"""Behaviour of the outbound send-proxy chokepoint (#117).

The proxy ships in ``warn`` mode (audit-only): it records a ``SendAudit`` row for
every send with the would-be allowlist verdict and redaction matches, but never
blocks a send and never mutates the live payload. ``enforce`` mode (opt-in, after
an operator seeds the allowlist from a WARN soak) is where a non-allowlisted
destination is denied and the payload is redacted — the attack surface these
tests pin.
"""

import pytest

from teatree.config.enums import SendProxyMode
from teatree.core.models import ConfigSetting, SendAudit
from teatree.core.models.provenance import Provenance
from teatree.core.send_proxy import (
    REDACTION_PLACEHOLDER,
    SendBlockedError,
    SendChannel,
    SendRequest,
    destination_allowed,
    read_posting_credential,
    redact_payload,
    route_send,
)

# These tests need pytest's monkeypatch fixture (patching _redact_terms / read_pass),
# which django.test.TestCase cannot provide — so pytest.mark.django_db is the right tool.
# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db


def _set_mode(mode: SendProxyMode) -> None:
    ConfigSetting.objects.set_value("send_proxy_mode", mode.value)


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


class TestWarnModeIsAuditOnly:
    def test_warn_never_blocks_a_non_allowlisted_destination(self) -> None:
        # Ship default: warn. An unseeded allowlist must NOT block a real send.
        verdict = route_send(_request(destination="C_UNKNOWN"))
        assert verdict.mode is SendProxyMode.WARN
        assert verdict.allowed is True
        assert verdict.allowlist_ok is False

    def test_warn_never_mutates_the_live_payload(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("teatree.core.send_proxy._redact_terms", lambda _overlay: ["SECRETCORP"])
        verdict = route_send(_request(payload="note about SECRETCORP internals"))
        assert verdict.payload == "note about SECRETCORP internals"
        assert verdict.payload_redacted is False
        # The match is still SURFACED for the audit soak, just not applied.
        assert verdict.redaction_matches == ("SECRETCORP",)

    def test_warn_records_a_warned_audit_row_for_a_non_allowlisted_destination(self) -> None:
        route_send(_request(destination="C_UNKNOWN", target="thread/1"))
        row = SendAudit.objects.get()
        assert row.channel == SendChannel.SLACK.value
        assert row.destination == "C_UNKNOWN"
        assert row.mode == SendProxyMode.WARN.value
        assert row.allowlist_verdict == SendAudit.Verdict.WARNED.value
        assert row.redaction_applied is False

    def test_warn_records_an_allowed_audit_row_for_an_allowlisted_destination(self) -> None:
        _set_allowlist(["C_TEAM"])
        verdict = route_send(_request(destination="C_TEAM"))
        assert verdict.allowlist_ok is True
        assert SendAudit.objects.get().allowlist_verdict == SendAudit.Verdict.ALLOWED.value


class TestEnforceModeAllowlistDeny:
    def test_enforce_denies_a_non_allowlisted_destination(self) -> None:
        _set_mode(SendProxyMode.ENFORCE)
        _set_allowlist(["C_TEAM"])
        verdict = route_send(_request(destination="C_ATTACKER"))
        assert verdict.allowed is False
        assert "not on the send-proxy allowlist" in verdict.reason
        assert SendAudit.objects.get().allowlist_verdict == SendAudit.Verdict.DENIED.value

    def test_enforce_allows_an_allowlisted_destination(self) -> None:
        _set_mode(SendProxyMode.ENFORCE)
        _set_allowlist(["C_TEAM"])
        verdict = route_send(_request(destination="C_TEAM"))
        assert verdict.allowed is True
        assert SendAudit.objects.get().allowlist_verdict == SendAudit.Verdict.ALLOWED.value

    def test_enforce_with_empty_allowlist_denies_every_non_self_destination(self) -> None:
        _set_mode(SendProxyMode.ENFORCE)
        verdict = route_send(_request(destination="C_ANY"))
        assert verdict.allowed is False

    def test_send_blocked_error_carries_the_verdict(self) -> None:
        _set_mode(SendProxyMode.ENFORCE)
        verdict = route_send(_request(destination="C_ANY"))
        err = SendBlockedError(verdict)
        assert err.verdict is verdict
        assert "allowlist" in str(err)


class TestSelfDmNeverLockout:
    def test_self_dm_is_allowed_even_in_enforce_with_empty_allowlist(self) -> None:
        _set_mode(SendProxyMode.ENFORCE)
        verdict = route_send(_request(destination="D_USER", is_self_dm=True))
        assert verdict.allowed is True
        assert verdict.allowlist_ok is True
        assert SendAudit.objects.get().allowlist_verdict == SendAudit.Verdict.ALLOWED.value


class TestEnforceModeRedaction:
    def test_enforce_redacts_a_matching_term_in_the_payload(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _set_mode(SendProxyMode.ENFORCE)
        _set_allowlist(["C_TEAM"])
        monkeypatch.setattr("teatree.core.send_proxy._redact_terms", lambda _overlay: ["SECRETCORP"])
        verdict = route_send(_request(destination="C_TEAM", payload="leak SECRETCORP here"))
        assert REDACTION_PLACEHOLDER in verdict.payload
        assert "SECRETCORP" not in verdict.payload
        assert verdict.payload_redacted is True
        assert verdict.redaction_matches == ("SECRETCORP",)
        assert SendAudit.objects.get().redaction_applied is True


class TestRedactPayloadUnit:
    def test_whole_token_match_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A short redact term must not surface inside a longer word (the shared
        # whole-token matcher, not naive substring).
        monkeypatch.setattr("teatree.core.send_proxy._redact_terms", lambda _overlay: ["op"])
        redacted, matches = redact_payload("the cooperative op works", overlay="")
        assert matches == ("op",)
        assert redacted == f"the cooperative {REDACTION_PLACEHOLDER} works"

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
        verdict = route_send(_request())
        assert verdict.allowed is True
        assert SendAudit.objects.count() == 0
