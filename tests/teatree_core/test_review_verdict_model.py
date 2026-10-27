"""``ReviewVerdict`` guarded factory + query/staleness helpers.

The model is the persisted read-side sibling of ``MergeClear``: a recorded
verdict keyed by ``(slug, pr_id, reviewed_sha, reviewer, verdict)`` so a later ``review status``
lookup can answer "safe to approve at the current head?" without re-deriving a
cold review. These cover the issue-time refusals, the structured-findings
round-trip, and the staleness / safe-to-approve logic the status command reads.
"""

import pytest
from django.test import TestCase

from teatree.core.modelkit.forge_readability import HEAD_SHA_UNREADABLE
from teatree.core.models import (
    AutoReviewDispatch,
    ClearRequest,
    CodexReviewMarker,
    Finding,
    MergeClear,
    MRReviewLock,
    ReviewHoldDisposition,
    ReviewVerdict,
    ReviewVerdictError,
    Ticket,
    normalize_reviewer_identity,
)
from teatree.core.models.review_verdict import HeadVerdictState
from teatree.loop.scanners.pr_findings import RecordedVerdictReader

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

_SHA = "a" * 40
_OTHER_SHA = "b" * 40


class TestRecordContract(TestCase):
    def test_unverified_evidence_gap_is_recorded_without_merge_authority(self) -> None:
        dispatch = AutoReviewDispatch.objects.create(slug="souliane/teatree", pr_id=42, head_sha=_SHA)
        marker = CodexReviewMarker.objects.create(slug="souliane/teatree", pr_id=42, head_sha=_SHA)
        verdict = ReviewVerdict.record(
            pr_id=42,
            slug="souliane/teatree",
            reviewed_sha=_SHA,
            verdict="unverified",
            reviewer_identity="cold-reviewer",
            gh_verify_result="pending",
            evidence_gaps=["ci_unreadable"],
        )

        assert verdict.evidence_gaps == ["ci_unreadable"]
        assert not verdict.stands_as_hold()
        dispatch.refresh_from_db()
        marker.refresh_from_db()
        assert dispatch.state == AutoReviewDispatch.State.UNVERIFIED
        assert marker.state == CodexReviewMarker.State.UNVERIFIED
        assert AutoReviewDispatch.enqueue(slug="souliane/teatree", pr_id=42, head_sha=_SHA) is not None
        assert (
            ReviewVerdict.objects.effective_state_at(slug="souliane/teatree", pr_id=42, head_sha=_SHA)
            is HeadVerdictState.NO_MERGE_SAFE
        )

    def test_unverified_does_not_hide_a_recorded_same_head_pass(self) -> None:
        passed = ReviewVerdict.record(
            pr_id=42,
            slug="souliane/teatree",
            reviewed_sha=_SHA,
            verdict="merge_safe",
            reviewer_identity="first-reviewer",
        )
        ReviewVerdict.record(
            pr_id=42,
            slug="souliane/teatree",
            reviewed_sha=_SHA,
            verdict="unverified",
            reviewer_identity="second-reviewer",
            evidence_gaps=["tests_unavailable"],
        )

        assert ReviewVerdict.objects.speaking_verdict_at(slug="souliane/teatree", pr_id=42, head_sha=_SHA) == passed

    def test_independent_cited_pass_can_disposition_a_same_head_hold(self) -> None:
        hold = ReviewVerdict.record(
            pr_id=42,
            slug="souliane/teatree",
            reviewed_sha=_SHA,
            verdict="hold",
            reviewer_identity="first-reviewer",
            findings=[
                Finding(
                    severity="blocker",
                    summary="CI result is stale",
                    file="a.py",
                    line=1,
                    basis="executed",
                    reproduction="checked the run at this head: stale",
                )
            ],
        )
        clearing = ReviewVerdict.record(
            pr_id=42,
            slug="souliane/teatree",
            reviewed_sha=_SHA,
            verdict="merge_safe",
            reviewer_identity="independent-reviewer",
        )

        disposition = ReviewHoldDisposition.record(
            hold=hold,
            clearing_verdict=clearing,
            points=[{"index": 0, "classification": "disproved_premise", "citation": "CI run at this head is green"}],
        )

        assert (
            ReviewVerdict.objects.effective_state_at(slug="souliane/teatree", pr_id=42, head_sha=_SHA)
            is HeadVerdictState.MERGE_SAFE
        )
        assert ReviewVerdict.objects.unreconciled_holds_at(slug="souliane/teatree", pr_id=42, head_sha=_SHA) == []
        clear = MergeClear.issue(
            ClearRequest(pr_id=42, slug="souliane/teatree", reviewed_sha=_SHA, reviewer_identity="independent-reviewer")
        )
        assert clear.hold_disposition_ids == [disposition.pk]

        ReviewVerdict.record(
            pr_id=42,
            slug="souliane/teatree",
            reviewed_sha=_SHA,
            verdict="hold",
            reviewer_identity="first-reviewer",
            findings=[
                Finding(
                    severity="major",
                    summary="a second issue",
                    file="b.py",
                    line=2,
                    reproduction="pytest test_second.py: failed",
                )
            ],
        )

        assert (
            ReviewVerdict.objects.effective_state_at(slug="souliane/teatree", pr_id=42, head_sha=_SHA)
            is HeadVerdictState.HOLD
        )

    def test_direct_recorder_retries_a_venue_only_hold(self) -> None:
        with pytest.raises(ReviewVerdictError, match="retry"):
            ReviewVerdict.record(
                pr_id=42,
                slug="souliane/teatree",
                reviewed_sha=_SHA,
                verdict="hold",
                reviewer_identity="cold-reviewer",
                gh_verify_result="green",
                findings=[
                    Finding(
                        severity="blocker",
                        kind="blocking",
                        basis="venue_limit",
                        summary="CI rollup could not be read in this venue",
                    )
                ],
            )

        assert not ReviewVerdict.objects.exists()

    def test_venue_only_pending_check_retries_without_recording_a_hold(self) -> None:
        with pytest.raises(ReviewVerdictError, match="retry"):
            ReviewVerdict.record(
                pr_id=42,
                slug="souliane/teatree",
                reviewed_sha=_SHA,
                verdict="hold",
                reviewer_identity="cold-reviewer",
                gh_verify_result="pending",
                findings=[
                    Finding(
                        severity="blocker",
                        kind="blocking",
                        basis="venue_limit",
                        summary="verify-gates could not run in this venue",
                    )
                ],
            )

        assert not ReviewVerdict.objects.exists()

    def test_failed_required_check_still_holds_with_a_venue_note(self) -> None:
        verdict = ReviewVerdict.record(
            pr_id=42,
            slug="souliane/teatree",
            reviewed_sha=_SHA,
            verdict="hold",
            reviewer_identity="cold-reviewer",
            gh_verify_result="failed",
            findings=[
                Finding(
                    severity="blocker",
                    kind="blocking",
                    basis="venue_limit",
                    summary="CI rollup could not be read in this venue",
                )
            ],
        )

        assert verdict.verdict == ReviewVerdict.Verdict.HOLD
        assert verdict.structured_findings[0].kind == "note"

    def test_records_a_merge_safe_verdict_with_structured_findings(self) -> None:
        verdict = ReviewVerdict.record(
            pr_id=42,
            slug="souliane/teatree",
            reviewed_sha=_SHA,
            verdict="merge_safe",
            reviewer_identity="cold-reviewer",
            findings=[Finding(severity="minor", kind="nit", summary="rename x", file="a.py", line=12)],
        )
        assert verdict.is_merge_safe()
        assert verdict.reviewed_sha == _SHA
        only = verdict.structured_findings[0]
        assert (only.severity, only.summary, only.location()) == ("minor", "rename x", "a.py:12")
        assert only.kind == "nit"

    def test_an_explicit_blocking_kind_survives_the_finding_round_trip(self) -> None:
        finding = Finding(
            severity="minor",
            summary="false statement",
            kind="blocking",
            reproduction="pytest test_statement.py: failed",
        )

        assert Finding.from_dict(finding.as_dict()).kind == "blocking"

    def test_records_a_hold_verdict_on_failed_checks(self) -> None:
        verdict = ReviewVerdict.record(
            pr_id=42,
            slug="souliane/teatree",
            reviewed_sha=_SHA,
            verdict="hold",
            reviewer_identity="cold-reviewer",
            gh_verify_result="failed",
            findings=[Finding(severity="blocker", summary="race window")],
        )
        assert not verdict.is_merge_safe()
        assert verdict.structured_findings[0].location() == "(MR-level)"

    def test_merge_safe_on_pending_checks_without_expedite_is_refused(self) -> None:
        # FIX-EXPEDITE split: merge_safe on PENDING checks requires the expedite waiver.
        with pytest.raises(ReviewVerdictError, match="requires the expedite waiver"):
            ReviewVerdict.record(
                pr_id=1,
                slug="souliane/teatree",
                reviewed_sha=_SHA,
                verdict="merge_safe",
                reviewer_identity="cold-reviewer",
                gh_verify_result="pending",
            )
        assert ReviewVerdict.objects.count() == 0

    def test_merge_safe_on_failed_checks_is_refused(self) -> None:
        # FIX-EXPEDITE: a merge_safe verdict can never carry a FAILED result — even expedited.
        with pytest.raises(ReviewVerdictError, match="never carry gh_verify_result=failed"):
            ReviewVerdict.record(
                pr_id=2,
                slug="souliane/teatree",
                reviewed_sha=_SHA,
                verdict="merge_safe",
                reviewer_identity="cold-reviewer",
                gh_verify_result="failed",
            )
        assert ReviewVerdict.objects.count() == 0

    def test_abbreviated_sha_is_refused(self) -> None:
        with pytest.raises(ReviewVerdictError, match="40-char"):
            ReviewVerdict.record(
                pr_id=1,
                slug="souliane/teatree",
                reviewed_sha="abc1234",
                verdict="merge_safe",
                reviewer_identity="cold-reviewer",
            )

    def test_empty_reviewer_identity_is_refused(self) -> None:
        with pytest.raises(ReviewVerdictError, match="reviewer_identity"):
            ReviewVerdict.record(
                pr_id=1,
                slug="souliane/teatree",
                reviewed_sha=_SHA,
                verdict="merge_safe",
                reviewer_identity="   ",
            )

    def test_unknown_verdict_is_refused(self) -> None:
        with pytest.raises(ReviewVerdictError, match="verdict"):
            ReviewVerdict.record(
                pr_id=1,
                slug="souliane/teatree",
                reviewed_sha=_SHA,
                verdict="maybe",
                reviewer_identity="cold-reviewer",
            )

    def test_maker_or_loop_reviewer_identity_is_refused(self) -> None:
        # A verdict records an INDEPENDENT cold review; a maker/coding-agent/
        # loop identity is a self-attestation and is refused, mirroring
        # MergeClear.issue (§17.8 clause 3) so the read-side safe-to-approve
        # check can never be satisfied by the author rubber-stamping itself.
        for identity in ("maker", "coding-agent", "merge-loop", "maker:opus", "loop"):
            with (
                self.subTest(identity=identity),
                pytest.raises(ReviewVerdictError, match="maker/coding-agent/loop"),
            ):
                ReviewVerdict.record(
                    pr_id=1,
                    slug="souliane/teatree",
                    reviewed_sha=_SHA,
                    verdict="merge_safe",
                    reviewer_identity=identity,
                )

    def test_unknown_blast_class_is_refused(self) -> None:
        with pytest.raises(ReviewVerdictError, match="blast_class"):
            ReviewVerdict.record(
                pr_id=1,
                slug="souliane/teatree",
                reviewed_sha=_SHA,
                verdict="hold",
                reviewer_identity="cold-reviewer",
                gh_verify_result="failed",
                blast_class="catastrophic",
            )

    def test_unknown_gh_verify_result_is_refused(self) -> None:
        with pytest.raises(ReviewVerdictError, match="gh_verify_result"):
            ReviewVerdict.record(
                pr_id=1,
                slug="souliane/teatree",
                reviewed_sha=_SHA,
                verdict="hold",
                reviewer_identity="cold-reviewer",
                gh_verify_result="exploded",
            )

    def test_sha_and_slug_are_normalised(self) -> None:
        verdict = ReviewVerdict.record(
            pr_id=1,
            slug="  souliane/teatree  ",
            reviewed_sha=_SHA.upper(),
            verdict="MERGE_SAFE",
            reviewer_identity="cold-reviewer",
        )
        assert verdict.slug == "souliane/teatree"
        assert verdict.reviewed_sha == _SHA
        assert verdict.verdict == ReviewVerdict.Verdict.MERGE_SAFE


class TestCarryForward(TestCase):
    """The reusable carry-forward primitive (re-record at a new tree, waiver-preserving)."""

    def test_carry_forward_copies_every_snapshot_field_to_the_new_tree(self) -> None:
        original = ReviewVerdict.record(
            pr_id=7,
            slug="souliane/teatree",
            reviewed_sha=_SHA,
            verdict="merge_safe",
            reviewer_identity="cold-reviewer",
            blast_class=MergeClear.BlastClass.SUBSTRATE,
            findings=[Finding(severity="nit", summary="rename x", file="a.py", line=3)],
        )
        carried = original.carry_forward(reviewed_sha=_OTHER_SHA)
        assert carried.pk != original.pk
        assert carried.reviewed_sha == _OTHER_SHA
        assert carried.reviewer_identity == "cold-reviewer"
        assert carried.blast_class == MergeClear.BlastClass.SUBSTRATE
        assert carried.gh_verify_result == MergeClear.VerifyResult.GREEN
        assert carried.structured_findings[0].location() == "a.py:3"

    def test_carry_forward_preserves_the_expedite_waiver(self) -> None:
        # A PENDING merge_safe verdict carries forward WITHOUT re-tripping the
        # expedite refusal — the waiver is re-passed from the source snapshot.
        original = ReviewVerdict.record(
            pr_id=8,
            slug="souliane/teatree",
            reviewed_sha=_SHA,
            verdict="merge_safe",
            reviewer_identity="cold-reviewer",
            gh_verify_result="pending",
            expedited=True,
        )
        carried = original.carry_forward(reviewed_sha=_OTHER_SHA)
        assert carried.is_merge_safe()
        assert carried.gh_verify_result == MergeClear.VerifyResult.PENDING

    def test_carry_forward_surfaces_a_typed_error_on_an_unwaivable_verdict(self) -> None:
        # A genuinely-unwaivable source (merge_safe on FAILED checks — a shape
        # record() forbids) surfaces the typed ReviewVerdictError, never a crash,
        # and writes no row.
        unwaivable = ReviewVerdict(
            pr_id=9,
            slug="souliane/teatree",
            reviewed_sha=_SHA,
            verdict=ReviewVerdict.Verdict.MERGE_SAFE,
            reviewer_identity="cold-reviewer",
            blast_class=MergeClear.BlastClass.LOGIC,
            gh_verify_result=MergeClear.VerifyResult.FAILED,
        )
        with pytest.raises(ReviewVerdictError, match="never carry gh_verify_result=failed"):
            unwaivable.carry_forward(reviewed_sha=_OTHER_SHA)
        assert not ReviewVerdict.objects.filter(reviewed_sha=_OTHER_SHA).exists()


class TestReviewerIdentityIdempotency(TestCase):
    """The (slug, pr, sha, normalized-identity, verdict) idempotency contract."""

    def test_normalize_collapses_case_and_whitespace_only(self) -> None:
        assert normalize_reviewer_identity("  Codex  Reviewer ") == normalize_reviewer_identity("codex reviewer")
        # Genuinely distinct identities stay distinct — no role-prefix stripping.
        assert normalize_reviewer_identity("t3:reviewer") != normalize_reviewer_identity("reviewer")

    def test_re_review_of_one_head_by_one_identity_keeps_both_verdicts(self) -> None:
        first = ReviewVerdict.record(
            pr_id=1,
            slug="souliane/teatree",
            reviewed_sha=_SHA,
            verdict="hold",
            reviewer_identity="Cold-Reviewer",
            gh_verify_result="failed",
        )
        second = ReviewVerdict.record(
            pr_id=1,
            slug="souliane/teatree",
            reviewed_sha=_SHA,
            verdict="merge_safe",
            reviewer_identity="cold-reviewer ",  # same identity, different spelling
        )
        assert ReviewVerdict.objects.for_pr("souliane/teatree", 1).count() == 2
        assert second.pk != first.pk
        assert first.stands_as_hold()
        assert (
            ReviewVerdict.objects.effective_state_at(slug="souliane/teatree", pr_id=1, head_sha=_SHA)
            is HeadVerdictState.HOLD
        )

    def test_distinct_identities_at_one_head_coexist(self) -> None:
        ReviewVerdict.record(
            pr_id=1,
            slug="souliane/teatree",
            reviewed_sha=_SHA,
            verdict="hold",
            reviewer_identity="reviewer-a",
            gh_verify_result="failed",
        )
        ReviewVerdict.record(
            pr_id=1,
            slug="souliane/teatree",
            reviewed_sha=_SHA,
            verdict="merge_safe",
            reviewer_identity="reviewer-b",
        )
        assert ReviewVerdict.objects.for_pr("souliane/teatree", 1).count() == 2

    def test_same_verdict_re_record_keeps_one_row(self) -> None:
        first = ReviewVerdict.record(
            pr_id=1,
            slug="souliane/teatree",
            reviewed_sha=_SHA,
            verdict="hold",
            reviewer_identity="Cold-Reviewer",
        )
        second = ReviewVerdict.record(
            pr_id=1,
            slug="Souliane/TeaTree",
            reviewed_sha=_SHA,
            verdict="hold",
            reviewer_identity="cold-reviewer ",
        )
        assert second.pk == first.pk
        assert ReviewVerdict.objects.for_pr("souliane/teatree", 1).count() == 1

    def test_a_differently_cased_slug_is_the_same_forge_repo(self) -> None:
        # Forge slugs are case-insensitive: an exact-match lookup hid the later HOLD
        # from the gate querying the other spelling, so the merge it was recorded
        # against went ahead on the earlier merge_safe alone.
        ReviewVerdict.record(
            pr_id=1,
            slug="souliane/teatree",
            reviewed_sha=_SHA,
            verdict="merge_safe",
            reviewer_identity="reviewer-b",
        )
        ReviewVerdict.record(
            pr_id=1,
            slug="Souliane/TeaTree",
            reviewed_sha=_SHA,
            verdict="hold",
            reviewer_identity="reviewer-a",
            gh_verify_result="failed",
        )

        assert ReviewVerdict.objects.for_pr("souliane/teatree", 1).count() == 2
        assert (
            ReviewVerdict.objects.effective_state_at(slug="souliane/teatree", pr_id=1, head_sha=_SHA)
            is HeadVerdictState.HOLD
        )

    def test_a_differently_cased_slug_keeps_the_hold(self) -> None:
        first = ReviewVerdict.record(
            pr_id=1,
            slug="Souliane/TeaTree",
            reviewed_sha=_SHA,
            verdict="hold",
            reviewer_identity="reviewer-a",
            gh_verify_result="failed",
        )
        second = ReviewVerdict.record(
            pr_id=1,
            slug="souliane/teatree",
            reviewed_sha=_SHA,
            verdict="merge_safe",
            reviewer_identity="reviewer-a",
        )

        assert second.pk != first.pk
        assert ReviewVerdict.objects.for_pr("souliane/teatree", 1).count() == 2
        assert (
            ReviewVerdict.objects.effective_state_at(slug="souliane/teatree", pr_id=1, head_sha=_SHA)
            is HeadVerdictState.HOLD
        )

    def test_a_moved_head_records_a_fresh_row(self) -> None:
        ReviewVerdict.record(
            pr_id=1,
            slug="souliane/teatree",
            reviewed_sha=_SHA,
            verdict="merge_safe",
            reviewer_identity="reviewer-a",
        )
        ReviewVerdict.record(
            pr_id=1,
            slug="souliane/teatree",
            reviewed_sha=_OTHER_SHA,
            verdict="merge_safe",
            reviewer_identity="reviewer-a",
        )
        assert ReviewVerdict.objects.for_pr("souliane/teatree", 1).count() == 2


class TestQueryHelpers(TestCase):
    def test_latest_for_pr_prefers_standing_hold_at_newest_head(self) -> None:
        held = ReviewVerdict.record(
            pr_id=7,
            slug="souliane/teatree",
            reviewed_sha=_SHA,
            verdict="hold",
            reviewer_identity="cold-reviewer-1",
        )
        ReviewVerdict.record(
            pr_id=7,
            slug="souliane/teatree",
            reviewed_sha=_SHA,
            verdict="merge_safe",
            reviewer_identity="cold-reviewer-2",
        )
        assert ReviewVerdict.objects.latest_for_pr("souliane/teatree", 7) == held
        assert (
            RecordedVerdictReader().state_for(url="https://github.com/souliane/teatree/pull/7", head_sha=_SHA)
            is HeadVerdictState.HOLD
        )

    def test_latest_for_pr_returns_the_freshest_verdict(self) -> None:
        ReviewVerdict.record(
            pr_id=7,
            slug="souliane/teatree",
            reviewed_sha=_OTHER_SHA,
            verdict="hold",
            reviewer_identity="cold-reviewer-1",
            gh_verify_result="failed",
        )
        newest = ReviewVerdict.record(
            pr_id=7,
            slug="souliane/teatree",
            reviewed_sha=_SHA,
            verdict="merge_safe",
            reviewer_identity="cold-reviewer-2",
        )
        assert ReviewVerdict.objects.latest_for_pr("souliane/teatree", 7) == newest

    def test_latest_for_pr_is_none_when_nothing_recorded(self) -> None:
        assert ReviewVerdict.objects.latest_for_pr("souliane/teatree", 999) is None

    def test_for_pr_is_scoped_by_slug_and_pr(self) -> None:
        ReviewVerdict.record(
            pr_id=7,
            slug="souliane/teatree",
            reviewed_sha=_SHA,
            verdict="merge_safe",
            reviewer_identity="cold-reviewer",
        )
        ReviewVerdict.record(
            pr_id=7,
            slug="other/repo",
            reviewed_sha=_SHA,
            verdict="merge_safe",
            reviewer_identity="cold-reviewer",
        )
        assert ReviewVerdict.objects.for_pr("souliane/teatree", 7).count() == 1


class TestUnreconciledHoldsAt(TestCase):
    """Every HOLD at the reviewed head remains active."""

    def _record(self, *, verdict: str, reviewer: str, sha: str = _SHA) -> ReviewVerdict:
        return ReviewVerdict.record(
            pr_id=4380,
            slug="souliane/teatree",
            reviewed_sha=sha,
            verdict=verdict,
            reviewer_identity=reviewer,
        )

    def _holds(self, *, slug: str = "souliane/teatree", head: str = _SHA) -> list[ReviewVerdict]:
        return ReviewVerdict.objects.unreconciled_holds_at(slug=slug, pr_id=4380, head_sha=head)

    def test_hold_survives_a_later_merge_safe_from_another_reviewer(self) -> None:
        held = self._record(verdict="hold", reviewer="cold-reviewer-a")
        self._record(verdict="merge_safe", reviewer="cold-reviewer-b")

        assert (
            ReviewVerdict.objects.effective_state_at(slug="souliane/teatree", pr_id=4380, head_sha=_SHA)
            is HeadVerdictState.HOLD
        )
        assert [row.pk for row in self._holds()] == [held.pk]

    def test_same_reviewer_pass_leaves_hold_standing(self) -> None:
        self._record(verdict="hold", reviewer="cold-reviewer-a")
        self._record(verdict="merge_safe", reviewer="cold-reviewer-a")

        assert len(self._holds()) == 1

    def test_stale_hold_is_not_returned_for_the_moved_head(self) -> None:
        self._record(verdict="hold", reviewer="cold-reviewer-a", sha=_OTHER_SHA)

        assert self._holds(head=_SHA) == []

    def test_slug_is_matched_case_insensitively(self) -> None:
        # Via ``for_pr``'s ``__iexact`` — an exact-match filter is what once made a
        # verdict invisible to the merge gate, which is this defect's own shape.
        held = self._record(verdict="hold", reviewer="cold-reviewer-a")

        assert [row.pk for row in self._holds(slug="Souliane/TeaTree")] == [held.pk]

    def test_duplicate_hold_preserves_first_provenance_and_every_finding(self) -> None:
        first_ticket = Ticket.objects.create(overlay="t3-teatree")
        second_ticket = Ticket.objects.create(overlay="t3-teatree")
        first = ReviewVerdict.record(
            pr_id=4380,
            slug="Souliane/TeaTree",
            reviewed_sha=_SHA,
            verdict="hold",
            reviewer_identity="cold-reviewer-a",
            findings=[
                Finding(severity="major", summary="first blocker", reproduction="pytest test_blocker.py: failed")
            ],
            ticket=first_ticket,
        )
        original_time = first.recorded_at
        second = ReviewVerdict.record(
            pr_id=4380,
            slug="souliane/teatree",
            reviewed_sha=_SHA,
            verdict="hold",
            reviewer_identity="COLD-REVIEWER-A",
            findings=[Finding(severity="minor", summary="later detail", reproduction="pytest test_detail.py: failed")],
            ticket=second_ticket,
        )

        first.refresh_from_db()
        assert second.pk == first.pk
        assert first.recorded_at == original_time
        assert first.reviewer_identity == "cold-reviewer-a"
        assert first.ticket_id == first_ticket.pk
        assert [finding.summary for finding in first.structured_findings] == ["first blocker", "later detail"]
        again = ReviewVerdict.record(
            pr_id=4380,
            slug="souliane/teatree",
            reviewed_sha=_SHA,
            verdict="hold",
            reviewer_identity="cold-reviewer-a",
            findings=[Finding(severity="minor", summary="later detail", reproduction="pytest test_detail.py: failed")],
        )
        assert [finding.summary for finding in again.structured_findings] == ["first blocker", "later detail"]

    def test_hold_only_head_is_held_even_without_a_pass(self) -> None:
        self._record(verdict="hold", reviewer="cold-reviewer-a")

        assert (
            ReviewVerdict.objects.effective_state_at(slug="souliane/teatree", pr_id=4380, head_sha=_SHA)
            is HeadVerdictState.HOLD
        )


class TestStalenessAndSafety(TestCase):
    def test_stale_when_head_moved_off_reviewed_sha(self) -> None:
        verdict = ReviewVerdict.record(
            pr_id=1,
            slug="souliane/teatree",
            reviewed_sha=_SHA,
            verdict="merge_safe",
            reviewer_identity="cold-reviewer",
        )
        assert verdict.is_stale_at(_OTHER_SHA)
        assert not verdict.is_stale_at(_SHA.upper())

    def test_an_unreadable_head_is_not_answerable_as_stale(self) -> None:
        """The sentinel is a NON-ANSWER — refused, never quietly reported as drift.

        ``reviewed_sha != <anything unreadable>`` is trivially true, so a silent
        answer here is always "stale" no matter what the tree did.
        """
        verdict = ReviewVerdict.record(
            pr_id=1,
            slug="souliane/teatree",
            reviewed_sha=_SHA,
            verdict="merge_safe",
            reviewer_identity="cold-reviewer",
        )
        assert verdict.is_head_unreadable(HEAD_SHA_UNREADABLE)
        assert verdict.is_head_unreadable("")
        assert not verdict.is_head_unreadable(_SHA)
        with pytest.raises(ReviewVerdictError, match="is_head_unreadable"):
            verdict.is_stale_at(HEAD_SHA_UNREADABLE)

    def test_an_empty_head_still_answers_stale_for_the_gates(self) -> None:
        # Deliberately NOT a raise: the merge-gate queryset helpers
        # (``effective_state_at`` and siblings) rest on an empty head reading as
        # stale to fail closed, and a scanner tick must not learn to raise.
        verdict = ReviewVerdict.record(
            pr_id=1,
            slug="souliane/teatree",
            reviewed_sha=_SHA,
            verdict="merge_safe",
            reviewer_identity="cold-reviewer",
        )
        assert verdict.is_stale_at("")

    def test_safe_to_approve_only_when_at_head_and_checks_green(self) -> None:
        verdict = ReviewVerdict.record(
            pr_id=1,
            slug="souliane/teatree",
            reviewed_sha=_SHA,
            verdict="merge_safe",
            reviewer_identity="cold-reviewer",
        )
        assert verdict.is_safe_to_approve_at(_SHA, live_checks_status="green")
        assert not verdict.is_safe_to_approve_at(_OTHER_SHA, live_checks_status="green")
        assert not verdict.is_safe_to_approve_at(_SHA, live_checks_status="failed")

    def test_hold_verdict_is_never_safe_to_approve(self) -> None:
        verdict = ReviewVerdict.record(
            pr_id=1,
            slug="souliane/teatree",
            reviewed_sha=_SHA,
            verdict="hold",
            reviewer_identity="cold-reviewer",
            gh_verify_result="failed",
        )
        assert not verdict.is_safe_to_approve_at(_SHA, live_checks_status="green")

    def test_blast_class_choices_mirror_merge_clear(self) -> None:
        verdict = ReviewVerdict.record(
            pr_id=1,
            slug="souliane/teatree",
            reviewed_sha=_SHA,
            verdict="merge_safe",
            reviewer_identity="cold-reviewer",
            blast_class="substrate",
        )
        assert verdict.blast_class == MergeClear.BlastClass.SUBSTRATE

    def test_str_summarises_the_verdict(self) -> None:
        verdict = ReviewVerdict.record(
            pr_id=99,
            slug="souliane/teatree",
            reviewed_sha=_SHA,
            verdict="merge_safe",
            reviewer_identity="cold-reviewer",
        )
        assert str(verdict) == f"review-verdict<souliane/teatree#99@{_SHA[:8]} merge_safe>"


class TestFindingRoundTrip(TestCase):
    def test_code_anchored_claim_without_reproduction_stays_blocking(self) -> None:
        finding = Finding(
            severity="critical", kind="blocking", summary="Untested shipped behavior", file="a.py", line=4
        )
        assert finding.kind == "blocking"
        assert finding.severity == "critical"

    def test_explicit_documentation_basis_without_reproduction_is_advisory(self) -> None:
        finding = Finding(severity="critical", kind="blocking", basis="documentation", summary="Spec suggests a bug")
        assert finding.kind == "note"

    def test_null_reproduction_does_not_become_the_word_none(self) -> None:
        finding = Finding.from_dict(
            {"severity": "major", "kind": "blocking", "basis": "documentation", "reproduction": None}
        )
        assert finding.reproduction == ""
        assert finding.kind == "note"

    def test_explicit_note_kind_wins_over_minor_severity(self) -> None:
        finding = Finding(severity="minor", kind="note", basis="executed", summary="Review venue could not inspect CI")
        assert finding.kind == "note"

    def test_executed_reproduction_still_blocks(self) -> None:
        finding = Finding(
            severity="critical",
            kind="blocking",
            summary="runtime failure",
            file="a.py",
            line=4,
            reproduction="pytest test_a.py: failed",
        )
        assert finding.kind == "blocking"

    def test_line_coerced_from_string_digits(self) -> None:
        assert Finding.from_dict({"severity": "nit", "summary": "s", "file": "a.py", "line": "7"}).line == 7

    def test_line_defaults_to_zero_for_non_numeric(self) -> None:
        assert Finding.from_dict({"severity": "nit", "summary": "s", "line": "abc"}).line == 0
        assert Finding.from_dict({"severity": "nit", "summary": "s"}).line == 0

    def test_file_level_location_when_no_line(self) -> None:
        assert Finding(severity="major", summary="s", file="a.py").location() == "a.py"


class TestRecordResolvesReviewLock(TestCase):
    """Recording a verdict resolves the PR's MRReviewLock (#1405)."""

    def test_recording_merge_safe_resolves_a_held_lock(self) -> None:
        MRReviewLock.acquire(slug="souliane/teatree", pr_id=42, holder="t3:reviewer-agent-a")

        ReviewVerdict.record(
            pr_id=42,
            slug="souliane/teatree",
            reviewed_sha=_SHA,
            verdict="merge_safe",
            reviewer_identity="cold-reviewer",
        )

        lock = MRReviewLock.objects.get(slug="souliane/teatree", pr_id=42)
        assert lock.state == MRReviewLock.State.RESOLVED

    def test_recording_hold_also_resolves_the_lock(self) -> None:
        MRReviewLock.acquire(slug="souliane/teatree", pr_id=42, holder="t3:reviewer-agent-a")

        ReviewVerdict.record(
            pr_id=42,
            slug="souliane/teatree",
            reviewed_sha=_SHA,
            verdict="hold",
            reviewer_identity="cold-reviewer",
            gh_verify_result="failed",
        )

        lock = MRReviewLock.objects.get(slug="souliane/teatree", pr_id=42)
        assert lock.state == MRReviewLock.State.RESOLVED

    def test_recording_with_a_mismatched_lock_holder_cannot_steal_the_release(self) -> None:
        MRReviewLock.acquire(slug="souliane/teatree", pr_id=42, holder="t3:reviewer-agent-a")

        ReviewVerdict.record(
            pr_id=42,
            slug="souliane/teatree",
            reviewed_sha=_SHA,
            verdict="merge_safe",
            reviewer_identity="cold-reviewer",
            lock_holder="codex-self-review",
        )

        lock = MRReviewLock.objects.get(slug="souliane/teatree", pr_id=42)
        assert lock.state == MRReviewLock.State.REVIEW_DISPATCHED

    def test_recording_with_no_held_lock_is_a_no_op(self) -> None:
        ReviewVerdict.record(
            pr_id=42,
            slug="souliane/teatree",
            reviewed_sha=_SHA,
            verdict="merge_safe",
            reviewer_identity="cold-reviewer",
        )

        assert MRReviewLock.objects.count() == 0
