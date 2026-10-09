"""The MR-triage ladder: one rung fires per MR, and no MR ever falls through silently.

Every case here is pure — :func:`triage` takes facts and returns a verdict, so
these assertions pin the DECISION, never a forge/Slack round-trip.

Repo names and branch names are SYNTHETIC.
"""

import datetime as dt
from dataclasses import replace
from typing import Any

import pytest

from teatree.core.review.mr_triage import (
    HEDGED_REVIEW_REQUEST_NOTE,
    CiState,
    MrFacts,
    RepoOwner,
    ReviewRequestState,
    TriageAction,
    TriageReason,
    TriageThresholds,
    triage,
)

_DAY = dt.timedelta(days=1)

_REVIEWED = MrFacts(
    ci=CiState.GREEN,
    review_request=ReviewRequestState.REQUESTED,
    idle_since_review_requested=3 * _DAY,
)

_UNREVIEWED = MrFacts(
    ci=CiState.GREEN,
    review_request=ReviewRequestState.NONE,
    age_since_opened=_DAY,
)


def _reviewed(**overrides: Any) -> MrFacts:
    """An open, green, review-requested, unapproved MR — the step-6 baseline."""
    return replace(_REVIEWED, **overrides)


def _unreviewed(**overrides: Any) -> MrFacts:
    """An open, green MR nobody has been asked to review yet — the step-5 baseline."""
    return replace(_UNREVIEWED, **overrides)


class TestDraftLeavesTriageBelowRedCi:
    def test_a_green_draft_produces_no_action(self) -> None:
        verdict = triage(_unreviewed(draft=True))

        assert verdict.action is TriageAction.NONE
        assert verdict.reason is TriageReason.DRAFT

    def test_a_bare_draft_produces_no_action(self) -> None:
        assert triage(MrFacts(draft=True)).reason is TriageReason.DRAFT

    def test_a_red_draft_still_owes_the_ci_fix(self) -> None:
        """THE ordering control: EVERY merge request owes green CI, a draft included.

        Were the draft rung placed ABOVE failed-CI, this fact set would report
        NONE and a broken draft would sit red in silence until someone opened it.
        """
        red_draft = _unreviewed(draft=True, ci=CiState.FAILED)

        verdict = triage(red_draft)

        assert verdict.action is TriageAction.FIX_CI
        assert verdict.reason is TriageReason.CI_FAILED

    def test_a_draft_wins_over_every_rung_below_it(self) -> None:
        loud = _unreviewed(
            draft=True,
            age_since_opened=90 * _DAY,
            target_branch="feature/base",
            repo_default_branch="main",
            base_review_request=ReviewRequestState.NONE,
        )

        assert triage(loud).action is TriageAction.NONE


class TestFailedCiIsTheFirstRung:
    def test_failed_ci_asks_for_a_fix(self) -> None:
        verdict = triage(_unreviewed(ci=CiState.FAILED))

        assert verdict.action is TriageAction.FIX_CI
        assert verdict.reason is TriageReason.CI_FAILED

    def test_unreadable_ci_is_never_reported_as_failed(self) -> None:
        """UNKNOWN means "we could not read it", not "it is red" — no fix dispatch."""
        assert triage(_unreviewed(ci=CiState.UNKNOWN)).action is not TriageAction.FIX_CI


class TestReviewExemptRepo:
    def test_an_exempt_merge_request_needs_no_action(self) -> None:
        verdict = triage(_unreviewed(review_exempt=True))

        assert verdict.action is TriageAction.NONE
        assert verdict.reason is TriageReason.REVIEW_EXEMPT

    def test_red_ci_outranks_the_exemption(self) -> None:
        """THE ordering control: an exempt repo owes no review request but still owes CI.

        Were the exemption placed ABOVE the failed-CI rung, this fact set would
        report NONE and a red exempt merge request would sit broken in silence.
        """
        red_and_exempt = _unreviewed(review_exempt=True, ci=CiState.FAILED)

        verdict = triage(red_and_exempt)

        assert verdict.action is TriageAction.FIX_CI
        assert verdict.reason is TriageReason.CI_FAILED

    def test_the_exemption_outranks_every_social_rung(self) -> None:
        social = _unreviewed(
            review_exempt=True,
            age_since_opened=90 * _DAY,
            repo_fit_tripwire="content belongs in another repo",
            work_group="tenant-onboarding",
            work_group_unready_members=2,
            target_branch="feature/base",
            repo_default_branch="main",
            base_review_request=ReviewRequestState.NONE,
        )

        assert triage(social).reason is TriageReason.REVIEW_EXEMPT

    def test_a_draft_still_leaves_triage_before_the_exemption_speaks(self) -> None:
        assert triage(_unreviewed(review_exempt=True, draft=True)).reason is TriageReason.DRAFT

    def test_a_non_exempt_repo_is_unaffected(self) -> None:
        assert MrFacts().review_exempt is False
        assert triage(_unreviewed()).action is TriageAction.REQUEST_REVIEW


class TestStackedOnAnUnreviewedBase:
    def test_non_default_target_with_unrequested_base_is_flagged(self) -> None:
        verdict = triage(
            _unreviewed(
                target_branch="feature/base",
                repo_default_branch="main",
                base_review_request=ReviewRequestState.NONE,
            )
        )

        assert verdict.action is TriageAction.FLAG_STACKED_ON_UNREVIEWED_BASE
        assert verdict.reason is TriageReason.STACKED_ON_UNREVIEWED_BASE

    def test_non_default_target_with_a_reviewed_base_is_not_flagged(self) -> None:
        deliberate_chain = _unreviewed(
            target_branch="feature/base",
            repo_default_branch="main",
            base_review_request=ReviewRequestState.REQUESTED,
        )

        assert triage(deliberate_chain).action is not TriageAction.FLAG_STACKED_ON_UNREVIEWED_BASE

    def test_unknown_base_state_is_not_flagged(self) -> None:
        """Only a determinate "the base was never sent for review" is the smell."""
        unknown_base = _unreviewed(target_branch="feature/base", repo_default_branch="main")

        assert triage(unknown_base).action is not TriageAction.FLAG_STACKED_ON_UNREVIEWED_BASE

    def test_default_target_is_never_flagged(self) -> None:
        on_default = _unreviewed(
            target_branch="main",
            repo_default_branch="main",
            base_review_request=ReviewRequestState.NONE,
        )

        assert triage(on_default).action is not TriageAction.FLAG_STACKED_ON_UNREVIEWED_BASE


class TestRepoFitEscalation:
    def test_repo_fit_tripwire_escalates(self) -> None:
        verdict = triage(_unreviewed(repo_fit_tripwire="content belongs in another repo"))

        assert verdict.action is TriageAction.ESCALATE_MODEL
        assert verdict.reason is TriageReason.REPO_FIT
        assert verdict.detail == "content belongs in another repo"


class TestWorkGroupHold:
    def test_an_unready_sibling_holds_the_whole_group(self) -> None:
        verdict = triage(_unreviewed(work_group="tenant-onboarding", work_group_unready_members=1))

        assert verdict.action is TriageAction.WAIT
        assert verdict.reason is TriageReason.WORK_GROUP_NOT_READY
        assert verdict.detail == "tenant-onboarding"

    def test_a_group_whose_siblings_are_all_ready_is_released(self) -> None:
        ready = _unreviewed(work_group="tenant-onboarding", work_group_unready_members=0)

        assert triage(ready).action is TriageAction.REQUEST_REVIEW

    def test_an_unready_count_without_a_group_holds_nothing(self) -> None:
        assert triage(_unreviewed(work_group_unready_members=3)).action is TriageAction.REQUEST_REVIEW

    def test_the_hold_outranks_the_review_request(self) -> None:
        """A per-merge-request review would judge a fragment of the unit of work."""
        held = _unreviewed(work_group="tenant-onboarding", work_group_unready_members=2, author_unsure=True)

        assert triage(held).reason is TriageReason.WORK_GROUP_NOT_READY


class TestNoReviewRequestYet:
    def test_green_and_fresh_requests_review(self) -> None:
        verdict = triage(_unreviewed())

        assert verdict.action is TriageAction.REQUEST_REVIEW
        assert verdict.reason is TriageReason.READY_FOR_REVIEW
        assert verdict.detail == ""

    def test_author_unsure_carries_the_hedge(self) -> None:
        verdict = triage(_unreviewed(author_unsure=True))

        assert verdict.action is TriageAction.REQUEST_REVIEW
        assert verdict.detail == HEDGED_REVIEW_REQUEST_NOTE

    @pytest.mark.parametrize("ci", [CiState.PENDING, CiState.UNKNOWN])
    def test_ci_not_confirmed_green_waits(self, ci: CiState) -> None:
        verdict = triage(_unreviewed(ci=ci))

        assert verdict.action is TriageAction.WAIT
        assert verdict.reason is TriageReason.CI_NOT_GREEN

    def test_stale_without_review_proposes_draft(self) -> None:
        verdict = triage(_unreviewed(age_since_opened=8 * _DAY))

        assert verdict.action is TriageAction.PROPOSE_DRAFT
        assert verdict.reason is TriageReason.STALE_NO_REVIEW

    def test_stale_threshold_is_overridable(self) -> None:
        patient = TriageThresholds(stale_no_review=30 * _DAY)

        assert triage(_unreviewed(age_since_opened=8 * _DAY), thresholds=patient).action is TriageAction.REQUEST_REVIEW


class TestReviewRequested:
    def test_approved_needs_nothing(self) -> None:
        verdict = triage(_reviewed(approved=True, idle_since_review_requested=90 * _DAY))

        assert verdict.action is TriageAction.NONE
        assert verdict.reason is TriageReason.APPROVED

    def test_engineering_repo_pings_the_group_after_two_idle_days(self) -> None:
        verdict = triage(_reviewed(repo_owner=RepoOwner.ENGINEERING, idle_since_review_requested=3 * _DAY))

        assert verdict.action is TriageAction.GROUP_PING
        assert verdict.reason is TriageReason.NAG_INTERVAL_EXCEEDED

    def test_engineering_repo_waits_inside_the_two_day_window(self) -> None:
        verdict = triage(_reviewed(repo_owner=RepoOwner.ENGINEERING, idle_since_review_requested=_DAY))

        assert verdict.action is TriageAction.WAIT
        assert verdict.reason is TriageReason.WITHIN_NAG_INTERVAL

    def test_devops_repo_is_more_patient_than_engineering(self) -> None:
        """The same 3 idle days that ping an engineering repo still wait on a DevOps one."""
        devops = _reviewed(repo_owner=RepoOwner.DEVOPS, idle_since_review_requested=3 * _DAY)

        assert triage(devops).action is TriageAction.WAIT

    def test_devops_repo_pings_once_its_own_window_passes(self) -> None:
        devops = _reviewed(repo_owner=RepoOwner.DEVOPS, idle_since_review_requested=6 * _DAY)

        assert triage(devops).action is TriageAction.GROUP_PING

    def test_unknown_owner_gets_the_patient_window(self) -> None:
        assert MrFacts().repo_owner is RepoOwner.DEVOPS
        assert triage(_reviewed(idle_since_review_requested=3 * _DAY)).action is TriageAction.WAIT

    @pytest.mark.parametrize("owner", list(RepoOwner))
    def test_nag_intervals_are_overridable(self, owner: RepoOwner) -> None:
        impatient = TriageThresholds(engineering_nag=dt.timedelta(hours=1), devops_nag=dt.timedelta(hours=1))
        facts = _reviewed(repo_owner=owner, idle_since_review_requested=dt.timedelta(hours=2))

        assert triage(facts, thresholds=impatient).action is TriageAction.GROUP_PING


class TestFallbackIsNeverASilentNoOp:
    def test_indeterminate_review_request_asks_the_owner(self) -> None:
        verdict = triage(MrFacts(ci=CiState.GREEN))

        assert verdict.action is TriageAction.ASK_OWNER
        assert verdict.reason is TriageReason.INDETERMINATE

    def test_a_bare_fact_set_still_produces_an_action(self) -> None:
        assert triage(MrFacts()).action is TriageAction.ASK_OWNER


class TestThresholdProvenance:
    def test_engineering_nag_is_the_two_days_the_review_nag_already_re_pings_on(self) -> None:
        assert TriageThresholds().engineering_nag == dt.timedelta(days=2)

    def test_devops_is_strictly_more_patient_than_engineering(self) -> None:
        thresholds = TriageThresholds()

        assert thresholds.devops_nag > thresholds.engineering_nag
        assert thresholds.nag_interval(RepoOwner.DEVOPS) == thresholds.devops_nag
        assert thresholds.nag_interval(RepoOwner.ENGINEERING) == thresholds.engineering_nag

    def test_a_repo_gets_a_review_request_window_before_it_is_called_stale(self) -> None:
        thresholds = TriageThresholds()

        assert thresholds.stale_no_review > thresholds.devops_nag


class TestNagIntervalWidensOnFibonacciSteps:
    """Each unanswered re-ask waits ``base * fib(attempt)``, up to the ceiling.

    The per-owner base is the multiplicand, so the engineering/DevOps patience
    distinction survives the backoff rather than being flattened by it.
    """

    @pytest.mark.parametrize(
        ("attempt", "expected_days"),
        [(0, 2), (1, 2), (2, 4), (3, 6), (4, 10), (5, 16), (6, 26), (7, 30), (8, 30), (20, 30)],
    )
    def test_the_engineering_schedule(self, attempt: int, expected_days: int) -> None:
        interval = TriageThresholds().nag_interval_for_attempt(RepoOwner.ENGINEERING, attempt)

        assert interval == dt.timedelta(days=expected_days)

    @pytest.mark.parametrize(
        ("attempt", "expected_days"),
        [(0, 5), (1, 5), (2, 10), (3, 15), (4, 25), (5, 30), (6, 30), (20, 30)],
    )
    def test_the_devops_schedule(self, attempt: int, expected_days: int) -> None:
        interval = TriageThresholds().nag_interval_for_attempt(RepoOwner.DEVOPS, attempt)

        assert interval == dt.timedelta(days=expected_days)

    def test_the_first_re_ask_is_the_flat_interval_the_scanner_always_used(self) -> None:
        thresholds = TriageThresholds()

        for owner in RepoOwner:
            assert thresholds.nag_interval_for_attempt(owner, 0) == thresholds.nag_interval(owner)

    def test_the_cap_is_configurable(self) -> None:
        weekly = TriageThresholds(nag_backoff_cap=dt.timedelta(days=7))

        assert weekly.nag_interval_for_attempt(RepoOwner.ENGINEERING, 4) == dt.timedelta(days=7)

    def test_a_negative_attempt_gets_the_first_step(self) -> None:
        thresholds = TriageThresholds()

        assert thresholds.nag_interval_for_attempt(RepoOwner.ENGINEERING, -3) == thresholds.engineering_nag

    @pytest.mark.parametrize(
        ("owner", "first_capped_attempt"),
        [(RepoOwner.ENGINEERING, 7), (RepoOwner.DEVOPS, 5)],
    )
    def test_at_cap_is_reported_from_the_step_before_the_ceiling_clips_it(
        self,
        owner: RepoOwner,
        first_capped_attempt: int,
    ) -> None:
        """``min`` erases the overshoot, so the predicate compares the UNCLIPPED product."""
        thresholds = TriageThresholds()

        assert not thresholds.nag_backoff_at_cap(owner, first_capped_attempt - 1)
        assert thresholds.nag_backoff_at_cap(owner, first_capped_attempt)
        assert thresholds.nag_backoff_at_cap(owner, first_capped_attempt + 5)
