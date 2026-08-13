"""Tests for the sweep's review-arm and the held-head predicate (#68, #4380).

Covers :func:`arm_cold_review` — the claimable-review enqueue the sweep fires when
it refuses to self-merge — and :func:`head_review_state`, the precondition that stops the
autonomous no-CLEAR merge from resolving a reviewer disagreement by timestamp, plus
the reason split that keeps a LONE hold from being reported as a disagreement. The
scanner-level decision ladder that calls both is pinned in ``test_pr_sweep_scanner.py``.
"""

import datetime as dt
from dataclasses import dataclass, field
from unittest.mock import patch

import pytest

from teatree.core.modelkit.notify_policy import NotifyAudience
from teatree.core.models.review_verdict import ReviewVerdict
from teatree.loop.scanners.pr_sweep_adapters import OWNER_ESCALATION_FLAG_REASONS, SlackMergeNotifier
from teatree.loop.scanners.pr_sweep_decision import head_review_state
from teatree.loop.scanners.pr_sweep_review_gate import ReviewArmContext, arm_cold_review, held_head_attempt
from teatree.loop.scanners.pr_sweep_types import CONTESTED_HOLD_REASON, HOLD_AT_HEAD_REASON, PrSummary

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

SLUG = "souliane/teatree"
HEAD = "feedfacecafebabe1234567890abcdef12345678"
STALE = "deadbeef00000000000000000000000000000000"
SELF_LOGIN = "souliane"
PR_ID = 4380
_ENQUEUE_ERROR = "forge unreachable"
_EARLIER = dt.datetime(2026, 6, 19, 1, 34, 32, tzinfo=dt.UTC)
_LATER = dt.datetime(2026, 6, 19, 2, 5, 36, tzinfo=dt.UTC)


@dataclass(slots=True)
class _FakeDispatcher:
    calls: list[tuple[str, int, str, str, str]] = field(default_factory=list)
    returns: bool = True
    raises: bool = False

    def enqueue(self, *, slug: str, pr_id: int, head_sha: str, pr_url: str, overlay: str) -> bool:
        if self.raises:
            raise RuntimeError(_ENQUEUE_ERROR)
        self.calls.append((slug, pr_id, head_sha, pr_url, overlay))
        return self.returns


def _pr(*, author: str = SELF_LOGIN, same_repo: bool | None = True) -> PrSummary:
    return PrSummary(
        slug=SLUG,
        number=PR_ID,
        head_sha=HEAD,
        is_draft=False,
        has_changes_requested=False,
        rollup=(),
        url=f"https://github.com/{SLUG}/pull/{PR_ID}",
        title=f"PR {PR_ID}",
        behind_main=False,
        author=author,
        same_repo=same_repo,
    )


def _ctx(dispatcher: _FakeDispatcher | None, *, enabled: bool = True) -> ReviewArmContext:
    return ReviewArmContext(
        dispatcher=dispatcher,
        enabled=enabled,
        self_identities=(SELF_LOGIN,),
        overlay="teatree",
    )


def _record(*, verdict: str, reviewer: str, sha: str = HEAD, at: dt.datetime | None = None) -> ReviewVerdict:
    """Record one verdict, optionally pinning ``recorded_at`` so newest-wins is deterministic."""
    row = ReviewVerdict.record(
        pr_id=PR_ID,
        slug=SLUG,
        reviewed_sha=sha,
        verdict=verdict,
        reviewer_identity=reviewer,
    )
    if at is not None:
        ReviewVerdict.objects.filter(pk=row.pk).update(recorded_at=at)
        row.refresh_from_db()
    return row


class TestArmColdReview:
    """The arm is best-effort: every refusal degrades to "no task armed"."""

    def test_arms_one_task_for_an_own_pr(self) -> None:
        dispatcher = _FakeDispatcher()

        with patch(
            "teatree.loop.scanners.pr_sweep_review_gate.pr_ticket_under_external_delivery",
            return_value=False,
        ):
            assert arm_cold_review(_pr(), ctx=_ctx(dispatcher)) is True

        assert dispatcher.calls == [(SLUG, PR_ID, HEAD, f"https://github.com/{SLUG}/pull/{PR_ID}", "teatree")]

    def test_disabled_flag_never_arms(self) -> None:
        dispatcher = _FakeDispatcher()

        assert arm_cold_review(_pr(), ctx=_ctx(dispatcher, enabled=False)) is False
        assert dispatcher.calls == []

    def test_missing_dispatcher_never_arms(self) -> None:
        assert arm_cold_review(_pr(), ctx=_ctx(None)) is False

    def test_fork_pr_is_not_armed(self) -> None:
        # #2210/#3244: the arm covers own PRs and same-repo bot branches (trusted
        # provenance); a FORK is excluded, matching the strict fork-holds rung.
        dispatcher = _FakeDispatcher()

        assert arm_cold_review(_pr(author="a-teammate", same_repo=False), ctx=_ctx(dispatcher)) is False
        assert dispatcher.calls == []

    def test_same_repo_bot_branch_is_armed(self) -> None:
        # Anti-vacuous companion to the fork case: a non-operator author on a
        # same-repo branch still needs the verdict the sweep merges on.
        dispatcher = _FakeDispatcher()

        with patch(
            "teatree.loop.scanners.pr_sweep_review_gate.pr_ticket_under_external_delivery",
            return_value=False,
        ):
            assert arm_cold_review(_pr(author="app/github-actions"), ctx=_ctx(dispatcher)) is True

    def test_external_delivery_lease_is_not_armed(self) -> None:
        # #2104: a hand-dispatched reviewer already holds this delivery.
        dispatcher = _FakeDispatcher()

        with patch(
            "teatree.loop.scanners.pr_sweep_review_gate.pr_ticket_under_external_delivery",
            return_value=True,
        ):
            assert arm_cold_review(_pr(), ctx=_ctx(dispatcher)) is False

        assert dispatcher.calls == []

    def test_enqueue_error_degrades_to_not_armed(self) -> None:
        # A dispatcher failure must never abort the sweep — the flag already fired.
        dispatcher = _FakeDispatcher(raises=True)

        with patch(
            "teatree.loop.scanners.pr_sweep_review_gate.pr_ticket_under_external_delivery",
            return_value=False,
        ):
            assert arm_cold_review(_pr(), ctx=_ctx(dispatcher)) is False


class TestHeadReviewState:
    """A hold nobody took back, independent of newest-wins supersession (#4380)."""

    def test_hold_superseded_by_another_reviewers_pass_is_contested(self) -> None:
        # The #4332 shape: newest-wins says MERGE_SAFE, the hold still stands. TWO
        # reviewers really do disagree here, so the contested wording is earned.
        hold = _record(verdict="hold", reviewer="cold-reviewer-a", at=_EARLIER)
        allow = _record(verdict="merge_safe", reviewer="cold-reviewer-b", at=_LATER)

        review = head_review_state(slug=SLUG, pr_id=PR_ID, head_sha=HEAD)

        assert review.hold_reason == CONTESTED_HOLD_REASON
        assert review.held_verdicts == ((hold.pk, "cold-reviewer-a"),)
        assert review.authorizing_verdict == (allow.pk, "cold-reviewer-b")

    def test_lone_hold_is_not_reported_as_a_disagreement(self) -> None:
        # The ORDINARY outcome of a cold review that holds — one verdict, nobody
        # disagreeing. Reporting it as "two cold reviews disagree" names a second
        # reviewer who does not exist, and this is the far more common shape.
        hold = _record(verdict="hold", reviewer="cold-reviewer-a")

        review = head_review_state(slug=SLUG, pr_id=PR_ID, head_sha=HEAD)

        assert review.hold_reason == HOLD_AT_HEAD_REASON
        assert review.authorizing_verdict is None
        assert review.hold_detail == f"holding: #{hold.pk} cold-reviewer-a"

    def test_a_hold_recorded_after_another_reviewers_pass_is_still_contested(self) -> None:
        # The mirror of the case above, and a third of contested heads in practice.
        # Recording ORDER decides what a merge may rest on; it does not decide
        # whether two reviewers disagree, so it must not decide the wording either.
        allow = _record(verdict="merge_safe", reviewer="cold-reviewer-b", at=_EARLIER)
        hold = _record(verdict="hold", reviewer="cold-reviewer-a", at=_LATER)

        review = head_review_state(slug=SLUG, pr_id=PR_ID, head_sha=HEAD)

        assert review.hold_reason == CONTESTED_HOLD_REASON
        assert review.hold_detail == f"holding: #{hold.pk} cold-reviewer-a; merge_safe: #{allow.pk} cold-reviewer-b"
        assert review.authorizing_verdict is None
        assert held_head_attempt(_pr(), review=review).authorizing_verdict == (allow.pk, "cold-reviewer-b")

    def test_stale_merge_safe_beside_a_hold_is_not_a_disagreement(self) -> None:
        # A merge_safe against a tree the PR has moved off contests nothing at the
        # LIVE head — the hold is still lone.
        _record(verdict="merge_safe", reviewer="cold-reviewer-b", sha=STALE)
        _record(verdict="hold", reviewer="cold-reviewer-a")

        review = head_review_state(slug=SLUG, pr_id=PR_ID, head_sha=HEAD)

        assert review.held_verdicts
        assert review.hold_reason == HOLD_AT_HEAD_REASON

    def test_reviewer_lifting_their_own_hold_clears_it(self) -> None:
        _record(verdict="hold", reviewer="cold-reviewer-a")
        _record(verdict="merge_safe", reviewer="cold-reviewer-a")

        assert head_review_state(slug=SLUG, pr_id=PR_ID, head_sha=HEAD).held_verdicts == ()

    def test_hold_at_a_superseded_head_does_not_block(self) -> None:
        _record(verdict="hold", reviewer="cold-reviewer-a", sha=STALE)

        assert head_review_state(slug=SLUG, pr_id=PR_ID, head_sha=HEAD).held_verdicts == ()

    def test_no_verdicts_at_all_is_not_a_hold(self) -> None:
        assert head_review_state(slug=SLUG, pr_id=PR_ID, head_sha=HEAD).held_verdicts == ()


class TestAuthorizingVerdict:
    """What the merge names as its authorisation (#4380 acceptance 3)."""

    def test_names_the_newest_non_stale_merge_safe(self) -> None:
        _record(verdict="merge_safe", reviewer="cold-reviewer-a", at=_EARLIER)
        newer = _record(verdict="merge_safe", reviewer="cold-reviewer-b", at=_LATER)

        assert head_review_state(slug=SLUG, pr_id=PR_ID, head_sha=HEAD).authorizing_verdict == (
            newer.pk,
            "cold-reviewer-b",
        )

    def test_a_standing_hold_authorises_nothing(self) -> None:
        # Reads the SHARED newest-wins ``effective_state_at`` rather than a second
        # copy of it, so a head the merge gate refuses can never be named here.
        _record(verdict="merge_safe", reviewer="cold-reviewer-b", at=_EARLIER)
        _record(verdict="hold", reviewer="cold-reviewer-a", at=_LATER)

        assert head_review_state(slug=SLUG, pr_id=PR_ID, head_sha=HEAD).authorizing_verdict is None

    def test_no_verdict_at_the_head_authorises_nothing(self) -> None:
        _record(verdict="merge_safe", reviewer="cold-reviewer-b", sha=STALE)

        assert head_review_state(slug=SLUG, pr_id=PR_ID, head_sha=HEAD).authorizing_verdict is None


class TestTheHeldReportReachesAHuman:
    """The owner DM is a held head's ONLY mover, so both reasons pin the report path.

    ``held_head_attempt`` leaves ``review_dispatched`` False and no reviewer is armed,
    so nothing else in the loop can move the PR: the escalation audience and the wording
    are load-bearing, not cosmetic. The ordinary-flag-stays-log-only companion lives in
    ``test_pr_sweep_unusable_clear_report.py`` and is not duplicated here.
    """

    @pytest.mark.parametrize(
        ("reason", "wording"),
        [(CONTESTED_HOLD_REASON, "two cold reviews disagree"), (HOLD_AT_HEAD_REASON, "nobody took it back")],
    )
    def test_each_held_reason_dms_the_owner_in_its_own_words(self, reason: str, wording: str) -> None:
        detail = "holding: #7 cold-reviewer-a; merge_safe: #8 cold-reviewer-b"
        assert reason in OWNER_ESCALATION_FLAG_REASONS

        with patch("teatree.core.notify.notify_user") as notify:
            SlackMergeNotifier(backend=None).flag(
                slug=SLUG, pr_id=PR_ID, reason=reason, url="https://example.test/pr", detail=detail
            )

        assert notify.call_args.kwargs["audience"] is NotifyAudience.OWNER_ESCALATION
        assert wording in notify.call_args.args[0]
        assert f"({detail})" in notify.call_args.args[0]
