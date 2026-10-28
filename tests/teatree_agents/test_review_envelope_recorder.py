"""The reviewing recorder is the rubric's PRODUCER — the done-gate's only automatic grader.

The done-gate is unconditional: an ungraded rubric refuses the merge. Nothing in the
tree graded one automatically, so every ticketed factory PR needed a human to grade by
hand. The reviewer already IS the independent verifier the gate requires (grader !=
maker, one per dispatched head), so its returned envelope carries the grades and THIS
actor — the orchestrator, not the reviewer — stamps them.

Two properties carry the weight, and each has its own pin below. COVERAGE: a verdict
that leaves any criterion PENDING records NOTHING, because a verdict recorded over a
half-graded rubric resolves the review claim and never re-arms, leaving the head
unmergeable forever. ATOMICITY: the verdict, the claim retirement, the lock release and
the grades land in ONE transaction, so a refused grade rolls the verdict back with it.
"""

from unittest.mock import Mock, patch

import pytest
from django.test import TestCase

from teatree.agents.attempt_recorder import AttemptUsage, record_result_envelope
from teatree.agents.envelope_refusal import MALFORMED_FIX_RECORD_PREFIX, is_recorder_refusal
from teatree.core.gates.anti_vacuity_gate import check_anti_vacuity_attestation
from teatree.core.gates.integration_review_gate import check_integration_review
from teatree.core.gates.review_request_state_gate import check_reviewed_state, has_review_evidence
from teatree.core.gates.rubric_gate import RubricNotSatisfiedError, check_rubric_satisfied
from teatree.core.merge.ticket_gates import assert_ticket_scoped_gates
from teatree.core.modelkit.task_failure_taxonomy import REVIEW_UNRECORDABLE_PREFIX, FailureKind, classify_failure
from teatree.core.models import (
    AutoReviewDispatch,
    ConfigSetting,
    HonestyEscalation,
    MRReviewLock,
    PullRequest,
    ReviewVerdict,
    RoundMetric,
    Rubric,
    RubricCriterion,
    Session,
    Task,
    Ticket,
)
from teatree.core.models.plan_artifact import PlanArtifact
from teatree.core.models.review_target import ReviewTarget, verdict_at
from teatree.core.models.reviewer_identity import assigned_reviewer_identity
from teatree.core.models.types import AdequacySection, PlanAdequacy
from teatree.core.review.live_head import LiveHeadRead
from teatree.core.review.nit_lane import NitLaneError, NitPoint, NitReceipt
from teatree.core.review.nit_publish import has_nit_receipts
from teatree.loop.transient_requeue import requeue_transient_failed

# ast-grep-ignore: ac-django-no-pytest-django-db
pytestmark = pytest.mark.django_db

_SLUG = "souliane/teatree"
_PR_ID = 2241
_HEAD = "3c7e1a95f0b482d6e13470ac95bd28ef6104a7b9"
_BASE = "d" * 40
_PR_URL = f"https://github.com/{_SLUG}/pull/{_PR_ID}"
_REVIEWER = "cold-reviewer-agent"
_AGENT = "11111111-2222-3333-4444-555555555555"
_CITATION = "unit: tests/teatree_agents/test_review_envelope_recorder.py"
_AC = ["the recorder stamps every returned grade", "an ungraded criterion records nothing"]


def _planned_adequacy(criteria: list[str]) -> PlanAdequacy:
    return PlanAdequacy(
        design=AdequacySection(content="the producer records the reviewer's grades"),
        integration_seams=AdequacySection(content=["src/teatree/agents/review_envelope_recorder.py"]),
        edge_cases=AdequacySection(content=["a PR no ticket owns"]),
        test_strategy=AdequacySection(content="this module"),
        acceptance_criteria=AdequacySection(content=criteria),
    )


def _author_ticket(*, criteria: list[str] | None = _AC, pr_id: int = _PR_ID) -> Ticket:
    """The ticket the PR DELIVERS — a maker-role row the PR ledger points at.

    Distinct from the reviewing task's own ``task.ticket``, which is the reviewer-role
    row keyed by the PR url. That distinction is the whole subject of this module: the
    rubric lives on the ticket the PR delivers, so the recorder must resolve it the way
    the merge gate does rather than reach for the task's own ticket.
    """
    ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.REVIEW_REQUESTED)
    Session.objects.create(overlay="t3-teatree", ticket=ticket, agent_id=_AGENT)
    PullRequest.objects.create(
        ticket=ticket, overlay="t3-teatree", url=f"https://github.com/{_SLUG}/pull/{pr_id}", repo=_SLUG, iid=str(pr_id)
    )
    if criteria is not None:
        PlanArtifact.record(
            ticket=ticket,
            plan_text="produce the grades from the reviewing envelope",
            recorded_by="planner",
            base_sha=_BASE,
            adequacy=_planned_adequacy(criteria),
        )
    return ticket


def _reviewing_task_via_dispatch(*, pr_id: int = _PR_ID, head_sha: str = _HEAD) -> Task:
    dispatch = AutoReviewDispatch.enqueue(
        slug=_SLUG,
        pr_id=pr_id,
        head_sha=head_sha,
        pr_url=f"https://github.com/{_SLUG}/pull/{pr_id}",
        overlay="teatree",
    )
    assert dispatch is not None
    task = dispatch.task
    assert task is not None
    task.claim(claimed_by="headless-reviewer")
    return task


def _envelope(
    *,
    grades: list[dict[str, object]] | None = None,
    verdict: str = "merge_safe",
    reviewer: str = _REVIEWER,
    reviewed_sha: str = _HEAD,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "verdict": verdict,
        "reviewed_sha": reviewed_sha,
        "reviewer_identity": reviewer,
        "gh_verify_result": "green",
        "blast_class": "logic",
        "findings": [],
    }
    if grades is not None:
        payload["rubric_grades"] = grades
    return {
        "summary": "Independent cold review of the pull request.",
        "review_verdict": payload,
        "review_context": {
            "work_item": _PR_URL,
            "documents": ["specs/reviewed-requirements.md"],
            "analysis": "Compared the diff and acceptance criteria to the downloaded requirements.",
        },
        "anti_vacuity": {
            "ac_coverage": "Both acceptance criteria map to the recorder and rubric tests.",
            "proven_tests": ["tests/teatree_agents/test_review_envelope_recorder.py::test_producer"],
            "no_new_tests": False,
        },
    }


def _envelope_with_raw_grades(payload: object) -> dict[str, object]:
    """An envelope whose ``rubric_grades`` is *payload* verbatim — the malformed-shape seam."""
    envelope = _envelope()
    verdict = envelope["review_verdict"]
    assert isinstance(verdict, dict)
    verdict["rubric_grades"] = payload
    return envelope


def _full_pass() -> list[dict[str, object]]:
    return [{"ordinal": index, "status": "pass", "rationale": _CITATION} for index in range(len(_AC))]


def _criteria(ticket: Ticket) -> list[RubricCriterion]:
    rubric = Rubric.objects.active_for_ticket(ticket)
    assert rubric is not None
    return list(rubric.criteria.all())


class TestTheReviewerGradesTheRubricItVerifies(TestCase):
    def test_venue_limit_refuses_without_recording_a_hold_and_marks_route(self) -> None:
        task = _reviewing_task_via_dispatch()
        RoundMetric.objects.open(kind="review", ticket=task.ticket, subject=_HEAD, size=0, detail={"case": "full"})
        result = {"summary": "Venue could not verify CI", "review_venue_limit": {"limits": ["CI unreadable"]}}

        attempt = record_result_envelope(
            task,
            result,
            phase="reviewing",
            usage=AttemptUsage(route_source_skill="review", route_candidate_index=0),
        )

        assert attempt.error.startswith("review_venue_limit: ")
        assert not ReviewVerdict.objects.filter(slug=_SLUG, pr_id=_PR_ID).exists()
        assert task.attempts.refused_brief_candidates("review") == {0: attempt.error}
        assert RoundMetric.objects.get(ticket=task.ticket, subject=_HEAD).outcome == "venue_limit"
        with patch("teatree.backends.loader.pr_is_merged_or_closed", return_value=False):
            assert requeue_transient_failed() == 1
        task.refresh_from_db()
        assert task.status == Task.Status.PENDING

    def test_venue_limit_with_verdict_is_refused_without_recording(self) -> None:
        task = _reviewing_task_via_dispatch()
        result = _envelope(grades=_full_pass())
        result["review_venue_limit"] = {"limits": ["CI unreadable"]}

        attempt = record_result_envelope(task, result, phase="reviewing")

        assert "review_venue_limit" in attempt.error
        assert not ReviewVerdict.objects.filter(slug=_SLUG, pr_id=_PR_ID).exists()

    def test_delta_review_carries_untouched_test_citation_after_source_change(self) -> None:
        ticket = _author_ticket()
        prior = "a" * 40
        criteria = _criteria(ticket)
        for criterion, rationale in zip(criteria, ("tests/unchanged.py:1", "src/touched.py:2"), strict=True):
            criterion.record_grade(status="pass", grader_identity=_REVIEWER, reviewed_sha=prior, rationale=rationale)
        ReviewVerdict.objects.create(
            ticket=ticket,
            slug=_SLUG,
            pr_id=_PR_ID,
            reviewed_sha=prior,
            verdict="hold",
            reviewer_identity=_REVIEWER,
            reviewer_identity_normalized=_REVIEWER,
            blast_class="logic",
            gh_verify_result="green",
            findings=[],
        )
        task = _reviewing_task_via_dispatch()
        RoundMetric.objects.open(
            kind="review",
            ticket=ticket,
            subject=_HEAD,
            size=2,
            detail={
                "case": "delta",
                "prior_head": prior,
                "patch": "+fixed",
                "delta_files": ["src/touched.py"],
                "carried": [0],
            },
        )

        result = _envelope(
            grades=[
                {"ordinal": 1, "status": "pass", "rationale": _CITATION},
            ]
        )
        attempt = record_result_envelope(task, result, phase="reviewing")

        assert attempt.error == ""
        ticket.refresh_from_db()
        grades = ticket.extra["rubric_grades_by_head"][_HEAD]
        assert grades[str(criteria[0].pk)]["grader_identity"] == _REVIEWER
        assert grades[str(criteria[0].pk)]["rationale"] == "tests/unchanged.py:1"
        assert Rubric.objects.active_for_ticket(ticket).is_fully_passed_at(_HEAD)

    def test_delta_refuses_a_carry_for_basename_or_prose(self) -> None:
        for rationale in ("touched.py:1", "e.g. a helper changed"):
            with self.subTest(rationale=rationale):
                ticket = _author_ticket(pr_id=_PR_ID + (1 if rationale.startswith("touched") else 2))
                prior = "a" * 40
                criteria = _criteria(ticket)
                for criterion, evidence in zip(criteria, (rationale, "src/untouched.py:1"), strict=True):
                    criterion.record_grade(
                        status="pass", grader_identity=_REVIEWER, reviewed_sha=prior, rationale=evidence
                    )
                ReviewVerdict.objects.create(
                    ticket=ticket,
                    slug=_SLUG,
                    pr_id=int(ticket.pull_requests.first().iid),
                    reviewed_sha=prior,
                    verdict="hold",
                    reviewer_identity=_REVIEWER,
                    reviewer_identity_normalized=_REVIEWER,
                    blast_class="logic",
                    gh_verify_result="green",
                    findings=[],
                )
                task = _reviewing_task_via_dispatch(pr_id=int(ticket.pull_requests.first().iid))
                RoundMetric.objects.open(
                    kind="review",
                    ticket=ticket,
                    subject=_HEAD,
                    size=2,
                    detail={
                        "case": "delta",
                        "prior_head": prior,
                        "patch": "+fixed",
                        "delta_files": ["src/touched.py"],
                        "carried": [0],
                    },
                )

                attempt = record_result_envelope(
                    task,
                    _envelope(grades=[{"ordinal": 1, "status": "pass", "rationale": _CITATION}]),
                    phase="reviewing",
                )

                assert "#0" in attempt.error
                assert not ReviewVerdict.objects.filter(ticket=ticket, reviewed_sha=_HEAD).exists()

    def test_delta_review_omitting_touched_criterion_records_no_verdict(self) -> None:
        ticket = _author_ticket()
        task = _reviewing_task_via_dispatch()
        RoundMetric.objects.open(
            kind="review",
            ticket=ticket,
            subject=_HEAD,
            size=2,
            detail={
                "case": "delta",
                "prior_head": "a" * 40,
                "patch": "+fixed",
                "delta_files": ["src/touched.py"],
                "carried": [0],
            },
        )

        attempt = record_result_envelope(task, _envelope(grades=[]), phase="reviewing")

        assert "#1" in attempt.error
        assert not ReviewVerdict.objects.filter(ticket=ticket).exists()

    def test_a_nit_only_hold_records_merge_safe(self) -> None:
        ticket = _author_ticket()
        result = _envelope(grades=_full_pass(), verdict="hold")
        verdict = result["review_verdict"]
        assert isinstance(verdict, dict)
        verdict["findings"] = [
            {"severity": "nit", "kind": "nit", "summary": "rename helper", "file": "a.py", "line": 2}
        ]

        with (
            patch("teatree.core.gates.privacy_gate.overlay_privacy_rules", return_value=([], [])),
            patch("teatree.core.gates.privacy_gate._db_banned_terms", return_value=[]),
            patch("teatree.core.gates.privacy_gate._registered_overlay_rules_union", return_value=([], [])),
            patch("teatree.core.review.nit_publish.get_overlay_for_ticket", return_value=object()),
            patch("teatree.core.review.nit_publish.get_backend_provider") as provider,
            patch(
                "teatree.core.review.nit_publish.track_nits",
                return_value=NitReceipt("https://github.com/souliane/teatree/issues/10", ("thread-1",), _HEAD),
            ) as track,
        ):
            provider.return_value.get_code_host_for_url.return_value = object()
            attempt = record_result_envelope(_reviewing_task_via_dispatch(), result, phase="reviewing")

        assert attempt.error == ""
        recorded = ReviewVerdict.objects.get(ticket=ticket, pr_id=_PR_ID)
        assert recorded.verdict == ReviewVerdict.Verdict.MERGE_SAFE
        assert recorded.structured_findings[0].kind == "nit"
        track.assert_called_once()
        ticket.refresh_from_db()
        assert has_nit_receipts(ticket, _PR_URL, _HEAD, (NitPoint("a.py", 2, "rename helper"),))

    def test_public_nit_home_path_refuses_before_verdict_recording(self) -> None:
        ticket = _author_ticket()
        result = _envelope(grades=_full_pass(), verdict="hold")
        verdict = result["review_verdict"]
        assert isinstance(verdict, dict)
        verdict["findings"] = [
            {
                "severity": "nit",
                "kind": "nit",
                "summary": "Inspect /home/example/private.txt",  # privacy-scan:allow (fake fixture path, not PII)
                "file": "a.py",
                "line": 2,
            }
        ]
        with (
            patch("teatree.core.gates.privacy_gate._target_is_public", return_value=True),
            patch("teatree.core.gates.privacy_gate.overlay_privacy_rules", return_value=([], [])),
            patch("teatree.core.gates.privacy_gate._db_banned_terms", return_value=[]),
            patch("teatree.core.gates.privacy_gate._registered_overlay_rules_union", return_value=([], [])),
        ):
            attempt = record_result_envelope(_reviewing_task_via_dispatch(), result, phase="reviewing")

        assert "review verdict recording refused" in attempt.error
        assert "generic:home_path" in attempt.error
        assert classify_failure(attempt.error) == FailureKind.RECORDING_REFUSED
        assert not ReviewVerdict.objects.filter(ticket=ticket, pr_id=_PR_ID).exists()

    def test_public_nit_file_home_path_refuses_before_verdict_recording(self) -> None:
        ticket = _author_ticket()
        result = _envelope(grades=_full_pass(), verdict="hold")
        verdict = result["review_verdict"]
        assert isinstance(verdict, dict)
        verdict["findings"] = [
            {
                "severity": "nit",
                "kind": "nit",
                "summary": "Rename helper",
                "file": "/home/example/private.txt",  # privacy-scan:allow (fake fixture path, not PII)
                "line": 0,
            }
        ]
        with (
            patch("teatree.core.gates.privacy_gate._target_is_public", return_value=True),
            patch("teatree.core.gates.privacy_gate.overlay_privacy_rules", return_value=([], [])),
            patch("teatree.core.gates.privacy_gate._db_banned_terms", return_value=[]),
            patch("teatree.core.gates.privacy_gate._registered_overlay_rules_union", return_value=([], [])),
        ):
            attempt = record_result_envelope(_reviewing_task_via_dispatch(), result, phase="reviewing")

        assert "review verdict recording refused" in attempt.error
        assert "generic:home_path" in attempt.error
        assert classify_failure(attempt.error) == FailureKind.RECORDING_REFUSED
        assert not ReviewVerdict.objects.filter(ticket=ticket, pr_id=_PR_ID).exists()

    def test_private_nit_home_path_records_and_publishes(self) -> None:
        ticket = _author_ticket()
        result = _envelope(grades=_full_pass(), verdict="hold")
        verdict = result["review_verdict"]
        assert isinstance(verdict, dict)
        verdict["findings"] = [
            {
                "severity": "nit",
                "kind": "nit",
                "summary": "Inspect /home/example/private.txt",  # privacy-scan:allow (fake fixture path, not PII)
                "file": "a.py",
                "line": 2,
            }
        ]
        with (
            patch("teatree.core.gates.privacy_gate._target_is_public", return_value=False),
            patch("teatree.core.review.nit_publish.get_overlay_for_ticket", return_value=object()),
            patch("teatree.core.review.nit_publish.get_backend_provider") as provider,
            patch(
                "teatree.core.review.nit_publish.track_nits",
                return_value=NitReceipt("https://github.com/souliane/teatree/issues/10", ("thread-1",), _HEAD),
            ) as track,
        ):
            provider.return_value.get_code_host_for_url.return_value = object()
            attempt = record_result_envelope(_reviewing_task_via_dispatch(), result, phase="reviewing")

        assert attempt.error == ""
        assert ReviewVerdict.objects.filter(ticket=ticket, pr_id=_PR_ID).exists()
        track.assert_called_once()

    def test_review_nit_publish_failure_keeps_a_recovery_marker(self) -> None:
        ticket = _author_ticket()
        result = _envelope(grades=_full_pass(), verdict="hold")
        verdict = result["review_verdict"]
        assert isinstance(verdict, dict)
        verdict["findings"] = [
            {"severity": "nit", "kind": "nit", "summary": "rename helper", "file": "a.py", "line": 2}
        ]
        with (
            patch("teatree.core.gates.privacy_gate.overlay_privacy_rules", return_value=([], [])),
            patch("teatree.core.gates.privacy_gate._db_banned_terms", return_value=[]),
            patch("teatree.core.gates.privacy_gate._registered_overlay_rules_union", return_value=([], [])),
            patch(
                "teatree.agents.review_envelope_recorder.publish_nits", side_effect=NitLaneError("forge unavailable")
            ),
        ):
            attempt = record_result_envelope(_reviewing_task_via_dispatch(), result, phase="reviewing")
        assert "forge unavailable" in attempt.error
        assert ReviewVerdict.objects.filter(ticket=ticket, pr_id=_PR_ID).exists()
        ticket.refresh_from_db()
        assert ticket.extra["nit_publication"][_HEAD]["status"] == "failed"

    def test_post_time_privacy_refusal_keeps_the_verdict_recoverable(self) -> None:
        ticket = _author_ticket()
        result = _envelope(grades=_full_pass(), verdict="hold")
        verdict = result["review_verdict"]
        assert isinstance(verdict, dict)
        verdict["findings"] = [
            {
                "severity": "nit",
                "kind": "nit",
                "summary": "Inspect /home/example/private.txt",  # privacy-scan:allow (fake fixture path, not PII)
                "file": "a.py",
                "line": 2,
            }
        ]
        host = Mock()
        host.fetch_live_head_sha.return_value = _HEAD
        host.get_pr_file_diffs.return_value = {"a.py": "@@ -1 +1,3 @@\n+one\n+two\n+three"}
        host.list_review_threads.return_value = []
        with (
            patch("teatree.core.gates.privacy_gate._target_is_public", side_effect=[False, True]),
            patch("teatree.core.gates.privacy_gate.overlay_privacy_rules", return_value=([], [])),
            patch("teatree.core.gates.privacy_gate._db_banned_terms", return_value=[]),
            patch("teatree.core.gates.privacy_gate._registered_overlay_rules_union", return_value=([], [])),
            patch("teatree.core.review.nit_publish.get_overlay_for_ticket", return_value=object()),
            patch("teatree.core.review.nit_publish.get_backend_provider") as provider,
            patch("teatree.core.review.nit_lane.confirmed_self_identity_set", return_value={"alice"}),
            patch("teatree.core.review.nit_lane.is_self_authored", return_value=True),
        ):
            provider.return_value.get_code_host_for_url.return_value = host
            attempt = record_result_envelope(_reviewing_task_via_dispatch(), result, phase="reviewing")

        assert "review NIT publication refused" in attempt.error
        assert "generic:home_path" in attempt.error
        assert ReviewVerdict.objects.filter(ticket=ticket, pr_id=_PR_ID).exists()
        ticket.refresh_from_db()
        assert ticket.extra["nit_publication"][_HEAD]["status"] == "failed"
        host.submit_pr_review.assert_not_called()

    def test_nit_only_hold_without_merge_safe_evidence_records_nothing(self) -> None:
        ticket = _author_ticket()
        result = _envelope(grades=_full_pass(), verdict="hold")
        result.pop("anti_vacuity")
        verdict = result["review_verdict"]
        assert isinstance(verdict, dict)
        verdict["findings"] = [{"severity": "nit", "kind": "nit", "summary": "rename helper"}]

        attempt = record_result_envelope(_reviewing_task_via_dispatch(), result, phase="reviewing")

        assert "anti-vacuity recording refused" in attempt.error
        assert not ReviewVerdict.objects.filter(ticket=ticket, pr_id=_PR_ID).exists()

    def test_unpostable_nit_refuses_before_verdict_recording(self) -> None:
        ticket = _author_ticket()
        result = _envelope(grades=_full_pass(), verdict="hold")
        verdict = result["review_verdict"]
        assert isinstance(verdict, dict)
        verdict["findings"] = [
            {"severity": "nit", "kind": "nit", "summary": "this is missing a blank line", "file": "a.py", "line": 2}
        ]

        attempt = record_result_envelope(_reviewing_task_via_dispatch(), result, phase="reviewing")

        assert "comment check" in attempt.error
        assert not ReviewVerdict.objects.filter(ticket=ticket, pr_id=_PR_ID).exists()

    def test_a_nit_ticket_treats_a_nit_finding_as_blocking(self) -> None:
        ticket = _author_ticket()
        ticket.extra = {"nit_follow_up": True}
        ticket.save(update_fields=["extra"])
        result = _envelope(grades=_full_pass(), verdict="merge_safe")
        verdict = result["review_verdict"]
        assert isinstance(verdict, dict)
        verdict["findings"] = [
            {"severity": "nit", "kind": "nit", "summary": "rename helper", "file": "a.py", "line": 2}
        ]

        attempt = record_result_envelope(_reviewing_task_via_dispatch(), result, phase="reviewing")

        assert attempt.error == ""
        recorded = ReviewVerdict.objects.get(ticket=ticket, pr_id=_PR_ID)
        assert recorded.verdict == ReviewVerdict.Verdict.HOLD
        assert recorded.structured_findings[0].kind == "blocking"

    def test_multi_repo_review_envelope_records_combined_integration_review(self) -> None:
        ticket = _author_ticket()
        ticket.repos = ["souliane/teatree", "example-org/adapter"]
        ticket.save(update_fields=["repos"])
        result = _envelope(grades=_full_pass())
        result["integration_review"] = {"repos": list(ticket.repos)}

        attempt = record_result_envelope(_reviewing_task_via_dispatch(), result, phase="reviewing")

        assert attempt.error == ""
        check_integration_review(ticket)

    def test_review_envelope_produces_all_merge_review_evidence_without_manual_recording(self) -> None:
        ticket = _author_ticket()
        task = _reviewing_task_via_dispatch()

        attempt = record_result_envelope(task, _envelope(grades=_full_pass()), phase="reviewing")

        assert attempt.error == ""
        ticket.refresh_from_db()
        check_anti_vacuity_attestation(ticket, _HEAD, transition="merge")
        check_rubric_satisfied(ticket, _HEAD, transition="merge")
        assert has_review_evidence(ticket)
        assert check_reviewed_state(ticket) == ""
        assert_ticket_scoped_gates(slug=_SLUG, pr_id=_PR_ID, head_sha=_HEAD)
        assert ReviewVerdict.objects.filter(ticket=ticket, slug=_SLUG, pr_id=_PR_ID, reviewed_sha=_HEAD).exists()

    def test_missing_anti_vacuity_refuses_before_verdict_is_recorded(self) -> None:
        ticket = _author_ticket()
        task = _reviewing_task_via_dispatch()
        result = _envelope(grades=_full_pass())
        result.pop("anti_vacuity")

        attempt = record_result_envelope(task, result, phase="reviewing")

        assert "anti-vacuity recording refused" in attempt.error
        assert not ReviewVerdict.objects.filter(ticket=ticket, slug=_SLUG, pr_id=_PR_ID).exists()

    def test_returned_grades_land_on_the_gated_ticket_at_the_dispatch_head(self) -> None:
        ticket = _author_ticket()
        task = _reviewing_task_via_dispatch()

        attempt = record_result_envelope(task, _envelope(grades=_full_pass()), phase="reviewing")

        assert attempt.error == ""
        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED
        graded = _criteria(ticket)
        assert [c.status for c in graded] == [RubricCriterion.Status.PASS, RubricCriterion.Status.PASS]
        # The DISPATCH head and the verdict's own grader — never a self-asserted sha.
        assert {c.reviewed_sha for c in graded} == {_HEAD}
        assert {c.grader_identity for c in graded} == {_REVIEWER}
        # The whole point: the merge gate now passes at this head.
        check_rubric_satisfied(ticket, _HEAD, transition="merge")

    def test_a_full_pass_clears_the_honesty_escalation(self) -> None:
        ticket = _author_ticket()
        HonestyEscalation.record(HonestyEscalation.Reason.SHIPPED_INCOMPLETE, session_id=_AGENT)
        assert HonestyEscalation.is_active(_AGENT) is True

        record_result_envelope(_reviewing_task_via_dispatch(), _envelope(grades=_full_pass()), phase="reviewing")

        assert HonestyEscalation.is_active(_AGENT) is False
        assert Rubric.objects.active_for_ticket(ticket) is not None


class TestAnUngradedCriterionRecordsNothing(TestCase):
    """THE anti-vacuity pin: partial coverage must not resolve the review claim.

    A verdict recorded over a half-graded rubric retires the per-head claim and
    releases the lock, so nothing re-arms review — while the done-gate goes on
    refusing the merge for the PENDING criterion. The head is then unmergeable
    forever, with no reviewer left to fix it. The refusal has to come BEFORE the
    write, which is why coverage is a DB question only this actor can answer.
    """

    def test_stale_text_hash_records_neither_verdict_nor_grades(self) -> None:
        ticket = _author_ticket()
        task = _reviewing_task_via_dispatch()
        grades = _full_pass()
        grades[1]["text_hash"] = "0" * 12

        attempt = record_result_envelope(task, _envelope(grades=grades), phase="reviewing")

        assert attempt.error.startswith("malformed rubric_grades:")
        assert not ReviewVerdict.objects.filter(slug=_SLUG, pr_id=_PR_ID).exists()
        assert [c.status for c in _criteria(ticket)] == ["pending", "pending"]

    def test_a_verdict_that_grades_no_criterion_records_nothing(self) -> None:
        ticket = _author_ticket()
        task = _reviewing_task_via_dispatch()

        attempt = record_result_envelope(task, _envelope(), phase="reviewing")

        assert not ReviewVerdict.objects.filter(slug=_SLUG, pr_id=_PR_ID).exists()
        assert [c.status for c in _criteria(ticket)] == [
            RubricCriterion.Status.PENDING,
            RubricCriterion.Status.PENDING,
        ]
        assert MRReviewLock.objects.get(slug=_SLUG, pr_id=_PR_ID).state == MRReviewLock.State.REVIEW_DISPATCHED
        task.refresh_from_db()
        assert task.status == Task.Status.FAILED
        assert "#0" in attempt.error
        assert "#1" in attempt.error

    def test_false_string_cannot_claim_no_new_tests(self) -> None:
        ticket = _author_ticket()
        task = _reviewing_task_via_dispatch()
        result = _envelope(grades=_full_pass())
        result["anti_vacuity"] = {"ac_coverage": "AC covered", "proven_tests": [], "no_new_tests": "false"}

        attempt = record_result_envelope(task, result, phase="reviewing")

        assert "anti-vacuity recording refused" in attempt.error
        assert not ReviewVerdict.objects.filter(ticket=ticket, reviewed_sha=_HEAD).exists()

    def test_a_partial_grade_records_nothing(self) -> None:
        ticket = _author_ticket()
        task = _reviewing_task_via_dispatch()

        attempt = record_result_envelope(
            task, _envelope(grades=[{"ordinal": 0, "status": "pass", "rationale": _CITATION}]), phase="reviewing"
        )

        assert not ReviewVerdict.objects.filter(slug=_SLUG, pr_id=_PR_ID).exists()
        assert [c.status for c in _criteria(ticket)] == [
            RubricCriterion.Status.PENDING,
            RubricCriterion.Status.PENDING,
        ]
        assert MRReviewLock.objects.get(slug=_SLUG, pr_id=_PR_ID).state == MRReviewLock.State.REVIEW_DISPATCHED
        task.refresh_from_db()
        assert task.status == Task.Status.FAILED
        assert "#1" in attempt.error
        assert "#0" not in attempt.error

    def test_a_non_list_grades_payload_records_nothing(self) -> None:
        ticket = _author_ticket()
        task = _reviewing_task_via_dispatch()

        attempt = record_result_envelope(
            task, _envelope_with_raw_grades({"ordinal": 0, "status": "pass"}), phase="reviewing"
        )

        assert not ReviewVerdict.objects.filter(slug=_SLUG, pr_id=_PR_ID).exists()
        assert [c.status for c in _criteria(ticket)] == [
            RubricCriterion.Status.PENDING,
            RubricCriterion.Status.PENDING,
        ]
        task.refresh_from_db()
        assert task.status == Task.Status.FAILED
        assert '"rubric_grades" was a dict' in attempt.error


class TestAMalformedGradeItemRecordsNothing(TestCase):
    """A malformed ITEM takes the envelope refusal, never an exception (#4574).

    The container was normalised and the items were not, so a reviewer that returned
    ``["pass"]`` crashed the recorder instead of being refused. The traceback surfaced as
    an ``sdk_error`` no refusal marker matches, so ``transient_requeue`` withheld the
    corrective retry and one dispatch attempt burned per occurrence — three of them
    saturate the head and page the owner with a traceback instead of a diagnosis.
    """

    def _refused(self, payload: object) -> tuple[Ticket, Task, str]:
        ticket = _author_ticket()
        task = _reviewing_task_via_dispatch()
        attempt = record_result_envelope(task, _envelope_with_raw_grades(payload), phase="reviewing")
        return ticket, task, attempt.error

    def _assert_nothing_recorded(self, ticket: Ticket, task: Task, error: str) -> None:
        assert not ReviewVerdict.objects.filter(slug=_SLUG, pr_id=_PR_ID).exists()
        assert [c.status for c in _criteria(ticket)] == [
            RubricCriterion.Status.PENDING,
            RubricCriterion.Status.PENDING,
        ]
        assert MRReviewLock.objects.get(slug=_SLUG, pr_id=_PR_ID).state == MRReviewLock.State.REVIEW_DISPATCHED
        task.refresh_from_db()
        assert task.status == Task.Status.FAILED
        assert is_recorder_refusal(error)

    def test_a_bare_string_item_is_refused_by_name(self) -> None:
        ticket, task, error = self._refused(["pass", "pass"])

        self._assert_nothing_recorded(ticket, task, error)
        assert "must be an object, not a str" in error

    def test_an_item_naming_the_wrong_status_field_is_refused_by_name(self) -> None:
        # Every criterion is COVERED, so this reaches the stamping loop the coverage
        # check guards — the shape refusal has to come from the normaliser itself.
        ticket, task, error = self._refused([{"ordinal": 0, "grade": "pass"}, {"ordinal": 1, "grade": "pass"}])

        self._assert_nothing_recorded(ticket, task, error)
        assert "needs an ordinal and a status" in error

    def test_a_non_integer_ordinal_is_refused_by_name(self) -> None:
        ticket, task, error = self._refused([{"ordinal": "first", "status": "pass"}])

        self._assert_nothing_recorded(ticket, task, error)
        assert "a grade ordinal must be an integer" in error


class TestARefusedGradeRollsTheVerdictBack(TestCase):
    def test_a_refused_grade_rolls_the_verdict_back(self) -> None:
        # Full coverage, so the pre-write check passes; the SECOND grade is an
        # uncited PASS the guarded factory refuses. Without the shared atomic the
        # verdict is already written and the claim already retired when that
        # refusal lands, so the head can never be re-reviewed.
        ticket = _author_ticket()
        task = _reviewing_task_via_dispatch()

        attempt = record_result_envelope(
            task,
            _envelope(
                grades=[
                    {"ordinal": 0, "status": "pass", "rationale": _CITATION},
                    {"ordinal": 1, "status": "pass"},
                ]
            ),
            phase="reviewing",
        )

        assert not ReviewVerdict.objects.filter(slug=_SLUG, pr_id=_PR_ID).exists()
        assert [c.status for c in _criteria(ticket)] == [
            RubricCriterion.Status.PENDING,
            RubricCriterion.Status.PENDING,
        ]
        assert MRReviewLock.objects.get(slug=_SLUG, pr_id=_PR_ID).state == MRReviewLock.State.REVIEW_DISPATCHED
        task.refresh_from_db()
        assert task.status == Task.Status.FAILED
        # The guarded factory's own words: the coverage refusal one layer up quotes
        # `"rationale": "<what proves it>"` in its remediation, so the bare token is
        # satisfied by a normaliser that silently DROPPED this grade instead.
        assert "a PASS needs a rationale citing what proves the criterion" in attempt.error

    def test_a_maker_identity_records_neither_verdict_nor_grade(self) -> None:
        ticket = _author_ticket()
        task = _reviewing_task_via_dispatch()

        record_result_envelope(task, _envelope(grades=_full_pass(), reviewer="merge-loop"), phase="reviewing")

        assert not ReviewVerdict.objects.filter(slug=_SLUG, pr_id=_PR_ID).exists()
        assert [c.status for c in _criteria(ticket)] == [
            RubricCriterion.Status.PENDING,
            RubricCriterion.Status.PENDING,
        ]
        task.refresh_from_db()
        assert task.status == Task.Status.FAILED

    def test_a_divergent_self_asserted_head_grades_nothing(self) -> None:
        ticket = _author_ticket()
        task = _reviewing_task_via_dispatch()

        record_result_envelope(task, _envelope(grades=_full_pass(), reviewed_sha="0" * 39 + "1"), phase="reviewing")

        assert not ReviewVerdict.objects.filter(slug=_SLUG, pr_id=_PR_ID).exists()
        assert [c.status for c in _criteria(ticket)] == [
            RubricCriterion.Status.PENDING,
            RubricCriterion.Status.PENDING,
        ]
        task.refresh_from_db()
        assert task.status == Task.Status.FAILED


class TestDuplicateReviewTasksPreserveHolds(TestCase):
    def test_two_tasks_without_returned_identity_leave_first_hold_standing(self) -> None:
        _author_ticket(criteria=None)
        first_task = _reviewing_task_via_dispatch()
        first_result = _envelope(verdict="hold", reviewer="")
        first_verdict = first_result["review_verdict"]
        assert isinstance(first_verdict, dict)
        first_verdict["findings"] = [{"severity": "major", "summary": "fix the branch", "file": "a.py", "line": 1}]
        assert record_result_envelope(first_task, first_result, phase="reviewing").error == ""

        first_task.ticket.extra = {**first_task.ticket.extra, "reviewed_sha": _HEAD}
        first_task.ticket.save(update_fields=["extra"])
        second_task = Task.objects.create(ticket=first_task.ticket, session=first_task.session, phase="reviewing")
        second_task.claim(claimed_by="headless-reviewer")
        assert record_result_envelope(second_task, _envelope(reviewer=""), phase="reviewing").error == ""

        rows = list(ReviewVerdict.objects.for_pr(_SLUG, _PR_ID))
        assert len(rows) == 2
        assert {row.reviewer_identity for row in rows} == {assigned_reviewer_identity(_PR_ID)}
        held = ReviewVerdict.objects.latest_for_pr(_SLUG, _PR_ID)
        assert held is not None
        assert held.stands_as_hold()
        assert held.structured_findings[0].summary == "fix the branch"
        assert verdict_at(ReviewTarget(slug=_SLUG, pr_id=_PR_ID, head_sha=_HEAD, claim_head_sha=_HEAD)) is not None


class TestAFailGradeIsRecordedAndNoBypassOverridesIt(TestCase):
    def test_hold_with_a_fail_grade_is_recorded_and_the_bypass_never_overrides_it(self) -> None:
        ticket = _author_ticket()
        task = _reviewing_task_via_dispatch()

        attempt = record_result_envelope(
            task,
            _envelope(
                verdict="hold",
                grades=[
                    {"ordinal": 0, "status": "pass", "rationale": _CITATION},
                    {"ordinal": 1, "status": "fail", "rationale": "the recorder ignores the second criterion"},
                ],
            ),
            phase="reviewing",
        )

        assert attempt.error == ""
        recorded = ReviewVerdict.objects.get(slug=_SLUG, pr_id=_PR_ID, reviewed_sha=_HEAD)
        assert not recorded.is_merge_safe()
        assert [c.status for c in _criteria(ticket)] == [RubricCriterion.Status.PASS, RubricCriterion.Status.FAIL]

        PlanArtifact.record_bypass(
            ticket=ticket, plan_text="[audited bypass by tests] nothing to declare", recorded_by="a-human"
        )
        with pytest.raises(RubricNotSatisfiedError, match="graded FAIL"):
            check_rubric_satisfied(ticket, _HEAD, transition="merge")

    def test_merge_safe_over_a_fail_grade_records_nothing(self) -> None:
        # The mirror of ``_assert_checks_admit_merge_safe``, one field over: a reviewer
        # that grades a criterion FAIL has said the ticket is not done, so merge_safe
        # contradicts its own envelope. Recorded, it retires the claim and releases the
        # lock, and the done-gate then refuses the merge on the never-overridable FAIL.
        ticket = _author_ticket()
        task = _reviewing_task_via_dispatch()

        attempt = record_result_envelope(
            task,
            _envelope(
                grades=[
                    {"ordinal": 0, "status": "pass", "rationale": _CITATION},
                    {"ordinal": 1, "status": "fail", "rationale": "the recorder ignores the second criterion"},
                ]
            ),
            phase="reviewing",
        )

        assert not ReviewVerdict.objects.filter(slug=_SLUG, pr_id=_PR_ID).exists()
        assert [c.status for c in _criteria(ticket)] == [
            RubricCriterion.Status.PENDING,
            RubricCriterion.Status.PENDING,
        ]
        assert MRReviewLock.objects.get(slug=_SLUG, pr_id=_PR_ID).state == MRReviewLock.State.REVIEW_DISPATCHED
        task.refresh_from_db()
        assert task.status == Task.Status.FAILED
        assert "#1" in attempt.error
        assert is_recorder_refusal(attempt.error)


class TestOutsideTheGatesSubjectNoGradesAreOwed(TestCase):
    """A direct dispatch with no owner cannot grade a rubric it cannot resolve."""

    def test_a_pr_no_ticket_owns_requires_no_grades(self) -> None:
        # Direct dispatch bypasses the production adapter's ticket adoption.
        task = _reviewing_task_via_dispatch()

        attempt = record_result_envelope(task, _envelope(), phase="reviewing")

        assert attempt.error == ""
        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED
        assert ReviewVerdict.objects.filter(slug=_SLUG, pr_id=_PR_ID, reviewed_sha=_HEAD).exists()

    def test_a_rubric_less_ticket_requires_no_grades(self) -> None:
        ticket = _author_ticket(criteria=None)
        task = _reviewing_task_via_dispatch()
        RoundMetric.objects.open(kind="review", ticket=ticket, subject=_HEAD, size=0, detail={"case": "full"})

        attempt = record_result_envelope(task, _envelope(), phase="reviewing")

        assert attempt.error == ""
        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED
        assert ReviewVerdict.objects.filter(slug=_SLUG, pr_id=_PR_ID, reviewed_sha=_HEAD).exists()
        assert Rubric.objects.active_for_ticket(ticket) is None
        assert RoundMetric.objects.get(ticket=ticket, subject=_HEAD).outcome == "merge_safe"

    def test_a_malformed_grade_on_a_rubric_less_ticket_still_records_the_verdict(self) -> None:
        # The per-item refusal is scoped to a rubric that EXISTS: with none, no grade is
        # ever stamped, so refusing here would refuse a verdict that owes no grades.
        ticket = _author_ticket(criteria=None)
        task = _reviewing_task_via_dispatch()

        attempt = record_result_envelope(task, _envelope_with_raw_grades(["pass"]), phase="reviewing")

        assert attempt.error == ""
        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED
        assert ReviewVerdict.objects.filter(slug=_SLUG, pr_id=_PR_ID, reviewed_sha=_HEAD).exists()
        assert Rubric.objects.active_for_ticket(ticket) is None


class TestAPhaseOutsideTheGradedSetOwesNoGrades(TestCase):
    """This seam's OWN phase narrowing, which ``ticket_gates`` knows nothing about."""

    def test_e2e_reviewing_owes_no_grades_because_its_brief_never_shows_them(self) -> None:
        # ``e2e_reviewing`` records through the shell ``t3 <overlay> review record`` from
        # its own skill and its brief carries no rubric block, so requiring coverage
        # there refuses a checklist the agent was never handed.
        ticket = _author_ticket()
        task = _reviewing_task_via_dispatch()
        task.phase = "e2e_reviewing"
        task.save(update_fields=["phase"])

        attempt = record_result_envelope(task, _envelope(), phase="e2e_reviewing")

        assert attempt.error == ""
        assert ReviewVerdict.objects.filter(slug=_SLUG, pr_id=_PR_ID, reviewed_sha=_HEAD).exists()
        assert [c.status for c in _criteria(ticket)] == [
            RubricCriterion.Status.PENDING,
            RubricCriterion.Status.PENDING,
        ]

    def test_grades_on_a_non_review_phase_are_never_read(self) -> None:
        ticket = _author_ticket()
        task = _reviewing_task_via_dispatch()

        record_result_envelope(task, {"summary": "coded it", **_envelope(grades=_full_pass())}, phase="coding")

        assert not ReviewVerdict.objects.filter(slug=_SLUG, pr_id=_PR_ID).exists()
        assert [c.status for c in _criteria(ticket)] == [
            RubricCriterion.Status.PENDING,
            RubricCriterion.Status.PENDING,
        ]


_FIX_RECORD = {
    "root_cause": "the review recorder never wrote the record the DoD gate reads",
    "evidence": "a fix PR merged without a coding envelope left extra['fix_record'] empty",
    "regression_test": "tests/teatree_agents/test_review_envelope_recorder.py::TestAMergeSafeReviewRecordsTheFixRecord",
    "observed_red": "reverted the fix and the test failed",
    "recurrence_fingerprint": "review_envelope_recorder:no_fix_record_writer",
}


class TestAMergeSafeReviewRecordsTheFixRecord(TestCase):
    def _fix_ticket(self) -> Ticket:
        ticket = _author_ticket()
        Ticket.objects.filter(pk=ticket.pk).update(kind=Ticket.Kind.FIX)
        return ticket

    def _review(self, *, verdict: str = "merge_safe", record: dict[str, str]) -> tuple[Task, str]:
        result = _envelope(grades=_full_pass(), verdict=verdict)
        result["fix_record"] = record
        task = _reviewing_task_via_dispatch()
        return task, record_result_envelope(task, result, phase="reviewing").error

    def test_merge_safe_lands_the_record_on_the_gated_ticket_not_the_reviewer_row(self) -> None:
        ticket = self._fix_ticket()

        task, error = self._review(record=dict(_FIX_RECORD))

        assert error == ""
        ticket.refresh_from_db()
        task.ticket.refresh_from_db()
        assert ticket.extra["fix_record"] == _FIX_RECORD
        assert "fix_record" not in task.ticket.extra

    def test_a_record_returned_for_a_feature_ticket_is_kept_like_the_coders_is(self) -> None:
        ticket = _author_ticket()

        _task, error = self._review(record=dict(_FIX_RECORD))

        assert error == ""
        ticket.refresh_from_db()
        assert ticket.extra["fix_record"] == _FIX_RECORD

    def test_a_hold_records_no_fix_record(self) -> None:
        ticket = self._fix_ticket()

        _task, error = self._review(verdict="hold", record=dict(_FIX_RECORD))

        assert error == ""
        ticket.refresh_from_db()
        assert "fix_record" not in ticket.extra

    def test_a_malformed_record_is_refused_before_any_verdict(self) -> None:
        self._fix_ticket()

        _task, error = self._review(record={"root_cause": "only one field"})

        assert error.startswith(MALFORMED_FIX_RECORD_PREFIX)
        assert not ReviewVerdict.objects.filter(slug=_SLUG, pr_id=_PR_ID).exists()

    def test_switched_off_review_records_a_fix_without_observed_red(self) -> None:
        ConfigSetting.objects.set_value("forced_repro_gate_enabled", value=False)
        ticket = self._fix_ticket()
        record = {key: value for key, value in _FIX_RECORD.items() if key != "observed_red"}
        _task, error = self._review(record=record)
        assert error == ""
        ticket.refresh_from_db()
        assert ticket.extra["fix_record"]["observed_red"] == ""

    def test_switched_on_review_still_requires_observed_red(self) -> None:
        self._fix_ticket()
        record = {key: value for key, value in _FIX_RECORD.items() if key != "observed_red"}
        _task, error = self._review(record=record)
        assert "observed_red" in error

    def test_switched_off_review_still_requires_root_cause(self) -> None:
        ConfigSetting.objects.set_value("forced_repro_gate_enabled", value=False)
        self._fix_ticket()
        record = {key: value for key, value in _FIX_RECORD.items() if key not in {"observed_red", "root_cause"}}
        _task, error = self._review(record=record)
        assert "root_cause" in error


class TestAReviewOnAnIssueAnchoredTicketRecordsOnItsOwnPr(TestCase):
    """Task 5582's shape: no dispatch row, the ticket keyed on its issue, the PR recorded on it."""

    def _task(self, *, prs: int) -> Task:
        ticket = Ticket.objects.create(
            overlay="t3-teatree",
            issue_url=f"https://github.com/{_SLUG}/issues/5072",
            state=Ticket.State.REVIEW_REQUESTED,
        )
        for offset in range(prs):
            pr_id = _PR_ID + offset
            PullRequest.objects.create(
                ticket=ticket, url=f"https://github.com/{_SLUG}/pull/{pr_id}", repo=_SLUG, iid=str(pr_id)
            )
        task = Task.objects.create(
            ticket=ticket, session=Session.objects.create(ticket=ticket, agent_id="review"), phase="reviewing"
        )
        task.claim(claimed_by="headless-reviewer")
        return task

    def _record(self, task: Task, *, live_head: str = _HEAD) -> str:
        with patch(
            "teatree.core.review.verdict_head_binding.live_head_at",
            return_value=LiveHeadRead(sha=live_head, unreadable=False),
        ):
            return record_result_envelope(task, _envelope(verdict="hold"), phase="reviewing").error

    def test_a_hold_lands_on_the_tickets_one_live_pr_at_the_live_head(self) -> None:
        task = self._task(prs=1)

        assert self._record(task) == ""

        verdict = ReviewVerdict.objects.get(slug=_SLUG, pr_id=_PR_ID)
        assert (verdict.verdict, verdict.reviewed_sha, verdict.ticket_id) == ("hold", _HEAD, task.ticket_id)

    def test_a_verdict_that_fits_no_single_pr_is_refused_loudly(self) -> None:
        task = self._task(prs=2)

        error = self._record(task)

        assert error.startswith(REVIEW_UNRECORDABLE_PREFIX)
        task.refresh_from_db()
        assert task.status == Task.Status.FAILED
        assert not ReviewVerdict.objects.exists()

    def test_a_pre_pr_self_review_keeps_its_verdict_on_the_attempt(self) -> None:
        task = self._task(prs=0)

        assert self._record(task) == ""
        assert not ReviewVerdict.objects.exists()

    def test_a_reviewer_who_judged_a_head_the_pr_no_longer_points_at_records_nothing(self) -> None:
        task = self._task(prs=1)

        assert self._record(task, live_head="9" * 40) != ""
        assert not ReviewVerdict.objects.exists()
