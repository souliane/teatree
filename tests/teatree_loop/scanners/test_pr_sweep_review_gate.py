"""Tests for the sweep's review-arm and the contested-hold predicate (#68, #4380).

Covers :func:`arm_cold_review` — the claimable-review enqueue the sweep fires when
it refuses to self-merge — and :func:`unreconciled_hold_at_head`, the precondition
that stops the autonomous no-CLEAR merge from resolving a reviewer disagreement by
timestamp. The scanner-level decision ladder that calls both is pinned in
``test_pr_sweep_scanner.py``.
"""

from dataclasses import dataclass, field
from unittest.mock import patch

import pytest

from teatree.core.models.review_verdict import ReviewVerdict
from teatree.loop.scanners.pr_sweep_decision import unreconciled_hold_at_head
from teatree.loop.scanners.pr_sweep_review_gate import ReviewArmContext, arm_cold_review
from teatree.loop.scanners.pr_sweep_types import PrSummary

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

SLUG = "souliane/teatree"
HEAD = "feedfacecafebabe1234567890abcdef12345678"
STALE = "deadbeef00000000000000000000000000000000"
SELF_LOGIN = "souliane"
PR_ID = 4380
_ENQUEUE_ERROR = "forge unreachable"


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


def _record(*, verdict: str, reviewer: str, sha: str = HEAD) -> ReviewVerdict:
    return ReviewVerdict.record(
        pr_id=PR_ID,
        slug=SLUG,
        reviewed_sha=sha,
        verdict=verdict,
        reviewer_identity=reviewer,
    )


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


class TestUnreconciledHoldAtHead:
    """A hold nobody took back, independent of newest-wins supersession (#4380)."""

    def test_hold_superseded_by_another_reviewers_pass_still_counts(self) -> None:
        # The #4332 shape: newest-wins says MERGE_SAFE, the hold still stands.
        _record(verdict="hold", reviewer="cold-reviewer-a")
        _record(verdict="merge_safe", reviewer="cold-reviewer-b")

        assert unreconciled_hold_at_head(slug=SLUG, pr_id=PR_ID, head_sha=HEAD) is True

    def test_reviewer_lifting_their_own_hold_clears_it(self) -> None:
        _record(verdict="hold", reviewer="cold-reviewer-a")
        _record(verdict="merge_safe", reviewer="cold-reviewer-a")

        assert unreconciled_hold_at_head(slug=SLUG, pr_id=PR_ID, head_sha=HEAD) is False

    def test_hold_at_a_superseded_head_does_not_block(self) -> None:
        _record(verdict="hold", reviewer="cold-reviewer-a", sha=STALE)

        assert unreconciled_hold_at_head(slug=SLUG, pr_id=PR_ID, head_sha=HEAD) is False

    def test_no_verdicts_at_all_is_not_a_hold(self) -> None:
        assert unreconciled_hold_at_head(slug=SLUG, pr_id=PR_ID, head_sha=HEAD) is False
