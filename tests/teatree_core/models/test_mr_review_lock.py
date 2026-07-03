"""Tests for :class:`MRReviewLock` — atomic per-MR review-dispatch dedup (#1405)."""

import datetime as dt

import pytest
from django.utils import timezone

from teatree.core.models.mr_review_lock import MRReviewLock

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

SLUG = "souliane/teatree"
PR_ID = 1405
URL = f"https://github.com/{SLUG}/pull/{PR_ID}"


class TestAcquireCreatesLock:
    def test_first_acquire_creates_review_dispatched_row(self) -> None:
        row = MRReviewLock.acquire(slug=SLUG, pr_id=PR_ID, holder="agent-a", mr_url=URL)

        assert row is not None
        assert row.state == MRReviewLock.State.REVIEW_DISPATCHED
        assert row.holder == "agent-a"
        assert row.mr_url == URL
        assert row.dispatched_at is not None
        assert row.deadline is not None
        assert row.is_locked()

    def test_blank_slug_or_holder_does_not_acquire(self) -> None:
        assert MRReviewLock.acquire(slug="", pr_id=PR_ID, holder="agent-a") is None
        assert MRReviewLock.acquire(slug=SLUG, pr_id=PR_ID, holder="") is None
        assert MRReviewLock.objects.count() == 0

    def test_str_renders_slug_pr_state_and_holder(self) -> None:
        row = MRReviewLock.acquire(slug=SLUG, pr_id=PR_ID, holder="agent-a")
        assert row is not None
        assert str(row) == f"mr-review-lock<{row.pk}:{SLUG}#{PR_ID} review_dispatched holder='agent-a'>"


class TestConcurrentDispatchDedup:
    """Acceptance: two concurrent dispatch attempts on the same MR — exactly one proceeds."""

    def test_second_acquire_while_held_is_a_deterministic_no_op(self) -> None:
        first = MRReviewLock.acquire(slug=SLUG, pr_id=PR_ID, holder="agent-a", mr_url=URL)
        second = MRReviewLock.acquire(slug=SLUG, pr_id=PR_ID, holder="agent-b", mr_url=URL)

        assert first is not None
        assert second is None
        assert MRReviewLock.objects.count() == 1
        held = MRReviewLock.objects.get(slug=SLUG, pr_id=PR_ID)
        assert held.holder == "agent-a"  # the loser never overwrote the holder

    def test_distinct_prs_are_independent(self) -> None:
        first = MRReviewLock.acquire(slug=SLUG, pr_id=PR_ID, holder="agent-a")
        other = MRReviewLock.acquire(slug=SLUG, pr_id=PR_ID + 1, holder="agent-b")

        assert first is not None
        assert other is not None
        assert MRReviewLock.objects.count() == 2

    def test_acquire_by_url_shares_the_same_key_as_acquire(self) -> None:
        first = MRReviewLock.acquire(slug=SLUG, pr_id=PR_ID, holder="agent-a", mr_url=URL)
        second = MRReviewLock.acquire_by_url(URL, holder="agent-b")

        assert first is not None
        assert second is None
        assert MRReviewLock.objects.count() == 1

    def test_acquire_by_url_unparseable_url_raises(self) -> None:
        with pytest.raises(ValueError, match="not a recognised PR/MR web URL"):
            MRReviewLock.acquire_by_url("not-a-url", holder="agent-a")


class TestReacquireAfterResolveOrStale:
    def test_acquire_after_resolve_succeeds_for_a_fresh_dispatch(self) -> None:
        MRReviewLock.acquire(slug=SLUG, pr_id=PR_ID, holder="agent-a")
        assert MRReviewLock.resolve(slug=SLUG, pr_id=PR_ID) is True

        second = MRReviewLock.acquire(slug=SLUG, pr_id=PR_ID, holder="agent-b")

        assert second is not None
        assert second.state == MRReviewLock.State.REVIEW_DISPATCHED
        assert second.holder == "agent-b"

    def test_acquire_on_a_lock_past_its_deadline_self_heals(self) -> None:
        past = timezone.now() - dt.timedelta(hours=1)
        row = MRReviewLock.acquire(slug=SLUG, pr_id=PR_ID, holder="agent-a", ttl=dt.timedelta(seconds=-1))
        assert row is not None
        assert row.deadline is not None
        assert row.deadline <= timezone.now()

        second = MRReviewLock.acquire(slug=SLUG, pr_id=PR_ID, holder="agent-b")

        assert second is not None
        assert second.holder == "agent-b"
        assert second.deadline is not None
        assert second.deadline > past


class TestMarkVerdictPending:
    def test_transitions_review_dispatched_to_verdict_pending(self) -> None:
        MRReviewLock.acquire(slug=SLUG, pr_id=PR_ID, holder="agent-a")

        assert MRReviewLock.mark_verdict_pending(slug=SLUG, pr_id=PR_ID) is True

        row = MRReviewLock.objects.get(slug=SLUG, pr_id=PR_ID)
        assert row.state == MRReviewLock.State.VERDICT_PENDING
        assert row.is_locked()

    def test_no_op_when_no_row_is_review_dispatched(self) -> None:
        assert MRReviewLock.mark_verdict_pending(slug=SLUG, pr_id=PR_ID) is False

        MRReviewLock.acquire(slug=SLUG, pr_id=PR_ID, holder="agent-a")
        MRReviewLock.mark_verdict_pending(slug=SLUG, pr_id=PR_ID)

        # Already verdict_pending — calling again is a no-op, not an error.
        assert MRReviewLock.mark_verdict_pending(slug=SLUG, pr_id=PR_ID) is False


class TestResolve:
    """Acceptance: lock resolution — verdict recorded -> resolved."""

    def test_resolve_from_review_dispatched(self) -> None:
        MRReviewLock.acquire(slug=SLUG, pr_id=PR_ID, holder="agent-a")

        assert MRReviewLock.resolve(slug=SLUG, pr_id=PR_ID) is True

        row = MRReviewLock.objects.get(slug=SLUG, pr_id=PR_ID)
        assert row.state == MRReviewLock.State.RESOLVED
        assert row.resolved_at is not None
        assert row.is_locked() is False

    def test_resolve_from_verdict_pending(self) -> None:
        MRReviewLock.acquire(slug=SLUG, pr_id=PR_ID, holder="agent-a")
        MRReviewLock.mark_verdict_pending(slug=SLUG, pr_id=PR_ID)

        assert MRReviewLock.resolve(slug=SLUG, pr_id=PR_ID) is True

        row = MRReviewLock.objects.get(slug=SLUG, pr_id=PR_ID)
        assert row.state == MRReviewLock.State.RESOLVED

    def test_resolve_with_no_row_is_a_no_op(self) -> None:
        assert MRReviewLock.resolve(slug=SLUG, pr_id=PR_ID) is False
        assert MRReviewLock.objects.count() == 0

    def test_resolve_already_resolved_is_a_no_op(self) -> None:
        MRReviewLock.acquire(slug=SLUG, pr_id=PR_ID, holder="agent-a")
        MRReviewLock.resolve(slug=SLUG, pr_id=PR_ID)

        assert MRReviewLock.resolve(slug=SLUG, pr_id=PR_ID) is False


class TestReconcileStale:
    """Acceptance: a crashed review agent's stale lock expires without manual surgery."""

    def test_reconcile_resets_expired_locks_to_idle(self) -> None:
        row = MRReviewLock.acquire(slug=SLUG, pr_id=PR_ID, holder="agent-a", ttl=dt.timedelta(seconds=-1))
        assert row is not None

        count = MRReviewLock.reconcile_stale()

        assert count == 1
        row.refresh_from_db()
        assert row.state == MRReviewLock.State.IDLE
        assert row.holder == ""
        assert row.dispatched_at is None
        assert row.deadline is None

    def test_reconcile_leaves_non_stale_locks_untouched(self) -> None:
        row = MRReviewLock.acquire(slug=SLUG, pr_id=PR_ID, holder="agent-a")
        assert row is not None

        count = MRReviewLock.reconcile_stale()

        assert count == 0
        row.refresh_from_db()
        assert row.state == MRReviewLock.State.REVIEW_DISPATCHED
        assert row.holder == "agent-a"

    def test_reconcile_leaves_idle_and_resolved_rows_untouched(self) -> None:
        MRReviewLock.acquire(slug=SLUG, pr_id=PR_ID, holder="agent-a")
        MRReviewLock.resolve(slug=SLUG, pr_id=PR_ID)

        count = MRReviewLock.reconcile_stale()

        assert count == 0


class TestActiveLockFor:
    def test_returns_none_with_no_row(self) -> None:
        assert MRReviewLock.active_lock_for(slug=SLUG, pr_id=PR_ID) is None

    def test_returns_the_row_while_held(self) -> None:
        MRReviewLock.acquire(slug=SLUG, pr_id=PR_ID, holder="agent-a")

        lock = MRReviewLock.active_lock_for(slug=SLUG, pr_id=PR_ID)

        assert lock is not None
        assert lock.holder == "agent-a"

    def test_returns_none_once_resolved(self) -> None:
        MRReviewLock.acquire(slug=SLUG, pr_id=PR_ID, holder="agent-a")
        MRReviewLock.resolve(slug=SLUG, pr_id=PR_ID)

        assert MRReviewLock.active_lock_for(slug=SLUG, pr_id=PR_ID) is None

    def test_returns_none_once_past_deadline_even_without_reconcile(self) -> None:
        MRReviewLock.acquire(slug=SLUG, pr_id=PR_ID, holder="agent-a", ttl=dt.timedelta(seconds=-1))

        # No reconcile_stale() call — the merge gate's consult self-heals at read time.
        assert MRReviewLock.active_lock_for(slug=SLUG, pr_id=PR_ID) is None
