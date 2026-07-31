"""Tests for :class:`CodexReviewMarker` — the codex/self-PR review dispatch claim (#1254, #3921).

The marker was the fourth claim on the review dispatch path and the last one still
shaped as the defect #3920 fixed on its siblings: ``get_or_create`` with no terminal
state, no deadline and no reaper. These tests pin both halves — the dedup that must
survive, and the expiry re-arm that closes the permanent block.
"""

import datetime as dt

import pytest
from django.utils import timezone

from teatree.core.models import CodexReviewMarker, ReviewVerdict
from teatree.core.models.auto_review_dispatch import MAX_DISPATCH_ATTEMPTS

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

SLUG = "souliane/teatree"
PR_ID = 1254
HEAD = "feedfacecafebabe1234567890abcdef12345678"


def _expire(row: CodexReviewMarker) -> None:
    """Push the claim's deadline into the past — the dispatched review never came back."""
    CodexReviewMarker.objects.filter(pk=row.pk).update(deadline=timezone.now() - dt.timedelta(minutes=1))


class TestClaimDedupSurvives:
    """The pre-#3921 contract, unchanged: one dispatch per head while the claim is live."""

    def test_a_fresh_head_claims(self) -> None:
        row = CodexReviewMarker.claim(slug=SLUG, pr_id=PR_ID, head_sha=HEAD, overlay="teatree", variant="codex:review")

        assert row is not None
        assert row.overlay == "teatree"
        assert row.variant == "codex:review"

    def test_a_second_claim_on_a_live_row_is_refused(self) -> None:
        assert CodexReviewMarker.claim(slug=SLUG, pr_id=PR_ID, head_sha=HEAD) is not None

        assert CodexReviewMarker.claim(slug=SLUG, pr_id=PR_ID, head_sha=HEAD) is None
        assert CodexReviewMarker.objects.count() == 1

    def test_a_new_head_claims_a_fresh_row(self) -> None:
        CodexReviewMarker.claim(slug=SLUG, pr_id=PR_ID, head_sha=HEAD)

        assert CodexReviewMarker.claim(slug=SLUG, pr_id=PR_ID, head_sha="0" * 40) is not None

    def test_blank_identifiers_never_claim(self) -> None:
        assert CodexReviewMarker.claim(slug="", pr_id=PR_ID, head_sha=HEAD) is None
        assert CodexReviewMarker.claim(slug=SLUG, pr_id=0, head_sha=HEAD) is None
        assert CodexReviewMarker.claim(slug=SLUG, pr_id=PR_ID, head_sha="") is None
        assert CodexReviewMarker.objects.count() == 0

    def test_str_renders_slug_pr_and_short_head(self) -> None:
        row = CodexReviewMarker.claim(slug=SLUG, pr_id=PR_ID, head_sha=HEAD)

        assert row is not None
        assert str(row) == f"codex-review-marker<{row.pk}:{SLUG}#{PR_ID}@{HEAD[:8]}>"


class TestExpiredClaimIsReArmable:
    """#3921: a dispatched review that died must not block its head forever.

    Pre-fix the row had no ``deadline`` at all, so every test here is RED on the
    unbounded ``get_or_create``: the second claim returned ``None`` regardless of
    how long the first had been stranded.
    """

    def test_a_live_claim_is_not_re_armable(self) -> None:
        row = CodexReviewMarker.claim(slug=SLUG, pr_id=PR_ID, head_sha=HEAD)
        assert row is not None
        assert row.deadline is not None
        assert row.deadline > timezone.now()

        assert CodexReviewMarker.claim(slug=SLUG, pr_id=PR_ID, head_sha=HEAD) is None

    def test_an_expired_claim_re_arms(self) -> None:
        first = CodexReviewMarker.claim(slug=SLUG, pr_id=PR_ID, head_sha=HEAD)
        assert first is not None
        _expire(first)

        again = CodexReviewMarker.claim(slug=SLUG, pr_id=PR_ID, head_sha=HEAD)

        assert again is not None, "an expired codex claim whose review died must be re-armable"
        assert again.pk == first.pk, "the re-arm reclaims the row, it never inserts a duplicate"

    def test_re_arming_increments_attempts(self) -> None:
        first = CodexReviewMarker.claim(slug=SLUG, pr_id=PR_ID, head_sha=HEAD)
        assert first is not None
        assert first.attempts == 1
        _expire(first)

        again = CodexReviewMarker.claim(slug=SLUG, pr_id=PR_ID, head_sha=HEAD)

        assert again is not None
        assert again.attempts == 2

    def test_the_re_armed_claim_gets_a_fresh_deadline(self) -> None:
        first = CodexReviewMarker.claim(slug=SLUG, pr_id=PR_ID, head_sha=HEAD)
        assert first is not None
        _expire(first)

        again = CodexReviewMarker.claim(slug=SLUG, pr_id=PR_ID, head_sha=HEAD)

        assert again is not None
        assert again.deadline is not None
        assert again.deadline > timezone.now()

    def test_a_null_deadline_is_never_stolen_by_expiry(self) -> None:
        """Legacy rows carry no deadline and keep the pre-#3921 permanent block."""
        row = CodexReviewMarker.claim(slug=SLUG, pr_id=PR_ID, head_sha=HEAD)
        assert row is not None
        CodexReviewMarker.objects.filter(pk=row.pk).update(deadline=None)

        assert CodexReviewMarker.claim(slug=SLUG, pr_id=PR_ID, head_sha=HEAD) is None


class TestResolvedIsTerminal:
    def test_a_resolved_claim_is_never_re_armed(self) -> None:
        row = CodexReviewMarker.claim(slug=SLUG, pr_id=PR_ID, head_sha=HEAD)
        assert row is not None
        assert CodexReviewMarker.mark_resolved(slug=SLUG, pr_id=PR_ID, head_sha=HEAD) is True
        _expire(row)

        assert CodexReviewMarker.claim(slug=SLUG, pr_id=PR_ID, head_sha=HEAD) is None, (
            "a verdict covers this exact tree — re-arming it would be review churn"
        )

    def test_mark_resolved_normalizes_the_head(self) -> None:
        CodexReviewMarker.claim(slug=SLUG, pr_id=PR_ID, head_sha=HEAD)

        assert CodexReviewMarker.mark_resolved(slug=f" {SLUG} ", pr_id=PR_ID, head_sha=HEAD.upper()) is True

    def test_resolving_an_unclaimed_head_is_a_no_op(self) -> None:
        assert CodexReviewMarker.mark_resolved(slug=SLUG, pr_id=PR_ID, head_sha=HEAD) is False

    def test_recording_a_verdict_resolves_the_marker(self) -> None:
        row = CodexReviewMarker.claim(slug=SLUG, pr_id=PR_ID, head_sha=HEAD)
        assert row is not None

        ReviewVerdict.record(
            slug=SLUG,
            pr_id=PR_ID,
            reviewed_sha=HEAD,
            verdict=ReviewVerdict.Verdict.MERGE_SAFE,
            reviewer_identity="codex",
        )

        row.refresh_from_db()
        assert row.state == CodexReviewMarker.State.RESOLVED
        assert row.resolved_at is not None


class TestSaturation:
    def test_an_expired_claim_with_budget_left_is_not_saturated(self) -> None:
        row = CodexReviewMarker.claim(slug=SLUG, pr_id=PR_ID, head_sha=HEAD)
        assert row is not None
        _expire(row)

        assert CodexReviewMarker.saturated().count() == 0

    def test_a_claim_that_spent_its_budget_is_refused_and_reported(self) -> None:
        row = CodexReviewMarker.claim(slug=SLUG, pr_id=PR_ID, head_sha=HEAD)
        assert row is not None
        for _ in range(MAX_DISPATCH_ATTEMPTS - 1):
            _expire(row)
            row = CodexReviewMarker.claim(slug=SLUG, pr_id=PR_ID, head_sha=HEAD)
            assert row is not None
        assert row.attempts == MAX_DISPATCH_ATTEMPTS

        _expire(row)

        assert CodexReviewMarker.claim(slug=SLUG, pr_id=PR_ID, head_sha=HEAD) is None
        assert CodexReviewMarker.saturated().count() == 1

    def test_the_last_attempt_is_not_saturated_while_its_deadline_is_live(self) -> None:
        row = CodexReviewMarker.claim(slug=SLUG, pr_id=PR_ID, head_sha=HEAD)
        assert row is not None
        for _ in range(MAX_DISPATCH_ATTEMPTS - 1):
            _expire(row)
            row = CodexReviewMarker.claim(slug=SLUG, pr_id=PR_ID, head_sha=HEAD)
            assert row is not None

        assert row.attempts == MAX_DISPATCH_ATTEMPTS
        assert CodexReviewMarker.saturated().count() == 0
