"""Behaviour tests for :class:`OutboundAuditScanner` (#1019).

The scanner is purely a verifier — it does not call third-party APIs
itself, it dispatches each claim to a per-kind ``Verifier`` callable.
Tests construct the scanner with explicit ``verifiers`` so the
production-default lazy loaders never run, and with an explicit
``notifier`` mock so the production lazy-imported ``notify_user``
never executes either.
"""

import datetime as dt
from http import HTTPStatus
from unittest.mock import MagicMock, patch

import httpx
import pytest
from django.test import TestCase
from django.utils import timezone

from teatree.core.models import BotPing, OutboundClaim
from teatree.loop.scanners.outbound_audit import (
    OutboundAuditScanner,
    VerifyResult,
    _default_verifier_for,
    _github_note_verifier,
    _gitlab_approve_verifier,
    _gitlab_note_verifier,
    _hash_body,
    _slack_dm_verifier,
    _usernames_from_approvers,
    kind_settling_seconds,
)


def _aged_claim(*, kind: OutboundClaim.Kind, key: str, **extra: object) -> OutboundClaim:
    """Build an OutboundClaim that is past every per-kind settling window."""
    age = max(kind_settling_seconds.values(), default=30) + 30
    return OutboundClaim.objects.create(
        kind=kind,
        idempotency_key=key,
        target_url="https://example.com/artifact",
        claim_ts=timezone.now() - dt.timedelta(seconds=age),
        extra=extra or {},
    )


class OutboundAuditScannerTests(TestCase):
    def test_aged_claim_verified_when_api_confirms(self) -> None:
        claim = _aged_claim(kind=OutboundClaim.Kind.SLACK_DM, key="ok-claim")

        notify = MagicMock()
        scanner = OutboundAuditScanner(
            verifiers={"slack_dm": lambda _c: VerifyResult.ok()},
            notifier=notify,
        )
        signals = scanner.scan()

        claim.refresh_from_db()
        assert signals == []
        assert claim.verified_at is not None
        assert claim.drift_detected is False
        assert claim.drift_alerted_at is None
        assert not BotPing.objects.exists()
        notify.assert_not_called()

    def test_drift_flips_row_emits_signal_and_dms_user(self) -> None:
        claim = _aged_claim(kind=OutboundClaim.Kind.GITLAB_NOTE, key="drift-claim")

        notify = MagicMock()
        scanner = OutboundAuditScanner(
            verifiers={
                "gitlab_note": lambda _c: VerifyResult.drift("GitLab returned 404 for note 99"),
            },
            notifier=notify,
        )
        signals = scanner.scan()

        claim.refresh_from_db()
        assert claim.drift_detected is True
        assert "404" in claim.drift_reason
        assert claim.drift_alerted_at is not None
        assert claim.verified_at is None
        assert len(signals) == 1
        assert signals[0].kind == "outbound.drift"
        assert signals[0].payload["claim_id"] == claim.pk
        notify.assert_called_once()
        alert_text, idempotency_key = notify.call_args.args
        assert idempotency_key == f"outbound_drift:{claim.idempotency_key}"
        assert "404" in alert_text

    def test_drift_alert_does_not_refire_on_subsequent_tick(self) -> None:
        claim = _aged_claim(kind=OutboundClaim.Kind.GITLAB_NOTE, key="dedupe")

        notify = MagicMock()
        scanner = OutboundAuditScanner(
            verifiers={"gitlab_note": lambda _c: VerifyResult.drift("not found")},
            notifier=notify,
        )
        scanner.scan()
        scanner.scan()
        scanner.scan()

        claim.refresh_from_db()
        assert claim.drift_detected is True
        assert notify.call_count == 1  # dedupe on drift_alerted_at

    def test_unaged_claim_is_skipped(self) -> None:
        OutboundClaim.objects.create(
            kind=OutboundClaim.Kind.SLACK_DM,
            idempotency_key="fresh",
            claim_ts=timezone.now(),  # right now — not yet past settling window
            extra={},
        )

        calls: list[OutboundClaim] = []

        def _spy(c: OutboundClaim) -> VerifyResult:
            calls.append(c)
            return VerifyResult.ok()

        scanner = OutboundAuditScanner(verifiers={"slack_dm": _spy})
        signals = scanner.scan()

        assert signals == []
        assert calls == []  # the verifier was never called

    def test_verified_claim_is_not_re_verified(self) -> None:
        claim = _aged_claim(kind=OutboundClaim.Kind.SLACK_DM, key="already-verified")
        claim.verified_at = timezone.now()
        claim.save(update_fields=["verified_at"])

        calls: list[OutboundClaim] = []
        scanner = OutboundAuditScanner(
            verifiers={"slack_dm": lambda c: calls.append(c) or VerifyResult.ok()},
        )
        scanner.scan()
        assert calls == []

    def test_unknown_kind_without_verifier_emits_audit_skipped(self) -> None:
        _aged_claim(kind=OutboundClaim.Kind.NOTION_EDIT, key="unknown-kind")

        # No verifier configured for notion_edit and no production default
        # registered — scanner emits ``outbound.audit_skipped`` so the
        # backlog is observable instead of silently growing (#1275).
        # Never a drift signal: an unverifiable kind is not a missing
        # artifact.
        scanner = OutboundAuditScanner(verifiers={})
        signals = scanner.scan()
        assert len(signals) == 1
        assert signals[0].kind == "outbound.audit_skipped"
        assert all(s.kind != "outbound.drift" for s in signals)

    def test_verifier_exception_does_not_break_tick(self) -> None:
        _aged_claim(kind=OutboundClaim.Kind.SLACK_DM, key="raises")

        def _boom(_c: OutboundClaim) -> VerifyResult:
            msg = "transport down"
            raise RuntimeError(msg)

        notify = MagicMock()
        scanner = OutboundAuditScanner(verifiers={"slack_dm": _boom}, notifier=notify)
        signals = scanner.scan()
        assert signals == []
        notify.assert_not_called()

    def test_per_kind_settling_window_respected(self) -> None:
        # Claim aged 60s — past slack_dm window (30s) but not past notion_edit (120s).
        slack_claim = OutboundClaim.objects.create(
            kind=OutboundClaim.Kind.SLACK_DM,
            idempotency_key="slack-aged",
            claim_ts=timezone.now() - dt.timedelta(seconds=60),
            extra={},
        )
        notion_claim = OutboundClaim.objects.create(
            kind=OutboundClaim.Kind.NOTION_COMMENT,
            idempotency_key="notion-not-aged",
            claim_ts=timezone.now() - dt.timedelta(seconds=60),
            extra={},
        )

        called: list[str] = []
        scanner = OutboundAuditScanner(
            verifiers={
                "slack_dm": lambda c: called.append(c.idempotency_key) or VerifyResult.ok(),
                "notion_comment": lambda c: called.append(c.idempotency_key) or VerifyResult.ok(),
            },
        )
        scanner.scan()

        assert "slack-aged" in called
        assert "notion-not-aged" not in called
        slack_claim.refresh_from_db()
        notion_claim.refresh_from_db()
        assert slack_claim.verified_at is not None
        assert notion_claim.verified_at is None

    def test_default_notifier_uses_verified_delivery_wrapper(self) -> None:
        """Production default forwards to the #1181 verified-delivery wrapper."""
        from unittest.mock import patch as _patch  # noqa: PLC0415

        from teatree.loop.domain_jobs import default_drift_notifier  # noqa: PLC0415

        with _patch("teatree.loop.domain_jobs.notify_with_fallback") as notify_mock:
            default_drift_notifier("drift alert text", "outbound_drift:key-1")

        notify_mock.assert_called_once()
        kwargs = notify_mock.call_args.kwargs
        assert kwargs["idempotency_key"] == "outbound_drift:key-1"

    def test_notifier_exception_leaves_alert_unsent_and_retries_next_tick(self) -> None:
        """A failing notifier never breaks the tick and never marks the alert sent.

        ``drift_alerted_at`` is the dedupe key, so stamping it before the DM
        lands would permanently bury an undelivered drift alert. It must stay
        null on failure so the next tick re-attempts the notifier.
        """
        claim = _aged_claim(kind=OutboundClaim.Kind.GITLAB_NOTE, key="notify-boom")

        calls: list[str] = []

        def _boom(_text: str, _key: str) -> None:
            calls.append(_key)
            msg = "slack 500"
            raise RuntimeError(msg)

        scanner = OutboundAuditScanner(
            verifiers={"gitlab_note": lambda _c: VerifyResult.drift("not found")},
            notifier=_boom,
        )
        signals = scanner.scan()

        claim.refresh_from_db()
        assert claim.drift_detected is True
        assert claim.drift_alerted_at is None
        assert len(signals) == 1
        assert len(calls) == 1

        scanner.scan()
        claim.refresh_from_db()
        assert claim.drift_alerted_at is None
        assert len(calls) == 2

    def test_limit_caps_number_of_verifications_per_tick(self) -> None:
        for i in range(5):
            _aged_claim(kind=OutboundClaim.Kind.SLACK_DM, key=f"capped-{i}")

        calls: list[OutboundClaim] = []
        scanner = OutboundAuditScanner(
            limit=2,
            verifiers={"slack_dm": lambda c: calls.append(c) or VerifyResult.ok()},
        )
        scanner.scan()
        assert len(calls) == 2

    def test_drift_alert_claim_is_excluded_from_re_scanning(self) -> None:
        """Recursion guard for the drift-on-drift feedback loop.

        A claim recorded for the drift-alert DM itself (idempotency_key
        prefixed ``outbound_drift:``) must not be picked up on a subsequent
        tick — otherwise a failing drift DM would feedback into another
        drift DM about the missing drift DM.
        """
        # Simulate: the previous tick emitted a drift alert; the notify path
        # recorded an OutboundClaim for that DM (record_claim does this) —
        # its key starts with the canonical ``outbound_drift:`` prefix.
        _aged_claim(
            kind=OutboundClaim.Kind.SLACK_DM,
            key="outbound_drift:slack_dm:original",
            channel="C12345",
            ts="1234567890.123",
        )
        # And a normal, non-drift claim that SHOULD be picked up.
        _aged_claim(
            kind=OutboundClaim.Kind.SLACK_DM,
            key="normal:slack_dm:1",
            channel="C00000",
            ts="9876543210.000",
        )
        seen: list[OutboundClaim] = []
        scanner = OutboundAuditScanner(
            verifiers={"slack_dm": lambda c: seen.append(c) or VerifyResult.ok()},
        )
        scanner.scan()
        assert {c.idempotency_key for c in seen} == {"normal:slack_dm:1"}


def _http_status_error(status: int) -> httpx.HTTPStatusError:
    """Build an httpx.HTTPStatusError with a real Response carrying *status*."""
    request = httpx.Request("GET", "https://example.com/x")
    response = httpx.Response(status_code=status, request=request)
    return httpx.HTTPStatusError("status error", request=request, response=response)


class DefaultVerifierForTests(TestCase):
    """``_default_verifier_for`` dispatches to the kind-specific lazy factory."""

    def test_gitlab_note_kind_dispatches_to_gitlab_note_factory(self) -> None:
        sentinel = lambda _c: VerifyResult.ok()  # noqa: E731 — sentinel comparison
        with patch("teatree.loop.scanners.outbound_audit._gitlab_note_verifier", return_value=sentinel) as factory:
            result = _default_verifier_for("gitlab_note")
        factory.assert_called_once()
        assert result is sentinel

    def test_gitlab_approve_kind_dispatches_to_gitlab_approve_factory(self) -> None:
        sentinel = lambda _c: VerifyResult.ok()  # noqa: E731
        with patch("teatree.loop.scanners.outbound_audit._gitlab_approve_verifier", return_value=sentinel) as factory:
            result = _default_verifier_for("gitlab_approve")
        factory.assert_called_once()
        assert result is sentinel

    def test_slack_dm_kind_dispatches_to_slack_dm_factory(self) -> None:
        sentinel = lambda _c: VerifyResult.ok()  # noqa: E731
        with patch("teatree.loop.scanners.outbound_audit._slack_dm_verifier", return_value=sentinel) as factory:
            result = _default_verifier_for("slack_dm")
        factory.assert_called_once()
        assert result is sentinel

    def test_github_note_kind_dispatches_to_github_note_factory(self) -> None:
        sentinel = lambda _c: VerifyResult.ok()  # noqa: E731
        with patch("teatree.loop.scanners.outbound_audit._github_note_verifier", return_value=sentinel) as factory:
            result = _default_verifier_for("github_note")
        factory.assert_called_once()
        assert result is sentinel

    def test_unknown_kind_returns_none(self) -> None:
        # Notion kinds and any other unmapped string -> no production verifier
        assert _default_verifier_for("notion_edit") is None
        assert _default_verifier_for("notion_comment") is None
        assert _default_verifier_for("totally_made_up") is None


class UsernamesFromApproversTests(TestCase):
    """``_usernames_from_approvers`` handles malformed GitLab approval payloads."""

    def test_extracts_usernames_from_well_formed_payload(self) -> None:
        payload = [
            {"user": {"username": "alice"}},
            {"user": {"username": "bob"}},
        ]
        assert _usernames_from_approvers(payload) == {"alice", "bob"}

    def test_returns_empty_set_when_list_is_empty(self) -> None:
        assert _usernames_from_approvers([]) == set()

    def test_skips_non_dict_entries(self) -> None:
        payload: list[object] = [
            "not a dict",
            {"user": {"username": "alice"}},
            42,
        ]
        assert _usernames_from_approvers(payload) == {"alice"}

    def test_skips_entries_without_user_key(self) -> None:
        payload: list[object] = [
            {"approver": {"username": "alice"}},  # wrong key
            {"user": {"username": "bob"}},
        ]
        assert _usernames_from_approvers(payload) == {"bob"}

    def test_skips_entries_where_user_is_not_a_dict(self) -> None:
        payload: list[object] = [
            {"user": "alice"},  # malformed — user should be dict
            {"user": None},
            {"user": {"username": "bob"}},
        ]
        assert _usernames_from_approvers(payload) == {"bob"}

    def test_skips_entries_where_username_is_not_a_string(self) -> None:
        payload: list[object] = [
            {"user": {"username": 42}},  # bad type
            {"user": {"username": None}},
            {"user": {"username": "charlie"}},
        ]
        assert _usernames_from_approvers(payload) == {"charlie"}


class GitLabNoteVerifierTests(TestCase):
    """``_gitlab_note_verifier`` matches the GitLab API behaviour for note lookups.

    Anti-vacuous proof: each test names the production path it guards;
    reverting the factory's import-guard / lookup logic flips the assertion.
    """

    def test_returns_none_when_gitlab_api_module_unimportable(self) -> None:
        # Forcing import-time failure: shadow the module name with None
        # so the lazy ``from teatree.backends.gitlab.api import GitLabAPI``
        # raises ImportError, which the factory catches and returns None.
        import sys  # noqa: PLC0415

        # Save real entry (if loaded) and shadow with None.
        saved = sys.modules.get("teatree.backends.gitlab.api")
        sys.modules["teatree.backends.gitlab.api"] = None  # type: ignore[assignment]
        try:
            assert _gitlab_note_verifier() is None
        finally:
            if saved is not None:
                sys.modules["teatree.backends.gitlab.api"] = saved
            else:
                sys.modules.pop("teatree.backends.gitlab.api", None)

    def test_returns_none_when_gitlab_api_constructor_raises(self) -> None:
        # Missing token -> resolve fails -> GitLabAPI() still constructs (token=""),
        # but our `_resolve_token` could raise; simulate constructor raise.
        with patch("teatree.backends.gitlab.api.GitLabAPI", side_effect=RuntimeError("no token")):
            assert _gitlab_note_verifier() is None

    def test_404_returns_drift(self) -> None:
        fake_api = MagicMock()
        fake_api.get_json.side_effect = _http_status_error(HTTPStatus.NOT_FOUND)
        with patch("teatree.backends.gitlab.api.GitLabAPI", return_value=fake_api):
            verifier = _gitlab_note_verifier()
        assert verifier is not None
        claim = OutboundClaim(
            kind=OutboundClaim.Kind.GITLAB_NOTE,
            idempotency_key="note-404",
            extra={"repo": "org/proj", "mr": 1, "artifact_id": "42", "endpoint": "notes"},
        )
        result = verifier(claim)
        assert result.verified is False
        assert "42" in result.drift_reason
        assert "not found" in result.drift_reason

    def test_500_re_raises_so_scan_skips(self) -> None:
        fake_api = MagicMock()
        fake_api.get_json.side_effect = _http_status_error(HTTPStatus.INTERNAL_SERVER_ERROR)
        with patch("teatree.backends.gitlab.api.GitLabAPI", return_value=fake_api):
            verifier = _gitlab_note_verifier()
        assert verifier is not None
        claim = OutboundClaim(
            kind=OutboundClaim.Kind.GITLAB_NOTE,
            idempotency_key="note-500",
            extra={"repo": "org/proj", "mr": 1, "artifact_id": "42", "endpoint": "notes"},
        )
        with pytest.raises(httpx.HTTPStatusError):
            verifier(claim)

    def test_500_in_scanner_is_caught_and_claim_not_flagged(self) -> None:
        """End-to-end: a 500 on get_json must NOT flip the claim to drift."""
        claim = _aged_claim(
            kind=OutboundClaim.Kind.GITLAB_NOTE,
            key="500-skip",
            repo="org/proj",
            mr=1,
            artifact_id="42",
            endpoint="notes",
        )
        fake_api = MagicMock()
        fake_api.get_json.side_effect = _http_status_error(HTTPStatus.INTERNAL_SERVER_ERROR)
        notify = MagicMock()
        with patch("teatree.backends.gitlab.api.GitLabAPI", return_value=fake_api):
            verifier = _gitlab_note_verifier()
        assert verifier is not None
        scanner = OutboundAuditScanner(verifiers={"gitlab_note": verifier}, notifier=notify)
        signals = scanner.scan()
        claim.refresh_from_db()
        assert signals == []
        assert claim.drift_detected is False
        assert claim.drift_alerted_at is None
        notify.assert_not_called()

    def test_returns_ok_when_extra_missing_required_fields(self) -> None:
        fake_api = MagicMock()
        with patch("teatree.backends.gitlab.api.GitLabAPI", return_value=fake_api):
            verifier = _gitlab_note_verifier()
        assert verifier is not None
        claim = OutboundClaim(
            kind=OutboundClaim.Kind.GITLAB_NOTE,
            idempotency_key="incomplete",
            extra={"repo": "org/proj"},  # missing mr / artifact_id
        )
        result = verifier(claim)
        assert result.verified is True
        fake_api.get_json.assert_not_called()

    def test_returns_ok_when_get_json_succeeds(self) -> None:
        fake_api = MagicMock()
        fake_api.get_json.return_value = {"id": 42, "body": "the note"}
        with patch("teatree.backends.gitlab.api.GitLabAPI", return_value=fake_api):
            verifier = _gitlab_note_verifier()
        assert verifier is not None
        claim = OutboundClaim(
            kind=OutboundClaim.Kind.GITLAB_NOTE,
            idempotency_key="note-ok",
            extra={"repo": "org/proj", "mr": 1, "artifact_id": "42", "endpoint": "notes"},
        )
        result = verifier(claim)
        assert result.verified is True

    def test_returns_ok_when_artifact_id_is_non_numeric(self) -> None:
        # Defensive guard: GitLab's note ids are numeric; a non-digit string
        # is a malformed claim — return ok rather than 404 on a non-route.
        fake_api = MagicMock()
        with patch("teatree.backends.gitlab.api.GitLabAPI", return_value=fake_api):
            verifier = _gitlab_note_verifier()
        assert verifier is not None
        claim = OutboundClaim(
            kind=OutboundClaim.Kind.GITLAB_NOTE,
            idempotency_key="non-numeric",
            extra={"repo": "org/proj", "mr": 1, "artifact_id": "abc", "endpoint": "notes"},
        )
        assert verifier(claim).verified is True
        fake_api.get_json.assert_not_called()


class GitLabApproveVerifierTests(TestCase):
    """``_gitlab_approve_verifier`` covers approve / unapprove drift detection."""

    def test_returns_none_when_username_resolution_fails(self) -> None:
        fake_api = MagicMock()
        fake_api.current_username.return_value = ""
        with patch("teatree.backends.gitlab.api.GitLabAPI", return_value=fake_api):
            assert _gitlab_approve_verifier() is None

    def test_returns_none_when_constructor_raises(self) -> None:
        with patch("teatree.backends.gitlab.api.GitLabAPI", side_effect=RuntimeError("auth")):
            assert _gitlab_approve_verifier() is None

    def test_returns_none_when_gitlab_api_module_unimportable(self) -> None:
        import sys  # noqa: PLC0415

        saved = sys.modules.get("teatree.backends.gitlab.api")
        sys.modules["teatree.backends.gitlab.api"] = None  # type: ignore[assignment]
        try:
            assert _gitlab_approve_verifier() is None
        finally:
            if saved is not None:
                sys.modules["teatree.backends.gitlab.api"] = saved
            else:
                sys.modules.pop("teatree.backends.gitlab.api", None)

    def test_approve_drift_when_user_not_in_approvers(self) -> None:
        fake_api = MagicMock()
        fake_api.current_username.return_value = "alice"
        fake_api.get_json.return_value = {"approved_by": [{"user": {"username": "bob"}}]}
        with patch("teatree.backends.gitlab.api.GitLabAPI", return_value=fake_api):
            verifier = _gitlab_approve_verifier()
        assert verifier is not None
        claim = OutboundClaim(
            kind=OutboundClaim.Kind.GITLAB_APPROVE,
            idempotency_key="approve-drift",
            extra={"repo": "org/proj", "mr": 1, "endpoint": "approve"},
        )
        result = verifier(claim)
        assert result.verified is False
        assert "alice" in result.drift_reason

    def test_approve_ok_when_user_present_in_approvers(self) -> None:
        fake_api = MagicMock()
        fake_api.current_username.return_value = "alice"
        fake_api.get_json.return_value = {"approved_by": [{"user": {"username": "alice"}}]}
        with patch("teatree.backends.gitlab.api.GitLabAPI", return_value=fake_api):
            verifier = _gitlab_approve_verifier()
        assert verifier is not None
        claim = OutboundClaim(
            kind=OutboundClaim.Kind.GITLAB_APPROVE,
            idempotency_key="approve-ok",
            extra={"repo": "org/proj", "mr": 1, "endpoint": "approve"},
        )
        assert verifier(claim).verified is True

    def test_unapprove_drift_when_user_still_present(self) -> None:
        fake_api = MagicMock()
        fake_api.current_username.return_value = "alice"
        fake_api.get_json.return_value = {"approved_by": [{"user": {"username": "alice"}}]}
        with patch("teatree.backends.gitlab.api.GitLabAPI", return_value=fake_api):
            verifier = _gitlab_approve_verifier()
        assert verifier is not None
        claim = OutboundClaim(
            kind=OutboundClaim.Kind.GITLAB_APPROVE,
            idempotency_key="unapprove-drift",
            extra={"repo": "org/proj", "mr": 1, "endpoint": "unapprove"},
        )
        result = verifier(claim)
        assert result.verified is False
        assert "still present" in result.drift_reason

    def test_unapprove_ok_when_user_absent(self) -> None:
        fake_api = MagicMock()
        fake_api.current_username.return_value = "alice"
        fake_api.get_json.return_value = {"approved_by": [{"user": {"username": "bob"}}]}
        with patch("teatree.backends.gitlab.api.GitLabAPI", return_value=fake_api):
            verifier = _gitlab_approve_verifier()
        assert verifier is not None
        claim = OutboundClaim(
            kind=OutboundClaim.Kind.GITLAB_APPROVE,
            idempotency_key="unapprove-ok",
            extra={"repo": "org/proj", "mr": 1, "endpoint": "unapprove"},
        )
        assert verifier(claim).verified is True

    def test_404_on_approvals_endpoint_returns_drift(self) -> None:
        fake_api = MagicMock()
        fake_api.current_username.return_value = "alice"
        fake_api.get_json.side_effect = _http_status_error(HTTPStatus.NOT_FOUND)
        with patch("teatree.backends.gitlab.api.GitLabAPI", return_value=fake_api):
            verifier = _gitlab_approve_verifier()
        assert verifier is not None
        claim = OutboundClaim(
            kind=OutboundClaim.Kind.GITLAB_APPROVE,
            idempotency_key="approvals-404",
            extra={"repo": "org/proj", "mr": 1, "endpoint": "approve"},
        )
        result = verifier(claim)
        assert result.verified is False
        assert "404" in result.drift_reason or "approvals" in result.drift_reason

    def test_500_re_raises_so_scan_skips(self) -> None:
        fake_api = MagicMock()
        fake_api.current_username.return_value = "alice"
        fake_api.get_json.side_effect = _http_status_error(HTTPStatus.INTERNAL_SERVER_ERROR)
        with patch("teatree.backends.gitlab.api.GitLabAPI", return_value=fake_api):
            verifier = _gitlab_approve_verifier()
        assert verifier is not None
        claim = OutboundClaim(
            kind=OutboundClaim.Kind.GITLAB_APPROVE,
            idempotency_key="approvals-500",
            extra={"repo": "org/proj", "mr": 1, "endpoint": "approve"},
        )
        with pytest.raises(httpx.HTTPStatusError):
            verifier(claim)

    def test_returns_ok_when_extra_missing_required_fields(self) -> None:
        fake_api = MagicMock()
        fake_api.current_username.return_value = "alice"
        with patch("teatree.backends.gitlab.api.GitLabAPI", return_value=fake_api):
            verifier = _gitlab_approve_verifier()
        assert verifier is not None
        claim = OutboundClaim(
            kind=OutboundClaim.Kind.GITLAB_APPROVE,
            idempotency_key="missing-mr",
            extra={"repo": "org/proj"},
        )
        result = verifier(claim)
        assert result.verified is True
        fake_api.get_json.assert_not_called()


class SlackDmVerifierTests(TestCase):
    """``_slack_dm_verifier`` distinguishes 404-equivalent from transport errors."""

    def test_returns_none_when_backend_unavailable(self) -> None:
        with patch("teatree.core.backend_factory.messaging_from_overlay", return_value=None):
            assert _slack_dm_verifier() is None

    def test_returns_none_when_factory_module_unimportable(self) -> None:
        import sys  # noqa: PLC0415

        saved = sys.modules.get("teatree.core.backend_factory")
        sys.modules["teatree.core.backend_factory"] = None  # type: ignore[assignment]
        try:
            assert _slack_dm_verifier() is None
        finally:
            if saved is not None:
                sys.modules["teatree.core.backend_factory"] = saved
            else:
                sys.modules.pop("teatree.core.backend_factory", None)

    def test_returns_ok_when_permalink_resolves(self) -> None:
        fake_backend = MagicMock()
        fake_backend.get_permalink.return_value = "https://slack.example/archives/C/p123"
        with patch("teatree.core.backend_factory.messaging_from_overlay", return_value=fake_backend):
            verifier = _slack_dm_verifier()
        assert verifier is not None
        claim = OutboundClaim(
            kind=OutboundClaim.Kind.SLACK_DM,
            idempotency_key="slack-ok",
            extra={"channel": "C12345", "ts": "1234567890.123"},
        )
        assert verifier(claim).verified is True

    def test_returns_drift_when_permalink_empty(self) -> None:
        """Empty permalink = Slack returned ok=false with channel/message_not_found."""
        fake_backend = MagicMock()
        fake_backend.get_permalink.return_value = ""
        with patch("teatree.core.backend_factory.messaging_from_overlay", return_value=fake_backend):
            verifier = _slack_dm_verifier()
        assert verifier is not None
        claim = OutboundClaim(
            kind=OutboundClaim.Kind.SLACK_DM,
            idempotency_key="slack-not-found",
            extra={"channel": "C12345", "ts": "1234567890.123"},
        )
        result = verifier(claim)
        assert result.verified is False
        assert "not found" in result.drift_reason

    def test_500_re_raises_so_scan_skips(self) -> None:
        """Slack 500 (HTTPStatusError) must propagate so scan() skips silently."""
        fake_backend = MagicMock()
        fake_backend.get_permalink.side_effect = _http_status_error(HTTPStatus.INTERNAL_SERVER_ERROR)
        with patch("teatree.core.backend_factory.messaging_from_overlay", return_value=fake_backend):
            verifier = _slack_dm_verifier()
        assert verifier is not None
        claim = OutboundClaim(
            kind=OutboundClaim.Kind.SLACK_DM,
            idempotency_key="slack-500",
            extra={"channel": "C12345", "ts": "1234567890.123"},
        )
        with pytest.raises(httpx.HTTPStatusError):
            verifier(claim)

    def test_network_error_re_raises_so_scan_skips(self) -> None:
        """Httpx network errors must propagate — not become drift."""
        fake_backend = MagicMock()
        fake_backend.get_permalink.side_effect = httpx.ConnectError("connection refused")
        with patch("teatree.core.backend_factory.messaging_from_overlay", return_value=fake_backend):
            verifier = _slack_dm_verifier()
        assert verifier is not None
        claim = OutboundClaim(
            kind=OutboundClaim.Kind.SLACK_DM,
            idempotency_key="slack-network",
            extra={"channel": "C12345", "ts": "1234567890.123"},
        )
        with pytest.raises(httpx.ConnectError):
            verifier(claim)

    def test_500_in_scanner_is_caught_and_claim_not_flagged(self) -> None:
        """End-to-end: a 500 from Slack must NOT flip the claim to drift."""
        claim = _aged_claim(
            kind=OutboundClaim.Kind.SLACK_DM,
            key="slack-500-end-to-end",
            channel="C12345",
            ts="1234567890.123",
        )
        fake_backend = MagicMock()
        fake_backend.get_permalink.side_effect = _http_status_error(HTTPStatus.INTERNAL_SERVER_ERROR)
        notify = MagicMock()
        with patch("teatree.core.backend_factory.messaging_from_overlay", return_value=fake_backend):
            verifier = _slack_dm_verifier()
        assert verifier is not None
        scanner = OutboundAuditScanner(verifiers={"slack_dm": verifier}, notifier=notify)
        signals = scanner.scan()
        claim.refresh_from_db()
        assert signals == []
        assert claim.drift_detected is False
        assert claim.drift_alerted_at is None
        notify.assert_not_called()

    def test_returns_ok_when_extra_missing_channel_or_ts(self) -> None:
        fake_backend = MagicMock()
        with patch("teatree.core.backend_factory.messaging_from_overlay", return_value=fake_backend):
            verifier = _slack_dm_verifier()
        assert verifier is not None
        claim = OutboundClaim(
            kind=OutboundClaim.Kind.SLACK_DM,
            idempotency_key="incomplete",
            extra={"channel": "C12345"},  # no ts
        )
        result = verifier(claim)
        assert result.verified is True
        fake_backend.get_permalink.assert_not_called()


def _gh_cmd_failed(stderr: str) -> Exception:
    """Build a CommandFailedError that mimics ``gh api`` on an HTTP error."""
    from teatree.utils.run import CommandFailedError  # noqa: PLC0415

    return CommandFailedError(["gh", "api", "x"], 1, "", stderr)


class GitHubNoteVerifierTests(TestCase):
    """``_github_note_verifier`` GETs the comment by id and asserts body hash (#1198).

    Mirrors :class:`GitLabNoteVerifierTests` — proves each branch of the
    factory's error doctrine so the resilience contract is anti-vacuous.

    Every test stubs :func:`_resolve_github_token` to "tok-test" so the
    factory does not block on the codex-found credential check.
    """

    _RESOLVE_PATH = "teatree.loop.scanners.outbound_audit._resolve_github_token"

    def test_returns_none_when_github_module_unimportable(self) -> None:
        import sys  # noqa: PLC0415

        saved = sys.modules.get("teatree.backends.github")
        sys.modules["teatree.backends.github"] = None  # type: ignore[assignment]
        try:
            assert _github_note_verifier() is None
        finally:
            if saved is not None:
                sys.modules["teatree.backends.github"] = saved
            else:
                sys.modules.pop("teatree.backends.github", None)

    def test_returns_none_when_no_github_token_resolves(self) -> None:
        """Codex-found gap: with no token a private-repo 404 is auth, not drift."""
        with patch(self._RESOLVE_PATH, return_value=""):
            assert _github_note_verifier() is None

    def test_404_returns_drift(self) -> None:
        with (
            patch(self._RESOLVE_PATH, return_value="tok-test"),
            patch(
                "teatree.backends.github.client._gh_api_get",
                side_effect=_gh_cmd_failed("HTTP 404: Not Found"),
            ),
        ):
            verifier = _github_note_verifier()
            assert verifier is not None
            claim = OutboundClaim(
                kind=OutboundClaim.Kind.GITHUB_NOTE,
                idempotency_key="github_note:org/repo#5:42",
                extra={"repo": "org/repo", "artifact_id": "42", "payload_digest": _hash_body("lgtm")},
            )
            result = verifier(claim)
        assert result.verified is False
        assert "42" in result.drift_reason
        assert "not found" in result.drift_reason

    def test_500_re_raises_so_scan_skips(self) -> None:
        with (
            patch(self._RESOLVE_PATH, return_value="tok-test"),
            patch(
                "teatree.backends.github.client._gh_api_get",
                side_effect=_gh_cmd_failed("HTTP 500: Internal Server Error"),
            ),
        ):
            verifier = _github_note_verifier()
            assert verifier is not None
            claim = OutboundClaim(
                kind=OutboundClaim.Kind.GITHUB_NOTE,
                idempotency_key="github_note:org/repo#5:42",
                extra={"repo": "org/repo", "artifact_id": "42", "payload_digest": _hash_body("lgtm")},
            )
            with pytest.raises(Exception, match="HTTP 500"):
                verifier(claim)

    def test_500_in_scanner_is_caught_and_claim_not_flagged(self) -> None:
        """End-to-end: a 500 on GET must NOT flip the claim to drift."""
        claim = _aged_claim(
            kind=OutboundClaim.Kind.GITHUB_NOTE,
            key="github-500-skip",
            repo="org/repo",
            artifact_id="42",
            payload_digest=_hash_body("lgtm"),
        )
        notify = MagicMock()
        with (
            patch(self._RESOLVE_PATH, return_value="tok-test"),
            patch(
                "teatree.backends.github.client._gh_api_get",
                side_effect=_gh_cmd_failed("HTTP 500: Internal Server Error"),
            ),
        ):
            verifier = _github_note_verifier()
            assert verifier is not None
            scanner = OutboundAuditScanner(verifiers={"github_note": verifier}, notifier=notify)
            signals = scanner.scan()
        claim.refresh_from_db()
        assert signals == []
        assert claim.drift_detected is False
        assert claim.drift_alerted_at is None
        notify.assert_not_called()

    def test_body_digest_mismatch_returns_drift(self) -> None:
        with (
            patch(self._RESOLVE_PATH, return_value="tok-test"),
            patch(
                "teatree.backends.github.client._gh_api_get",
                return_value={"id": 42, "body": "tampered"},
            ),
        ):
            verifier = _github_note_verifier()
            assert verifier is not None
            claim = OutboundClaim(
                kind=OutboundClaim.Kind.GITHUB_NOTE,
                idempotency_key="github_note:org/repo#5:42",
                extra={
                    "repo": "org/repo",
                    "artifact_id": "42",
                    "payload_digest": _hash_body("the original body"),
                },
            )
            result = verifier(claim)
        assert result.verified is False
        assert "digest mismatch" in result.drift_reason

    def test_body_digest_match_returns_ok(self) -> None:
        with (
            patch(self._RESOLVE_PATH, return_value="tok-test"),
            patch(
                "teatree.backends.github.client._gh_api_get",
                return_value={"id": 42, "body": "lgtm"},
            ),
        ):
            verifier = _github_note_verifier()
            assert verifier is not None
            claim = OutboundClaim(
                kind=OutboundClaim.Kind.GITHUB_NOTE,
                idempotency_key="github_note:org/repo#5:42",
                extra={
                    "repo": "org/repo",
                    "artifact_id": "42",
                    "payload_digest": _hash_body("lgtm"),
                },
            )
            result = verifier(claim)
        assert result.verified is True

    def test_verifier_forwards_resolved_token_to_gh_api_get(self) -> None:
        """Codex-found gap: token must reach _gh_api_get or private 404s look like drift."""
        captured: dict[str, str] = {}

        def _fake_get(endpoint: str, *, token: str = "") -> dict[str, object]:
            captured["endpoint"] = endpoint
            captured["token"] = token
            return {"id": 42, "body": "lgtm"}

        with (
            patch(self._RESOLVE_PATH, return_value="pat_abc"),
            patch("teatree.backends.github.client._gh_api_get", side_effect=_fake_get),
        ):
            verifier = _github_note_verifier()
            assert verifier is not None
            claim = OutboundClaim(
                kind=OutboundClaim.Kind.GITHUB_NOTE,
                idempotency_key="github_note:org/repo#5:42",
                extra={"repo": "org/repo", "artifact_id": "42", "payload_digest": _hash_body("lgtm")},
            )
            verifier(claim)
        assert captured["token"] == "pat_abc"
        assert captured["endpoint"] == "repos/org/repo/issues/comments/42"

    def test_returns_ok_when_extra_missing_required_fields(self) -> None:
        with (
            patch(self._RESOLVE_PATH, return_value="tok-test"),
            patch("teatree.backends.github.client._gh_api_get") as mock_get,
        ):
            verifier = _github_note_verifier()
            assert verifier is not None
            claim = OutboundClaim(
                kind=OutboundClaim.Kind.GITHUB_NOTE,
                idempotency_key="github-incomplete",
                extra={"repo": "org/repo"},  # missing artifact_id
            )
            result = verifier(claim)
        assert result.verified is True
        mock_get.assert_not_called()

    def test_returns_ok_when_artifact_id_is_non_numeric(self) -> None:
        with (
            patch(self._RESOLVE_PATH, return_value="tok-test"),
            patch("teatree.backends.github.client._gh_api_get") as mock_get,
        ):
            verifier = _github_note_verifier()
            assert verifier is not None
            claim = OutboundClaim(
                kind=OutboundClaim.Kind.GITHUB_NOTE,
                idempotency_key="github-non-numeric",
                extra={"repo": "org/repo", "artifact_id": "abc"},
            )
            assert verifier(claim).verified is True
        mock_get.assert_not_called()

    def test_non_dict_payload_returns_drift(self) -> None:
        with (
            patch(self._RESOLVE_PATH, return_value="tok-test"),
            patch("teatree.backends.github.client._gh_api_get", return_value=[]),
        ):
            verifier = _github_note_verifier()
            assert verifier is not None
            claim = OutboundClaim(
                kind=OutboundClaim.Kind.GITHUB_NOTE,
                idempotency_key="github-bad-payload",
                extra={"repo": "org/repo", "artifact_id": "42"},
            )
            result = verifier(claim)
        assert result.verified is False
        assert "non-dict" in result.drift_reason

    def test_empty_payload_digest_skips_body_check(self) -> None:
        """Claims with no digest (legacy / extra-stripped) still verify on existence."""
        with (
            patch(self._RESOLVE_PATH, return_value="tok-test"),
            patch(
                "teatree.backends.github.client._gh_api_get",
                return_value={"id": 42, "body": "anything"},
            ),
        ):
            verifier = _github_note_verifier()
            assert verifier is not None
            claim = OutboundClaim(
                kind=OutboundClaim.Kind.GITHUB_NOTE,
                idempotency_key="github-no-digest",
                extra={"repo": "org/repo", "artifact_id": "42"},
            )
            result = verifier(claim)
        assert result.verified is True


class ResolveGithubTokenTests(TestCase):
    """:func:`_resolve_github_token` reads env → pass in order, defaulting to ''."""

    def test_returns_gh_token_env_when_set(self) -> None:
        from teatree.loop.scanners.outbound_audit import _resolve_github_token  # noqa: PLC0415

        with patch.dict("os.environ", {"GH_TOKEN": "from-env"}, clear=False):
            assert _resolve_github_token() == "from-env"

    def test_falls_back_to_github_token_env(self) -> None:
        from teatree.loop.scanners.outbound_audit import _resolve_github_token  # noqa: PLC0415

        env = {"GITHUB_TOKEN": "alt-env"}
        # ensure GH_TOKEN absent
        with patch.dict("os.environ", env, clear=True):
            assert _resolve_github_token() == "alt-env"

    def test_falls_back_to_pass_when_env_empty(self) -> None:
        from teatree.loop.scanners.outbound_audit import _resolve_github_token  # noqa: PLC0415

        with (
            patch.dict("os.environ", {}, clear=True),
            patch("teatree.utils.secrets.read_pass", return_value="from-pass"),
        ):
            assert _resolve_github_token() == "from-pass"

    def test_returns_empty_when_pass_lookup_raises(self) -> None:
        from teatree.loop.scanners.outbound_audit import _resolve_github_token  # noqa: PLC0415

        with (
            patch.dict("os.environ", {}, clear=True),
            patch("teatree.utils.secrets.read_pass", side_effect=RuntimeError("no pass")),
        ):
            assert _resolve_github_token() == ""


class GitHubNoteSettlingWindowTests(TestCase):
    """``kind_settling_seconds`` contains ``github_note`` per #1198."""

    def test_github_note_settling_window_present(self) -> None:
        assert kind_settling_seconds["github_note"] == 30
