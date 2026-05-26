"""Behaviour tests for overlay-bound outbound-audit verifiers (#1275).

The outbound-audit scanner verifies each :class:`OutboundClaim` by
contacting the third-party system that originally received the post.
Multi-overlay setups configure different credentials per overlay
(`github_token_ref = "github/work-token"` on one overlay,
`github_token_ref = "github/personal"` on another); a verifier built
with a process-global resolver lands on the wrong identity for at least
one of them, producing a false 404 → false drift DM (failure mode #1)
or no token at all → silent claim-skipping with `kind=slack_dm —
skipping claim N` debug noise (failure mode #3).

These tests pin the new contract:

- Every record helper stamps ``overlay`` on ``extra`` at claim-time.
- The scanner uses ``claim.extra["overlay"]`` to build a verifier
    scoped to that overlay's credentials.
- A claim whose overlay can't resolve credentials surfaces as an
    ``outbound.audit_skipped`` ScanSignal — observable, never drift.
"""

import datetime as dt
import os
from unittest.mock import MagicMock, patch

from django.test import TestCase
from django.utils import timezone

from teatree.core.models import OutboundClaim
from teatree.loop.scanners.outbound_audit import OutboundAuditScanner, kind_settling_seconds


def _aged_claim(*, kind: OutboundClaim.Kind, key: str, **extra: object) -> OutboundClaim:
    age = max(kind_settling_seconds.values(), default=30) + 30
    return OutboundClaim.objects.create(
        kind=kind,
        idempotency_key=key,
        target_url="https://example.com/artifact",
        claim_ts=timezone.now() - dt.timedelta(seconds=age),
        extra=extra or {},
    )


class OverlayBoundVerifierDispatchTests(TestCase):
    """Scanner routes each claim through its recorded-overlay's verifier."""

    def test_slack_dm_verifier_uses_recorded_overlay_messaging_backend(self) -> None:
        """A claim recorded under overlay 'work' verifies through that overlay's backend.

        ``messaging_from_overlay(overlay_name='work')`` resolves the
        backend; the default overlay's backend is never consulted.
        """
        _aged_claim(
            kind=OutboundClaim.Kind.SLACK_DM,
            key="overlay-work:slack:1",
            overlay="work",
            channel="C_WORK",
            ts="1700000000.0001",
        )

        work_backend = MagicMock()
        work_backend.get_permalink.return_value = "https://slack.example/archives/C_WORK/p1"
        default_backend = MagicMock()

        def _factory(overlay_name: str | None = None) -> object:
            if overlay_name == "work":
                return work_backend
            return default_backend

        with patch(
            "teatree.core.backend_factory.messaging_from_overlay",
            side_effect=_factory,
        ):
            scanner = OutboundAuditScanner()
            signals = scanner.scan()

        work_backend.get_permalink.assert_called_once_with(channel="C_WORK", ts="1700000000.0001")
        default_backend.get_permalink.assert_not_called()
        assert signals == []

    def test_github_note_verifier_uses_overlay_specific_token(self) -> None:
        """Two GitHub overlays send their own token to ``_gh_api_get`` per claim.

        Each overlay has its own ``github_token_ref``; the verifier
        resolves the right token for each claim independently.
        """
        _aged_claim(
            kind=OutboundClaim.Kind.GITHUB_NOTE,
            key="github_note:org/work#5:42",
            overlay="work",
            repo="org/work",
            artifact_id="42",
            token_ref="github/work-token",
        )
        _aged_claim(
            kind=OutboundClaim.Kind.GITHUB_NOTE,
            key="github_note:org/personal#7:99",
            overlay="personal",
            repo="org/personal",
            artifact_id="99",
            token_ref="github/personal",
        )

        seen_tokens: list[str] = []

        def _resolve(overlay_name: str) -> str:
            return f"tok-for-{overlay_name}"

        def _gh_get(_endpoint: str, *, token: str = "") -> object:
            seen_tokens.append(token)
            return {"id": 42, "body": ""}

        with (
            patch(
                "teatree.loop.scanners.outbound_audit._resolve_github_token_for_overlay",
                side_effect=_resolve,
            ),
            patch("teatree.backends.github._gh_api_get", side_effect=_gh_get),
        ):
            scanner = OutboundAuditScanner()
            scanner.scan()

        assert "tok-for-work" in seen_tokens
        assert "tok-for-personal" in seen_tokens

    def test_gitlab_note_verifier_uses_recorded_overlay_credentials(self) -> None:
        """A GitLab note claim built under overlay 'client-A' verifies through that overlay.

        The verifier calls ``_gitlab_api_for_overlay('client-A')`` to
        build the GitLabAPI instance — client-A's token and URL apply.
        """
        _aged_claim(
            kind=OutboundClaim.Kind.GITLAB_NOTE,
            key="gitlab_note:org/proj!1:7",
            overlay="client-A",
            repo="org/proj",
            mr=1,
            artifact_id="7",
            endpoint="notes",
        )

        seen_overlays: list[str] = []
        fake_api = MagicMock()
        fake_api.get_json.return_value = {"id": 7, "body": "x"}

        def _build_api(overlay_name: str) -> object:
            seen_overlays.append(overlay_name)
            return fake_api

        with patch(
            "teatree.loop.scanners.outbound_audit._gitlab_api_for_overlay",
            side_effect=_build_api,
        ):
            scanner = OutboundAuditScanner()
            scanner.scan()

        assert seen_overlays == ["client-A"]
        fake_api.get_json.assert_called_once()


class NoVerifierForKindObservabilityTests(TestCase):
    """Unresolvable overlay credentials emit ``outbound.audit_skipped``.

    Never silent skipping (the legacy log-debug behaviour) and never
    classified as drift — the credential gap is its own observable
    backlog signal.
    """

    def test_unresolvable_overlay_credentials_emit_audit_skipped(self) -> None:
        _aged_claim(
            kind=OutboundClaim.Kind.SLACK_DM,
            key="overlay-missing:slack:1",
            overlay="archived-overlay",
            channel="C123",
            ts="1.1",
        )

        notify = MagicMock()
        with patch(
            "teatree.core.backend_factory.messaging_from_overlay",
            return_value=None,
        ):
            scanner = OutboundAuditScanner(notifier=notify)
            signals = scanner.scan()

        # Drift was NOT emitted — that would have spammed a false alert.
        assert all(s.kind != "outbound.drift" for s in signals)
        # Observability: at least one signal of the new kind is emitted.
        kinds = {s.kind for s in signals}
        assert "outbound.audit_skipped" in kinds
        notify.assert_not_called()


class RecordClaimStampsOverlayTests(TestCase):
    """Every record helper writes the active overlay name into ``extra``."""

    def test_record_claim_stamps_active_overlay(self) -> None:
        from teatree.outbound_claim import record_claim  # noqa: PLC0415

        with patch.dict(os.environ, {"T3_OVERLAY_NAME": "work"}, clear=False):
            row = record_claim(
                kind=OutboundClaim.Kind.SLACK_DM,
                idempotency_key="rcs:1",
                extra={"channel": "C1", "ts": "1.1"},
            )

        assert row is not None
        assert row.extra["overlay"] == "work"
        # Pre-existing extra survives.
        assert row.extra["channel"] == "C1"
        assert row.extra["ts"] == "1.1"

    def test_record_claim_with_explicit_overlay_wins_over_env(self) -> None:
        """An explicit overlay in ``extra`` wins over ``T3_OVERLAY_NAME``."""
        from teatree.outbound_claim import record_claim  # noqa: PLC0415

        with patch.dict(os.environ, {"T3_OVERLAY_NAME": "env-overlay"}, clear=False):
            row = record_claim(
                kind=OutboundClaim.Kind.GITLAB_NOTE,
                idempotency_key="rcs:2",
                extra={
                    "repo": "org/proj",
                    "mr": 1,
                    "artifact_id": "1",
                    "overlay": "explicit-overlay",
                },
            )

        assert row is not None
        assert row.extra["overlay"] == "explicit-overlay"

    def test_record_note_claim_stamps_overlay(self) -> None:
        from teatree.cli.review_audit import record_note_claim  # noqa: PLC0415

        with patch.dict(os.environ, {"T3_OVERLAY_NAME": "gitlab-overlay"}, clear=False):
            record_note_claim(
                lambda: "https://gitlab.example/api/v4",
                "org/proj",
                1,
                42,
                endpoint="notes",
            )

        row = OutboundClaim.objects.get(idempotency_key="gitlab_note:org/proj!1:42")
        assert row.extra["overlay"] == "gitlab-overlay"

    def test_record_github_note_claim_stamps_overlay(self) -> None:
        from teatree.backends.github import _record_github_note_claim  # noqa: PLC0415

        with patch.dict(os.environ, {"T3_OVERLAY_NAME": "gh-overlay"}, clear=False):
            _record_github_note_claim(
                repo="org/repo",
                target_number=5,
                comment_id=42,
                body="lgtm",
                target_url="https://github.com/org/repo/issues/5#issuecomment-42",
            )

        row = OutboundClaim.objects.get(idempotency_key="github_note:org/repo#5:42")
        assert row.extra["overlay"] == "gh-overlay"

    def test_notify_user_stamps_overlay_on_recorded_slack_dm_claim(self) -> None:
        """``notify_user`` records a SLACK_DM claim with the active overlay.

        Lets the audit verifier re-read with the same credentials that
        posted the DM, closing the multi-overlay drift gap.
        """
        from teatree.core.notify import _record_outbound_claim  # noqa: PLC0415

        with patch.dict(os.environ, {"T3_OVERLAY_NAME": "slack-overlay"}, clear=False):
            _record_outbound_claim(
                idempotency_key="slack_dm:notify-1",
                target_url="https://slack.example/p/1",
                channel="C123",
                posted_ts="1.1",
            )

        row = OutboundClaim.objects.get(idempotency_key="slack_dm:notify-1")
        assert row.extra["overlay"] == "slack-overlay"
        assert row.extra["channel"] == "C123"
        assert row.extra["ts"] == "1.1"
