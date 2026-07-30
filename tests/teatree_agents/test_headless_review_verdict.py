"""Headless reviewer records its verdict via the result envelope (corr-11).

The verdict is recorded through the RESULT ENVELOPE by maker≠checker design: the
reviewer RETURNS a typed ``review_verdict`` and the orchestrator
(``record_result_envelope`` — a DIFFERENT actor) records the ``ReviewVerdict``
server-side, resolving the per-MR :class:`MRReviewLock` and advancing any open
review loop. Routing the recording through the orchestrator (never a reviewer-side
``t3 review record``) is what keeps the maker (the review sub-agent) from also
being the checker (the actor that persists the verdict). These tests drive the
orchestrator path directly with a returned envelope and assert the verdict lands
and the lock releases.
"""

import pytest
from django.test import TestCase

from teatree.agents.attempt_recorder import record_result_envelope, validate_result_keys
from teatree.agents.result_schema import check_evidence
from teatree.core.models import (
    AutoReviewDispatch,
    MRReviewLock,
    PullRequest,
    ReviewLoop,
    ReviewLoopRound,
    ReviewVerdict,
    Session,
    Task,
    Ticket,
)

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

_SLUG = "souliane/teatree"
_PR_ID = 4242
_HEAD = "1f4b9c2ad0e7f61c83b25d90ac174e5f60a1b2c3"
_PR_URL = f"https://github.com/{_SLUG}/pull/{_PR_ID}"


def _reviewing_task_via_dispatch() -> tuple[Task, AutoReviewDispatch]:
    dispatch = AutoReviewDispatch.enqueue(slug=_SLUG, pr_id=_PR_ID, head_sha=_HEAD, pr_url=_PR_URL, overlay="teatree")
    assert dispatch is not None
    task = dispatch.task
    assert task is not None
    task.claim(claimed_by="headless-reviewer")
    return task, dispatch


def _verdict_envelope(*, verdict: str = "merge_safe", reviewer: str = "cold-reviewer-agent") -> dict[str, object]:
    return {
        "summary": "Completed an independent cold review of the pull request.",
        "review_verdict": {
            "verdict": verdict,
            "reviewed_sha": _HEAD,
            "reviewer_identity": reviewer,
            "gh_verify_result": "green",
            "findings": [],
        },
    }


class TestHeadlessReviewerRecordsVerdictWithoutBash(TestCase):
    def test_returned_envelope_records_verdict_and_releases_lock(self) -> None:
        task, _ = _reviewing_task_via_dispatch()
        held = MRReviewLock.objects.get(slug=_SLUG, pr_id=_PR_ID)
        assert held.state == MRReviewLock.State.REVIEW_DISPATCHED

        record_result_envelope(task, _verdict_envelope(), phase="reviewing")

        recorded = ReviewVerdict.objects.filter(slug=_SLUG, pr_id=_PR_ID, reviewed_sha=_HEAD).first()
        assert recorded is not None
        assert recorded.is_merge_safe()
        assert recorded.reviewer_identity == "cold-reviewer-agent"

        held.refresh_from_db()
        assert held.state == MRReviewLock.State.RESOLVED

        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED

    def test_hold_verdict_also_records_and_releases_lock(self) -> None:
        task, _ = _reviewing_task_via_dispatch()
        record_result_envelope(task, _verdict_envelope(verdict="hold"), phase="reviewing")

        recorded = ReviewVerdict.objects.filter(slug=_SLUG, pr_id=_PR_ID).first()
        assert recorded is not None
        assert recorded.verdict == ReviewVerdict.Verdict.HOLD
        assert MRReviewLock.objects.get(slug=_SLUG, pr_id=_PR_ID).state == MRReviewLock.State.RESOLVED

    def test_maker_reviewer_identity_is_refused_and_lock_stays_held(self) -> None:
        task, _ = _reviewing_task_via_dispatch()
        record_result_envelope(task, _verdict_envelope(reviewer="coding-agent"), phase="reviewing")

        assert not ReviewVerdict.objects.filter(slug=_SLUG, pr_id=_PR_ID).exists()
        assert MRReviewLock.objects.get(slug=_SLUG, pr_id=_PR_ID).state == MRReviewLock.State.REVIEW_DISPATCHED
        task.refresh_from_db()
        assert task.status == Task.Status.FAILED

    def test_reviewing_task_without_verdict_envelope_fails_loudly(self) -> None:
        # #3654: this used to COMPLETE, which is how 138 reviewing tasks finished
        # having recorded nothing while every open PR stayed unmergeable.
        task, _ = _reviewing_task_via_dispatch()
        attempt = record_result_envelope(task, {"summary": "Reviewed.", "decisions": ["looks good"]}, phase="reviewing")

        assert not ReviewVerdict.objects.filter(slug=_SLUG, pr_id=_PR_ID).exists()
        assert MRReviewLock.objects.get(slug=_SLUG, pr_id=_PR_ID).state == MRReviewLock.State.REVIEW_DISPATCHED
        task.refresh_from_db()
        assert task.status == Task.Status.FAILED
        assert "review_verdict" in attempt.error


class TestOrchestratorRecordingAdvancesReviewLoop(TestCase):
    def test_merge_safe_envelope_passes_open_external_loop(self) -> None:
        ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, state=Ticket.State.STARTED)
        PullRequest.objects.create(ticket=ticket, url=_PR_URL)
        loop = ReviewLoop.objects.create(
            ticket=ticket,
            variant=ReviewLoop.Variant.EXTERNAL,
            author_phase="e2e",
            reviewer_phase="e2e_reviewing",
            state=ReviewLoop.State.REVIEWING,
        )
        reviewer_task = _loop_reviewer_task(loop)

        record_result_envelope(reviewer_task, _verdict_envelope(), phase="e2e_reviewing")

        loop.refresh_from_db()
        assert loop.state == ReviewLoop.State.PASSED
        assert loop.passed is True


class TestReviewingEvidenceAcceptsVerdict(TestCase):
    def test_returned_verdict_satisfies_the_reviewing_evidence_gate(self) -> None:
        envelope = _verdict_envelope()
        # No `decisions` field — the verdict alone must clear the gate (corr-11).
        assert "decisions" not in envelope
        assert check_evidence(envelope, "reviewing") == ""
        assert validate_result_keys(envelope) == ""

    def test_reviewing_without_a_verdict_fails_evidence(self) -> None:
        assert "missing required evidence" in check_evidence({"summary": "looked at it"}, "reviewing")

    def test_decisions_alone_no_longer_substitutes_for_the_verdict(self) -> None:
        # #3654: `decisions` was an accepted alternative, so a reviewer that never
        # returned a verdict completed indistinguishably from one that did.
        assert "missing required evidence" in check_evidence({"decisions": ["looks good"]}, "reviewing")

    def test_verdict_outside_the_recorder_vocabulary_fails_evidence(self) -> None:
        envelope = {"review_verdict": {"verdict": "PASS", "reviewer_identity": "cold-reviewer-agent"}}
        assert "missing required evidence" in check_evidence(envelope, "reviewing")


def _loop_reviewer_task(loop: ReviewLoop) -> Task:
    session = Session.objects.create(ticket=loop.ticket, agent_id="review-loop-reviewer")
    task = Task.objects.create(
        ticket=loop.ticket,
        session=session,
        phase="e2e_reviewing",
        execution_target=Task.ExecutionTarget.HEADLESS,
    )
    task.claim(claimed_by="review-loop-reviewer")
    ReviewLoopRound.objects.create(review_loop=loop, round=0, leg=ReviewLoop.LEG_REVIEWER, task=task)
    return task
