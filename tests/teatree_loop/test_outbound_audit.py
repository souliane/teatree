"""Behaviour tests for :class:`OutboundAuditScanner` (#1019).

The scanner is purely a verifier — it does not call third-party APIs
itself, it dispatches each claim to a per-kind ``Verifier`` callable.
Tests construct the scanner with explicit ``verifiers`` so the
production-default lazy loaders never run, and with an explicit
``notifier`` mock so the production lazy-imported ``notify_user``
never executes either.
"""

import datetime as dt
from unittest.mock import MagicMock

from django.test import TestCase
from django.utils import timezone

from teatree.core.models import BotPing, OutboundClaim
from teatree.loop.scanners.outbound_audit import OutboundAuditScanner, VerifyResult, kind_settling_seconds


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

    def test_unknown_kind_without_verifier_is_skipped(self) -> None:
        _aged_claim(kind=OutboundClaim.Kind.NOTION_EDIT, key="unknown-kind")

        # No verifier configured for notion_edit and no production default
        # registered — scanner skips silently rather than crashing.
        scanner = OutboundAuditScanner(verifiers={})
        signals = scanner.scan()
        assert signals == []

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

    def test_default_notifier_calls_notify_user_lazily(self) -> None:
        """Production default forwards to teatree.notify.notify_user."""
        from unittest.mock import patch as _patch  # noqa: PLC0415

        from teatree.loop.scanners.outbound_audit import _default_notifier  # noqa: PLC0415

        with _patch("teatree.notify.notify_user") as notify_mock:
            _default_notifier("drift alert text", "outbound_drift:key-1")

        notify_mock.assert_called_once()
        kwargs = notify_mock.call_args.kwargs
        assert kwargs["idempotency_key"] == "outbound_drift:key-1"

    def test_notifier_exception_is_swallowed_and_does_not_break_tick(self) -> None:
        """A failing notifier never breaks the tick — drift row still flips."""
        claim = _aged_claim(kind=OutboundClaim.Kind.GITLAB_NOTE, key="notify-boom")

        def _boom(_text: str, _key: str) -> None:
            msg = "slack 500"
            raise RuntimeError(msg)

        scanner = OutboundAuditScanner(
            verifiers={"gitlab_note": lambda _c: VerifyResult.drift("not found")},
            notifier=_boom,
        )
        signals = scanner.scan()

        claim.refresh_from_db()
        assert claim.drift_detected is True
        assert claim.drift_alerted_at is not None
        assert len(signals) == 1

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
