"""Head carry-over: verdicts, critic rows, attestations and clearances follow the PR patch, not the head (#5450)."""

import logging
from dataclasses import dataclass, field
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.gates.merge_quality_gate import MergeQualityVerdictError, check_merge_quality_verdict
from teatree.core.gates.rubric_gate import RubricNotSatisfiedError, check_rubric_satisfied
from teatree.core.merge.head_carry_over import PatchLineage, carry_over_head
from teatree.core.models import (
    BranchUpdateAttempt,
    CriticVerdict,
    HeadCarryOver,
    MergeClear,
    MRReviewLock,
    ReviewVerdict,
    ReviewVerdictError,
    Ticket,
)
from teatree.core.models.review_verdict import Finding, HeadVerdictState
from teatree.core.models.ticket_evidence import recorded_anti_vacuity_attestation
from teatree.core.review.nit_lane import NitPoint, nit_point_marker
from tests.factories import PullRequestFactory, RubricCriterionFactory, RubricFactory, TicketFactory

SLUG = "souliane/teatree"
PR_ID = 5341
OLD = "a" * 40
MIDDLE = "c" * 40
NEW = "b" * 40
PATCH_ID = "6208fe14" + "0" * 32


def _lineage(
    *,
    old_patch_id: str = PATCH_ID,
    new_patch_id: str = PATCH_ID,
    descends: bool = True,
    added_all_merges: bool = True,
    refusal: str = "",
) -> PatchLineage:
    return PatchLineage(
        old_patch_id=old_patch_id,
        new_patch_id=new_patch_id,
        descends=descends,
        added_all_merges=added_all_merges,
        refusal=refusal,
    )


@dataclass(slots=True)
class _Reader:
    lineage: PatchLineage | None
    calls: list[tuple[str, str]] = field(default_factory=list)

    def __call__(self, *, slug: str, pr_id: int, host_kind: str, old_head: str, new_head: str) -> PatchLineage | None:
        self.calls.append((old_head, new_head))
        return self.lineage


def _carry(reader: _Reader, *, live_head: str = NEW, host_kind: str = "github") -> HeadCarryOver | None:
    return carry_over_head(slug=SLUG, pr_id=PR_ID, live_head=live_head, host_kind=host_kind, read_lineage=reader)


@dataclass(frozen=True, slots=True)
class _Seeded:
    ticket: Ticket
    clear: MergeClear


def _ticket() -> Ticket:
    ticket = TicketFactory.create()
    PullRequestFactory(ticket=ticket, repo=SLUG, iid=str(PR_ID), url=f"https://github.com/{SLUG}/pull/{PR_ID}")
    return ticket


def _clear(ticket: Ticket, head: str, *, slug: str) -> MergeClear:
    return MergeClear.objects.create(
        ticket=ticket,
        pr_id=PR_ID,
        slug=slug,
        reviewed_sha=head,
        reviewer_identity="cold-reviewer",
        gh_verify_result=MergeClear.VerifyResult.GREEN,
        blast_class=MergeClear.BlastClass.LOGIC,
    )


def _seed(head: str = OLD, *, slug: str = SLUG) -> _Seeded:
    ticket = _ticket()
    ReviewVerdict.record(
        pr_id=PR_ID,
        slug=slug,
        reviewed_sha=head,
        verdict="merge_safe",
        reviewer_identity="cold-reviewer",
        ticket=ticket,
    )
    CriticVerdict.record_from_envelope(
        ticket=ticket,
        transition="merge",
        head_sha=head,
        envelope={
            "grader_identity": "critic-agent-7",
            "items": [{"slug": "test_value", "status": "pass", "citation": "tests/x.py::test_y was read"}],
        },
    )
    ticket.record_anti_vacuity_attestation(head, "every AC maps to a test", ["tests/x.py::test_y"])
    return _Seeded(ticket=ticket, clear=_clear(ticket, head, slug=slug))


def _counts() -> tuple[int, int, int]:
    return ReviewVerdict.objects.count(), CriticVerdict.objects.count(), HeadCarryOver.objects.count()


class TestCarriesOnIdenticalPatch(TestCase):
    def test_every_store_follows_the_patch_and_one_ledger_row_names_both_heads(self) -> None:
        seeded = _seed()

        row = _carry(_Reader(_lineage()))

        assert row is not None
        assert (row.outcome, row.reason) == (HeadCarryOver.Outcome.CARRIED, "")
        assert (row.slug, row.pr_id, row.from_head, row.to_head, row.patch_id) == (SLUG, PR_ID, OLD, NEW, PATCH_ID)
        assert row.carried == {
            "review_verdicts": 1,
            "critic_verdicts": 1,
            "merge_clears": 1,
            "anti_vacuity": 1,
            "rubric_grades": 0,
            "nit_receipts": 0,
        }
        verdict = ReviewVerdict.objects.get(reviewed_sha=NEW)
        assert (verdict.reviewer_identity, verdict.verdict) == ("cold-reviewer", "merge_safe")
        assert (
            ReviewVerdict.objects.effective_state_at(slug=SLUG, pr_id=PR_ID, head_sha=NEW)
            is HeadVerdictState.MERGE_SAFE
        )
        seeded.clear.refresh_from_db()
        assert seeded.clear.reviewed_sha == NEW
        critic = CriticVerdict.objects.latest_for(ticket=seeded.ticket, transition="merge", head_sha=NEW)
        assert critic is not None
        assert critic.grader_identity == "critic-agent-7"
        assert [item.slug for item in critic.item_verdicts()] == ["test_value"]
        seeded.ticket.refresh_from_db()
        assert recorded_anti_vacuity_attestation(seeded.ticket, NEW)["head_sha"] == NEW

    def test_a_second_call_changes_no_row_and_asks_the_forge_nothing(self) -> None:
        _seed()
        reader = _Reader(_lineage())
        first = _carry(reader)
        before = _counts()

        second = _carry(reader)

        assert second == first
        assert _counts() == before
        assert reader.calls == [(OLD, NEW)]

    def test_a_chain_of_main_merges_carries_from_the_newest_carried_head(self) -> None:
        seeded = _seed()
        _carry(_Reader(_lineage()), live_head=MIDDLE)

        row = _carry(_Reader(_lineage()), live_head=NEW)

        assert row is not None
        assert row.from_head == MIDDLE
        seeded.clear.refresh_from_db()
        assert seeded.clear.reviewed_sha == NEW

    def test_a_late_verdict_at_an_older_head_does_not_strand_a_newer_clearance(self) -> None:
        seeded = _seed()
        _carry(_Reader(_lineage()), live_head=MIDDLE)
        ReviewVerdict.record(
            pr_id=PR_ID,
            slug=SLUG,
            reviewed_sha=OLD,
            verdict="hold",
            reviewer_identity="reviewer-b",
            ticket=seeded.ticket,
        )

        row = _carry(_Reader(_lineage()))

        assert row is not None
        assert row.from_head == MIDDLE
        seeded.clear.refresh_from_db()
        assert seeded.clear.reviewed_sha == NEW

    def test_carry_does_not_release_a_review_lock_held_by_another_reviewer(self) -> None:
        _seed()
        lock = MRReviewLock.acquire(slug=SLUG, pr_id=PR_ID, holder="reviewer-in-flight")
        assert lock is not None

        _carry(_Reader(_lineage()))

        lock.refresh_from_db()
        assert lock.state == MRReviewLock.State.REVIEW_DISPATCHED
        assert lock.holder == "reviewer-in-flight"

    def test_the_expedite_waiver_attestation_is_not_moved_with_a_clearance(self) -> None:
        seeded = _seed()
        MergeClear.objects.filter(pk=seeded.clear.pk).update(
            expedite_authorizer="owner", local_ci_green_sha=OLD, gh_verify_result="pending"
        )

        _carry(_Reader(_lineage()))

        seeded.clear.refresh_from_db()
        assert seeded.clear.reviewed_sha == NEW
        assert seeded.clear.expedite_pending_waived_by("owner") is False


class TestDoesNotCarryDifferingPatch(TestCase):
    def test_a_different_patch_records_a_refusal_and_moves_nothing(self) -> None:
        seeded = _seed()
        before = _counts()

        row = _carry(_Reader(_lineage(new_patch_id="9" * 40)))

        assert row is not None
        assert (row.outcome, row.reason) == (HeadCarryOver.Outcome.REFUSED, "patch_differs")
        assert _counts() == (before[0], before[1], before[2] + 1)
        seeded.clear.refresh_from_db()
        assert seeded.clear.reviewed_sha == OLD
        assert (
            ReviewVerdict.objects.effective_state_at(slug=SLUG, pr_id=PR_ID, head_sha=NEW)
            is HeadVerdictState.NO_MERGE_SAFE
        )
        assert recorded_anti_vacuity_attestation(seeded.ticket, NEW) == {}

    def test_an_empty_patch_id_never_counts_as_identical(self) -> None:
        _seed()

        row = _carry(_Reader(_lineage(old_patch_id="", new_patch_id="")))

        assert row is not None
        assert row.reason == "patch_differs"

    def test_a_non_merge_commit_added_since_the_reviewed_head_is_refused_even_with_equal_patch_ids(self) -> None:
        _seed()

        row = _carry(_Reader(_lineage(added_all_merges=False)))

        assert row is not None
        assert row.reason == "added_commits_not_merges"
        assert not ReviewVerdict.objects.filter(reviewed_sha=NEW).exists()

    def test_a_rewritten_history_is_refused_as_not_a_descendant(self) -> None:
        _seed()

        row = _carry(_Reader(_lineage(descends=False)))

        assert row is not None
        assert row.reason == "not_descendant"

    def test_a_forge_that_could_not_answer_completely_names_its_reason(self) -> None:
        _seed()

        row = _carry(_Reader(_lineage(refusal="history_truncated")))

        assert row is not None
        assert row.reason == "history_truncated"
        assert not ReviewVerdict.objects.filter(reviewed_sha=NEW).exists()

    def test_a_host_the_reader_does_not_support_is_refused_once_without_a_forge_call(self) -> None:
        _seed()
        reader = _Reader(_lineage())

        row = _carry(reader, host_kind="gitlab")

        assert row is not None
        assert row.reason == "unsupported_host"
        assert reader.calls == []

    def test_a_verdict_that_cannot_be_carried_rolls_the_whole_carry_back(self) -> None:
        seeded = _seed()
        msg = "unwaivable"

        with patch.object(ReviewVerdict, "carry_forward", side_effect=ReviewVerdictError(msg)):
            row = _carry(_Reader(_lineage()))

        assert row is not None
        assert row.reason == "verdict_not_carryable"
        seeded.clear.refresh_from_db()
        assert seeded.clear.reviewed_sha == OLD
        assert not CriticVerdict.objects.filter(head_sha=NEW).exists()


class TestHoldStandsOnlyForItsPatch(TestCase):
    def _hold_at_old(self) -> None:
        _seed()
        ReviewVerdict.record(
            pr_id=PR_ID,
            slug=SLUG,
            reviewed_sha=OLD,
            verdict="hold",
            reviewer_identity="reviewer-b",
            gh_verify_result="failed",
        )

    def test_a_hold_follows_a_main_merge_head_with_the_same_patch(self) -> None:
        self._hold_at_old()

        _carry(_Reader(_lineage()))

        assert ReviewVerdict.objects.effective_state_at(slug=SLUG, pr_id=PR_ID, head_sha=NEW) is HeadVerdictState.HOLD

    def test_a_hold_is_absent_at_a_fix_head_whose_patch_differs(self) -> None:
        self._hold_at_old()

        _carry(_Reader(_lineage(new_patch_id="9" * 40)))

        state = ReviewVerdict.objects.effective_state_at(slug=SLUG, pr_id=PR_ID, head_sha=NEW)
        assert state is HeadVerdictState.NO_MERGE_SAFE


class TestUnreadableFailsLoud(TestCase):
    def test_an_unreadable_lineage_writes_nothing_warns_and_is_retried(self) -> None:
        _seed()
        reader = _Reader(None)

        with self.assertLogs("teatree.core.merge.head_carry_over", level=logging.WARNING) as logs:
            first = _carry(reader)
        second = _carry(reader)

        assert first is None
        assert second is None
        assert not HeadCarryOver.objects.exists()
        assert reader.calls == [(OLD, NEW), (OLD, NEW)]
        message = logs.output[0]
        assert f"{SLUG}#{PR_ID}" in message
        assert OLD[:8] in message
        assert NEW[:8] in message


class TestCandidateSelection(TestCase):
    def test_a_pr_with_no_prior_record_is_left_alone_without_a_forge_call(self) -> None:
        reader = _Reader(_lineage())

        assert _carry(reader) is None
        assert reader.calls == []

    def test_a_branch_update_without_review_evidence_records_patch_only(self) -> None:
        BranchUpdateAttempt.objects.create(slug=SLUG, pr_id=PR_ID, head_sha=OLD)
        reader = _Reader(_lineage())

        row = _carry(reader)
        assert row is not None
        assert (row.outcome, row.reason) == (HeadCarryOver.Outcome.REFUSED, "no_records")
        assert reader.calls == [(OLD, NEW)]
        assert HeadCarryOver.objects.left_patch_unchanged_by_update(slug=SLUG, pr_id=PR_ID, head=NEW)

    def test_a_consumed_clearance_alone_is_no_candidate(self) -> None:
        clear = _clear(_ticket(), OLD, slug=SLUG)
        MergeClear.objects.filter(pk=clear.pk).update(consumed_at=clear.issued_at)
        reader = _Reader(_lineage())

        assert _carry(reader) is None
        assert reader.calls == []

    def test_the_newest_recorded_head_is_the_one_compared(self) -> None:
        seeded = _seed(OLD)
        ReviewVerdict.record(
            pr_id=PR_ID,
            slug=SLUG,
            reviewed_sha=MIDDLE,
            verdict="hold",
            reviewer_identity="reviewer-b",
            ticket=seeded.ticket,
        )
        reader = _Reader(_lineage())

        _carry(reader)

        assert reader.calls == [(MIDDLE, NEW)]

    def test_a_head_holding_a_record_outranks_a_newer_head_only_a_branch_update_names(self) -> None:
        _seed(OLD)
        BranchUpdateAttempt.objects.create(slug=SLUG, pr_id=PR_ID, head_sha=MIDDLE)
        reader = _Reader(_lineage())

        _carry(reader)

        assert reader.calls == [(OLD, NEW)]

    def test_a_critic_row_of_a_ticket_with_two_pull_requests_names_no_head_of_this_one(self) -> None:
        seeded = _seed(OLD)
        PullRequestFactory(
            ticket=seeded.ticket, repo="souliane/other", iid="7", url="https://github.com/souliane/other/pull/7"
        )
        CriticVerdict.record_from_envelope(
            ticket=seeded.ticket,
            transition="merge",
            head_sha=MIDDLE,
            envelope={
                "grader_identity": "critic-agent-7",
                "items": [{"slug": "test_value", "status": "pass", "citation": "tests/x.py::test_y was read"}],
            },
        )
        reader = _Reader(_lineage())

        _carry(reader)

        assert reader.calls == [(OLD, NEW)]

    def test_a_slug_spelled_in_another_case_is_the_same_pull_request(self) -> None:
        seeded = _seed(slug="Souliane/Teatree")

        row = _carry(_Reader(_lineage()))

        assert row is not None
        assert row.outcome == HeadCarryOver.Outcome.CARRIED
        seeded.clear.refresh_from_db()
        assert seeded.clear.reviewed_sha == NEW


_PR_URL = f"https://github.com/{SLUG}/pull/{PR_ID}"
_NIT = NitPoint("src/example.py", 8, "Rename helper")
_NIT_RECEIPT = {"issue_url": f"https://github.com/{SLUG}/issues/10", "thread_id": "thread-1"}


def _grade_rubric(ticket: Ticket, head: str) -> None:
    criterion = RubricCriterionFactory.create(rubric=RubricFactory.create(ticket=ticket), ordinal=0)
    criterion.record_grade(
        status="pass",
        grader_identity="cold-reviewer",
        reviewed_sha=head,
        rationale="tests/x.py::test_y proves it",
    )


def _publish_nit_receipt(ticket: Ticket, head: str, *, status: str) -> None:
    receipts = {nit_point_marker(_PR_URL, head, _NIT): {"head_sha": head, **_NIT_RECEIPT}}
    published = {"nit_lane_receipts": receipts} if status == "published" else {}
    ticket.merge_extra(merge_into_dicts={**published, "nit_publication": {head: {"status": status}}})


def _critic_with_a_nit(ticket: Ticket, head: str) -> None:
    nit = {"kind": "nit", "summary": _NIT.summary, "file": _NIT.file, "line": _NIT.line}
    CriticVerdict.record_from_envelope(
        ticket=ticket,
        transition="merge",
        head_sha=head,
        envelope={
            "grader_identity": "critic-agent-7",
            "items": [
                {"slug": "test_value", "status": "pass", "citation": "tests/x.py::test_y was read"},
                {"slug": "cleanliness", "status": "fail", "citation": "src/example.py:8", "points": [nit]},
            ],
        },
    )


class TestACarriedClearanceStillMerges(TestCase):
    """The merge gates beside the clearance bind to the live head too; the carry moves their evidence with it."""

    def test_rubric_grades_follow_the_patch_to_the_new_head(self) -> None:
        seeded = _seed()
        _grade_rubric(seeded.ticket, OLD)
        check_rubric_satisfied(seeded.ticket, OLD, transition="merge")
        with pytest.raises(RubricNotSatisfiedError, match="stale"):
            check_rubric_satisfied(seeded.ticket, NEW, transition="merge")

        row = _carry(_Reader(_lineage()))

        assert row is not None
        assert row.carried["rubric_grades"] == 1
        check_rubric_satisfied(seeded.ticket, NEW, transition="merge")

    def test_a_criterion_already_graded_at_the_new_head_keeps_its_own_grade(self) -> None:
        ticket = _seed().ticket
        rubric = RubricFactory.create(ticket=ticket)
        first, second = (RubricCriterionFactory.create(rubric=rubric, ordinal=n) for n in range(2))
        for criterion in (first, second):
            criterion.record_grade(status="pass", grader_identity="cold-reviewer", reviewed_sha=OLD, rationale="at old")
        first.record_grade(status="pass", grader_identity="reviewer-c", reviewed_sha=NEW, rationale="at new")

        row = _carry(_Reader(_lineage()))

        assert row is not None
        assert row.carried["rubric_grades"] == 1
        grades = Ticket.objects.get(pk=ticket.pk).extra["rubric_grades_by_head"][NEW]
        assert {key: grade["rationale"] for key, grade in grades.items()} == {
            str(first.pk): "at new",
            str(second.pk): "at old",
        }

    def test_nothing_is_added_when_no_grade_was_recorded_at_the_old_head(self) -> None:
        ticket = _seed().ticket
        RubricCriterionFactory.create(rubric=RubricFactory.create(ticket=ticket), ordinal=0).record_grade(
            status="pass", grader_identity="cold-reviewer", reviewed_sha=MIDDLE, rationale="at middle"
        )

        row = _carry(_Reader(_lineage()))

        assert row is not None
        assert row.carried["rubric_grades"] == 0

    def test_a_refused_carry_leaves_the_rubric_graded_at_the_old_head_only(self) -> None:
        seeded = _seed()
        _grade_rubric(seeded.ticket, OLD)

        _carry(_Reader(_lineage(new_patch_id="9" * 40)))

        with pytest.raises(RubricNotSatisfiedError, match="stale"):
            check_rubric_satisfied(seeded.ticket, NEW, transition="merge")

    def test_a_published_nit_receipt_follows_the_patch_to_the_new_head(self) -> None:
        ticket = _ticket()
        _critic_with_a_nit(ticket, OLD)
        _publish_nit_receipt(ticket, OLD, status="published")
        check_merge_quality_verdict(ticket, OLD)

        row = _carry(_Reader(_lineage()))

        assert row is not None
        assert row.carried["nit_receipts"] == 1
        ticket.refresh_from_db()
        check_merge_quality_verdict(ticket, NEW)

    def test_a_nit_of_a_carried_review_verdict_keeps_its_receipt(self) -> None:
        ticket = _ticket()
        ReviewVerdict.record(
            pr_id=PR_ID,
            slug=SLUG,
            reviewed_sha=OLD,
            verdict="merge_safe",
            reviewer_identity="cold-reviewer",
            findings=[Finding(severity="low", summary=_NIT.summary, file=_NIT.file, line=_NIT.line, kind="nit")],
            ticket=ticket,
        )
        _publish_nit_receipt(ticket, OLD, status="published")

        _carry(_Reader(_lineage()))

        ticket.refresh_from_db()
        marker = nit_point_marker(_PR_URL, NEW, _NIT)
        assert ticket.extra["nit_lane_receipts"][marker]["head_sha"] == NEW

    def test_a_failed_publication_is_retried_at_the_new_head(self) -> None:
        ticket = _ticket()
        _critic_with_a_nit(ticket, OLD)
        _publish_nit_receipt(ticket, OLD, status="failed")

        _carry(_Reader(_lineage()))

        ticket.refresh_from_db()
        with (
            patch("teatree.core.gates.merge_quality_gate.publish_nits") as publish,
            pytest.raises(MergeQualityVerdictError, match="NIT publication receipt is missing"),
        ):
            check_merge_quality_verdict(ticket, NEW)
        publish.assert_called_once_with(ticket, _PR_URL, NEW, (_NIT,))


class TestTheCarryNeverClaimsWhatItDidNotMove(TestCase):
    def test_a_head_known_only_from_a_branch_update_carries_no_review_verdict(self) -> None:
        BranchUpdateAttempt.objects.create(slug=SLUG, pr_id=PR_ID, head_sha=OLD)

        row = _carry(_Reader(_lineage()))

        assert row is not None
        assert (row.outcome, row.reason) == (HeadCarryOver.Outcome.REFUSED, "no_records")
        assert not row.reviewed_by_carry
        assert HeadCarryOver.objects.left_patch_unchanged_by_update(slug=SLUG, pr_id=PR_ID, head=NEW)

    def test_a_head_that_received_a_verdict_is_reviewed_by_the_carry(self) -> None:
        _seed()

        row = _carry(_Reader(_lineage()))

        assert row is not None
        assert row.reviewed_by_carry

    def test_a_hold_follows_its_patch_although_another_reviewer_passed_the_new_head(self) -> None:
        _seed()
        ReviewVerdict.record(pr_id=PR_ID, slug=SLUG, reviewed_sha=OLD, verdict="hold", reviewer_identity="reviewer-b")
        ReviewVerdict.record(
            pr_id=PR_ID, slug=SLUG, reviewed_sha=NEW, verdict="merge_safe", reviewer_identity="reviewer-c"
        )

        _carry(_Reader(_lineage()))

        assert ReviewVerdict.objects.effective_state_at(slug=SLUG, pr_id=PR_ID, head_sha=NEW) is HeadVerdictState.HOLD
