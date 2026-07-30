"""Tests for :class:`MRReviewLock` — atomic per-MR review-dispatch dedup (#1405)."""

import datetime as dt

import pytest
from django.utils import timezone

from teatree.core.modelkit.expiring_claim import acquirable_q
from teatree.core.models.auto_review_dispatch import LOOP_SCANNER_HOLDER
from teatree.core.models.mr_review_lock import DEFAULT_LOCK_TTL, MRReviewLock
from teatree.core.models.review_verdict import ReviewVerdict

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
        assert MRReviewLock.resolve(slug=SLUG, pr_id=PR_ID, holder="agent-a") is True

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

        assert MRReviewLock.resolve(slug=SLUG, pr_id=PR_ID, holder="agent-a") is True

        row = MRReviewLock.objects.get(slug=SLUG, pr_id=PR_ID)
        assert row.state == MRReviewLock.State.RESOLVED
        assert row.resolved_at is not None
        assert row.is_locked() is False

    def test_resolve_from_verdict_pending(self) -> None:
        MRReviewLock.acquire(slug=SLUG, pr_id=PR_ID, holder="agent-a")
        MRReviewLock.mark_verdict_pending(slug=SLUG, pr_id=PR_ID)

        assert MRReviewLock.resolve(slug=SLUG, pr_id=PR_ID, holder="agent-a") is True

        row = MRReviewLock.objects.get(slug=SLUG, pr_id=PR_ID)
        assert row.state == MRReviewLock.State.RESOLVED

    def test_resolve_with_no_row_is_a_no_op(self) -> None:
        assert MRReviewLock.resolve(slug=SLUG, pr_id=PR_ID, holder="agent-a") is False
        assert MRReviewLock.objects.count() == 0

    def test_resolve_already_resolved_is_a_no_op(self) -> None:
        MRReviewLock.acquire(slug=SLUG, pr_id=PR_ID, holder="agent-a")
        MRReviewLock.resolve(slug=SLUG, pr_id=PR_ID, holder="agent-a")

        assert MRReviewLock.resolve(slug=SLUG, pr_id=PR_ID, holder="agent-a") is False


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
        MRReviewLock.resolve(slug=SLUG, pr_id=PR_ID, holder="agent-a")

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
        MRReviewLock.resolve(slug=SLUG, pr_id=PR_ID, holder="agent-a")

        assert MRReviewLock.active_lock_for(slug=SLUG, pr_id=PR_ID) is None

    def test_returns_none_once_past_deadline_even_without_reconcile(self) -> None:
        MRReviewLock.acquire(slug=SLUG, pr_id=PR_ID, holder="agent-a", ttl=dt.timedelta(seconds=-1))

        # No reconcile_stale() call — the merge gate's consult self-heals at read time.
        assert MRReviewLock.active_lock_for(slug=SLUG, pr_id=PR_ID) is None


class TestResolveIsHolderAware:
    """O3: a SELF-IDENTIFYING verdict from a lockless path must not release another's lock.

    Six code paths produce a ``Task(phase="reviewing")`` and only two acquire
    this lock. ``resolve`` used to filter on ``(slug, pr_id)`` alone, so a
    codex / self-PR review — which takes no lock — released the lock held by a
    still-running #68 reviewer. The merge then proceeded on that verdict while
    the real reviewer was mid-flight and about to record a HOLD, which is the
    exact race #1405 was built to prevent.

    The check is anti-theft, not proof-of-ownership: it binds a caller that
    NAMES a lock identity. The mirror invariant — an UNNAMED releaser must still
    release, or the lock strands — is pinned in
    :class:`TestResolveNeverStrandsTheLock`.
    """

    def test_a_different_holder_cannot_release_the_lock(self) -> None:
        MRReviewLock.acquire(slug=SLUG, pr_id=PR_ID, holder="reviewer-68")

        assert MRReviewLock.resolve(slug=SLUG, pr_id=PR_ID, holder="codex-self-review") is False

        row = MRReviewLock.objects.get(slug=SLUG, pr_id=PR_ID)
        assert row.state == MRReviewLock.State.REVIEW_DISPATCHED
        assert row.is_locked() is True

    def test_the_holder_still_releases_its_own_lock(self) -> None:
        MRReviewLock.acquire(slug=SLUG, pr_id=PR_ID, holder="reviewer-68")

        assert MRReviewLock.resolve(slug=SLUG, pr_id=PR_ID, holder="reviewer-68") is True
        assert MRReviewLock.objects.get(slug=SLUG, pr_id=PR_ID).is_locked() is False

    def test_a_foreign_verdict_leaves_the_merge_gate_still_blocked(self) -> None:
        # The consequence the race produced, asserted at the gate's own consult
        # rather than only on the row: the merge gate must still see a review in
        # flight after a reviewer that named a DIFFERENT lock identity records a
        # verdict.
        MRReviewLock.acquire(slug=SLUG, pr_id=PR_ID, holder="reviewer-68")
        ReviewVerdict.record(
            pr_id=PR_ID,
            slug=SLUG,
            reviewed_sha="a" * 40,
            verdict=ReviewVerdict.Verdict.MERGE_SAFE,
            reviewer_identity="codex-self-review",
            lock_holder="codex-self-review",
        )

        assert MRReviewLock.active_lock_for(slug=SLUG, pr_id=PR_ID) is not None


class TestResolveNeverStrandsTheLock:
    """#3920: a PR with no live reviewing work must never be left holding a lock.

    The recorded ``holder`` is a DISPATCHER identity (``LOOP_SCANNER_HOLDER``, or
    a manual ``lock-acquire --holder``) while the releaser is the REVIEWER that
    concluded, and a reviewer shelling ``t3 <overlay> review record`` cannot learn
    the dispatcher's. So an unnamed releaser means "I cannot know who holds this",
    not "I hold nothing", and releases. Demanding a match instead would hold the
    lock for its whole ``deadline`` on every CLI-recorded verdict, and the merge
    gate would then escalate on a PR whose review had already concluded.
    """

    def test_an_unnamed_releaser_releases_a_lock_held_by_a_dispatcher(self) -> None:
        MRReviewLock.acquire(slug=SLUG, pr_id=PR_ID, holder=LOOP_SCANNER_HOLDER)

        assert MRReviewLock.resolve(slug=SLUG, pr_id=PR_ID) is True
        assert MRReviewLock.objects.get(slug=SLUG, pr_id=PR_ID).is_locked() is False

    def test_the_ordinary_no_holder_verdict_path_does_not_strand_the_lock(self) -> None:
        MRReviewLock.acquire(slug=SLUG, pr_id=PR_ID, holder=LOOP_SCANNER_HOLDER)

        ReviewVerdict.record(
            pr_id=PR_ID,
            slug=SLUG,
            reviewed_sha="b" * 40,
            verdict=ReviewVerdict.Verdict.MERGE_SAFE,
            reviewer_identity="cold-reviewer",
        )

        assert MRReviewLock.active_lock_for(slug=SLUG, pr_id=PR_ID) is None
        # And never escalates later either: the gate's expired-unresolved consult
        # is empty even once the original deadline has passed.
        past_deadline = timezone.now() + DEFAULT_LOCK_TTL + dt.timedelta(minutes=1)
        assert MRReviewLock.expired_unresolved_lock_for(slug=SLUG, pr_id=PR_ID, at=past_deadline) is None


class TestAcquirablePredicateIsTheOneRule:
    """`acquirable_q` is now the single acquirability rule for all three claim models.

    Pinned directly rather than only through its callers: three models (#3920 —
    MRReviewLock, AutoReviewDispatch, CriticDispatch) drive their CAS off this one
    predicate, so a change here moves all three at once. The three-way split below
    is the whole contract — always-acquirable, expired-active, terminal — and the
    NULL-deadline case is the one an expiry-based reclaim is most likely to get
    wrong, because "no bound" must mean "never stolen", not "infinitely stale".
    """

    def _matches(self, row: MRReviewLock, *, now: dt.datetime) -> bool:
        predicate = acquirable_q(
            always_acquirable=[MRReviewLock.State.IDLE, MRReviewLock.State.RESOLVED],
            active=[MRReviewLock.State.REVIEW_DISPATCHED, MRReviewLock.State.VERDICT_PENDING],
            now=now,
        )
        return MRReviewLock.objects.filter(predicate, pk=row.pk).exists()

    def test_a_live_active_claim_is_not_acquirable(self) -> None:
        row = MRReviewLock.acquire(slug=SLUG, pr_id=PR_ID, holder="agent-a", mr_url=URL)
        assert row is not None

        assert not self._matches(row, now=timezone.now())

    def test_an_active_claim_past_its_deadline_is_acquirable(self) -> None:
        row = MRReviewLock.acquire(slug=SLUG, pr_id=PR_ID, holder="agent-a", mr_url=URL)
        assert row is not None

        assert self._matches(row, now=row.deadline + dt.timedelta(seconds=1))

    def test_a_released_claim_is_acquirable_regardless_of_deadline(self) -> None:
        row = MRReviewLock.acquire(slug=SLUG, pr_id=PR_ID, holder="agent-a", mr_url=URL)
        assert row is not None
        MRReviewLock.resolve(slug=SLUG, pr_id=PR_ID, holder="agent-a")
        row.refresh_from_db()

        assert self._matches(row, now=timezone.now())

    def test_an_active_claim_with_no_deadline_is_never_stolen_by_expiry(self) -> None:
        """A NULL deadline reads as 'no bound' — released by its holder or not at all."""
        row = MRReviewLock.acquire(slug=SLUG, pr_id=PR_ID, holder="agent-a", mr_url=URL)
        assert row is not None
        MRReviewLock.objects.filter(pk=row.pk).update(deadline=None)

        assert not self._matches(row, now=timezone.now() + dt.timedelta(days=365))
