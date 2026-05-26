"""Tests for the ``OutboundClaim`` model and ``record_claim`` helper (#1019)."""

from unittest.mock import patch

from django.db import DatabaseError, IntegrityError
from django.test import TestCase

from teatree.core.models import OutboundClaim
from teatree.outbound_claim import record_claim


class RecordClaimTests(TestCase):
    def test_records_a_new_claim_with_kind_url_and_idempotency_key(self) -> None:
        claim = record_claim(
            kind=OutboundClaim.Kind.GITLAB_NOTE,
            idempotency_key="gitlab_note:repo!1:42",
            target_url="https://gitlab.example.com/repo/-/merge_requests/1",
            extra={"repo": "repo", "mr": 1},
        )

        assert claim is not None
        row = OutboundClaim.objects.get(idempotency_key="gitlab_note:repo!1:42")
        assert row.kind == OutboundClaim.Kind.GITLAB_NOTE
        assert row.target_url.endswith("/merge_requests/1")
        # ``record_claim`` stamps the active overlay name on extra (#1275).
        # No env override here -> empty string (default overlay).
        assert row.extra == {"repo": "repo", "mr": 1, "overlay": ""}
        assert row.verified_at is None
        assert row.drift_detected is False
        assert row.drift_alerted_at is None

    def test_duplicate_idempotency_key_returns_existing_row_no_create(self) -> None:
        first = record_claim(
            kind=OutboundClaim.Kind.SLACK_DM,
            idempotency_key="slack_dm:dup",
        )
        second = record_claim(
            kind=OutboundClaim.Kind.SLACK_DM,
            idempotency_key="slack_dm:dup",
        )
        assert first is not None
        assert second is not None
        assert first.pk == second.pk
        assert OutboundClaim.objects.filter(idempotency_key="slack_dm:dup").count() == 1

    def test_database_error_returns_none_and_does_not_raise(self) -> None:
        with patch.object(
            OutboundClaim.objects,
            "get_or_create",
            side_effect=DatabaseError("boom"),
        ):
            claim = record_claim(
                kind=OutboundClaim.Kind.GITLAB_NOTE,
                idempotency_key="key-fail",
            )
        assert claim is None

    def test_kind_accepts_string_alias(self) -> None:
        claim = record_claim(kind="gitlab_approve", idempotency_key="gitlab_approve:repo!2:approve")
        assert claim is not None
        assert claim.kind == OutboundClaim.Kind.GITLAB_APPROVE

    def test_str_method_reflects_status(self) -> None:
        from django.utils import timezone  # noqa: PLC0415

        claim = record_claim(kind=OutboundClaim.Kind.SLACK_DM, idempotency_key="strtest")
        assert claim is not None
        assert "pending" in str(claim)
        claim.verified_at = timezone.now()
        claim.save(update_fields=["verified_at"])
        assert "verified" in str(claim)
        claim.verified_at = None
        claim.drift_detected = True
        claim.save(update_fields=["verified_at", "drift_detected"])
        assert "drift" in str(claim)

    def test_resolves_agent_session_id_from_env(self) -> None:
        with patch.dict("os.environ", {"CLAUDE_SESSION_ID": "sess-123"}, clear=False):
            claim = record_claim(
                kind=OutboundClaim.Kind.SLACK_DM,
                idempotency_key="sess-test",
            )
        assert claim is not None
        assert claim.agent_session_id == "sess-123"

    def test_integrity_error_refetches_existing_row(self) -> None:
        # First create an existing row so the refetch can succeed.
        existing = record_claim(
            kind=OutboundClaim.Kind.SLACK_DM,
            idempotency_key="ie-refetch",
        )
        assert existing is not None
        # Simulate a race: get_or_create raises IntegrityError; the helper
        # must catch it and refetch the existing row by idempotency_key.
        with patch.object(
            OutboundClaim.objects,
            "get_or_create",
            side_effect=IntegrityError("uniq race"),
        ):
            claim = record_claim(
                kind=OutboundClaim.Kind.SLACK_DM,
                idempotency_key="ie-refetch",
            )
        assert claim is not None
        assert claim.pk == existing.pk

    def test_integrity_error_with_failing_refetch_returns_none(self) -> None:
        # Both the initial write AND the refetch fail -> caller gets None.
        with (
            patch.object(
                OutboundClaim.objects,
                "get_or_create",
                side_effect=IntegrityError("uniq race"),
            ),
            patch.object(
                OutboundClaim.objects,
                "filter",
                side_effect=RuntimeError("DB down during refetch"),
            ),
        ):
            claim = record_claim(
                kind=OutboundClaim.Kind.SLACK_DM,
                idempotency_key="ie-refetch-fail",
            )
        assert claim is None

    def test_unexpected_exception_returns_none(self) -> None:
        # A non-DatabaseError, non-IntegrityError exception is the last
        # safety net — record_claim must never break the publish path.
        with patch.object(
            OutboundClaim.objects,
            "get_or_create",
            side_effect=RuntimeError("totally unexpected"),
        ):
            claim = record_claim(
                kind=OutboundClaim.Kind.GITLAB_NOTE,
                idempotency_key="unexpected",
            )
        assert claim is None
