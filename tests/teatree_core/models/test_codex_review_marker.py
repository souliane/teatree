"""Tests for :class:`CodexReviewMarker` — the codex/self-PR review dispatch claim (#1254, #3921).

The marker was the fourth claim on the review dispatch path and the last one still
shaped as the defect #3920 fixed on its siblings: ``get_or_create`` with no terminal
state, no deadline and no reaper. These tests pin both halves — the dedup that must
survive, and the expiry re-arm that closes the permanent block.
"""

import datetime as dt

import pytest
from django.utils import timezone

from teatree.core.models import CodexReviewMarker, MRReviewLock, ReviewVerdict
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


NEW_HEAD = "0badc0de1234567890abcdef1234567890abcdef"


class TestARefusedHeadIsTerminalButThePrIsNot:
    """#4530: the latch #4522 gave the #68 ledger belongs on THIS claim too.

    A ``merge_safe`` verdict over required checks the reviewer itself reports RED cannot
    be recorded, so the head keeps no verdict, so :meth:`CodexReviewMarker.claim` re-arms
    it the moment the deadline lapses and burns another whole review session on the same
    refusal — bounded by :data:`MAX_DISPATCH_ATTEMPTS`, but bounded at THREE sessions, and
    this is the path most of the measured refusals arrived on. The latch makes it one.
    """

    def test_a_refused_head_is_never_re_armed_even_past_its_deadline(self) -> None:
        row = CodexReviewMarker.claim(slug=SLUG, pr_id=PR_ID, head_sha=HEAD)
        assert row is not None
        assert CodexReviewMarker.mark_refused(slug=SLUG, pr_id=PR_ID, head_sha=HEAD) is True
        row.refresh_from_db()
        assert row.state == CodexReviewMarker.State.REFUSED

        _expire(row)

        assert CodexReviewMarker.claim(slug=SLUG, pr_id=PR_ID, head_sha=HEAD) is None
        row.refresh_from_db()
        assert row.attempts == 1, "a latched head must not spend another attempt"

    def test_a_live_claim_at_another_head_survives_the_refusal(self) -> None:
        # Two live claims, one PR — the shape a push mid-review produces on this path,
        # which takes no per-MR lock. A latch keyed on the PR would take both.
        CodexReviewMarker.claim(slug=SLUG, pr_id=PR_ID, head_sha=HEAD)
        pushed = CodexReviewMarker.claim(slug=SLUG, pr_id=PR_ID, head_sha=NEW_HEAD)
        assert pushed is not None

        assert CodexReviewMarker.mark_refused(slug=SLUG, pr_id=PR_ID, head_sha=HEAD) is True

        pushed.refresh_from_db()
        assert pushed.state == CodexReviewMarker.State.DISPATCHED, (
            "a refusal at one head latched a DIFFERENT head's live claim on the same PR"
        )

    def test_a_new_head_on_a_refused_pr_arms_normally(self) -> None:
        # The safety valve. The latch binds ONE tree, never the pull request: a push must
        # always earn a fresh review, or the fix silently suppresses legitimate ones.
        CodexReviewMarker.claim(slug=SLUG, pr_id=PR_ID, head_sha=HEAD)
        CodexReviewMarker.mark_refused(slug=SLUG, pr_id=PR_ID, head_sha=HEAD)

        rearmed = CodexReviewMarker.claim(slug=SLUG, pr_id=PR_ID, head_sha=NEW_HEAD, variant="claude:review")

        assert rearmed is not None
        assert rearmed.state == CodexReviewMarker.State.DISPATCHED
        assert CodexReviewMarker.objects.filter(slug=SLUG, pr_id=PR_ID).count() == 2

    def test_the_unique_key_is_per_head_so_a_new_head_is_a_different_row(self) -> None:
        # Read off the schema rather than inferred: nothing keyed on (slug, pr_id) alone
        # can be latched by a refusal at one head.
        key = next(
            constraint
            for constraint in CodexReviewMarker._meta.constraints
            if constraint.name == "uniq_codexreviewmarker_slug_pr_sha"
        )
        assert list(key.fields) == ["slug", "pr_id", "head_sha"]

    def test_a_refused_head_is_not_reported_as_saturated(self) -> None:
        # Saturation means "the retry budget ran out"; this head has budget left and is
        # unarmable for a different reason, so reporting it there would misname the cause.
        row = CodexReviewMarker.claim(slug=SLUG, pr_id=PR_ID, head_sha=HEAD)
        assert row is not None
        CodexReviewMarker.mark_refused(slug=SLUG, pr_id=PR_ID, head_sha=HEAD)
        _expire(row)

        assert CodexReviewMarker.saturated().count() == 0

    def test_mark_refused_normalizes_the_head(self) -> None:
        CodexReviewMarker.claim(slug=SLUG, pr_id=PR_ID, head_sha=HEAD)

        assert CodexReviewMarker.mark_refused(slug=f" {SLUG} ", pr_id=PR_ID, head_sha=HEAD.upper()) is True

    def test_refusing_an_unclaimed_head_is_a_no_op(self) -> None:
        assert CodexReviewMarker.mark_refused(slug=SLUG, pr_id=PR_ID, head_sha=HEAD) is False

    def test_a_resolved_claim_is_not_downgraded_to_refused(self) -> None:
        # A verdict already covers this tree; a later refusal must not erase that record.
        row = CodexReviewMarker.claim(slug=SLUG, pr_id=PR_ID, head_sha=HEAD)
        assert row is not None
        CodexReviewMarker.mark_resolved(slug=SLUG, pr_id=PR_ID, head_sha=HEAD)

        assert CodexReviewMarker.mark_refused(slug=SLUG, pr_id=PR_ID, head_sha=HEAD) is False
        row.refresh_from_db()
        assert row.state == CodexReviewMarker.State.RESOLVED

    def test_refusing_frees_no_per_mr_lock_this_path_never_took(self) -> None:
        """The one place the two terminals deliberately DIFFER (see the model docstring).

        This path takes no :class:`MRReviewLock`, so any lock held for the PR belongs to
        somebody else — a manual ``review lock-acquire`` here. A refusal records nothing a
        merge is waiting on, so releasing that lock would remove a live reviewer's guard
        and give nothing back.
        """
        MRReviewLock.acquire(slug=SLUG, pr_id=PR_ID, holder="manual-reviewer")
        CodexReviewMarker.claim(slug=SLUG, pr_id=PR_ID, head_sha=HEAD)

        assert CodexReviewMarker.mark_refused(slug=SLUG, pr_id=PR_ID, head_sha=HEAD) is True

        assert MRReviewLock.objects.get(slug=SLUG, pr_id=PR_ID).state == MRReviewLock.State.REVIEW_DISPATCHED
