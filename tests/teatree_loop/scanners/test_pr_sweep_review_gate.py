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
from django.db import connection
from django.test.utils import CaptureQueriesContext

from teatree.core.modelkit.notify_policy import NotifyAudience
from teatree.core.models import HeadCarryOver, PullRequest, ReviewHoldDisposition, RoundMetric, Session, Task, Ticket
from teatree.core.models.review_verdict import ReviewVerdict
from teatree.loop.scanners.pr_sweep_adapters import OWNER_ESCALATION_FLAG_REASONS, SlackMergeNotifier
from teatree.loop.scanners.pr_sweep_decision import head_review_state
from teatree.loop.scanners.pr_sweep_review_gate import (
    ReviewArmContext,
    arm_cold_review,
    held_head_attempt,
    review_rounds_exhausted,
)
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
PR_URL = f"https://github.com/{SLUG}/pull/{PR_ID}"


def _reviewing_task(*, status: str = Task.Status.PENDING) -> Task:
    """One reviewing Task on the ticket that IS this PR — what the other arm mints."""
    ticket, _ = Ticket.objects.get_or_create(issue_url=PR_URL, defaults={"role": Ticket.Role.REVIEWER})
    return Task.objects.create(
        ticket=ticket,
        session=Session.objects.create(ticket=ticket),
        phase="reviewing",
        status=status,
    )


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
    """Record one verdict, optionally pinning ``recorded_at`` for ordering cases."""
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

    def test_three_recorded_verdicts_on_one_head_refuse_a_fourth_review(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://github.com/souliane/teatree/issues/4804")
        PullRequest.objects.create(ticket=ticket, overlay="teatree", url=PR_URL, repo=SLUG, iid=str(PR_ID))
        for reviewer in ("reviewer-agent-7", "reviewer-agent-8", "reviewer-agent-9"):
            ReviewVerdict.record(
                pr_id=PR_ID,
                slug=SLUG,
                reviewed_sha="a" * 40,
                verdict="hold",
                reviewer_identity=reviewer,
                ticket=ticket,
            )
        dispatcher = _FakeDispatcher()
        with patch("teatree.loop.scanners.pr_sweep_review_gate.notify_user") as notify:
            assert arm_cold_review(_pr(), ctx=_ctx(dispatcher)) is False
        assert dispatcher.calls == []
        assert notify.call_count == 1

    def test_three_recorded_heads_refuse_a_fourth_review(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://github.com/souliane/teatree/issues/4804")
        PullRequest.objects.create(ticket=ticket, overlay="teatree", url=PR_URL, repo=SLUG, iid=str(PR_ID))
        for digit in "abc":
            ReviewVerdict.record(
                pr_id=PR_ID,
                slug=SLUG,
                reviewed_sha=digit * 40,
                verdict="hold",
                reviewer_identity="reviewer-agent-7",
                ticket=ticket,
            )
        dispatcher = _FakeDispatcher()
        with patch("teatree.loop.scanners.pr_sweep_review_gate.notify_user") as notify:
            assert arm_cold_review(_pr(), ctx=_ctx(dispatcher)) is False
        assert dispatcher.calls == []
        assert notify.call_count == 1
        assert all(digit * 40 in notify.call_args.args[0] for digit in "abc")

    def test_round_limit_query_does_not_send_a_notice(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://github.com/souliane/teatree/issues/4808")
        PullRequest.objects.create(ticket=ticket, overlay="teatree", url=PR_URL, repo=SLUG, iid=str(PR_ID))
        for digit in "abc":
            _record(verdict="hold", reviewer=f"reviewer-agent-{digit}", sha=digit * 40)
        with patch("teatree.loop.scanners.pr_sweep_review_gate.notify_user") as notify:
            assert review_rounds_exhausted(slug=SLUG, pr_id=PR_ID, pr_url=PR_URL) is True
        notify.assert_not_called()

    def test_carried_verdict_does_not_spend_a_review_round(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://github.com/souliane/teatree/issues/4805")
        PullRequest.objects.create(ticket=ticket, overlay="teatree", url=PR_URL, repo=SLUG, iid=str(PR_ID))
        for digit in "abc":
            ReviewVerdict.record(
                pr_id=PR_ID,
                slug=SLUG,
                reviewed_sha=digit * 40,
                verdict="hold",
                reviewer_identity="reviewer-agent-7",
                ticket=ticket,
            )
        HeadCarryOver.objects.create(
            slug=SLUG,
            pr_id=PR_ID,
            from_head="b" * 40,
            to_head="c" * 40,
            outcome=HeadCarryOver.Outcome.CARRIED,
            carried={"review_verdicts": 1},
        )
        dispatcher = _FakeDispatcher()
        assert arm_cold_review(_pr(), ctx=_ctx(dispatcher)) is True
        assert len(dispatcher.calls) == 1

    def test_venue_limit_round_does_not_spend_the_third_slot(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://github.com/souliane/teatree/issues/4806")
        PullRequest.objects.create(ticket=ticket, overlay="teatree", url=PR_URL, repo=SLUG, iid=str(PR_ID))
        for digit in "ab":
            ReviewVerdict.record(
                pr_id=PR_ID,
                slug=SLUG,
                reviewed_sha=digit * 40,
                verdict="hold",
                reviewer_identity="reviewer-agent-7",
                ticket=ticket,
            )
        RoundMetric.objects.open(kind="review", ticket=ticket, subject="c" * 40, size=1, detail={})
        RoundMetric.objects.settle(kind="review", ticket=ticket, subject="c" * 40, outcome="venue_limit")

        dispatcher = _FakeDispatcher()
        assert arm_cold_review(_pr(), ctx=_ctx(dispatcher)) is True
        assert len(dispatcher.calls) == 1

    def test_unverified_metric_does_not_spend_a_round(self) -> None:
        ticket = Ticket.objects.create(issue_url="https://github.com/souliane/teatree/issues/4807")
        PullRequest.objects.create(ticket=ticket, overlay="teatree", url=PR_URL, repo=SLUG, iid=str(PR_ID))
        for digit in "abc":
            ReviewVerdict.record(
                pr_id=PR_ID,
                slug=SLUG,
                reviewed_sha=digit * 40,
                verdict="unverified",
                reviewer_identity=f"reviewer-agent-{digit}",
                ticket=ticket,
                evidence_gaps=["ci_unreadable"],
            )
            RoundMetric.objects.open(kind="review", ticket=ticket, subject=digit * 40, size=1, detail={})
            RoundMetric.objects.settle(kind="review", ticket=ticket, subject=digit * 40, outcome="unverified")

        dispatcher = _FakeDispatcher()
        with patch("teatree.loop.scanners.pr_sweep_review_gate.notify_user") as notify:
            assert arm_cold_review(_pr(), ctx=_ctx(dispatcher)) is True
        assert len(dispatcher.calls) == 1
        notify.assert_not_called()

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

    def test_a_review_already_in_flight_is_not_armed_a_second_time(self) -> None:
        # The self-PR scanner mints its reviewer through CodexReviewMarker and this
        # arm through AutoReviewDispatch; the two ledgers dedup per head
        # INDEPENDENTLY, so one green head reliably bought two cold reviews of one
        # tree. The verdict the sweep waits for is the one already in flight.
        dispatcher = _FakeDispatcher()
        _reviewing_task()

        with patch(
            "teatree.loop.scanners.pr_sweep_review_gate.pr_ticket_under_external_delivery",
            return_value=False,
        ):
            assert arm_cold_review(_pr(), ctx=_ctx(dispatcher)) is False

        assert dispatcher.calls == []

    def test_a_finished_reviewer_leaves_the_head_armable(self) -> None:
        # Anti-vacuous control for the refusal above, and the self-healing property:
        # a reviewer that died must not latch its head out of review forever.
        dispatcher = _FakeDispatcher()
        _reviewing_task(status=Task.Status.FAILED)

        with patch(
            "teatree.loop.scanners.pr_sweep_review_gate.pr_ticket_under_external_delivery",
            return_value=False,
        ):
            assert arm_cold_review(_pr(), ctx=_ctx(dispatcher)) is True

    def test_a_task_in_another_phase_never_blocks_the_arm(self) -> None:
        # Only a REVIEWING task is the reviewer this refusal defers to; a coding task
        # on the same PR ticket says nothing about whether the head was reviewed.
        dispatcher = _FakeDispatcher()
        ticket, _ = Ticket.objects.get_or_create(issue_url=PR_URL, defaults={"role": Ticket.Role.REVIEWER})
        Task.objects.create(ticket=ticket, session=Session.objects.create(ticket=ticket), phase="coding")

        with patch(
            "teatree.loop.scanners.pr_sweep_review_gate.pr_ticket_under_external_delivery",
            return_value=False,
        ):
            assert arm_cold_review(_pr(), ctx=_ctx(dispatcher)) is True

    def test_enqueue_error_degrades_to_not_armed(self) -> None:
        # A dispatcher failure must never abort the sweep — the flag already fired.
        dispatcher = _FakeDispatcher(raises=True)

        with patch(
            "teatree.loop.scanners.pr_sweep_review_gate.pr_ticket_under_external_delivery",
            return_value=False,
        ):
            assert arm_cold_review(_pr(), ctx=_ctx(dispatcher)) is False


class TestHeadReviewState:
    """A hold remains active at its reviewed head (#4380)."""

    def test_one_head_read_supplies_holds_and_authorization(self) -> None:
        _record(verdict="hold", reviewer="cold-reviewer-a")
        _record(verdict="merge_safe", reviewer="cold-reviewer-b")

        with CaptureQueriesContext(connection) as queries:
            review = head_review_state(slug=SLUG, pr_id=PR_ID, head_sha=HEAD)

        assert len(queries) == 2
        assert len(review.held_verdicts) == 1
        assert review.standing_merge_safe is not None
        assert review.authorizing_verdict is None

    def test_hold_beside_another_reviewers_pass_is_contested(self) -> None:
        # The #4332 shape: a pass sits beside a standing hold. TWO
        # reviewers really do disagree here, so the contested wording is earned.
        hold = _record(verdict="hold", reviewer="cold-reviewer-a", at=_EARLIER)
        _record(verdict="merge_safe", reviewer="cold-reviewer-b", at=_LATER)

        review = head_review_state(slug=SLUG, pr_id=PR_ID, head_sha=HEAD)

        assert review.hold_reason == CONTESTED_HOLD_REASON
        assert review.held_verdicts == ((hold.pk, "cold-reviewer-a"),)
        assert review.authorizing_verdict is None

    def test_dispositioned_hold_does_not_block_authorization(self) -> None:
        hold = _record(verdict="hold", reviewer="cold-reviewer-a")
        clearing = _record(verdict="merge_safe", reviewer="cold-reviewer-b")
        ReviewHoldDisposition.record(
            hold=hold,
            clearing_verdict=clearing,
            points=[{"index": -1, "classification": "disproved_premise", "citation": "fresh independent review"}],
        )

        review = head_review_state(slug=SLUG, pr_id=PR_ID, head_sha=HEAD)
        assert review.held_verdicts == ()
        assert review.authorizing_verdict == (clearing.pk, "cold-reviewer-b")

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

    def test_reviewer_pass_leaves_their_own_hold(self) -> None:
        _record(verdict="hold", reviewer="cold-reviewer-a")
        _record(verdict="merge_safe", reviewer="cold-reviewer-a")

        assert head_review_state(slug=SLUG, pr_id=PR_ID, head_sha=HEAD).held_verdicts

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
        # The sweep and merge gate both refuse a head carrying any active HOLD.
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
        [(CONTESTED_HOLD_REASON, "two cold reviews disagree"), (HOLD_AT_HEAD_REASON, "remains active at this head")],
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
